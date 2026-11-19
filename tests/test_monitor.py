import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from windows_vm import monitor


class TestMonitorSnapshot(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.target = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_missing_target_dir_phase_0(self):
        phase, _ = monitor.snapshot(self.target / "nonexistent")
        self.assertEqual(phase, 0)

    def test_missing_launch_sh_phase_0(self):
        phase, _ = monitor.snapshot(self.target)
        self.assertEqual(phase, 0)

    @mock.patch("windows_vm.monitor.qemu_running", return_value=False)
    def test_launch_exists_qemu_not_running_phase_1(self, mock_qemu):
        (self.target / "launch-vm.sh").write_text("#!/bin/bash")
        phase, _ = monitor.snapshot(self.target)
        self.assertEqual(phase, 1)

    @mock.patch("windows_vm.monitor.qemu_running", return_value=True)
    def testqemu_running_guest_agent_down_phase_2(self, mock_qemu):
        (self.target / "launch-vm.sh").write_text("#!/bin/bash")
        (self.target / "monitor.sock").touch()
        (self.target / "install-info.json").write_text(json.dumps({
            "sockets": {"qga": "/tmp/fake-qga.sock"},
            "rdp_forward": "127.0.0.1:3389",
            "marker": "C:\\marker.txt",
        }))
        with mock.patch("windows_vm.monitor.GuestAgent.ping", return_value=False), \
             mock.patch("windows_vm.monitor.rdp_ready", return_value=False):
            phase, detail = monitor.snapshot(self.target)
            self.assertEqual(phase, 2)
            self.assertFalse(detail.get("guest_agent"))

    @mock.patch("windows_vm.monitor.qemu_running", return_value=True)
    def test_no_guest_agent_rdp_answers_phase_5(self, mock_qemu):
        (self.target / "launch-vm.sh").write_text("#!/bin/bash")
        (self.target / "monitor.sock").touch()
        (self.target / "install-info.json").write_text(json.dumps({
            "sockets": {"qga": "/tmp/fake-qga.sock"},
            "rdp_forward": "127.0.0.1:3389",
            "marker": "C:\\marker.txt",
        }))
        with mock.patch("windows_vm.monitor.GuestAgent.ping", return_value=False), \
             mock.patch("windows_vm.monitor.rdp_ready", return_value=True):
            phase, _ = monitor.snapshot(self.target)
            self.assertEqual(phase, 5)

    @mock.patch("windows_vm.monitor.qemu_running", return_value=True)
    def test_guest_agent_up_marker_missing_phase_3(self, mock_qemu):
        (self.target / "launch-vm.sh").write_text("#!/bin/bash")
        (self.target / "monitor.sock").touch()
        (self.target / "install-info.json").write_text(json.dumps({
            "sockets": {"qga": "/tmp/fake-qga.sock"},
            "rdp_forward": "127.0.0.1:3389",
            "marker": "C:\\marker.txt",
        }))
        with mock.patch("windows_vm.monitor.GuestAgent.ping", return_value=True), \
             mock.patch("windows_vm.monitor.GuestAgent.read_file", return_value=""):
            phase, detail = monitor.snapshot(self.target)
            self.assertEqual(phase, 3)
            self.assertTrue(detail.get("guest_agent"))

    @mock.patch("windows_vm.monitor.qemu_running", return_value=True)
    def test_marker_present_rdp_closed_phase_4(self, mock_qemu):
        (self.target / "launch-vm.sh").write_text("#!/bin/bash")
        (self.target / "monitor.sock").touch()
        (self.target / "install-info.json").write_text(json.dumps({
            "sockets": {"qga": "/tmp/fake-qga.sock"},
            "rdp_forward": "127.0.0.1:3389",
            "marker": "C:\\marker.txt",
        }))
        with mock.patch("windows_vm.monitor.GuestAgent.ping", return_value=True), \
             mock.patch("windows_vm.monitor.GuestAgent.read_file", return_value="installed"), \
             mock.patch("windows_vm.monitor.rdp_ready", return_value=False):
            phase, detail = monitor.snapshot(self.target)
            self.assertEqual(phase, 4)
            self.assertEqual(detail.get("marker"), "installed")

    @mock.patch("windows_vm.monitor.qemu_running", return_value=True)
    def test_marker_present_rdp_open_phase_5(self, mock_qemu):
        (self.target / "launch-vm.sh").write_text("#!/bin/bash")
        (self.target / "monitor.sock").touch()
        (self.target / "install-info.json").write_text(json.dumps({
            "sockets": {"qga": "/tmp/fake-qga.sock"},
            "rdp_forward": "127.0.0.1:3389",
            "marker": "C:\\marker.txt",
        }))
        with mock.patch("windows_vm.monitor.GuestAgent.ping", return_value=True), \
             mock.patch("windows_vm.monitor.GuestAgent.read_file", return_value="installed"), \
             mock.patch("windows_vm.monitor.rdp_ready", return_value=True):
            phase, detail = monitor.snapshot(self.target)
            self.assertEqual(phase, 5)
            self.assertEqual(detail.get("marker"), "installed")

    @mock.patch("windows_vm.monitor.qemu_running", return_value=True)
    def test_guest_agent_failure_mid_read_does_not_crash(self, mock_qemu):
        from windows_vm.qga import QGAError
        (self.target / "launch-vm.sh").write_text("#!/bin/bash")
        (self.target / "monitor.sock").touch()
        (self.target / "install-info.json").write_text(json.dumps(INFO))
        with mock.patch("windows_vm.monitor.GuestAgent.ping", return_value=True), \
             mock.patch("windows_vm.monitor.GuestAgent.read_file",
                        side_effect=QGAError("socket timed out")):
            phase, detail = monitor.snapshot(self.target)
        self.assertEqual(phase, 3)
        self.assertEqual(detail.get("marker"), "")


INFO = {
    "sockets": {"qga": "/tmp/fake-qga.sock"},
    "rdp_forward": "127.0.0.1:3389",
    "marker": "C:\\marker.txt",
}


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.target = Path(self.td.name)
        (self.target / "install-info.json").write_text(json.dumps(INFO))

    def tearDown(self):
        self.td.cleanup()

    def test_verify_survives_guest_agent_errors(self):
        from windows_vm import orchestrator
        from windows_vm.qga import QGAError
        with mock.patch("windows_vm.monitor.GuestAgent.ping", return_value=True), \
             mock.patch("windows_vm.monitor.GuestAgent.read_file",
                        side_effect=[QGAError("timeout"), "done"]), \
             mock.patch("windows_vm.monitor.rdp_ready", return_value=True), \
             mock.patch("time.sleep"):
            self.assertTrue(orchestrator.verify(self.target, timeout=60, interval=0))
        self.assertTrue((self.target / monitor.INSTALL_COMPLETE).exists())

    def test_verify_needs_guest_rdp_not_just_forward(self):
        from windows_vm import orchestrator
        with mock.patch("windows_vm.monitor.GuestAgent.ping", return_value=True), \
             mock.patch("windows_vm.monitor.GuestAgent.read_file", return_value="done"), \
             mock.patch("windows_vm.monitor.rdp_ready", return_value=False), \
             mock.patch("time.sleep"), \
             mock.patch("time.time", side_effect=[0, 0, 100]):
            self.assertFalse(orchestrator.verify(self.target, timeout=10, interval=0))
        self.assertFalse((self.target / monitor.INSTALL_COMPLETE).exists())

    def test_verify_gives_up_when_launcher_exits(self):
        from windows_vm import orchestrator
        launcher = mock.Mock()
        launcher.poll.return_value = 1
        with mock.patch("windows_vm.orchestrator.guest_phase") as phase, \
             mock.patch("time.sleep"):
            self.assertFalse(orchestrator.verify(self.target, timeout=60, interval=0,
                                                 launcher=launcher))
        phase.assert_not_called()
        self.assertFalse((self.target / monitor.INSTALL_COMPLETE).exists())

    def test_qemu_running_does_not_match_sibling_prefix(self):
        with mock.patch("subprocess.run") as run:
            run.return_value.returncode = 1
            monitor.qemu_running(Path("/vms/windows-11"))
        pattern = run.call_args[0][0][2]
        self.assertTrue(pattern.endswith("windows\\-11/"))


if __name__ == "__main__":
    unittest.main()
