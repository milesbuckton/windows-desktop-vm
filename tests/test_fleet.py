import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build-fleet-sequential.py"
_spec = importlib.util.spec_from_file_location("build_fleet_sequential", SCRIPT)
fleet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fleet)


class FakeClock:
    """time.time/time.sleep stand-in: sleep advances the clock instantly."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TestShutdownVm(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.target = Path("/tmp/fleet-test-vm")
        patches = [
            mock.patch.object(fleet.time, "time", self.clock.time),
            mock.patch.object(fleet.time, "sleep", self.clock.sleep),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_shutdown(self, running, send=None, timeout=600.0):
        send = send or mock.Mock(return_value=True)
        with mock.patch.object(fleet, "qemu_running", side_effect=running), \
                mock.patch.object(fleet, "send_powerdown", send), \
                redirect_stdout(io.StringIO()):
            result = fleet.shutdown_vm(self.target, timeout=timeout, resend_interval=60.0)
        return result, send

    def test_not_running_sends_nothing(self):
        result, send = self.run_shutdown(lambda _t: False)
        self.assertTrue(result)
        send.assert_not_called()

    def test_powers_off_after_first_press(self):
        states = iter([True, False])
        result, send = self.run_shutdown(lambda _t: next(states))
        self.assertTrue(result)
        self.assertEqual(send.call_count, 1)

    def test_resends_until_vm_powers_off(self):
        def running(_target):
            return self.clock.now < 1000.0 + 150

        result, send = self.run_shutdown(running)
        self.assertTrue(result)
        self.assertEqual(send.call_count, 3)  # t=0, 60, 120

    def test_resends_after_socket_error(self):
        send = mock.Mock(side_effect=[False, True])

        def running(_target):
            return send.call_count < 2

        result, _ = self.run_shutdown(running, send=send)
        self.assertTrue(result)
        self.assertEqual(send.call_count, 2)

    def test_times_out_when_vm_ignores_powerdown(self):
        result, send = self.run_shutdown(lambda _t: True, timeout=600.0)
        self.assertFalse(result)
        self.assertEqual(send.call_count, 10)  # every 60s within 600s


class TestRunFleet(unittest.TestCase):
    def run_fleet(self, returncodes, shutdown_results):
        run = mock.Mock(side_effect=[SimpleNamespace(returncode=rc) for rc in returncodes])
        shutdown = mock.Mock(side_effect=shutdown_results)
        with mock.patch.object(fleet.subprocess, "run", run), \
                mock.patch.object(fleet, "shutdown_vm", shutdown), \
                redirect_stdout(io.StringIO()):
            results = fleet.run_fleet()
        return results, run

    def test_all_ok(self):
        results, run = self.run_fleet([0, 0, 0], [True, True, True])
        self.assertEqual(list(results.values()), ["OK", "OK", "OK"])
        self.assertEqual(run.call_count, 3)

    def test_setup_failure_continues_with_next_vm(self):
        results, run = self.run_fleet([0, 1, 0], [True, True, True])
        self.assertEqual(list(results.values()), ["OK", "FAIL", "OK"])
        self.assertEqual(run.call_count, 3)

    def test_failed_shutdown_marks_remaining_not_run(self):
        results, run = self.run_fleet([0, 0], [True, False])
        self.assertEqual(list(results.values()), ["OK", "FAIL", "NOT RUN"])
        self.assertEqual(run.call_count, 2)

    def test_3d_command_carries_vulkan_driver_and_hostmem(self):
        cfg = fleet.CONFIGS[2]
        cmd = fleet.build_command(cfg, Path("/tmp/x"))
        self.assertIn("--host-vulkan-driver", cmd)
        self.assertEqual(cmd[cmd.index("--host-vulkan-driver") + 1], "kosmickrisp")
        self.assertEqual(cmd[cmd.index("--hostmem-mb") + 1], "2048")
        self.assertEqual(cmd[cmd.index("--rdp-port") + 1], "3391")


class TestMain(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        patch = mock.patch.object(fleet, "LOCK_PATH", Path(self.td.name) / "fleet.lock")
        patch.start()
        self.addCleanup(patch.stop)

    def run_main(self, results, running=None):
        out = io.StringIO()
        with mock.patch.object(fleet, "running_fleet_vm", return_value=running), \
                mock.patch.object(fleet, "run_fleet", return_value=results) as run, \
                redirect_stdout(out):
            code = fleet.main()
        return code, out.getvalue(), run

    def test_summary_shows_not_run_and_fails(self):
        code, out, _ = self.run_main({"a": "OK", "b": "FAIL", "c": "NOT RUN"})
        self.assertEqual(code, 1)
        self.assertIn("NOT RUN  c", out)
        self.assertIn("FAIL     b", out)

    def test_all_ok_exits_zero(self):
        code, _, _ = self.run_main({"a": "OK", "b": "OK"})
        self.assertEqual(code, 0)

    def test_refuses_when_fleet_vm_running(self):
        code, out, run = self.run_main({}, running=Path("/tmp/windows-11-2d"))
        self.assertEqual(code, 1)
        self.assertIn("refusing fleet build", out)
        run.assert_not_called()

    def test_refuses_when_another_fleet_holds_lock(self):
        with open(fleet.LOCK_PATH, "w", encoding="utf-8") as held:
            fleet.fcntl.flock(held.fileno(), fleet.fcntl.LOCK_EX | fleet.fcntl.LOCK_NB)
            code, out, run = self.run_main({})
        self.assertEqual(code, 1)
        self.assertIn("another fleet build is already running", out)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
