#!/usr/bin/env bash
# Build the unattended "seed" ISO carrying autounattend.xml at its root --
# the first place Windows Setup searches. macOS-native (hdiutil makehybrid).
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <staging-dir-containing-autounattend.xml> <seed.iso>" >&2
  exit 2
fi
STAGE="$1"; OUT="$2"

[ -f "$STAGE/autounattend.xml" ] || { echo "ERROR: $STAGE/autounattend.xml not found" >&2; exit 1; }

hdiutil makehybrid -ov -iso -joliet -default-volume-name "UNATTEND" -o "$OUT" "$STAGE"
echo "wrote $OUT"
