#!/usr/bin/env python3
"""Sequential multi-VM builder (the fleet runner).

Each entry builds + starts one VM, waits for VERIFY-OK, then shuts the VM down
gracefully (ACPI system_powerdown, re-sent periodically because Windows can
ignore the first press) before moving on, so only one guest runs at a time. If
a VM does not power off, the remaining builds are skipped and reported as NOT
RUN. Each entry pins its RDP port so the VMs can still be started side by side
later.
"""
from __future__ import annotations

import fcntl
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from windows_vm.monitor import qemu_running

# Windows may install pending updates on an ACPI shutdown.
SHUTDOWN_TIMEOUT = 600.0
# Windows can ignore a power-button press (e.g. right after first logon), so
# re-send system_powerdown at this interval until the timeout.
POWERDOWN_RESEND_INTERVAL = 60.0
LOCK_PATH = Path("/tmp/windows-desktop-vm-fleet.lock")

CONFIGS = [
    {"name": "windows-11-2d", "hostname": "win11-2d", "edition": "Windows 11 Pro",
     "rdp_port": 3389, "vcpus": 4, "memory_mb": 8192, "gpu": "2d"},
    {"name": "windows-11-3d-moltenvk", "hostname": "win11-3d-mvk",
     "edition": "Windows 11 Pro", "rdp_port": 3390, "vcpus": 4, "memory_mb": 8192,
     "gpu": "3d", "host_vulkan_driver": "moltenvk", "hostmem_mb": 2048},
    {"name": "windows-11-3d-kosmickrisp", "hostname": "win11-3d-kk",
     "edition": "Windows 11 Pro", "rdp_port": 3391, "vcpus": 4, "memory_mb": 8192,
     "gpu": "3d", "host_vulkan_driver": "kosmickrisp", "hostmem_mb": 2048},
]


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _elapsed(start: float) -> str:
    seconds = int(time.time() - start)
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _read_prompt(client: socket.socket) -> None:
    """Read HMP output up to the next "(qemu) " prompt."""
    buf = b""
    while not buf.endswith(b"(qemu) "):
        chunk = client.recv(4096)
        if not chunk:
            break
        buf += chunk


def send_powerdown(target: Path) -> bool:
    """Send system_powerdown over the VM's HMP monitor socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10.0)
            client.connect(str(target / "monitor.sock"))
            _read_prompt(client)
            client.sendall(b"system_powerdown\n")
            _read_prompt(client)
    except OSError as err:
        log(f"  system_powerdown failed: {err}")
        return False
    return True


def shutdown_vm(target: Path, timeout: float = SHUTDOWN_TIMEOUT,
                resend_interval: float = POWERDOWN_RESEND_INTERVAL) -> bool:
    """ACPI-powerdown the VM and wait for QEMU to exit, re-sending the
    powerdown every resend_interval. True when no VM is left running from
    this target dir."""
    if not qemu_running(target):
        return True
    log(f"=== shutting down {target.name} (system_powerdown) ===")
    start = time.time()
    deadline = start + timeout
    next_send = start
    attempt = 0
    while time.time() < deadline:
        if time.time() >= next_send:
            attempt += 1
            if attempt > 1:
                log(f"  {target.name} still running after {_elapsed(start)}; "
                    f"re-sending system_powerdown (attempt {attempt})")
            send_powerdown(target)
            next_send = time.time() + resend_interval
        time.sleep(2.0)
        if not qemu_running(target):
            log(f"  {target.name} powered off after {_elapsed(start)}")
            return True
    log(f"  {target.name} still running after {timeout:.0f}s "
        f"({attempt} system_powerdown attempt(s))")
    return False


def running_fleet_vm() -> Path | None:
    """Return the first target currently running, for fleet-wide preflight."""
    for cfg in CONFIGS:
        target = (Path("~/VMs") / cfg["name"]).expanduser()
        if qemu_running(target):
            return target
    return None


def build_command(cfg: dict, target: Path) -> list[str]:
    cmd = [sys.executable, str(REPO / "setup_vm.py"),
           "--target-dir", str(target),
           "--edition", cfg["edition"],
           "--hostname", cfg.get("hostname", f"{cfg['name']}-vm"),
           "--rdp-port", str(cfg["rdp_port"]),
           "--vcpus", str(cfg.get("vcpus", 4)),
           "--memory-mb", str(cfg.get("memory_mb", 8192)),
           "--gpu", cfg.get("gpu", "2d")]
    if cfg.get("gpu") == "3d":
        cmd += ["--host-vulkan-driver", cfg.get("host_vulkan_driver", "moltenvk")]
        if cfg.get("hostmem_mb") is not None:
            cmd += ["--hostmem-mb", str(cfg["hostmem_mb"])]
    return cmd


def run_fleet() -> dict[str, str]:
    """Build every CONFIGS entry in order. Returns name -> status
    (OK, FAIL or NOT RUN)."""
    results = {cfg["name"]: "NOT RUN" for cfg in CONFIGS}
    for cfg in CONFIGS:
        target = (Path("~/VMs") / cfg["name"]).expanduser()
        start = time.time()
        log(f"=== building {cfg['name']} (RDP 127.0.0.1:{cfg['rdp_port']}) ===")
        proc = subprocess.run(build_command(cfg, target), cwd=REPO)
        ok = proc.returncode == 0
        log(f"=== setup_vm.py for {cfg['name']} exited {proc.returncode} "
            f"after {_elapsed(start)} ===")
        if not shutdown_vm(target):
            log(f"{cfg['name']} did not shut down; not starting further builds "
                "(only one VM may run at a time)")
            results[cfg["name"]] = "FAIL"
            break
        results[cfg["name"]] = "OK" if ok else "FAIL"
        log(f"=== {cfg['name']} {results[cfg['name']]} after {_elapsed(start)} ===")
    return results


def main() -> int:
    with open(LOCK_PATH, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another fleet build is already running")
            return 1

        already_running = running_fleet_vm()
        if already_running is not None:
            log(f"refusing fleet build: {already_running.name} is already running "
                "(only one fleet VM may run)")
            return 1

        start = time.time()
        results = run_fleet()
    print(f"\n== fleet summary ({_elapsed(start)}) ==")
    for name, status in results.items():
        print(f"  {status:<7}  {name}")
    return 0 if all(status == "OK" for status in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
