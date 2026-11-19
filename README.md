# windows-desktop-vm

[![Pylint](https://github.com/milesbuckton/windows-desktop-vm/actions/workflows/pylint.yml/badge.svg)](https://github.com/milesbuckton/windows-desktop-vm/actions/workflows/pylint.yml)

Unattended **Windows 11 (ARM64)** desktop VMs on **QEMU/HVF, Apple Silicon** —
the Windows sibling of [linux-desktop-vm](https://github.com/milesbuckton/linux-desktop-vm).
Same philosophy: one command, runtime discovery, generated per-VM launcher,
loopback-only management plane, ground-truth marker file, gated fleet builds.

> **Honest status up front**
> * **Display defaults to 2D.** The launcher uses plain `virtio-gpu-pci` with the
>   Microsoft-signed `viogpudo` display-only driver from the virtio-win ISO: stable,
>   no 3D acceleration. Use RDP for the smoothest desktop.
> * **3D is opt-in and experimental (`--gpu 3d`).** The launcher then uses
>   `virtio-gpu-gl-pci` with `venus=on,blob=on,hostmem=<MB>` and the build injects the
>   ARM64 `viogpu3d` driver from the Latest
>   [kvm-guest-drivers-windows](https://github.com/milesbuckton/kvm-guest-drivers-windows/releases/latest)
>   release. It is alpha software that can hang or
>   crash a guest (currently Explorer crash-loops: no taskbar) -- do not use it for
>   production work or data you cannot
>   afford to lose. The test-signed driver requires Secure Boot **off** (the
>   qemu-virgl tap's `edk2-aarch64` firmware does not enforce it); first logon
>   installs it unattended — see [viogpu3d](#viogpu3d-experimental-3d). Either mode needs the
>   [qemu-virgl](https://github.com/milesbuckton/homebrew-qemu-virgl) Homebrew tap.
> * **Audio** (speakers and a microphone) rides Intel HDA (inbox driver) — there
>   is no virtio-snd Windows driver — but RDP audio is the reliable path; see
>   [Audio & microphone](#audio--microphone).
> * **Nothing Microsoft is redistributed.** You download the ISO and supply
>   your own license. The default `product_key` is the public generic KMS
>   client key for the selected edition (Pro, Home, Enterprise, Education, etc.),
>   used only to select the edition without activation.

## Components

This repo orchestrates a stack of forks and packages (how they fit together:
[docs/ARCHITECTURE.md → GPU & display](docs/ARCHITECTURE.md#gpu--display)):

| Repository | Layer | Role |
|---|---|---|
| [windows-desktop-vm](https://github.com/milesbuckton/windows-desktop-vm) (this repo) | Orchestration | ISO preparation, answer file, driver staging, NVRAM, launcher, install monitoring |
| [homebrew-qemu-virgl](https://github.com/milesbuckton/homebrew-qemu-virgl) | Packaging | Homebrew tap: `qemu-virgl`, `virglrenderer`, `libangle`, `libepoxy-angle`, `kosmickrisp` |
| [milesbuckton/qemu](https://gitlab.com/milesbuckton/qemu) | Hypervisor | Patched QEMU AArch64: HVF, Cocoa `gl=es` display, `virtio-gpu-gl-pci` with `venus=on,blob=on,hostmem=<MB>`; ships the `edk2-aarch64` firmware |
| [miles.buckton/virglrenderer](https://gitlab.freedesktop.org/miles.buckton/virglrenderer) | Host renderer | macOS/HVF fixes, Darwin DRM, Venus host path (`-Dvenus=true`); dlopens host Vulkan (picked by `--host-vulkan-driver`) |
| [miles.buckton/mesa](https://gitlab.freedesktop.org/miles.buckton/mesa) | Host/guest 3D | Guest Gallium/Venus user-mode drivers; also the KosmicKrisp Vulkan-on-Metal driver (macOS 26+, opt-in host Vulkan driver) |
| [kvm-guest-drivers-windows](https://github.com/milesbuckton/kvm-guest-drivers-windows) | Guest drivers | ARM64 `viogpu3d.sys` (16 KiB blob alignment for Apple Silicon) + ARM64X Mesa UMDs; publishes the test-signed release used by `--gpu 3d` |

## How a build works

```
setup_vm.py
  1. Resolve + fetch media:  Windows 11 ARM64 ISO (pinned in manifest/windows.json;
     --iso override) and virtio-win stable ISO (discoverable URL).
  2. Gate 1: resolve the edition index in sources/install.wim (wimlib-imagex).
     Gate 2: check the virtio-win ISO's ARM64 drivers (+ fetch viogpu3d).
  3. Gate 3: render autounattend.xml (templates/autounattend.j2);
     scripts/inject-drivers.sh stages the ARM64 virtio drivers as $WinPEDriver$
     on the seed ISO (Setup scans every drive's root for it);
     scripts/make-seed-iso.sh builds seed.iso with autounattend.xml at root
     (macOS-native hdiutil makehybrid).
  4. Gate 4 (media.make_noprompt_boot_iso): <iso>-noprompt.iso, an APFS clone
     (copy fallback) of the Windows ISO with efisys_noprompt.bin written over
     the El Torito boot image, so it boots without "Press any key".
  5. qemu-img create the qcow2; copy OVMF vars per VM; render launch-vm.sh;
     write install-info.json/.txt.
  6. Launch: Windows Setup runs fully unattended (TPM via swtpm satisfies
     the Win11 hardware check; LabConfig bypass keys are belt-and-braces).
  7. FirstLogonCommands: enable RDP (network set Private), install qemu-ga
     (only if virtio-win has an ARM64 msi), write marker
     C:\Windows\Setup\windows-vm-setup.log  (ground truth, like *-vm-setup.log),
     powercfg /hibernate off; with --gpu 3d also test signing + reboot.
     The build waits (up to 90 min) for an RDP handshake (plus the marker when
     qemu-ga answers) and prints VERIFY-OK.
     It then writes .install-complete, so later boots of launch-vm.sh leave
     out the three install CDs (see "Install media").
```

## Requirements (host)

* **Mac:** Apple Silicon (M1–M4; Pro/Max/Ultra recommended for more vCPUs),
  at least 16 GB unified memory (the VM takes 8 GB by default).
* **Disk:** 100 GB+ free on APFS (the no-prompt ISO is an instant
  copy-on-write clone).
* **macOS:** a recent release (not checked by the build); see the tap note
  below for bottles.

| Tool | Install | Why |
|---|---|---|
| Python 3.9+ with Jinja2 | `python3 -m pip install -r requirements.txt` | renders the answer file and launcher |
| QEMU with virglrenderer (`qemu-system-aarch64`, `qemu-img`) | `brew tap milesbuckton/qemu-virgl && brew install qemu-virgl` | hypervisor (HVF) + Venus 3D; **detect, don't install** |
| swtpm | `brew install swtpm` | TPM 2.0 — Windows 11 requirement |
| xorriso | `brew install xorriso` | extracting drivers from virtio-win.iso |
| wimlib | `brew install wimlib` | install.wim index resolution (Gate 1) |

Stock Homebrew `qemu` does **not** work: it lacks `virtio-gpu-gl-pci` and names
its AArch64 vars template `edk2-arm-vars.fd`. The build never uses it: Gate 0
takes `qemu-system-aarch64`, `qemu-img` and the edk2 firmware from qemu-virgl's
opt prefix (`/opt/homebrew/opt/qemu-virgl`), falling back to a
`qemu-system-aarch64` on `PATH` only when it resolves into a `qemu-virgl` keg
(`Cellar/qemu-virgl/...`); any other QEMU is rejected. It then checks
`-device help` for `virtio-gpu-gl-pci`. `launch-vm.sh` calls that absolute
binary, so stock `qemu` may stay installed and linked side by side;
`brew link --force qemu-virgl` is optional (only for running qemu-virgl by bare
name in your shell). The tap ships
bottles only for macOS 27 (`arm64_golden_gate`); other versions build from source.

Paths are fixed to `/opt/homebrew` (qemu-virgl, firmware, `DYLD_LIBRARY_PATH`, the host
Vulkan ICDs). `--gpu 3d` also needs a host Vulkan driver (see
[What it needs (host)](#what-it-needs-host)).

Firmware: the `edk2-aarch64-code.fd` / `edk2-aarch64-vars.fd` pair shipped by
qemu-virgl (`/opt/homebrew/opt/qemu-virgl/share/qemu/`), with a per-VM writable vars copy.
Secure Boot is **not** enforced by that firmware; the Win11 check is satisfied
via the emulated TPM plus LabConfig bypass. True Secure Boot (e.g. Fedora's
edk2 `AAVMF_VARS…secboot` with Microsoft keys) is not supported yet.

## Getting the Windows ISO

The Windows 11 ARM64 ISO is not downloaded for you — Microsoft only hands out
time-limited links after you pick options in a browser.

1. Open <https://www.microsoft.com/software-download/windows11arm64>.
2. Choose **Windows 11 (multi-edition ISO for Arm64)**, then your language,
   and download it (e.g. to `~/Downloads`).
3. Leave it in `~/Downloads` (picked up automatically: newest `*.iso` whose
   name matches Windows 11 + Arm64), pass it with `--iso`, or record its
   URL + `shasum -a 256` in `manifest/windows.json`.

The x64 ISO from the regular Windows 11 download page will **not** boot on
Apple Silicon. The virtio-win ISO is taken from `--virtio-iso`, else the
target dir, else the newest `~/Downloads/virtio-win*.iso` (copied into the
target dir so the VM stays self-contained), else fetched
automatically.

## Usage

```
# Build, launch, and wait for the unattended install to finish (VERIFY-OK):
python3 setup_vm.py --iso ~/Downloads/Win11_Arm64.iso

# Experimental 3D (see "viogpu3d" below), with a larger graphics aperture:
python3 setup_vm.py --iso ~/Downloads/Win11_Arm64.iso --gpu 3d --hostmem-mb 2048

# Watch progress with the live phase-based progress bar
# (phases: media, launch, firmware, setup, first-logon, complete):
python3 setup_vm.py monitor ~/VMs/windows-11          # live
python3 setup_vm.py monitor ~/VMs/windows-11 --once   # print once and exit
python3 setup_vm.py monitor ~/VMs/windows-11 --tail   # include marker text

# Start an already-built VM (also printed at the end of a build):
bash ~/VMs/windows-11/launch-vm.sh
```

`--gpu 2d` (the default) is the mode for day-to-day work over RDP.

### Options

`python3 setup_vm.py --help` prints the same list.

| Flag | Default | Description |
|---|---|---|
| `--iso` | Newest Win11 Arm64 ISO in `~/Downloads`, else `manifest/windows.json` (a placeholder until you record your own) | Windows 11 ARM64 install ISO |
| `--virtio-iso` | Target dir, else copied from `~/Downloads/virtio-win*.iso`, else downloaded | virtio-win driver ISO |
| `--target-dir` | `~/VMs/windows-11` | Where the disk, NVRAM, launcher and metadata go |
| `--hostname` | `windows-vm` | Windows computer name (1–15 letters, digits, hyphens) |
| `--edition` | `Windows 11 Pro` | Edition, matched against the WIM image names (exact name first, else an unambiguous substring); Pro or higher is needed for the RDP server |
| `--product-key` | Public generic KMS key for `--edition` | Product key written to the answer file |
| `--ui-language` | Detected from the ISO (else `en-US`) | UI language and locale tag |
| `--timezone` | `South Africa Standard Time` | Windows time zone ID (`tzutil /l`) |
| `--vcpus` | `4` | Guest CPU cores |
| `--memory-mb` | `8192` | Guest RAM in MB |
| `--disk-gb` | `80` | Size of the dynamic qcow2 system disk |
| `--gpu` | `2d` | `2d`: `virtio-gpu-pci` + `viogpudo` (stable); `3d`: `virtio-gpu-gl-pci` with Venus + `viogpu3d` (experimental) |
| `--hostmem-mb` | `1024` | Host-visible graphics memory aperture for `--gpu 3d` |
| `--host-vulkan-driver` | `moltenvk` | Host Vulkan driver for Venus (`moltenvk` or `kosmickrisp`; `--gpu 3d` only) |
| `--viogpu3d-drivers` | Download the Latest release | Local extracted viogpu3d tree (`--gpu 3d` only) |
| `--viogpu3d-url` | Latest `viogpu3d-arm64-win11.zip` release | viogpu3d archive URL |
| `--viogpu3d-sha256` | The release's `<zip>.sha256` sidecar | Expected archive digest |
| `--no-tpm` | Off | Skip `swtpm`; rely on the answer file's LabConfig TPM bypass |
| `--rdp-port` | Previous build's port (`install-info.json`), else first free in 3389–3399 | Host loopback port forwarded to guest RDP port 3389 |
| `--username` | `windows` | Administrator account name |
| `--password` | Same as `--username` (with a warning) | Administrator password (recorded in `install-info.txt`, chmod 600) |

### Install media

During installation the VM has three CD drives: the Windows ISO, the virtio-win
ISO and `UNATTEND` (`seed.iso`, the answer file plus drivers). None of them are
needed once Windows is installed. When the install verifies (the build finishing,
or `setup_vm.py monitor` reaching `complete`), the target dir gets a
`.install-complete` file and `launch-vm.sh` stops attaching them. To attach them
again for one boot (for example to reinstall `viogpu3d` from `UNATTEND`), run
`INSTALL_MEDIA=1 bash ~/VMs/windows-11/launch-vm.sh`; delete `.install-complete`
to attach them every time.

A build writes everything into `--target-dir` (default `~/VMs/windows-11`):
`disk.qcow2`, `edk2-aarch64-vars.fd`, `virtio-win.iso` (unless `--virtio-iso`),
`seed.iso` + `.seed/` (answer file, `$WinPEDriver$`, `viogpu3d/`),
`<iso-name>-noprompt.iso` (APFS clone of the Windows ISO that boots without
"Press any key"), `launch-vm.sh`, `install-info.json` (RDP port, user, sockets;
no password) / `install-info.txt` (adds the password, chmod 600), `.cache/`
(wim index), `viogpu3d/` + `viogpu3d.zip` + `viogpu3d.source.json`
(delete them to fetch a newer release), `swtpm-state/`
(the VM's TPM), `launch.log` (output of the `launch-vm.sh` the build starts),
`.install-complete` once the install verifies, plus
`monitor.sock` / `qga.sock` / `swtpm.sock` / `swtpm.pid` while running.

Fleet builds: `python3 scripts/build-fleet-sequential.py` (edit CONFIGS at the
top; each VM is built and verified, then shut down with `system_powerdown`
before the next one starts, so only one VM runs at a time; `system_powerdown` is
re-sent every 60 s because Windows can ignore the first one; if a VM does not
power off within 10 minutes, the remaining builds are skipped and the summary
lists them as `NOT RUN`; output lines are timestamped with per-stage elapsed
times; a second fleet run exits at once, and
the fleet refuses to start while one of its VMs is already running). By default it builds three VMs:
`windows-11-2d` (RDP port 3389), `windows-11-3d-moltenvk` (3390) and
`windows-11-3d-kosmickrisp` (3391, macOS 26+); each takes 8 GB of guest RAM and
4 vCPUs while running. The latest fleet run took 72 min on an Apple M5 Pro
MacBook Pro (2D 29.9 min, MoltenVK 25.4 min, KosmicKrisp 16.9 min, each
including shutdown; details:
[docs/ARCHITECTURE.md → Timing](docs/ARCHITECTURE.md#timing)). Tests, lint and CI:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#development-environment--testing).

## Day-2 operations

Everything lives in the target dir (default `~/VMs/windows-11`). The VM always
opens a QEMU Cocoa window on the host, but RDP is the primary interface.

| Task | Command |
|---|---|
| Start | `bash ~/VMs/windows-11/launch-vm.sh` |
| Watch status | `python3 setup_vm.py monitor ~/VMs/windows-11` (see [Usage](#usage)) |
| RDP in | any RDP client (Microsoft Remote Desktop from the Mac App Store, Jump Desktop, FreeRDP) → the address, user and password in `install-info.txt` (e.g. `127.0.0.1:3389`, user `windows` or `--username`) |
| Graceful poweroff | `echo system_powerdown \| socat - "UNIX-CONNECT:$HOME/VMs/windows-11/monitor.sock"` (`brew install socat`) |
| Snapshots | VM off: `qemu-img snapshot -c <name> disk.qcow2` (create), `-l disk.qcow2` (list), `-a <name> disk.qcow2` (restore). Covers the disk only, not the vars file or TPM state |
| Re-attach install media | `INSTALL_MEDIA=1 bash ~/VMs/windows-11/launch-vm.sh` (see [Install media](#install-media)) |
| Check guest state | marker: `C:\Windows\Setup\windows-vm-setup.log`; qemu-ga via `qga.sock` |
| Re-run installer | VM off; delete `disk.qcow2`, `edk2-aarch64-vars.fd` and `swtpm-state/`, then rebuild (an existing disk/vars is kept, not overwritten). Deleting the whole target dir also works but re-copies (or re-downloads) `virtio-win.iso`. |

## Audio & microphone

The VM has an Intel HDA codec that Windows' inbox driver shows as speakers plus
a **microphone**, wired to the Mac's default output and input devices through
QEMU's CoreAudio backend (device details:
[docs/ARCHITECTURE.md → Audio](docs/ARCHITECTURE.md#audio)).

* **Needs a recent qemu-virgl.** Microphone capture comes from the CoreAudio
  input support in the [milesbuckton/qemu](https://gitlab.com/milesbuckton/qemu)
  fork (November 2026). Older qemu-virgl builds play sound but the microphone
  records silence; run `brew update && brew upgrade qemu-virgl`.
* **macOS microphone permission goes to the app that launches QEMU** — the
  terminal (Terminal, iTerm, VS Code, …) that runs `launch-vm.sh`. macOS asks
  on first capture; if it was denied, or QEMU is started from something without
  microphone access (e.g. a launchd job or a background script), the guest
  just records silence. Grant it in **System Settings → Privacy & Security →
  Microphone** and restart the VM.
* **Alternative: RDP microphone redirection.** Over RDP, audio playback and
  recording go through the client instead of the HDA device. In Windows App
  (Microsoft Remote Desktop) enable **Use microphone** in the PC's settings
  (`audiocapturemode:i:1` in an `.rdp` file); FreeRDP uses `/microphone`. If
  the guest still has no redirected microphone, enable the group policy
  **Allow audio recording redirection** (Computer Configuration → Administrative
  Templates → Windows Components → Remote Desktop Services → Remote Desktop
  Session Host → Device and Resource Redirection).

## Security

* The RDP forward listens on loopback (`127.0.0.1`) only; the QEMU monitor
  (HMP, `monitor.sock`) and guest agent (`qga.sock`) are Unix sockets in the
  target dir.
* The password is stored in plain text: in `install-info.txt` (chmod 600) and
  also in `.seed/autounattend.xml` and `seed.iso`. Pass your own `--password`
  rather than relying on the default.
* Report security problems privately — see
  [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md#security-issues).

## Template variables (templates/autounattend.j2)

`wim_index` (required, build-resolved) · `product_key` · `hostname` · `username` /
`display_name` / `user_password` (the whole template is XML-autoescaped) ·
`user_description` · `registered_owner` / `registered_org` · `timezone` ·
`ui_language` / `*_locale` · `gpu` (`2d`/`3d`) ·
`bypass_requirements` (default true) · `driver_path_winpe` (fallback driver
path; empty = rely on `$WinPEDriver$` on the seed ISO) · `install_qemu_ga` +
`virtio_volume_label` + `qemu_ga_msi` (verify the msi name against your
virtio-win version — it varies) · `autologon_count`.

## Gates

Every build runs fail-fast gates 0–7 (plus 2b for `--gpu 3d`) in
`windows_vm/orchestrator.py`: host tools, Windows media, virtio-win drivers,
answer file and seed ISO, no-prompt ISO, disk/vars/launcher, install-info,
then launch and verification (Gate 0 exits with brew hints when a host tool
is missing). Details:
[docs/ARCHITECTURE.md → Gated provisioning pipeline](docs/ARCHITECTURE.md#gated-provisioning-pipeline).

## Troubleshooting

* **QEMU window opens small (800×600) during Setup** → expected. Windows
  Setup draws through the firmware framebuffer until it has installed Windows
  and booted from the disk; once the virtio GPU driver loads after the first
  reboot, its resolution is set in Windows' Display settings.
* **Display resolution is greyed out in Settings** → `launch-vm.sh` is missing
  `ramfb` (a launcher generated before it was always attached). Re-run the
  build to regenerate it; with `ramfb` attached, the virtio GPU display's
  resolution can be set in Windows' Display settings.
* **Two displays in Windows** → expected: `launch-vm.sh` always attaches the
  `ramfb` framebuffer (Setup's boot display) next to the virtio GPU. The
  larger, resizable one is the virtio GPU; pick "Show only on" it in Windows'
  Display settings to hide the `ramfb` one. In the QEMU window, switch between
  them from the View menu.
* **"This PC doesn't meet the minimum system requirements"** → swtpm not
  attached. Check `brew install swtpm`; `launch-vm.sh` spawns its own swtpm,
  so check its terminal output and that `swtpm.sock` appears in the target dir.
* **Guest drops to the UEFI shell** → storage driver missing at setup time;
  check that `.seed/$WinPEDriver$/viostor` exists in the target dir (it is
  packed into `seed.iso`).
* **Setup error `0xD000A000 - 0x40031`** → an unsigned/test-signed driver is in
  `$WinPEDriver$`; only the virtio-win drivers belong there (`viogpu3d`
  installs after OOBE — see
  [guest test-signing flow](docs/ARCHITECTURE.md#guest-test-signing-flow)).
* **"Let's connect you to a network" / Microsoft-account OOBE** → the
  oobeSystem `Hide*` settings didn't apply; almost always means the answer
  file was not found (wrong seed ISO layout — autounattend.xml must be at the
  ISO root) or a component architecture typo (all must be `arm64`).
* **No sound, or the microphone records silence** → see
  [Audio & microphone](#audio--microphone): grant the app that launched
  `launch-vm.sh` microphone access, check qemu-virgl is new enough, or use RDP
  audio instead.
* **`'virtio-gpu-gl-pci' is not a valid device model name`** or **`venus`
  unknown** → the launcher runs a QEMU without virglrenderer (a `launch-vm.sh`
  rendered before Gate 0 resolved qemu-virgl by absolute path still calls bare
  `qemu-system-aarch64`, i.e. stock Homebrew QEMU); install the `qemu-virgl`
  tap and rerun the build against the same `--target-dir` to regenerate the
  launcher with the absolute qemu-virgl path — no relinking needed (see
  [viogpu3d](#what-it-needs-host)).
* **`viogpu3d archive sha256 mismatch`** → the download was corrupt or the
  release changed; the build already removed the bad `viogpu3d.zip`, so rerun.
* **`no ….sha256 published`** → the Latest kvm-guest-drivers-windows release has no
  checksum file (or there is no release); pin one with `--viogpu3d-url` +
  `--viogpu3d-sha256`.
* **Still "Microsoft Basic Display Adapter" after installing viogpu3d** → test
  signing is off or the certificate isn't trusted; check `bcdedit` shows
  `testsigning Yes` and redo the certificate step.

## viogpu3d (experimental 3D)

With `--gpu 3d` the VM uses `virtio-gpu-gl-pci` with `venus=on,blob=on,hostmem=<MB>`
(`-display cocoa,gl=es`) and the
ARM64 `viogpu3d` driver from [milesbuckton/kvm-guest-drivers-windows](https://github.com/milesbuckton/kvm-guest-drivers-windows).
By default it downloads `viogpu3d-arm64-win11.zip` from the **Latest** release and
checks it against the `viogpu3d-arm64-win11.zip.sha256` file published alongside it.
Override with `--viogpu3d-url` (plus `--viogpu3d-sha256` to pin a build; a custom URL
with neither a `--viogpu3d-sha256` nor a published `.sha256` is used unverified) or point at an
already-extracted tree with `--viogpu3d-drivers`.

### What it needs (host)

| Requirement | Why |
|---|---|
| QEMU with `virtio-gpu-gl-pci` + virglrenderer (`venus`, `blob`) | Venus/Vulkan path. Stock Homebrew `qemu` on macOS is built without virglrenderer; use the [milesbuckton/qemu-virgl](https://github.com/milesbuckton/homebrew-qemu-virgl) tap: `brew tap milesbuckton/qemu-virgl && brew install qemu-virgl` (no linking needed; bottles for macOS 27 only; elsewhere it builds from source) |
| Vulkan-capable host GPU | Apple Silicon GPU through the driver picked by `--host-vulkan-driver`: `moltenvk` (default; `brew install molten-vk`, ICD `/opt/homebrew/etc/vulkan/icd.d/MoltenVK_icd.json`) or `kosmickrisp` (macOS 26+, where `qemu-virgl` installs it as a dependency; keg-only, bottled on macOS 27 and built from source elsewhere, ICD under `/opt/homebrew/opt/kosmickrisp/share/vulkan/icd.d/`). The build fails at gate 0 if the ICD is missing; `launch-vm.sh` exports it as `VK_DRIVER_FILES` and `VK_ICD_FILENAMES` |
| `hostmem` aperture | guest-visible graphics memory; `--hostmem-mb`, default 1024 MB, raise for heavy workloads |

Check qemu-virgl supports it:

```bash
/opt/homebrew/opt/qemu-virgl/bin/qemu-system-aarch64 -device virtio-gpu-gl-pci,help | grep -E 'venus|blob|hostmem'
```

### What it needs (guest)

The driver is test-signed, not Microsoft-signed. The build installs it
unattended; how, and where the logs are, is in
[docs/ARCHITECTURE.md → Guest test-signing flow](docs/ARCHITECTURE.md#guest-test-signing-flow).

If performing the steps manually from an administrator prompt:

1. Secure Boot must be off. The qemu-virgl tap's edk2 firmware does not enforce
   it, so this is already satisfied.
2. Trust the test certificate and enable test-signing mode, then reboot.
   The driver files are in `C:\viogpu3d` (after first logon) and on the
   `UNATTEND` seed drive under `viogpu3d` — once `.install-complete` exists that
   drive is only attached with `INSTALL_MEDIA=1` (check its drive letter in
   Explorer; `D:` below):
   ```bat
   certutil -addstore -f Root "D:\viogpu3d\VirtIOTestCert.cer"
   certutil -addstore -f TrustedPublisher "D:\viogpu3d\VirtIOTestCert.cer"
   bcdedit /set testsigning on
   ```
3. Disable hibernation and Windows Fast Startup:
   ```bat
   powercfg /hibernate off
   ```
   Fast Startup is not supported by the alpha driver; it can restore stale
   graphics-driver state after QEMU and virglrenderer have started with fresh
   host state, causing resource-allocation failures on the next boot. Use a full
   shutdown or restart instead.
4. Install the driver, then reboot:
   ```bat
   pnputil /add-driver "D:\viogpu3d\viogpu3d.inf" /install
   ```
5. Optional: install the Vulkan Runtime from [lunarg.com](https://vulkan.lunarg.com/sdk/home)
   for the `vulkan-1.dll` loader and `vulkaninfo` utility.

Check it worked: `dxdiag` → Display shows `viogpu3d.sys` as the main driver
(not `basicdisplay.sys`). The "Test Mode" watermark alone only proves test
signing is on. The virtio-win ISO's display-only driver is not staged, because
Windows would prefer it (Microsoft-signed) over `viogpu3d` for the same GPU.

### Where the driver comes from

[milesbuckton/kvm-guest-drivers-windows](https://github.com/milesbuckton/kvm-guest-drivers-windows)
(a fork of [virtio-win/kvm-guest-drivers-windows](https://github.com/virtio-win/kvm-guest-drivers-windows),
started from arehnman's Yttrium work ([arehnman/yttrium-virtio-gpu](https://github.com/arehnman/yttrium-virtio-gpu));
the original viogpu3d is upstream [PR #943](https://github.com/virtio-win/kvm-guest-drivers-windows/pull/943),
and many fixes plus the ARM64X packaging are ported from
[utmapp/viogpu-wddm](https://github.com/utmapp/viogpu-wddm) `neptune`)
builds the Win11 ARM64 package in GitHub Actions. PRs to `master` only build;
a manual `workflow_dispatch` run on `master` builds the
Mesa UMD from [miles.buckton/mesa](https://gitlab.freedesktop.org/miles.buckton/mesa)
(pinned commit), the kernel driver, test-signing, then publishes a UTC date-stamped
release (`viogpu3d-arm64-<yyyyMMdd.HHmmss>`) marked Latest with a `.sha256` file.
To pin a build instead of following Latest:

```bash
python3 setup_vm.py --iso ~/Downloads/Win11_Arm64.iso \
  --viogpu3d-url https://github.com/milesbuckton/kvm-guest-drivers-windows/releases/download/<tag>/viogpu3d-arm64-win11.zip \
  --viogpu3d-sha256 <sha256 from the release notes>
```

To build it yourself instead, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#building-arm64-viogpu3d-driver-from-source).

### Known limitations

* **The Mesa UMD comes from [miles.buckton/mesa](https://gitlab.freedesktop.org/miles.buckton/mesa)**
  at a commit pinned in the CI workflow (listed in each release's notes) and is
  built with MSVC. The UMDs ship as ARM64X DLLs (arm64 + arm64ec), so x64 apps
  under emulation are covered; 32-bit x86 apps are not. Which API takes which
  path: [3D graphics pipeline](docs/ARCHITECTURE.md#3d-graphics-pipeline---gpu-3d).
* **Alpha software.** The package installs unattended across first logon and
  the following reboot, but it may crash or hang a guest. Not for production
  work or data you cannot afford to lose.

## License

Code: provided as-is. Windows, virtio-win and all Microsoft/Google/Fedora
assets are downloaded by you at runtime and remain under their own licenses.
