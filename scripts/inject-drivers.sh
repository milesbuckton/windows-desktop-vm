#!/usr/bin/env bash
# Stage the virtio ARM64 driver set as $WinPEDriver$ inside the seed ISO's
# staging dir. Windows Setup scans the root of every attached drive for
# $WinPEDriver$, loads those drivers into WinPE before disk configuration
# (so the virtio-blk disk is visible with no prompts) and injects them into
# the offline image.
#
# The Windows ISO itself is NOT rebuilt: it is UDF-only, and xorriso would
# rewrite it as ISO9660 without UDF, which the Windows boot manager cannot
# read ("No mapping").
#
# Requires: xorriso            (brew install xorriso)
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <seed-staging-dir> <virtio-win.iso> [<viogpu3d-dir>]" >&2
  exit 2
fi
STAGE="$1"; VIRTIO_ISO="$2"; VIOGPU3D_DIR="${3:-}"
[ -z "$VIOGPU3D_DIR" ] || [ -d "$VIOGPU3D_DIR" ] || { echo "ERROR: viogpu3d dir not found: $VIOGPU3D_DIR" >&2; exit 1; }

# Display driver: without a viogpu3d dir (--gpu 2d) the virtio-win ISO's
# Microsoft-signed display-only viogpudo is staged with the rest. With
# viogpu3d (--gpu 3d) viogpudo is left out: it matches the same PCI ID and,
# being Microsoft-signed, Windows would rank it above the test-signed viogpu3d.
DRIVERS="viostor NetKVM Balloon viorng"
[ -n "$VIOGPU3D_DIR" ] || DRIVERS="$DRIVERS viogpudo"
dest="$STAGE/\$WinPEDriver\$"
[ -d "$dest" ] && chmod -R u+w "$dest"
rm -rf "$dest"; mkdir -p "$dest"

echo "extracting ARM64 drivers from $VIRTIO_ISO ..."
for d in $DRIVERS; do
  xorriso -osirrox on -indev "$VIRTIO_ISO" -extract "/$d/w11/ARM64" "$dest/$d" >/dev/null 2>&1
  [ -d "$dest/$d" ] || { echo "ERROR: $d/w11/ARM64 not found in virtio-win ISO (layout changed?)" >&2; exit 1; }
done

# viogpu3d comes from a yttrium release. It is
# test-signed, so it must NOT go into $WinPEDriver$: Setup rejects it and
# aborts with 0xD000A000 - 0x40031. It is staged at <seed>/viogpu3d for a
# install at first logon, after test signing is enabled.
v3d="$STAGE/viogpu3d"
[ -d "$v3d" ] && chmod -R u+w "$v3d"
rm -rf "$v3d"
if [ -z "$VIOGPU3D_DIR" ]; then
  chmod -R u+w "$dest"
  echo "done: $dest"
  exit 0
fi
mkdir -p "$v3d"
cp -R "$VIOGPU3D_DIR/." "$v3d/"
chmod -R u+w "$v3d"
find "$v3d" -name 'viogpu3d.sys' -print -quit | grep -q . \
  || { echo "ERROR: no viogpu3d.sys found under $VIOGPU3D_DIR (wrong layout?)" >&2; exit 1; }
# Run once at the first boot with test signing active (scheduled by
# FirstLogonCommands), then removes its own task. Certificates are imported
# here because this runs as SYSTEM; the first-logon shell is not elevated.
printf '%s\r\n' '@echo off' \
  'certutil -addstore -f Root "%~dp0VirtIOTestCert.cer" > C:\Windows\Setup\viogpu3d-install.log 2>&1' \
  'certutil -addstore -f TrustedPublisher "%~dp0VirtIOTestCert.cer" >> C:\Windows\Setup\viogpu3d-install.log 2>&1' \
  'pnputil /add-driver "%~dp0viogpu3d.inf" /install >> C:\Windows\Setup\viogpu3d-install.log 2>&1' \
  'schtasks /delete /tn viogpu3d-install /f' > "$v3d/install.cmd"
chmod -R u+w "$dest"
echo "done: $dest"
