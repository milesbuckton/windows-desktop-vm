import subprocess
import unittest
from unittest import mock

from windows_vm import orchestrator

VIRGL_HELP = 'name "virtio-gpu-gl-pci", bus PCI, alias "virtio-gpu-gl"\n'
STOCK_HELP = 'name "virtio-gpu-pci", bus PCI, alias "virtio-gpu"\n'


def _proc(stdout, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class QemuVirglTest(unittest.TestCase):
    def test_detects_gl_device(self):
        self.assertTrue(orchestrator.qemu_has_virgl(VIRGL_HELP))
        self.assertFalse(orchestrator.qemu_has_virgl(STOCK_HELP))

    def test_stock_qemu_fails_with_relink_hint(self):
        with mock.patch.object(orchestrator, "_run", return_value=_proc(STOCK_HELP)):
            with self.assertRaises(orchestrator.GateError) as ctx:
                orchestrator.check_qemu_virgl()
        self.assertIn(orchestrator.QEMU_VIRGL_RELINK, str(ctx.exception))

    def test_virgl_qemu_passes(self):
        with mock.patch.object(orchestrator, "_run", return_value=_proc(VIRGL_HELP)):
            orchestrator.check_qemu_virgl()


if __name__ == "__main__":
    unittest.main()
