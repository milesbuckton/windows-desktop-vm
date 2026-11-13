#!/usr/bin/env python3
"""Unit-test gate (CI and local pre-commit).

Runs the repo's unittest suite (under ``tests/``) and fails on any
error or failure.

Scope: deliberately every test in ``tests/`` (whole suite).

A missing test directory is a hard failure, not a pass: an empty or deleted
suite must never look green.

Exit codes: 0 = all passed, 1 = failures/errors, 2 = setup failure (no
``tests/`` directory, no discoverable tests). No network, no VM.

Usage:
    python scripts/run-tests.py
    python scripts/run-tests.py -v          # verbose test names
"""
from __future__ import annotations
import argparse
import contextlib
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO / "tests"


def _die(msg: str) -> None:
    print(f"[FAIL] {msg}")
    print("TESTS-FAIL: setup error -- " + msg)
    sys.exit(2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true",
                   help="verbose test names (equivalent to unittest -v)")
    args = p.parse_args()

    if not TESTS_DIR.is_dir():
        _die(f"no tests directory at {TESTS_DIR}")

    print(f"=== unittest: discover -s {TESTS_DIR} ===")
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(TESTS_DIR))
    n_loaded = suite.countTestCases()
    if n_loaded == 0:
        _die(f"discovered 0 tests under {TESTS_DIR}")

    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1,
                                     stream=sys.stderr)
    with contextlib.redirect_stdout(sys.stderr):
        result = runner.run(suite)
    if result.wasSuccessful():
        print(f"TESTS-OK: {n_loaded} tests passed")
        return 0
    print(f"TESTS-FAIL: {len(result.failures)} failure(s), "
          f"{len(result.errors)} error(s) out of {n_loaded} tests")
    return 1


if __name__ == "__main__":
    sys.exit(main())
