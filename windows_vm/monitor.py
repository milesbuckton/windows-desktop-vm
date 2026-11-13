r"""Phase-based install monitor -- the analog of `setup_vm.py monitor` on Linux.

Phases:
  media       target dir + ISOs exist
  launch      launch-vm.sh generated (VM definition ready)
  firmware    QEMU running (monitor socket present)
  setup       qemu-ga answers, marker not yet written (Windows is installing)
  first-logon marker written (C:\Windows\Setup\windows-vm-setup.log)
  complete    marker + RDP forward accepting
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .ports import port_open, rdp_ready
from .qga import GuestAgent, QGAError

PHASES = ["media", "launch", "firmware", "setup", "first-logon", "complete"]
# Written once the install verifies; launch-vm.sh stops attaching the install
# media (Windows ISO, virtio-win ISO, seed ISO) when it exists.
INSTALL_COMPLETE = ".install-complete"


def snapshot(target: Path) -> tuple:
    """Return (phase_index, detail_dict)."""
    target = Path(target)
    if not target.exists():
        return 0, {}
    launch = target / "launch-vm.sh"
    if not launch.exists():
        return 0, {}
    info_f = target / "install-info.json"
    mon = target / "monitor.sock"
    if not mon.exists() or not _qemu_running(target):
        return 1, {}
    detail = {}
    phase = 2
    if info_f.exists():
        info = json.loads(info_f.read_text())
        ga = GuestAgent(info["sockets"]["qga"])
        detail["guest_agent"] = ga.ping()
        if detail["guest_agent"]:
            phase = 3
            try:
                marker = ga.read_file(info["marker"])
            except QGAError:
                marker = ""
            detail["marker"] = marker.strip()
            if marker.strip():
                phase = 4
                host, port = info["rdp_forward"].rsplit(":", 1)
                if port_open(host, int(port)):
                    phase = 5
        else:
            # No ARM64 qemu-ga on virtio-win: RDP answering means first logon ran.
            host, port = info["rdp_forward"].rsplit(":", 1)
            if rdp_ready(host, int(port)):
                phase = 5
    return phase, detail


def _qemu_running(target: Path) -> bool:
    proc = subprocess.run(["pgrep", "-f", f"qemu-system-aarch64.*{target}"],
                          capture_output=True)
    return proc.returncode == 0


def _bar(index: int, width: int = 30) -> str:
    filled = int(width * (index + 1) / len(PHASES))
    return "#" * filled + "-" * (width - filled)


def watch(target: Path, interval: float = 5.0, once: bool = False,
          tail: bool = False) -> int:
    last = None
    while True:
        index, detail = snapshot(target)
        line = f"[{_bar(index)}] {index + 1}/{len(PHASES)} {PHASES[index]}"
        if tail and detail.get("marker"):
            line += f"  | marker: {detail['marker'][:60]}"
        if line != last:
            print(line, flush=True)
            last = line
        if once or index == len(PHASES) - 1:
            if index == len(PHASES) - 1:
                (Path(target) / INSTALL_COMPLETE).touch()
            return 0 if index == len(PHASES) - 1 else 1
        time.sleep(interval)
