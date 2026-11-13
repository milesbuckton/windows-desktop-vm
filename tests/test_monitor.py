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

    @mock.patch("windows_vm.monitor._qemu_running", return_value=False)
    def test_launch_exists_qemu_not_running_phase_1(self, mock_qemu):
        (self.target / "launch-vm.sh").write_text("#!/bin/bash")
        phase, _ = monitor.snapshot(self.target)
        self.assertEqual(phase, 1)

    @mock.patch("windows_vm.monitor._qemu_running", return_value=True)
    def test_qemu_running_guest_agent_down_phase_2(self, mock_qemu):
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

    @mock.patch("windows_vm.monitor._qemu_running", return_value=True)
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

    @mock.patch("windows_vm.monitor._qemu_running", return_value=True)
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

    @mock.patch("windows_vm.monitor._qemu_running", return_value=True)
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
             mock.patch("windows_vm.monitor.port_open", return_value=False):
            phase, detail = monitor.snapshot(self.target)
            self.assertEqual(phase, 4)
            self.assertEqual(detail.get("marker"), "installed")

    @mock.patch("windows_vm.monitor._qemu_running", return_value=True)
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
             mock.patch("windows_vm.monitor.port_open", return_value=True):
            phase, detail = monitor.snapshot(self.target)
            self.assertEqual(phase, 5)
            self.assertEqual(detail.get("marker"), "installed")


if __name__ == "__main__":
    unittest.main()
