"""Media acquisition and verification: ISOs, install.wim index resolution.

Design notes (mirrors the linux-desktop-vm resolvers):
  * virtio-win has a stable direct-download URL -> fetched at runtime unless
    a copy is in the target dir or ~/Downloads.
  * The Windows ISO is user-supplied (Microsoft's download API is
    session-tokenized and brittle to scrape). --iso wins, then a Windows 11
    Arm64 ISO in ~/Downloads, then a pinned manifest entry (manifest/windows.json).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

VIRTIO_WIN_URL = (
    "https://fedorapeople.org/groups/virt/virtio-win/"
    "direct-downloads/stable-virtio/virtio-win.iso"
)
VIRTIO_VOLUME_LABEL = "virtio-win"

# Driver set staged into $WinPEDriver$ (scripts/inject-drivers.sh receives
# this list). Directory names as spelled on the virtio-win ISO.
DRIVER_SET = ("viostor", "NetKVM", "Balloon", "viorng")
# Microsoft-signed display-only driver, staged for --gpu 2d only.
DISPLAY_DRIVER_2D = "viogpudo"


def winpe_drivers(gpu: str) -> tuple:
    return DRIVER_SET + ((DISPLAY_DRIVER_2D,) if gpu == "2d" else ())


def driver_dir(name: str) -> str:
    return f"{name}/w11/ARM64"

# viogpu3d (experimental 3D display driver) is not part of the virtio-win ISO.
# Its ARM64 package comes from the milesbuckton/kvm-guest-drivers-windows
# releases (or --viogpu3d-url / --viogpu3d-drivers), so it is treated as a
# runtime-supplied directory tree rather than a virtio-win ISO directory.

MANIFEST = Path(__file__).resolve().parent.parent / "manifest" / "windows.json"


class MediaError(RuntimeError):
    pass


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


DOWNLOAD_TIMEOUT = 60  # seconds without data before a download is abandoned


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading {url} -> {dest}", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp, \
                open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh, 1 << 20)
    except (urllib.error.URLError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise MediaError(f"download of {url} failed: {exc}") from exc
    tmp.replace(dest)


def manifest_entry() -> dict:
    """The pinned Windows ISO, or {} while manifest/windows.json is a placeholder."""
    if not MANIFEST.exists():
        return {}
    entry = json.loads(MANIFEST.read_text())
    url = entry.get("url") or ""
    host = urllib.parse.urlparse(url).hostname or ""
    if not url.startswith("https://") or host.endswith(".invalid"):
        return {}
    digest = (entry.get("sha256") or "").strip()
    if not _is_sha256(digest):
        digest = ""
    return {"url": url, "sha256": digest}


def _is_sha256(text: str) -> bool:
    return len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text)


DOWNLOADS = Path("~/Downloads").expanduser()


def find_in_downloads(pattern: re.Pattern) -> Path | None:
    if not DOWNLOADS.is_dir():
        return None
    hits = [p for p in DOWNLOADS.glob("*.iso") if pattern.search(p.name)]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


WINDOWS_ISO_NAME = re.compile(r"win.*11.*arm64|win.*arm64.*11", re.IGNORECASE)
VIRTIO_ISO_NAME = re.compile(r"^virtio-win", re.IGNORECASE)


def resolve_windows_iso(cli_iso: Path | None) -> Path:
    if cli_iso:
        p = Path(cli_iso).expanduser()
        if not p.exists():
            raise MediaError(f"ISO not found: {p}")
        return p
    found = find_in_downloads(WINDOWS_ISO_NAME)
    if found:
        print(f"using Windows ISO from Downloads: {found}")
        return found
    entry = manifest_entry()
    url = entry.get("url")
    if not url:
        raise MediaError(
            "No Windows ISO. Either pass --iso, or pin one in manifest/windows.json:\n"
            '    {"url": "https://.../Win11_25H2_English_Arm64.iso",\n'
            '     "sha256": "<self-recorded, checked after download>"}\n'
            "Official media: https://www.microsoft.com/software-download/windows11arm64"
        )
    dest = Path("~/VMs/_media").expanduser() / url.rsplit("/", 1)[-1]
    if not dest.exists():
        download(url, dest)
    expected = entry.get("sha256")
    if expected:
        actual = sha256(dest)
        if actual.lower() != expected.lower():
            raise MediaError(f"sha256 mismatch for {dest}: {actual}")
    return dest


def ensure_virtio_iso(cli_iso: Path | None, target: Path) -> Path:
    dest = Path(cli_iso).expanduser() if cli_iso else target / "virtio-win.iso"
    if not cli_iso and not dest.exists():
        found = find_in_downloads(VIRTIO_ISO_NAME)
        if found:
            print(f"copying virtio-win ISO from Downloads: {found} -> {dest}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            # APFS clone (instant, no extra space) when possible, else a plain copy.
            if subprocess.run(["cp", "-c", str(found), str(dest)],
                              capture_output=True).returncode != 0:
                shutil.copyfile(found, dest)
            return dest
    if not dest.exists():
        print(f"fetching virtio-win stable ISO -> {dest}")
        download(VIRTIO_WIN_URL, dest)
    return dest


def xorriso(*args: str) -> str:
    exe = shutil.which("xorriso")
    if not exe:
        raise MediaError("xorriso not found on PATH (brew install xorriso)")
    proc = subprocess.run([exe, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"xorriso failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def iso_listing(iso: Path) -> str:
    return xorriso("-indev", str(iso), "-find", "/", "-maxdepth", "4")


@contextmanager
def mount_iso_macos(iso: Path) -> Iterator[Path]:
    """Mount an ISO on macOS using hdiutil attach and yield its mount path."""
    cmd = ["hdiutil", "attach", "-nobrowse", "-readonly", str(iso)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"hdiutil attach failed for {iso}: {proc.stderr.strip()}")
    mount_point: Path | None = None
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            candidate = Path(parts[-1].strip())
            if candidate.is_dir():
                mount_point = candidate
    if not mount_point:
        raise MediaError(f"Could not determine mount point from hdiutil output: {proc.stdout}")
    try:
        yield mount_point
    finally:
        subprocess.run(["hdiutil", "detach", str(mount_point)], capture_output=True)


SECTOR = 2048
NOPROMPT_BOOT_IMAGE = "efi/microsoft/boot/efisys_noprompt.bin"


def el_torito_boot_images(data: bytes, catalog: bytes) -> list:
    """(rba, 512-byte sector count) of every bootable El Torito entry.

    `data` is the Boot Record Volume Descriptor (sector 17), `catalog` the
    boot catalog sector it points to.
    """
    if data[1:6] != b"CD001" or b"EL TORITO" not in data[7:39]:
        raise MediaError("no El Torito boot record at sector 17")
    entries = []
    for off in range(32, SECTOR, 32):
        entry = catalog[off:off + 32]
        if entry[0] == 0x88:
            entries.append((int.from_bytes(entry[8:12], "little"),
                            int.from_bytes(entry[6:8], "little")))
    return entries


def make_noprompt_boot_iso(src: Path, dest: Path) -> None:
    """Copy the Windows ISO and make it boot without "Press any key".

    Microsoft's default UEFI boot image (efisys.bin -> cdboot.efi) waits 5 s
    for a key and otherwise falls through to network boot. The ISO also
    ships efisys_noprompt.bin, which has the same size, so it is written over
    the El Torito boot image in place. The UDF file system is untouched.
    The copy is an APFS clone (`cp -c`) where possible.
    """
    if dest.exists():
        dest.unlink()
    if subprocess.run(["cp", "-c", str(src), str(dest)],
                      capture_output=True).returncode != 0:
        shutil.copyfile(src, dest)
    with mount_iso_macos(src) as mount:
        image = (mount / NOPROMPT_BOOT_IMAGE).read_bytes()
    with open(dest, "r+b") as fh:
        fh.seek(17 * SECTOR)
        brvd = fh.read(SECTOR)
        fh.seek(int.from_bytes(brvd[71:75], "little") * SECTOR)
        entries = el_torito_boot_images(brvd, fh.read(SECTOR))
        if len(entries) != 1:
            raise MediaError(f"expected one El Torito boot image, found {entries}")
        rba, count = entries[0]
        if count * 512 != len(image):
            raise MediaError(f"{NOPROMPT_BOOT_IMAGE} is {len(image)} bytes, "
                             f"boot image slot is {count * 512}")
        fh.seek(rba * SECTOR)
        fh.write(image)


ESD_ONLY = ("This ISO ships sources/install.esd, not install.wim. Convert it "
            "first (dism /export or wimlib-imagex export from a Windows host) "
            "-- driver injection into an ESD is not supported.")


def install_wim_info(iso: Path) -> str:
    """`wimlib-imagex info` of the ISO's sources/install.wim, read in place.

    The ISO is mounted read-only with hdiutil (Windows 11 ISOs are UDF-only,
    which xorriso cannot read), so the multi-GB install.wim is never copied.
    """
    with mount_iso_macos(iso) as mount:
        wim = mount / "sources" / "install.wim"
        if wim.is_file():
            return wim_info(wim)
        if (mount / "sources" / "install.esd").is_file():
            raise MediaError(ESD_ONLY)
    raise MediaError(f"sources/install.wim not found in {iso}")


def wim_info(wim: Path) -> str:
    exe = shutil.which("wimlib-imagex")
    if not exe:
        raise MediaError("wimlib-imagex not found on PATH (brew install wimlib)")
    proc = subprocess.run([exe, "info", str(wim)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"wimlib-imagex info failed: {proc.stderr.strip()}")
    return proc.stdout


_INDEX_RE = re.compile(r"^\s*Index:\s+(\d+)\s*$", re.M)
_NAME_RE = re.compile(r"^\s*Name:\s+(.+?)\s*$", re.M)


def resolve_wim_index(info: str, edition: str) -> int:
    """Map an edition name ('Windows 11 Pro') to its wim index, or raise.

    An exact (case-insensitive) name match wins; only without one does a
    substring match count, and then it must be unambiguous.
    """
    indexes = [(int(m.group(1)), m.start()) for m in _INDEX_RE.finditer(info)]
    names = [(m.group(1), m.start()) for m in _NAME_RE.finditer(info)]
    images = []
    for idx, ipos in indexes:
        later = [n for n, npos in names if npos > ipos]
        images.append((idx, later[0] if later else ""))
    wanted = edition.strip().lower()
    exact = [idx for idx, name in images if name.lower() == wanted]
    if exact:
        return exact[0]
    partial = [(idx, name) for idx, name in images if wanted in name.lower()]
    if len(partial) == 1:
        return partial[0][0]
    available = ", ".join(n for n, _ in names) or "<none parsed>"
    if partial:
        raise MediaError(f"edition {edition!r} is ambiguous "
                         f"({', '.join(n for _, n in partial)}); use the full name")
    raise MediaError(f"edition {edition!r} not in image (available: {available})")


def resolve_wim_language(info: str, index: int | None = None) -> str:
    """Extract default language (e.g. 'en-US') from wim info, or fallback to 'en-US'."""
    if index is not None:
        indexes = [(int(m.group(1)), m.start()) for m in _INDEX_RE.finditer(info)]
        target_start = None
        target_end = len(info)
        for i, (idx, ipos) in enumerate(indexes):
            if idx == index:
                target_start = ipos
                if i + 1 < len(indexes):
                    target_end = indexes[i + 1][1]
                break
        if target_start is not None:
            section = info[target_start:target_end]
            m = re.search(r"Default Language:\s+(\S+)", section)
            if m:
                return m.group(1)
    m = re.search(r"Default Language:\s+(\S+)", info)
    return m.group(1) if m else "en-US"


def verify_virtio_iso(iso: Path, gpu: str = "2d") -> list:
    """Return a list of problems (empty = OK).

    The 3D display driver (viogpu3d) comes from a separate release and is
    not expected on the virtio-win ISO; the 2D one (viogpudo) is.
    """
    listing = iso_listing(iso).lower()
    problems = [f"missing driver dir {driver_dir(d)}" for d in winpe_drivers(gpu)
                if driver_dir(d).lower() not in listing]
    if "qemu-ga" not in listing or ".msi" not in listing:
        problems.append(f"{NO_QEMU_GA} (agent install will skip)")
    return problems


NO_QEMU_GA = "no qemu-ga .msi under guest-agent/"


# Default viogpu3d archive: whichever kvm-guest-drivers-windows release is Latest.
# Each release also carries "<zip>.sha256" for download verification.
VIOGPU3D_URL = (
    "https://github.com/milesbuckton/kvm-guest-drivers-windows/releases/latest/download/"
    "viogpu3d-arm64-win11.zip"
)


def fetch_sha256_sidecar(url: str) -> str | None:
    """Return the digest from "<url>.sha256", or None if it does not exist."""
    try:
        with urllib.request.urlopen(url + ".sha256", timeout=DOWNLOAD_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise MediaError(f"fetching {url}.sha256 failed: {exc}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise MediaError(f"fetching {url}.sha256 failed: {exc}") from exc
    digest = text.split()[0] if text.split() else ""
    if not _is_sha256(digest):
        raise MediaError(f"malformed sha256 file at {url}.sha256: {text!r}")
    return digest


def ensure_viogpu3d_drivers(target: Path, url: str | None = None,
                            expected_sha256: str | None = None) -> Path:
    """Fetch + extract the viogpu3d driver package into target/viogpu3d.

    The archive is verified against expected_sha256, else against the
    release's "<zip>.sha256" file (required for the default URL). The
    extracted tree is reused only while its URL and digest still match.
    """
    dest = target / "viogpu3d"
    marker = dest / ".fetched"
    url = url or VIOGPU3D_URL
    archive = target / "viogpu3d.zip"
    source = target / "viogpu3d.source.json"
    if marker.exists():
        try:
            fetched = json.loads(marker.read_text() or "{}")
        except json.JSONDecodeError:
            fetched = {}
        if fetched.get("url") == url and (
                not expected_sha256
                or fetched.get("sha256", "").lower() == expected_sha256.lower()):
            return dest
    expected = expected_sha256
    if not expected:
        expected = fetch_sha256_sidecar(url)
        if not expected and url == VIOGPU3D_URL:
            raise MediaError(f"no {url}.sha256 published; cannot verify viogpu3d archive")
    # A cached archive is only trusted for the URL it was downloaded from.
    if archive.exists():
        try:
            cached_url = json.loads(source.read_text()).get("url")
        except (OSError, json.JSONDecodeError):
            cached_url = None
        if cached_url != url:
            archive.unlink()
    if not archive.exists():
        download(url, archive)
        source.write_text(json.dumps({"url": url}))
    actual = sha256(archive)
    if expected and actual.lower() != expected.lower():
        archive.unlink()
        source.unlink(missing_ok=True)
        raise MediaError(
            f"viogpu3d archive sha256 mismatch: {actual} (expected {expected}); "
            "the download was removed, rerun to fetch it again")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    proc = subprocess.run(["unzip", "-q", "-o", str(archive), "-d", str(dest)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"unzip of {archive} failed: "
                         f"{proc.stderr.strip() or proc.stdout.strip()}")
    marker.write_text(json.dumps({"url": url, "sha256": actual}))
    return dest


VIOGPU3D_FILES = ("viogpu3d.inf", "viogpu3d.sys", "VirtIOTestCert.cer")


def _is_viogpu3d_dir(path: Path) -> bool:
    """True when path holds everything install.cmd uses (inf, sys, test cert)."""
    return all((path / name).is_file() for name in VIOGPU3D_FILES)


def viogpu3d_arm64_dir(drivers_root: Path) -> Path:
    """Return the ARM64 driver directory inside a viogpu3d release tree.

    The directory must hold viogpu3d.inf, viogpu3d.sys and VirtIOTestCert.cer
    directly: install.cmd references them relative to its own location.
    """
    for base in (drivers_root, drivers_root / "Install", drivers_root / "Install_Debug"):
        for variant in ("", "Win11", "Win10"):
            candidate = base / variant if variant else base
            for path in (candidate, candidate / "arm64", candidate / "ARM64"):
                if _is_viogpu3d_dir(path):
                    return path
    raise MediaError(
        f"viogpu3d ARM64 driver directory not found under {drivers_root} "
        f"(need {', '.join(VIOGPU3D_FILES)} side by side)")
