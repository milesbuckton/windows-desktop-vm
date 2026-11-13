"""Jinja2 rendering + validation for autounattend.xml and launch-vm.sh."""
from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import jinja2

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

# Defaults that mirror the linux-desktop-vm conventions (see README).
DEFAULTS = {
    "ui_language": "en-US", "input_locale": "en-US",
    "system_locale": "en-US", "user_locale": "en-US",
    "timezone": "South Africa Standard Time",
    "registered_owner": "windows-desktop-vm", "registered_org": "",
    "display_name": "Windows", "user_description": "Primary user",
    "autologon_count": "2",
    "virtio_volume_label": "virtio-win",
    "driver_path_winpe": "",       # empty -> rely on sources/$WinPEDriver$ ISO rebuild
    "bypass_requirements": True,   # harmless alongside a real TPM; required with --no-tpm
    "install_qemu_ga": True,
    "qemu_ga_msi": r"guest-agent\qemu-ga-aarch64.msi",  # verify against your virtio-win
    "product_key": "",
    "no_tpm": False,
    "gpu": "2d",
}

GENERIC_KMS_KEYS = {  # public Microsoft generic client setup keys (edition selection only)
    "Windows 11 Pro": "W269N-WFGWX-YVC9B-4J6C9-T83GX",
    "Windows 11 Pro N": "MH37W-N47XK-V7XM9-C7227-GCQG9",
    "Windows 11 Pro for Workstations": "NRG8B-RKK3B-6F4D8-XJ436-P882C",
    "Windows 11 Enterprise": "NPPR9-FWDCX-D2C8J-H872K-2YT43",
    "Windows 11 Education": "NW6C2-QMPVW-D7KKK-3GKT6-VCFB2",
    "Windows 11 Home": "YTMG3-N6DKC-DKB77-7M9GH-8HVX7",
    "Windows 11 Home Single Language": "BT79Q-G7N6G-PGBYW-4YWX6-6F4BT",
}

_env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True,
                          keep_trailing_newline=True)


def _render(name: str, variables: dict) -> str:
    src = (TEMPLATES / name).read_text()
    merged = {**DEFAULTS, **variables}
    return _env.from_string(src).render(**merged)


def render_autounattend(variables: dict) -> str:
    return _render("autounattend.j2", variables)


def render_launcher(variables: dict) -> str:
    return _render("launcher.j2", variables)


class ValidationError(RuntimeError):
    pass


def validate_autounattend(xml_text: str) -> None:
    """Gate 3: well-formed XML, every component arm64, sane wim index/key."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValidationError(f"autounattend.xml is not well-formed: {exc}") from exc
    components = [el for el in root.iter()
                  if el.tag.endswith("}component") or el.tag == "component"]
    if not components:
        raise ValidationError("no <component> elements found")
    bad = [c.get("name") for c in components
           if c.get("processorArchitecture") != "arm64"]
    if bad:
        raise ValidationError(
            f"components not arm64 (a silent-failure classic): {bad}")
    if "<Value></Value>" in xml_text or "<Key></Key>" in xml_text:
        raise ValidationError("empty required value (wim index / product key?)")


def bash_check(path: Path) -> None:
    bash = shutil.which("bash")
    if not bash:
        return
    proc = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise ValidationError(f"bash -n failed for {path}: {proc.stderr.strip()}")


def pick_qemu_ga_msi(virtio_listing: str) -> str:
    """Choose a qemu-ga msi path from an xorriso listing of the virtio ISO.

    Prefers the aarch64 build; warns by raising only when nothing is found.
    """
    candidates = re.findall(r"([\w./\\-]*qemu-ga[^\"'\s]*\.msi)", virtio_listing, re.I)
    if not candidates:
        raise ValidationError("no qemu-ga .msi found on the virtio-win ISO")
    for c in candidates:
        if "aarch64" in c.lower() or "arm64" in c.lower():
            return c.lstrip("./").replace("/", "\\")
    return candidates[0].lstrip("./").replace("/", "\\")
