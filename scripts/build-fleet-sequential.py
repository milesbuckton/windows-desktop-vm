#!/usr/bin/env python3
"""Sequential multi-VM builder (the fleet runner).

Each entry builds + starts one VM and waits for VERIFY-OK before moving on --
a Windows host builds one guest at a time comfortably (HVF is per-core).
Ports must be pinned explicitly for parallel use, as on the Linux fleet.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CONFIGS = [
    {"name": "windows-11", "edition": "Windows 11 Pro", "rdp_port": 3389,
     "vcpus": 4, "memory_mb": 8192},
    # {"name": "windows-11-2", "edition": "Windows 11 Pro", "rdp_port": 3390},
]


def main() -> int:
    failures = []
    for cfg in CONFIGS:
        target = Path("~/VMs") / cfg["name"]
        cmd = [sys.executable, str(REPO / "setup_vm.py"),
               "--target-dir", str(target),
               "--edition", cfg["edition"],
               "--hostname", f"{cfg['name']}-vm",
               "--rdp-port", str(cfg["rdp_port"]),
               "--vcpus", str(cfg.get("vcpus", 4)),
               "--memory-mb", str(cfg.get("memory_mb", 8192)),
               "--start"]
        print(f"=== building {cfg['name']} (RDP 127.0.0.1:{cfg['rdp_port']}) ===",
              flush=True)
        proc = subprocess.run(cmd, cwd=REPO)
        if proc.returncode != 0:
            failures.append(cfg["name"])
    print("\n== fleet summary ==")
    for cfg in CONFIGS:
        status = "FAIL" if cfg["name"] in failures else "OK"
        print(f"  {status}  {cfg['name']}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
