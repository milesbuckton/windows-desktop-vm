#!/usr/bin/env python3
"""windows-desktop-vm -- thin shim; delegates to the windows_vm package.

Usage:
    python setup_vm.py [--iso Win11.iso] [options]   # build (default)
    python setup_vm.py monitor <target_dir> [--once|--tail]
"""
from windows_vm.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
