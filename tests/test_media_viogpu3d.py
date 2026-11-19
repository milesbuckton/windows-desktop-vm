import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from windows_vm import media


def _driver_files(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name in media.VIOGPU3D_FILES:
        (path / name).touch()
    return path


class TestMediaViogpu3d(unittest.TestCase):
    def test_display_only_driver_not_staged(self):
        self.assertNotIn("viogpudo", [d.lower() for d in media.DRIVER_SET])

    def test_viogpu3d_arm64_dir_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _driver_files(Path(tmp))
            # Release layout: user-mode DLLs in arm64/, driver files at the root.
            (root / "arm64").mkdir()
            (root / "arm64" / "viogpu_wgl_arm64.dll").touch()
            self.assertEqual(media.viogpu3d_arm64_dir(root), root)

    def test_viogpu3d_arm64_dir_nested_arm64(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm64 = _driver_files(root / "arm64")
            self.assertEqual(media.viogpu3d_arm64_dir(root), arm64)

    def test_viogpu3d_arm64_dir_install_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm64 = _driver_files(root / "Install" / "Win11" / "arm64")
            self.assertEqual(media.viogpu3d_arm64_dir(root), arm64)

    def test_viogpu3d_arm64_dir_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(media.MediaError):
                media.viogpu3d_arm64_dir(root)

    def test_viogpu3d_arm64_dir_rejects_empty_arm64(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "arm64").mkdir()
            (root / "arm64" / "viogpu_wgl_arm64.dll").touch()
            with self.assertRaises(media.MediaError):
                media.viogpu3d_arm64_dir(root)

    def test_viogpu3d_arm64_dir_needs_test_cert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "viogpu3d.sys").touch()
            (root / "viogpu3d.inf").touch()
            with self.assertRaises(media.MediaError):
                media.viogpu3d_arm64_dir(root)

    def test_winpe_drivers_by_gpu(self):
        self.assertIn("viogpudo", media.winpe_drivers("2d"))
        self.assertNotIn("viogpudo", media.winpe_drivers("3d"))
        self.assertIn("Balloon", media.winpe_drivers("3d"))


def _unzip_ok(*_a, **_k):
    return mock.Mock(returncode=0, stdout="", stderr="")


class TestViogpu3dChecksum(unittest.TestCase):
    def _run(self, sidecar, url=None, expected=None, payload=b"zip", target=None):
        digest = hashlib.sha256(payload).hexdigest()

        def fake_download(_url, dest):
            dest.write_bytes(payload)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(media, "download", side_effect=fake_download) as dl, \
                mock.patch.object(media, "fetch_sha256_sidecar",
                                  return_value=sidecar(digest) if callable(sidecar) else sidecar), \
                mock.patch.object(media.subprocess, "run", side_effect=_unzip_ok):
            media.ensure_viogpu3d_drivers(target or Path(tmp), url=url,
                                          expected_sha256=expected)
            return dl

    def test_default_url_tracks_latest_release(self):
        self.assertIn("/releases/latest/download/", media.VIOGPU3D_URL)

    def test_sidecar_digest_accepted(self):
        self._run(lambda d: d)

    def test_sidecar_mismatch_rejected(self):
        with self.assertRaises(media.MediaError):
            self._run("0" * 64)

    def test_default_url_requires_sidecar(self):
        with self.assertRaises(media.MediaError):
            self._run(None)

    def test_custom_url_without_sidecar_allowed(self):
        self._run(None, url="https://example.invalid/x.zip")

    def test_explicit_digest_wins(self):
        import hashlib
        self._run("0" * 64, expected=hashlib.sha256(b"zip").hexdigest())

    def test_mismatch_removes_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            with self.assertRaises(media.MediaError):
                self._run("0" * 64, target=target)
            self.assertFalse((target / "viogpu3d.zip").exists())
            # A rerun downloads again and succeeds once the digest matches.
            dl = self._run(lambda d: d, target=target)
            self.assertEqual(dl.call_count, 1)

    def test_cached_tree_reused_for_same_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self._run(lambda d: d, target=target)
            dl = self._run(lambda d: d, target=target)
            self.assertEqual(dl.call_count, 0)

    def test_changed_url_refetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self._run(lambda d: d, target=target)
            dl = self._run(None, url="https://example.invalid/other.zip", target=target)
            self.assertEqual(dl.call_count, 1)
            fetched = json.loads((target / "viogpu3d" / ".fetched").read_text())
            self.assertEqual(fetched["url"], "https://example.invalid/other.zip")

    def test_unzip_failure_is_media_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(media, "download",
                                  side_effect=lambda _u, d: d.write_bytes(b"zip")), \
                mock.patch.object(media.subprocess, "run",
                                  return_value=mock.Mock(returncode=9, stdout="",
                                                         stderr="bad zip")):
            with self.assertRaises(media.MediaError):
                media.ensure_viogpu3d_drivers(
                    Path(tmp), url="https://example.invalid/x.zip",
                    expected_sha256=hashlib.sha256(b"zip").hexdigest())


class TestManifest(unittest.TestCase):
    def _entry(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "windows.json"
            path.write_text(json.dumps(data))
            with mock.patch.object(media, "MANIFEST", path):
                return media.manifest_entry()

    def test_placeholder_is_not_configured(self):
        self.assertEqual(self._entry({
            "url": "https://example.invalid/Win11_25H2_English_Arm64.iso",
            "sha256": "<self-recorded: shasum -a 256 of your downloaded ISO>"}), {})

    def test_real_entry_kept(self):
        digest = "a" * 64
        self.assertEqual(self._entry({"url": "https://x.example/w.iso", "sha256": digest}),
                         {"url": "https://x.example/w.iso", "sha256": digest})

    def test_placeholder_digest_dropped(self):
        self.assertEqual(self._entry({"url": "https://x.example/w.iso",
                                      "sha256": "<todo>"})["sha256"], "")

    def test_repo_manifest_is_placeholder(self):
        self.assertEqual(media.manifest_entry(), {})
