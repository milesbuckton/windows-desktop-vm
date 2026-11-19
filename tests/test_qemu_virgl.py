import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from windows_vm import cli, orchestrator

VIRGL_HELP = 'name "virtio-gpu-gl-pci", bus PCI, alias "virtio-gpu-gl"\n'
STOCK_HELP = 'name "virtio-gpu-pci", bus PCI, alias "virtio-gpu"\n'


def _proc(stdout, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _keg(root: Path) -> Path:
    """A fake qemu install: bin/qemu-system-aarch64, bin/qemu-img, edk2 firmware."""
    (root / "bin").mkdir(parents=True)
    (root / "share" / "qemu").mkdir(parents=True)
    for rel in ("bin/qemu-system-aarch64", "bin/qemu-img",
                "share/qemu/edk2-aarch64-code.fd", "share/qemu/edk2-aarch64-vars.fd"):
        (root / rel).touch()
    return root


class QemuVirglTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_detects_gl_device(self):
        self.assertTrue(orchestrator.qemu_has_virgl(VIRGL_HELP))
        self.assertFalse(orchestrator.qemu_has_virgl(STOCK_HELP))

    def test_paths_derive_from_prefix(self):
        qemu = orchestrator.QemuVirgl(Path("/opt/homebrew/opt/qemu-virgl"))
        self.assertEqual(str(qemu.system),
                         "/opt/homebrew/opt/qemu-virgl/bin/qemu-system-aarch64")
        self.assertEqual(str(qemu.img), "/opt/homebrew/opt/qemu-virgl/bin/qemu-img")
        self.assertEqual(str(qemu.ovmf_code),
                         "/opt/homebrew/opt/qemu-virgl/share/qemu/edk2-aarch64-code.fd")
        self.assertEqual(str(qemu.ovmf_vars),
                         "/opt/homebrew/opt/qemu-virgl/share/qemu/edk2-aarch64-vars.fd")

    def test_opt_prefix_wins_over_path(self):
        opt = _keg(self.root / "opt" / "qemu-virgl")
        which = mock.Mock(return_value="/opt/homebrew/bin/qemu-system-aarch64")
        qemu = orchestrator.resolve_qemu_virgl(opt, which=which)
        self.assertEqual(qemu.prefix, opt)
        which.assert_not_called()

    def test_path_fallback_into_qemu_virgl_keg(self):
        keg = _keg(self.root / "Cellar" / "qemu-virgl" / "master")
        link = self.root / "bin" / "qemu-system-aarch64"
        link.parent.mkdir()
        link.symlink_to(keg / "bin" / "qemu-system-aarch64")
        qemu = orchestrator.resolve_qemu_virgl(self.root / "missing",
                                               which=lambda _: str(link))
        self.assertEqual(qemu.prefix, keg)
        self.assertEqual(qemu.img, keg / "bin" / "qemu-img")

    def test_stock_qemu_on_path_rejected(self):
        keg = _keg(self.root / "Cellar" / "qemu" / "10.1.0")
        with self.assertRaises(orchestrator.GateError) as ctx:
            orchestrator.resolve_qemu_virgl(
                self.root / "missing",
                which=lambda _: str(keg / "bin" / "qemu-system-aarch64"))
        self.assertIn("stock Homebrew qemu", str(ctx.exception))
        self.assertIn(orchestrator.QEMU_VIRGL_HINT, str(ctx.exception))

    def test_no_qemu_at_all(self):
        with self.assertRaises(orchestrator.GateError) as ctx:
            orchestrator.resolve_qemu_virgl(self.root / "missing", which=lambda _: None)
        self.assertIn(orchestrator.QEMU_VIRGL_HINT, str(ctx.exception))

    def test_hint_does_not_force_link(self):
        self.assertNotIn("link", orchestrator.QEMU_VIRGL_HINT)

    def test_probe_uses_absolute_binary(self):
        qemu = orchestrator.QemuVirgl(_keg(self.root / "qemu-virgl"))
        with mock.patch.object(orchestrator, "_run",
                               return_value=_proc(VIRGL_HELP)) as run:
            orchestrator.check_qemu_virgl(qemu)
        self.assertEqual(run.call_args[0][0][0], str(qemu.system))

    def test_probe_without_gl_device_fails(self):
        qemu = orchestrator.QemuVirgl(_keg(self.root / "qemu-virgl"))
        with mock.patch.object(orchestrator, "_run", return_value=_proc(STOCK_HELP)):
            with self.assertRaises(orchestrator.GateError) as ctx:
                orchestrator.check_qemu_virgl(qemu)
        self.assertIn(orchestrator.QEMU_VIRGL_HINT, str(ctx.exception))

    def test_missing_firmware_fails(self):
        qemu = orchestrator.QemuVirgl(_keg(self.root / "qemu-virgl"))
        qemu.ovmf_vars.unlink()
        with mock.patch.object(orchestrator, "_run", return_value=_proc(VIRGL_HELP)):
            with self.assertRaises(orchestrator.GateError) as ctx:
                orchestrator.check_qemu_virgl(qemu)
        self.assertIn("edk2-aarch64-vars.fd", str(ctx.exception))


class HostVulkanTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.moltenvk = self.root / "etc" / "MoltenVK_icd.json"
        self.kk_dir = self.root / "kosmickrisp" / "icd.d"

    def _resolve(self, driver):
        return orchestrator.resolve_host_vulkan_icd(
            driver, moltenvk_icd=self.moltenvk, kosmickrisp_dir=self.kk_dir)

    def test_cli_default_is_moltenvk(self):
        args = cli._build_parser().parse_args([])
        self.assertEqual(args.host_vulkan_driver, "moltenvk")
        args = cli._build_parser().parse_args(["--host-vulkan-driver", "kosmickrisp"])
        self.assertEqual(args.host_vulkan_driver, "kosmickrisp")

    def test_cli_rejects_auto(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            cli._build_parser().parse_args(["--host-vulkan-driver", "auto"])

    def test_moltenvk(self):
        self.moltenvk.parent.mkdir(parents=True)
        self.moltenvk.touch()
        self.assertEqual(self._resolve("moltenvk"), self.moltenvk)

    def test_kosmickrisp(self):
        self.kk_dir.mkdir(parents=True)
        icd = self.kk_dir / "kosmickrisp_mesa_icd.aarch64.json"
        icd.touch()
        self.assertEqual(self._resolve("kosmickrisp"), icd)

    def test_missing_kosmickrisp(self):
        with self.assertRaises(orchestrator.GateError) as ctx:
            self._resolve("kosmickrisp")
        msg = str(ctx.exception)
        self.assertIn("brew install milesbuckton/qemu-virgl/kosmickrisp", msg)
        self.assertIn("macOS 26+", msg)

    def test_missing_moltenvk(self):
        with self.assertRaises(orchestrator.GateError) as ctx:
            self._resolve("moltenvk")
        self.assertIn("brew install molten-vk", str(ctx.exception))

    def test_default_paths(self):
        self.assertEqual(str(orchestrator.MOLTENVK_ICD),
                         "/opt/homebrew/etc/vulkan/icd.d/MoltenVK_icd.json")
        self.assertEqual(str(orchestrator.KOSMICKRISP_ICD_DIR),
                         "/opt/homebrew/opt/kosmickrisp/share/vulkan/icd.d")


if __name__ == "__main__":
    unittest.main()
