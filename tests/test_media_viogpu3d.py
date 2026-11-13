import tempfile
import unittest
from pathlib import Path

from windows_vm import media


class TestMediaViogpu3d(unittest.TestCase):
    def test_display_only_driver_not_staged(self):
        self.assertNotIn("viogpudo", [d.lower() for d in media.DRIVER_SET])

    def test_viogpu3d_arm64_dir_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "viogpu3d.sys").touch()
            (root / "viogpu3d.inf").touch()
            self.assertEqual(media.viogpu3d_arm64_dir(root), root)

    def test_viogpu3d_arm64_dir_nested_arm64(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm64 = root / "arm64"
            arm64.mkdir(parents=True)
            (arm64 / "viogpu3d.sys").touch()
            self.assertEqual(media.viogpu3d_arm64_dir(root), arm64)

    def test_viogpu3d_arm64_dir_install_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm64 = root / "Install" / "Win11" / "arm64"
            arm64.mkdir(parents=True)
            (arm64 / "viogpu3d.sys").touch()
            self.assertEqual(media.viogpu3d_arm64_dir(root), arm64)

    def test_viogpu3d_arm64_dir_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(media.MediaError):
                media.viogpu3d_arm64_dir(root)


class TestViogpu3dChecksum(unittest.TestCase):
    def _run(self, sidecar, url=None, expected=None, payload=b"zip"):
        import hashlib
        from unittest import mock
        digest = hashlib.sha256(payload).hexdigest()

        def fake_download(_url, dest):
            dest.write_bytes(payload)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(media, "download", side_effect=fake_download), \
                mock.patch.object(media, "fetch_sha256_sidecar",
                                  return_value=sidecar(digest) if callable(sidecar) else sidecar), \
                mock.patch.object(media.subprocess, "run"):
            return media.ensure_viogpu3d_drivers(Path(tmp), url=url,
                                                 expected_sha256=expected)

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
