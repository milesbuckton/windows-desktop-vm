"""Build orchestration: gates 0-7 (plus 2b for --gpu 3d) from bare ISOs to a launchable VM."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import media, templates
from .monitor import INSTALL_COMPLETE, guest_phase, PHASES, qemu_running
from .ports import probe_free_port
from .qga import MARKER

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
INSTALL_WAIT_TIMEOUT = 90 * 60  # Windows installs land well inside this on an idle host

# qemu-virgl's keg-only opt prefix: used whether or not the formula is linked,
# so stock Homebrew qemu on PATH never gets picked up.
QEMU_VIRGL_PREFIX = Path("/opt/homebrew/opt/qemu-virgl")
QEMU_VIRGL_HINT = "brew tap milesbuckton/qemu-virgl && brew install qemu-virgl"

# Host Vulkan ICD manifests for the Venus renderer (--gpu 3d), picked by
# --host-vulkan-driver. KosmicKrisp is keg-only, so it is read from its opt prefix.
MOLTENVK_ICD = Path("/opt/homebrew/etc/vulkan/icd.d/MoltenVK_icd.json")
KOSMICKRISP_ICD_DIR = Path("/opt/homebrew/opt/kosmickrisp/share/vulkan/icd.d")
HOST_VULKAN_DRIVERS = ("moltenvk", "kosmickrisp")
HOST_VULKAN_HINTS = {
    "moltenvk": "brew install molten-vk",
    "kosmickrisp": "brew install milesbuckton/qemu-virgl/kosmickrisp "
                   "(macOS 26+; normally installed with qemu-virgl)",
}

REQUIRED_TOOLS = {
    "xorriso": "brew install xorriso",
    "wimlib-imagex": "brew install wimlib",
    "swtpm": "brew install swtpm",
    "hdiutil": None,  # macOS builtin
}


class GateError(RuntimeError):
    pass


def _gate(n: str, name: str) -> None:
    print(f"GATE {n} {name} ...", flush=True)


def _ok() -> None:
    print("  OK", flush=True)


def _run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def qemu_has_virgl(device_help: str) -> bool:
    """True when `qemu-system-aarch64 -device help` lists virtio-gpu-gl-pci.

    Stock Homebrew qemu lacks it (no virglrenderer) and ships its AArch64
    NVRAM template under another name, so only the qemu-virgl tap build works.
    """
    return '"virtio-gpu-gl-pci"' in device_help


@dataclass(frozen=True)
class QemuVirgl:
    """Absolute paths of the qemu-virgl install the build and launcher use."""
    prefix: Path

    @property
    def system(self) -> Path:
        return self.prefix / "bin" / "qemu-system-aarch64"

    @property
    def img(self) -> Path:
        return self.prefix / "bin" / "qemu-img"

    @property
    def ovmf_code(self) -> Path:
        return self.prefix / "share" / "qemu" / "edk2-aarch64-code.fd"

    @property
    def ovmf_vars(self) -> Path:
        return self.prefix / "share" / "qemu" / "edk2-aarch64-vars.fd"


def resolve_qemu_virgl(opt_prefix: Path = QEMU_VIRGL_PREFIX,
                       which=shutil.which) -> QemuVirgl:
    """Locate qemu-virgl: its opt prefix first, else a PATH qemu-system-aarch64
    whose real path lies in a qemu-virgl keg (e.g. Cellar/qemu-virgl/<ver>).

    Any other qemu (stock Homebrew qemu has no virglrenderer) is rejected.
    """
    if (opt_prefix / "bin" / "qemu-system-aarch64").exists():
        return QemuVirgl(opt_prefix)
    found = which("qemu-system-aarch64")
    if not found:
        raise GateError(f"qemu-virgl not found at {opt_prefix} or on PATH\n"
                        f"  install: {QEMU_VIRGL_HINT}")
    real = Path(found).resolve()
    if "qemu-virgl" not in real.parts:
        raise GateError(f"{found} (-> {real}) is not the qemu-virgl build; "
                        "stock Homebrew qemu is not supported\n"
                        f"  install: {QEMU_VIRGL_HINT}")
    return QemuVirgl(real.parent.parent)


def check_qemu_virgl(qemu: QemuVirgl) -> None:
    """Gate 0: qemu-virgl's binaries and firmware exist and it has virtio-gpu-gl-pci."""
    missing = [p for p in (qemu.system, qemu.img, qemu.ovmf_code, qemu.ovmf_vars)
               if not p.exists()]
    if missing:
        raise GateError(f"qemu-virgl at {qemu.prefix} is incomplete, missing: "
                        f"{', '.join(map(str, missing))}\n  install: {QEMU_VIRGL_HINT}")
    proc = _run([str(qemu.system), "-device", "help"])
    if proc.returncode != 0 or not qemu_has_virgl(proc.stdout + proc.stderr):
        raise GateError(
            f"{qemu.system} has no virtio-gpu-gl-pci: this is not the qemu-virgl build\n"
            f"  install: {QEMU_VIRGL_HINT}")


def resolve_host_vulkan_icd(driver: str, moltenvk_icd: Path = MOLTENVK_ICD,
                            kosmickrisp_dir: Path = KOSMICKRISP_ICD_DIR) -> Path:
    """Gate 0 (--gpu 3d): the host Vulkan ICD manifest the launcher exports."""
    if driver not in HOST_VULKAN_DRIVERS:
        raise GateError(f"--host-vulkan-driver {driver!r}: choose one of "
                        f"{', '.join(HOST_VULKAN_DRIVERS)}")
    if driver == "moltenvk":
        manifests = [moltenvk_icd] if moltenvk_icd.exists() else []
        where = moltenvk_icd
    else:
        manifests = sorted(kosmickrisp_dir.glob("kosmickrisp_mesa_icd*.json"))
        where = kosmickrisp_dir
    if not manifests:
        raise GateError(f"no {driver} Vulkan ICD manifest at {where}\n"
                        f"  install: {HOST_VULKAN_HINTS[driver]}")
    return manifests[0]


# NetBIOS computer name: at most 15 characters, letters/digits/hyphens.
_HOSTNAME_RE = re.compile(r"^(?!-)(?![0-9]+$)[A-Za-z0-9-]{1,15}(?<!-)$")
# Characters Windows rejects in local account names.
_BAD_USERNAME_CHARS = set('"/\\[]:;|=,+*?<>@')


def validate_identity(hostname: str, username: str) -> None:
    if not _HOSTNAME_RE.match(hostname):
        raise GateError(f"--hostname {hostname!r}: Windows needs 1-15 letters, digits "
                        "or hyphens (not all digits, no leading/trailing hyphen)")
    if (not username or len(username) > 20 or username.strip(". ") != username
            or set(username) & _BAD_USERNAME_CHARS):
        raise GateError(f"--username {username!r}: Windows needs 1-20 characters, "
                        "none of \" / \\ [ ] : ; | = , + * ? < > @, "
                        "no leading/trailing dot or space")


def iso_fingerprint(iso: Path) -> dict:
    """Cheap identity of an ISO for the wim-index cache (no multi-GB hash)."""
    st = iso.stat()
    return {"path": str(iso.resolve()), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def write_private(path: Path, text: str) -> None:
    """Write text to a file that is owner-only from the moment it exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def build(args) -> Path:
    target = Path(args.target_dir).expanduser()
    validate_identity(args.hostname, args.username)
    target.mkdir(parents=True, exist_ok=True)

    # -- Gate 0: host tools (detect, don't install) -------------------------
    _gate("0", "host tools")
    missing = [t for t in REQUIRED_TOOLS if not shutil.which(t)]
    if args.no_tpm and "swtpm" in missing:
        missing.remove("swtpm")
    if missing:
        hints = "\n".join(f"  {t}: {REQUIRED_TOOLS[t]}" for t in missing
                          if REQUIRED_TOOLS.get(t))
        raise GateError(f"missing host tools: {', '.join(missing)}\n{hints}")
    qemu = resolve_qemu_virgl()
    check_qemu_virgl(qemu)
    print(f"  qemu-virgl: {qemu.prefix}")
    host_vulkan_icd = ""
    if args.gpu == "3d":
        host_vulkan_icd = str(resolve_host_vulkan_icd(args.host_vulkan_driver))
        print(f"  host Vulkan ({args.host_vulkan_driver}): {host_vulkan_icd}")
    _ok()

    # -- Gate 1: media present, install.wim extractable ---------------------
    _gate("1", "media + install.wim")
    win_iso = media.resolve_windows_iso(args.iso)
    cache = target / ".cache"
    cache.mkdir(exist_ok=True)
    (cache / "install.wim").unlink(missing_ok=True)  # left by older builds
    index_cache = cache / "wim-index.json"
    cached = {}
    if index_cache.exists():
        try:
            cached = json.loads(index_cache.read_text())
        except json.JSONDecodeError:
            cached = {}
    fingerprint = iso_fingerprint(win_iso)
    if cached.get("iso") == fingerprint and cached.get("edition") == args.edition:
        wim_index = cached["wim_index"]
        wim_lang = cached.get("ui_language")
    else:
        info = media.install_wim_info(win_iso)
        wim_index = media.resolve_wim_index(info, args.edition)
        wim_lang = media.resolve_wim_language(info, wim_index)
        index_cache.write_text(json.dumps({
            "iso": fingerprint, "edition": args.edition,
            "wim_index": wim_index, "ui_language": wim_lang}, indent=2))
    edition = args.edition
    ui_lang = args.ui_language or wim_lang or "en-US"
    _ok()
    print(f"  {win_iso.name} -> {edition} (wim index {wim_index}, lang {ui_lang})")

    # -- Gate 2: virtio ISO + driver layout ---------------------------------
    _gate("2", "virtio-win ISO driver layout")
    virtio_iso = media.ensure_virtio_iso(args.virtio_iso, target)
    problems = media.verify_virtio_iso(virtio_iso, args.gpu)
    hard = [p for p in problems if not p.startswith(media.NO_QEMU_GA)]
    for p in problems:
        print(f"  FAIL {p}" if p in hard else f"  WARN {p}")
    if hard:
        raise GateError("virtio-win ISO does not match the expected ARM64 layout")
    try:
        qemu_ga_msi = templates.pick_qemu_ga_msi(media.iso_listing(virtio_iso))
    except templates.ValidationError as exc:
        print(f"  WARN {exc}; agent install will be skipped")
        qemu_ga_msi = ""
    _ok()

    # -- Gate 2b: viogpu3d 3D drivers (yttrium release; --gpu 3d only) -----
    viogpu3d_dir = None
    if args.gpu == "3d":
        _gate("2b", "viogpu3d 3D drivers (yttrium release)")
        if args.viogpu3d_drivers:
            viogpu3d_dir = media.viogpu3d_arm64_dir(
                Path(args.viogpu3d_drivers).expanduser())
        else:
            viogpu3d_dir = media.viogpu3d_arm64_dir(
                media.ensure_viogpu3d_drivers(
                    target, url=args.viogpu3d_url,
                    expected_sha256=args.viogpu3d_sha256))
        print(f"  ARM64 drivers: {viogpu3d_dir}")
        _ok()

    # -- Gate 3: answer file render + validate, seed ISO --------------------
    password = args.password or args.username
    if not args.password:
        print(f"  WARN no --password: the account password is the username "
              f"({args.username!r}); fine for a loopback-only lab VM")
    _gate("3", "autounattend.xml + seed ISO")
    variables = {
        "wim_index": wim_index,
        "product_key": args.product_key or templates.GENERIC_KMS_KEYS.get(
            args.edition, ""),
        "hostname": args.hostname,
        "timezone": args.timezone,
        "username": args.username,
        "display_name": args.username,
        "user_password": password,
        "ui_language": ui_lang,
        "input_locale": ui_lang,
        "system_locale": ui_lang,
        "user_locale": ui_lang,
        "bypass_requirements": True,   # harmless alongside a real TPM; required with --no-tpm
        "install_qemu_ga": bool(qemu_ga_msi),
        "qemu_ga_msi": qemu_ga_msi,
        "gpu": args.gpu,
    }
    xml_text = templates.render_autounattend(variables)
    templates.validate_autounattend(xml_text)
    stage = target / ".seed"
    stage.mkdir(exist_ok=True)
    (stage / "autounattend.xml").write_text(xml_text)
    cmd = ["bash", str(SCRIPTS / "inject-drivers.sh"), str(stage), str(virtio_iso)]
    if viogpu3d_dir:
        cmd.append(str(viogpu3d_dir))
    proc = _run(cmd, env={**os.environ,
                          "WINPE_DRIVERS": " ".join(media.winpe_drivers(args.gpu))})
    if proc.returncode != 0:
        raise GateError(f"driver staging failed:\n{proc.stdout}{proc.stderr}")
    seed_iso = target / "seed.iso"
    proc = _run(["bash", str(SCRIPTS / "make-seed-iso.sh"), str(stage), str(seed_iso)])
    if proc.returncode != 0:
        raise GateError(f"seed ISO build failed: {proc.stderr.strip()}")
    _ok()

    # -- Gate 4: Windows ISO that boots without "Press any key" -------------
    _gate("4", "no-prompt boot copy of the Windows ISO")
    drivers_iso = target / f"{win_iso.stem}-noprompt.iso"
    try:
        media.make_noprompt_boot_iso(win_iso, drivers_iso)
    except (media.MediaError, OSError) as exc:
        raise GateError(f"boot ISO build failed: {exc}") from exc
    _ok()

    # -- Gate 5: per-VM platform files --------------------------------------
    _gate("5", "disk, OVMF vars, launcher")
    disk = target / "disk.qcow2"
    if not disk.exists():
        (target / INSTALL_COMPLETE).unlink(missing_ok=True)
        proc = _run([str(qemu.img), "create", "-f", "qcow2", str(disk),
                     f"{args.disk_gb}G"])
        if proc.returncode != 0:
            raise GateError(proc.stderr.strip())
    vars_fd = target / "edk2-aarch64-vars.fd"
    if not vars_fd.exists():
        shutil.copy(qemu.ovmf_vars, vars_fd)
    rdp_port = args.rdp_port or previous_rdp_port(target) or probe_free_port()
    launcher = templates.render_launcher({
        "vm_name": target.name,
        "vcpus": args.vcpus,
        "memory_mb": args.memory_mb,
        "rdp_port": rdp_port,
        "target_dir": str(target),
        "qemu_system": str(qemu.system),
        "ovmf_code": str(qemu.ovmf_code), "ovmf_vars": str(vars_fd),
        "windows_iso": str(drivers_iso), "virtio_iso": str(virtio_iso),
        "seed_iso": str(seed_iso), "disk_qcow2": str(disk),
        "no_tpm": args.no_tpm,
        "hostmem_mb": args.hostmem_mb,
        "gpu": args.gpu,
        "host_vulkan_icd": host_vulkan_icd,
    })
    launch_sh = target / "launch-vm.sh"
    launch_sh.write_text(launcher)
    launch_sh.chmod(0o755)
    templates.bash_check(launch_sh)
    _ok()

    # -- Gate 6: install-info (single place credentials are recorded) --------
    _gate("6", "install-info")
    info = {
        "vm_name": target.name, "hostname": args.hostname,
        "username": args.username, "edition": edition, "wim_index": wim_index,
        "rdp_forward": f"127.0.0.1:{rdp_port}",
        "marker": MARKER,
        "sockets": {"monitor": str(target / "monitor.sock"),
                    "qga": str(target / "qga.sock")},
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (target / "install-info.json").write_text(json.dumps(info, indent=2))
    write_private(
        target / "install-info.txt",
        f"VM: {target.name}\nRDP: 127.0.0.1:{rdp_port} (loopback only)\n"
        f"user: {args.username}   password: {password}\n"
        f"marker: {MARKER}\n")
    _ok()

    print(f"\nSetup complete -> {target}\n"
          f"  RDP:    127.0.0.1:{rdp_port}\n"
          f"  user:   {args.username} / {password}\n"
          f"  start:  bash {launch_sh}")

    # A second launch-vm.sh against a running VM would fail on the qcow2 lock
    # and its exit trap would kill the running VM's swtpm, so reuse it instead.
    launch_log = target / "launch.log"
    proc = None
    if qemu_running(target):
        print("  VM already running from this target dir; not launching it again")
    else:
        with open(launch_log, "ab") as log:
            proc = subprocess.Popen(["bash", str(launch_sh)], start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT)
    _gate("7", "verify (VM launched; waiting for the install to finish)")
    if not verify(target, timeout=INSTALL_WAIT_TIMEOUT, launcher=proc):
        if proc is not None and proc.poll() is not None:
            raise GateError(f"VM exited (code {proc.returncode}) before the install "
                            f"verified; see {launch_log}")
        raise GateError("install did not verify in time; see README troubleshooting")
    _ok()
    print("VERIFY-OK")
    return target


def previous_rdp_port(target: Path) -> int | None:
    """RDP port recorded by an earlier build, so rebuilds keep the same forward."""
    try:
        info = json.loads((target / "install-info.json").read_text())
        return int(info["rdp_forward"].rsplit(":", 1)[1])
    except (OSError, ValueError, KeyError, IndexError, AttributeError):
        return None


def verify(target: Path, timeout: float = 60.0, interval: float = 10.0,
           launcher: subprocess.Popen | None = None) -> bool:
    """Gate 7: install verified (monitor's "complete" phase). Poll up to timeout.

    Complete = the guest's RDP service answering, plus the marker whenever
    qemu-ga is reachable (see monitor.guest_phase). Gives up early once the
    launcher process has exited.
    """
    info = json.loads((target / "install-info.json").read_text())
    deadline = time.time() + timeout
    while time.time() < deadline:
        if launcher is not None and launcher.poll() is not None:
            return False
        phase, _ = guest_phase(info)
        if phase == len(PHASES) - 1:
            (target / INSTALL_COMPLETE).touch()
            return True
        time.sleep(interval)
    return False
