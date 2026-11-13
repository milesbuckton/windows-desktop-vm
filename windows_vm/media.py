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
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

VIRTIO_WIN_URL = (
    "https://fedorapeople.org/groups/virt/virtio-win/"
    "direct-downloads/stable-virtio/virtio-win.iso"
)
VIRTIO_VOLUME_LABEL = "virtio-win"

# Driver set injected into sources/$WinPEDriver$ (see scripts/inject-drivers.sh).
DRIVER_SET = ("viostor", "NetKVM", "balloon", "viorng")
REQUIRED_DRIVER_DIRS = tuple(f"{d}/w11/ARM64" for d in DRIVER_SET)

# viogpu3d (Yttrium experimental) — the display driver; not part of the stable virtio-win ISO.
# The release archive ships amd64/x86 builds; ARM64 is built from source. We
# therefore treat the driver set as a runtime-supplied directory tree rather than
# a virtio-win ISO directory.

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


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh, 1 << 20)
    tmp.replace(dest)


def manifest_entry() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return {}


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


def extract_install_wim(iso: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Fast path: try xorriso extraction first (works for standard ISO9660/Joliet/RockRidge).
    # If the ISO uses UDF without RockRidge (common for modern Windows 11 ISOs > 4GB),
    # xorriso cannot access /sources/install.wim directly, so fall back to macOS hdiutil.
    try:
        xorriso("-osirrox", "on", "-indev", str(iso), "-extract",
                "/sources/install.wim", str(dest))
    except MediaError:
        pass

    if not dest.exists():
        # Fallback to macOS hdiutil mounting
        try:
            with mount_iso_macos(iso) as mp:
                wim_source = mp / "sources" / "install.wim"
                if wim_source.exists():
                    shutil.copyfile(wim_source, dest)
                else:
                    esd_source = mp / "sources" / "install.esd"
                    if esd_source.exists():
                        raise MediaError(
                            "This ISO ships sources/install.esd, not install.wim. Convert it "
                            "first (dism /export or wimlib-imagex export from a Windows host) "
                            "-- driver injection into an ESD is not supported."
                        )
        except MediaError:
            raise
        except Exception as exc:
            raise MediaError(f"Failed to extract install.wim from {iso}: {exc}") from exc

    if not dest.exists():
        listing = iso_listing(iso).lower()
        if "install.esd" in listing:
            raise MediaError(
                "This ISO ships sources/install.esd, not install.wim. Convert it "
                "first (dism /export or wimlib-imagex export from a Windows host) "
                "-- driver injection into an ESD is not supported."
            )
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
    """Map an edition name ('Windows 11 Pro') to its wim index, or raise."""
    indexes = [(int(m.group(1)), m.start()) for m in _INDEX_RE.finditer(info)]
    names = [(m.group(1), m.start()) for m in _NAME_RE.finditer(info)]
    for idx, ipos in indexes:
        later = [n for n, npos in names if npos > ipos]
        name = later[0] if later else ""
        if edition.lower() in name.lower():
            return idx
    available = ", ".join(n for n, _ in names) or "<none parsed>"
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


def verify_virtio_iso(iso: Path) -> list:
    """Return a list of problems (empty = OK).

    The display driver (viogpu3d) comes from a separate yttrium release and is
    not expected on the virtio-win ISO.
    """
    listing = iso_listing(iso).lower()
    problems = [f"missing driver dir {d}" for d in REQUIRED_DRIVER_DIRS
                if d.lower() not in listing]
    if "qemu-ga" not in listing or ".msi" not in listing:
        problems.append("no qemu-ga .msi under guest-agent/ (agent install will skip)")
    return problems


# Default viogpu3d archive: whichever kvm-guest-drivers-windows release is Latest.
# Each release also carries "<zip>.sha256" for download verification.
VIOGPU3D_URL = (
    "https://github.com/milesbuckton/kvm-guest-drivers-windows/releases/latest/download/"
    "viogpu3d-arm64-win11.zip"
)


def fetch_sha256_sidecar(url: str) -> str | None:
    """Return the digest from "<url>.sha256", or None if it does not exist."""
    try:
        with urllib.request.urlopen(url + ".sha256") as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    digest = text.split()[0] if text.split() else ""
    if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
        raise MediaError(f"malformed sha256 file at {url}.sha256: {text!r}")
    return digest


def ensure_viogpu3d_drivers(target: Path, url: str | None = None,
                            expected_sha256: str | None = None) -> Path:
    """Fetch + extract the viogpu3d driver package into target/viogpu3d.

    The archive is verified against expected_sha256, else against the
    release's "<zip>.sha256" file (required for the default URL).
    """
    dest = target / "viogpu3d"
    marker = dest / ".fetched"
    if marker.exists():
        return dest
    url = url or VIOGPU3D_URL
    expected = expected_sha256
    if not expected:
        expected = fetch_sha256_sidecar(url)
        if not expected and url == VIOGPU3D_URL:
            raise MediaError(f"no {url}.sha256 published; cannot verify viogpu3d archive")
    archive = target / "viogpu3d.zip"
    if not archive.exists():
        download(url, archive)
    if expected:
        actual = sha256(archive)
        if actual.lower() != expected.lower():
            raise MediaError(
                f"viogpu3d archive sha256 mismatch: {actual} (expected {expected})")
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["unzip", "-q", "-o", str(archive), "-d", str(dest)],
                   check=True, capture_output=True, text=True)
    marker.touch()
    return dest


def viogpu3d_arm64_dir(drivers_root: Path) -> Path:
    """Return the ARM64 driver directory inside a viogpu3d release tree."""
    # Check if drivers_root itself contains viogpu3d.sys / viogpu3d.inf
    if (drivers_root / "viogpu3d.sys").exists() or (drivers_root / "viogpu3d.inf").exists():
        return drivers_root
    for base in (drivers_root, drivers_root / "Install", drivers_root / "Install_Debug"):
        for variant in ("", "Win11", "Win10"):
            candidate = base / variant if variant else base
            if (candidate / "viogpu3d.sys").exists() or (candidate / "viogpu3d.inf").exists():
                return candidate
            arm64_candidate = candidate / "arm64"
            if (arm64_candidate / "viogpu3d.sys").exists() or (arm64_candidate / "viogpu3d.inf").exists():
                return arm64_candidate
            if arm64_candidate.is_dir():
                return arm64_candidate
    raise MediaError(
        "viogpu3d ARM64 driver directory not found in extracted archive "
        f"({drivers_root})")
