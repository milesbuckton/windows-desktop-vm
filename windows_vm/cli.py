"""CLI contract:

    setup_vm.py [--iso ...] [build options]      build (default)
    setup_vm.py monitor <target_dir> [--once|--tail]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="setup_vm.py",
        description="Build an unattended Windows 11 ARM64 desktop VM "
                    "(QEMU/HVF, Apple Silicon).")
    p.add_argument("--iso", type=Path, help="Windows 11 ARM64 ISO "
                   "(default: newest Windows 11 Arm64 ISO in ~/Downloads, "
                   "else manifest/windows.json)")
    p.add_argument("--virtio-iso", type=Path, help="virtio-win ISO (default: "
                   "target dir, else copied from ~/Downloads/virtio-win*.iso, else download)")
    p.add_argument("--target-dir", type=Path, default=Path("~/VMs/windows-11"))
    p.add_argument("--hostname", default="windows-vm")
    p.add_argument("--username", default="windows")
    p.add_argument("--password", default=None,
                   help="primary user password (default: the username itself)")
    p.add_argument("--edition", default="Windows 11 Pro")
    p.add_argument("--ui-language", default=None,
                   help="Windows UI language and locale tag (default: auto-detect from ISO)")
    p.add_argument("--product-key", default=None,
                   help="default: public generic KMS key for --edition")
    p.add_argument("--vcpus", type=int, default=4)
    p.add_argument("--memory-mb", type=int, default=8192)
    p.add_argument("--disk-gb", type=int, default=80)
    p.add_argument("--xres", type=int, default=3456)
    p.add_argument("--yres", type=int, default=2234)
    p.add_argument("--rdp-port", type=int, default=0,
                   help="host RDP forward port (default: probe 3389-3399)")
    p.add_argument("--no-tpm", action="store_true",
                   help="skip swtpm; the LabConfig bypass path is asserted instead")
    p.add_argument("--gpu", choices=("2d", "3d"), default="2d",
                   help="display: 2d = virtio-gpu with the Microsoft-signed "
                        "viogpudo driver (default, stable); 3d = virtio-gpu-gl "
                        "with the test-signed viogpu3d driver (experimental, Venus)")
    p.add_argument("--viogpu3d-drivers", type=Path,
                   help="path to extracted viogpu3d driver files (from a yttrium "
                        "release); default: download the Latest release")
    p.add_argument("--viogpu3d-url", default=None,
                   help="URL to a viogpu3d release archive; fetched at build time "
                        "when --viogpu3d-drivers is not set")
    p.add_argument("--viogpu3d-sha256", default=None,
                   help="expected sha256 of the viogpu3d archive (default: the "
                        "release's <zip>.sha256 file)")
    p.add_argument("--hostmem-mb", type=int, default=1024,
                   help="host-visible graphics memory aperture in MB (MB)")
    p.add_argument("--start", action="store_true",
                   help="launch the VM after building and wait for VERIFY-OK")
    return p


def _monitor_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="setup_vm.py monitor")
    p.add_argument("target_dir", type=Path)
    p.add_argument("--once", action="store_true")
    p.add_argument("--tail", action="store_true",
                   help="include marker text in the progress line")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from . import monitor, orchestrator
    if argv and argv[0] == "monitor":
        args = _monitor_parser().parse_args(argv[1:])
        return monitor.watch(args.target_dir.expanduser(), once=args.once,
                             tail=args.tail)
    args = _build_parser().parse_args(argv)
    try:
        orchestrator.build(args)
    except Exception as exc:  # gates surface cleanly for CI
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0
