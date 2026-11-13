#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="$HOME/VMs/windows-11"
SWTPM_SOCK="$TARGET_DIR/swtpm.sock"
TPM_STATE="$TARGET_DIR/swtpm-state"
MON_SOCK="$TARGET_DIR/monitor.sock"
QGA_SOCK="$TARGET_DIR/qga.sock"
rm -f "$MON_SOCK" "$QGA_SOCK"

# Install media (Windows ISO, virtio-win ISO, UNATTEND seed ISO) is only needed
# until Windows is installed. The build writes .install-complete once the guest
# verifies; after that the VM boots without the three CD drives. Delete the
# file (or pass INSTALL_MEDIA=1) to attach them again.
INSTALL_MEDIA_ARGS=()
RAMFB_ARGS=()
if [ "${INSTALL_MEDIA:-0}" = 1 ] || [ ! -e "$TARGET_DIR/.install-complete" ]; then
  INSTALL_MEDIA_ARGS=(
    -drive if=none,id=cd0,file="$HOME/VMs/windows-11/windows-11-arm64-noprompt.iso",media=cdrom,format=raw,readonly=on
    -device usb-storage,drive=cd0,bus=xhci.0,bootindex=1
    -drive if=none,id=cd1,file="$HOME/VMs/windows-11/virtio-win.iso",media=cdrom,format=raw,readonly=on
    -device usb-storage,drive=cd1,bus=xhci.0
    -drive if=none,id=cd2,file="$HOME/VMs/windows-11/seed.iso",media=cdrom,format=raw,readonly=on
    -device usb-storage,drive=cd2,bus=xhci.0
  )
  RAMFB_ARGS=(-device ramfb)
fi
# ramfb is the boot display while Windows installs: the edk2 virtio-gpu GOP is
# blit-only, so Windows' Basic Display Driver needs a linear framebuffer until
# a virtio-gpu driver loads. Once installed it would only show up as a second
# display, so it comes and goes with the install media (RAMFB_ARGS above).

# libepoxy resolves EGL at runtime via dlopen("libEGL.dylib"), which lives in
# /opt/homebrew/lib and is NOT on QEMU's default dlopen search path. Export it
# so cocoa,gl=es display init works.
export DYLD_LIBRARY_PATH="/opt/homebrew/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
# Venus renderer (virgl_render_server) needs VK_ICD_FILENAMES to locate MoltenVK
# on the host. Without it, venus init fails.
export VK_ICD_FILENAMES="/opt/homebrew/etc/vulkan/icd.d/MoltenVK_icd.json"

# --- swtpm (TPM 2.0): spawned per VM, state kept inside the target dir.
#     Not installed for you (detect, don't install) -- brew install swtpm.
if [ ! -S "$SWTPM_SOCK" ]; then
  mkdir -p "$TPM_STATE"
  swtpm socket --tpm2 \
    --tpmstate "dir=$TPM_STATE" \
    --ctrl "type=unixio,path=$SWTPM_SOCK" \
    --daemon
  for _ in 1 2 3 4 5; do [ -S "$SWTPM_SOCK" ] && break; sleep 1; done
fi
cleanup() {
  pkill -f "swtpm.*--tpmstate dir=$TPM_STATE" 2>/dev/null || true
  rm -f "$SWTPM_SOCK"
}
trap cleanup EXIT

qemu-system-aarch64 \
  -name "windows-11" \
  -machine virt,accel=hvf,gic-version=3 \
  -cpu host \
  -smp 4 \
  -m 8192 \
  -rtc base=localtime,clock=host \
  -drive if=pflash,format=raw,readonly=on,file="/opt/homebrew/share/qemu/edk2-aarch64-code.fd" \
  -drive if=pflash,format=raw,file="$HOME/VMs/windows-11/edk2-aarch64-vars.fd" \
  -chardev socket,id=chrtpm,path="$SWTPM_SOCK" \
  -tpmdev emulator,id=tpm0,chardev=chrtpm \
  -device tpm-tis-device,tpmdev=tpm0 \
  -device qemu-xhci,id=xhci \
  -device usb-kbd,bus=xhci.0 \
  -device usb-tablet,bus=xhci.0 \
  "${INSTALL_MEDIA_ARGS[@]+"${INSTALL_MEDIA_ARGS[@]}"}" \
  -drive if=none,id=hd0,file="$HOME/VMs/windows-11/disk.qcow2",format=qcow2 \
  -device virtio-blk-pci,drive=hd0,bootindex=0 \
  -device virtio-gpu-gl-pci,xres=3456,yres=2234,\
blob=on,venus=on,hostmem=1024M \
  "${RAMFB_ARGS[@]+"${RAMFB_ARGS[@]}"}" \
  -object rng-random,id=rng0,filename=/dev/urandom \
  -device virtio-rng-pci,rng=rng0 \
  -device virtio-balloon-pci \
  -device virtio-serial-pci \
  -chardev socket,id=qga0,path="$QGA_SOCK",server=on,wait=off \
  -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0 \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:3389-:3389 \
  -device virtio-net-pci,netdev=net0 \
  -audiodev coreaudio,id=snd0 \
  -device intel-hda \
  -device hda-output,audiodev=snd0 \
  -monitor unix:"$MON_SOCK",server=on,wait=off \
  -display cocoa,gl=es \
  "$@"
