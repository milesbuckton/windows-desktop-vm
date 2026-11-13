"""Build orchestration: seven gates from bare ISOs to a launchable VM."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import media, templates
from .monitor import INSTALL_COMPLETE
from .ports import probe_free_port, port_open, rdp_ready
from .qga import GuestAgent, MARKER, QGAError

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
INSTALL_WAIT_TIMEOUT = 90 * 60  # Windows installs land well inside this on an idle host

OVMF_CODE = Path("/opt/homebrew/share/qemu/edk2-aarch64-code.fd")
OVMF_VARS = Path("/opt/homebrew/share/qemu/edk2-aarch64-vars.fd")

QEMU_VIRGL_HINT = ("brew tap milesbuckton/qemu-virgl && brew install qemu-virgl && "
                   "brew link --force qemu-virgl")
QEMU_VIRGL_RELINK = "brew unlink qemu && brew link --force qemu-virgl"

REQUIRED_TOOLS = {
    "qemu-system-aarch64": QEMU_VIRGL_HINT,
    "qemu-img": QEMU_VIRGL_HINT,
    "xorriso": "brew install xorriso",
    "wimlib-imagex": "brew install wimlib",
    "swtpm": "brew install swtpm",
    "hdiutil": None,  # macOS builtin
}


class GateError(RuntimeError):
    pass


def _gate(n: int, name: str) -> None:
    print(f"GATE {n}/7 {name} ...", flush=True)


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


def check_qemu_virgl() -> None:
    proc = _run(["qemu-system-aarch64", "-device", "help"])
    if proc.returncode != 0 or not qemu_has_virgl(proc.stdout + proc.stderr):
        qemu = shutil.which("qemu-system-aarch64")
        raise GateError(
            f"{qemu} has no virtio-gpu-gl-pci: this is not the qemu-virgl build "
            "(stock Homebrew qemu is probably linked)\n"
            f"  install: {QEMU_VIRGL_HINT}\n"
            f"  stock qemu linked: {QEMU_VIRGL_RELINK}")


def build(args) -> Path:
    target = Path(args.target_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)

    # -- Gate 0: host tools (detect, don't install) -------------------------
    _gate(0, "host tools")
    missing = [t for t in REQUIRED_TOOLS if not shutil.which(t)]
    if args.no_tpm and "swtpm" in missing:
        missing.remove("swtpm")
    if missing:
        hints = "\n".join(f"  {t}: {REQUIRED_TOOLS[t]}" for t in missing
                          if REQUIRED_TOOLS.get(t))
        raise GateError(f"missing host tools: {', '.join(missing)}\n{hints}")
    check_qemu_virgl()
    _ok()

    # -- Gate 1: media present, install.wim extractable ---------------------
    _gate(1, "media + install.wim")
    win_iso = media.resolve_windows_iso(args.iso)
    cache = target / ".cache"
    cache.mkdir(exist_ok=True)
    index_cache = cache / "wim-index.json"
    cached = {}
    if index_cache.exists():
        cached = json.loads(index_cache.read_text())
    wim_lang = None
    if (cached.get("iso_sha256") == media.sha256(win_iso) and
            cached.get("edition") == args.edition):
        wim_index = cached["wim_index"]
        edition = cached["edition"]
        wim_lang = cached.get("ui_language")
    else:
        wim = cache / "install.wim"
        if wim.exists():
            wim.unlink()
        media.extract_install_wim(win_iso, wim)
        info = media.wim_info(wim)
        wim_index = media.resolve_wim_index(info, args.edition)
        edition = args.edition
        wim_lang = media.resolve_wim_language(info, wim_index)
        index_cache.write_text(json.dumps({
            "iso_sha256": media.sha256(win_iso),
            "wim_index": wim_index, "edition": edition,
            "ui_language": wim_lang}, indent=2))
    ui_lang = args.ui_language or wim_lang or "en-US"
    _ok()
    print(f"  {win_iso.name} -> {edition} (wim index {wim_index}, lang {ui_lang})")

    # -- Gate 2: virtio ISO + driver layout ---------------------------------
    _gate(2, "virtio-win ISO driver layout")
    virtio_iso = media.ensure_virtio_iso(args.virtio_iso, target)
    problems = media.verify_virtio_iso(virtio_iso)
    hard = [p for p in problems if not p.startswith("no qemu-ga")]
    for p in problems:
        print(f"  WARN {p}" if p.startswith("no qemu-ga") else f"  FAIL {p}")
    if hard:
        raise GateError("virtio-win ISO does not match the expected ARM64 layout")
    try:
        qemu_ga_msi = templates.pick_qemu_ga_msi(media.iso_listing(virtio_iso))
    except templates.ValidationError as exc:
        print(f"  WARN {exc}; agent install will be skipped")
        qemu_ga_msi = r"guest-agent\qemu-ga-aarch64.msi"
    _ok()

    # -- Gate 2b: viogpu3d 3D drivers (yttrium release; --gpu 3d only) -----
    viogpu3d_dir = None
    if args.gpu == "3d":
        _gate(2, "viogpu3d 3D drivers (yttrium release)")
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
    _gate(3, "autounattend.xml + seed ISO")
    variables = {
        "wim_index": wim_index,
        "product_key": args.product_key or templates.GENERIC_KMS_KEYS.get(
            args.edition, ""),
        "hostname": args.hostname,
        "username": args.username,
        "display_name": args.username,
        "user_password": args.password or args.username,
        "ui_language": ui_lang,
        "input_locale": ui_lang,
        "system_locale": ui_lang,
        "user_locale": ui_lang,
        "bypass_requirements": True,   # harmless alongside a real TPM; required with --no-tpm
        "install_qemu_ga": True,
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
    proc = _run(cmd)
    if proc.returncode != 0:
        raise GateError(f"driver staging failed:\n{proc.stdout}{proc.stderr}")
    seed_iso = target / "seed.iso"
    proc = _run(["bash", str(SCRIPTS / "make-seed-iso.sh"), str(stage), str(seed_iso)])
    if proc.returncode != 0:
        raise GateError(f"seed ISO build failed: {proc.stderr.strip()}")
    _ok()

    # -- Gate 4: Windows ISO that boots without "Press any key" -------------
    _gate(4, "no-prompt boot copy of the Windows ISO")
    drivers_iso = target / f"{win_iso.stem}-noprompt.iso"
    try:
        media.make_noprompt_boot_iso(win_iso, drivers_iso)
    except (media.MediaError, OSError) as exc:
        raise GateError(f"boot ISO build failed: {exc}") from exc
    _ok()

    # -- Gate 5: per-VM platform files --------------------------------------
    _gate(5, "disk, OVMF vars, launcher")
    if not OVMF_CODE.exists():
        raise GateError(f"OVMF firmware not found at {OVMF_CODE} (brew install milesbuckton/qemu-virgl/qemu-virgl)")
    disk = target / "disk.qcow2"
    if not disk.exists():
        (target / INSTALL_COMPLETE).unlink(missing_ok=True)
        proc = _run(["qemu-img", "create", "-f", "qcow2", str(disk),
                     f"{args.disk_gb}G"])
        if proc.returncode != 0:
            raise GateError(proc.stderr.strip())
    vars_fd = target / "edk2-aarch64-vars.fd"
    if not vars_fd.exists():
        shutil.copy(OVMF_VARS, vars_fd)
    rdp_port = args.rdp_port or probe_free_port()
    launcher = templates.render_launcher({
        "vm_name": target.name,
        "vcpus": args.vcpus,
        "memory_mb": args.memory_mb,
        "xres": args.xres, "yres": args.yres,
        "rdp_port": rdp_port,
        "target_dir": str(target),
        "ovmf_code": str(OVMF_CODE), "ovmf_vars": str(vars_fd),
        "windows_iso": str(drivers_iso), "virtio_iso": str(virtio_iso),
        "seed_iso": str(seed_iso), "disk_qcow2": str(disk),
        "no_tpm": args.no_tpm,
        "hostmem_mb": args.hostmem_mb,
        "gpu": args.gpu,
    })
    launch_sh = target / "launch-vm.sh"
    launch_sh.write_text(launcher)
    launch_sh.chmod(0o755)
    templates.bash_check(launch_sh)
    _ok()

    # -- Gate 6: install-info (single place credentials are recorded) --------
    _gate(6, "install-info")
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
    txt = (target / "install-info.txt")
    txt.write_text(
        f"VM: {target.name}\nRDP: 127.0.0.1:{rdp_port} (loopback only)\n"
        f"user: {args.username}   password: {args.password or args.username}\n"
        f"marker: {MARKER}\n")
    os.chmod(txt, 0o600)
    _ok()

    print(f"\nSetup complete -> {target}\n"
          f"  RDP:    127.0.0.1:{rdp_port}\n"
          f"  user:   {args.username} / {args.password or args.username}\n"
          f"  start:  bash {launch_sh}")

    if args.start:
        subprocess.Popen(["bash", str(launch_sh)], start_new_session=True)
        print("VM launched; waiting for install to finish ...")
        if not verify(target, timeout=INSTALL_WAIT_TIMEOUT):
            raise GateError("install did not verify in time; see README troubleshooting")
        print("VERIFY-OK")
    return target


def verify(target: Path, timeout: float = 60.0, interval: float = 10.0) -> bool:
    """Gate 7: marker via qemu-ga + RDP forward accepting. Poll up to timeout.

    virtio-win ships no ARM64 qemu-ga, so a guest RDP service answering also
    counts: RDP is only enabled by FirstLogonCommands, after Setup finishes.
    """
    info = json.loads((target / "install-info.json").read_text())
    host, port = info["rdp_forward"].rsplit(":", 1)
    ga = GuestAgent(info["sockets"]["qga"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ga.ping():
            try:
                marker = ga.read_file(info["marker"])
            except QGAError:
                marker = ""
            if marker.strip() and port_open(host, int(port)):
                (target / INSTALL_COMPLETE).touch()
                return True
        elif rdp_ready(host, int(port)):
            (target / INSTALL_COMPLETE).touch()
            return True
        time.sleep(interval)
    return False
