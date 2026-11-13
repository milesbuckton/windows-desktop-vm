# windows-desktop-vm

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
>   stock Homebrew `edk2-aarch64` firmware does not enforce it) and a few manual
>   steps in the guest — see [viogpu3d](#viogpu3d-experimental-3d). Either mode needs the
>   [qemu-virgl](https://github.com/milesbuckton/homebrew-qemu-virgl) Homebrew tap.
> * **Audio** rides Intel HDA (inbox driver) — there is no virtio-snd Windows
>   driver — but RDP audio is the reliable path.
> * **Nothing Microsoft is redistributed.** You download the ISO and supply
>   your own license. The default `product_key` is the public generic KMS
>   client key for the selected edition (Pro, Home, Enterprise, Education, etc.),
>   used only to select the edition without activation.

## How a build works

```
setup_vm.py
  1. Resolve + fetch media:  Windows 11 ARM64 ISO (pinned in manifest/windows.json;
     --iso override) and virtio-win stable ISO (discoverable URL).
  2. Gate 1-2: inspect sources/install.wim with wimlib-imagex; resolve the
     edition index; render autounattend.xml (templates/autounattend.j2).
  3. scripts/inject-drivers.sh: stage the ARM64 virtio drivers as $WinPEDriver$
     on the seed ISO (Setup scans every drive's root for it); the Windows ISO is
     an APFS clone with the no-prompt boot image swapped in.
  4. scripts/make-seed-iso.sh: build seed.iso carrying autounattend.xml at root
     (macOS-native hdiutil makehybrid).
  5. qemu-img create the qcow2; copy OVMF vars per VM; render launch-vm.sh;
     write install-info.json/.txt.
  6. Launch (only with --start; otherwise run launch-vm.sh yourself). Windows
     Setup runs fully unattended (TPM via swtpm satisfies the Win11 hardware
     check; LabConfig bypass keys are belt-and-braces).
  7. FirstLogonCommands: enable RDP, install qemu-ga, write marker
     C:\Windows\Setup\windows-vm-setup.log  (ground truth, like *-vm-setup.log).
     With --start, the build waits (up to 90 min) for the marker + RDP and
     prints VERIFY-OK. It then writes .install-complete, so later boots of
     launch-vm.sh leave out the three install CDs (see "Install media").
```

## Requirements (host)

| Tool | Install | Why |
|---|---|---|
| Python 3.9+ with Jinja2 | `python3 -m pip install -r requirements.txt` | renders the answer file and launcher |
| QEMU with virglrenderer (`qemu-system-aarch64`, `qemu-img`) | `brew tap milesbuckton/qemu-virgl && brew install qemu-virgl && brew link --force qemu-virgl` | hypervisor (HVF) + Venus 3D; **detect, don't install** |
| swtpm | `brew install swtpm` | TPM 2.0 — Windows 11 requirement |
| xorriso | `brew install xorriso` | extracting drivers from virtio-win.iso |
| wimlib | `brew install wimlib` | install.wim index resolution (Gate 2) |

Stock Homebrew `qemu` does **not** work: it lacks `virtio-gpu-gl-pci` and names
its AArch64 vars template `edk2-arm-vars.fd`. If it is installed, it keeps the
`qemu-system-aarch64` link even after `brew install qemu-virgl`; Gate 0 checks
`qemu-system-aarch64 -device help` for `virtio-gpu-gl-pci` and fails early. Fix:
`brew unlink qemu && brew link --force qemu-virgl` (or `brew uninstall qemu`
when nothing depends on it: `brew uses --installed qemu`).

Firmware: the `edk2-aarch64-code.fd` / `edk2-aarch64-vars.fd` pair shipped by
qemu-virgl, with a per-VM writable vars copy.
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
# Build only (prints the launch command at the end):
python3 setup_vm.py --iso ~/Downloads/Win11_Arm64.iso

# Build, launch, and wait for the unattended install to finish (VERIFY-OK):
python3 setup_vm.py --iso ~/Downloads/Win11_Arm64.iso --start

# Watch progress with the live phase-based progress bar:
python3 setup_vm.py monitor ~/VMs/windows-11          # live
python3 setup_vm.py monitor ~/VMs/windows-11 --once   # print once and exit
python3 setup_vm.py monitor ~/VMs/windows-11 --tail   # include marker text

# Start an already-built VM (also printed at the end of a build):
bash ~/VMs/windows-11/launch-vm.sh
```

### Install media

During installation the VM has three CD drives: the Windows ISO, the virtio-win
ISO and `UNATTEND` (`seed.iso`, the answer file plus drivers). None of them are
needed once Windows is installed. When the install verifies (the build with
`--start`, or `setup_vm.py monitor` reaching `complete`), the target dir gets a
`.install-complete` file and `launch-vm.sh` stops attaching them. To attach them
again for one boot (for example to reinstall `viogpu3d` from `UNATTEND`), run
`INSTALL_MEDIA=1 bash ~/VMs/windows-11/launch-vm.sh`; delete `.install-complete`
to attach them every time.

Without `--iso`, a Windows 11 Arm64 ISO in `~/Downloads` is used, else the
one in `manifest/windows.json` (a placeholder
until you record your own). `python3 setup_vm.py --help` lists every option
(`--edition`, `--password`, `--vcpus`, `--memory-mb`, `--disk-gb`,
`--xres`/`--yres`, `--gpu 2d|3d`, `--rdp-port`, `--no-tpm`, the viogpu3d flags, …).

A build writes everything into `--target-dir` (default `~/VMs/windows-11`):
`disk.qcow2`, `edk2-aarch64-vars.fd`, `virtio-win.iso` (unless `--virtio-iso`),
`seed.iso` + `.seed/` (answer file, `$WinPEDriver$`, `viogpu3d/`),
`<iso-name>-noprompt.iso` (APFS clone of the Windows ISO that boots without
"Press any key"), `launch-vm.sh`, `install-info.json` / `install-info.txt` (RDP port and
credentials, chmod 600), `.cache/` (wim index), `viogpu3d/` + `viogpu3d.zip`
(delete both to fetch a newer release), `swtpm-state/`
(the VM's TPM), `.install-complete` once the install verifies, plus
`monitor.sock` / `qga.sock` / `swtpm.sock` while running.

Tests: `python3 scripts/run-tests.py` (or `python3 -m unittest discover -s tests`):
template rendering + validation, wim-index parsing, port probing, qemu-ga
client, install monitor, viogpu3d download/checksum. Lint:
`python3 scripts/lint-python.py`. Fleet builds:
`python3 scripts/build-fleet-sequential.py` (edit CONFIGS at the top; each VM
is built with `--start`, one after another).

| Setting | Default |
|---|---|
| Distro | Windows 11 Pro (ARM64), index resolved at build time |
| vCPU / RAM / disk | 4 / 8192 MB / 80 GB qcow2 |
| User / password | `windows` / `windows` (recorded in `install-info.txt`, chmod 600) |
| Hostname | `windows-vm` |
| Timezone / locale | `South Africa Standard Time` / auto-detected from ISO (override with `--ui-language`) |
| Display | `virtio-gpu-pci` 3456×2234 + `viogpudo` (2D); `--gpu 3d`: `virtio-gpu-gl-pci` with Venus + `viogpu3d` (experimental) |
| Management | RDP on `127.0.0.1`, first free port in 3389–3399 (or `--rdp-port`); loopback-only forward; Pro required for RDP server |

## Day-2 operations

| Task | Command |
|---|---|
| RDP in | any RDP client → the address in `install-info.txt` (e.g. `127.0.0.1:3389`) |
| Graceful poweroff | `echo system_powerdown | socat - UNIX:~/VMs/windows-11/monitor.sock` |
| Snapshot | `qemu-img snapshot -c <name> disk.qcow2` (VM off) |
| Check guest state | marker: `C:\Windows\Setup\windows-vm-setup.log`; qemu-ga via `qga.sock` |
| Re-run installer | VM off; delete `disk.qcow2`, `edk2-aarch64-vars.fd` and `swtpm-state/`, then rebuild (an existing disk/vars is kept, not overwritten). Deleting the whole target dir also works but re-copies (or re-downloads) `virtio-win.iso`. |

## Template variables (templates/autounattend.j2)

`wim_index` (required, build-resolved) · `product_key` · `hostname` · `username` /
`user_password` (XML-escaped via `| e`) · `timezone` · `*_locale` ·
`bypass_requirements` (default true) · `driver_path_winpe` (fallback driver
path; empty = rely on `$WinPEDriver$` on the seed ISO) · `install_qemu_ga` +
`virtio_volume_label` + `qemu_ga_msi` (verify the msi name against your
virtio-win version — it varies) · `autologon_count`.

## Gates (`windows_vm/orchestrator.py`, mirrors the seven-gate Linux builder)

0. Host tools on PATH (`qemu-img`, `xorriso`, `wimlib-imagex`, `swtpm` unless `--no-tpm`, `hdiutil`) — else exit with brew hints
1. ISO readable; `sources/install.wim` present (`install.esd` → reject, convert first); `wimlib-imagex info` resolves the `--edition` index (cached in `.cache/`)
2. virtio-win ISO contains the `*/w11/ARM64` driver directories (missing qemu-ga msi is only a warning); the viogpu3d package is fetched/verified (2b)
3. Rendered autounattend.xml well-formed, every component `processorArchitecture="arm64"`; seed ISO built
4. No-prompt copy of the Windows ISO (`efisys_noprompt.bin` swapped into the El Torito slot; UDF untouched); drivers are staged in gate 3
5. OVMF firmware present; qcow2 created; per-VM vars copied; `launch-vm.sh` rendered and `bash -n` checked
6. `install-info.json` / `.txt` written
7. With `--start`: qemu-ga answers on `qga.sock`, marker file present, RDP forward accepting. virtio-win has no ARM64 qemu-ga, so without the agent the guest's RDP service answering an RDP handshake counts instead (RDP only turns on at first logon); writes `.install-complete`

## Troubleshooting

* **QEMU window opens small (800×600) during Setup** → expected. Windows
  Setup draws through the firmware framebuffer until it has installed Windows
  and booted from the disk; the resolution jumps to `--xres`×`--yres`
  (default 3456×2234) once the virtio GPU driver loads after the first reboot.
* **Two displays in Windows** → the install-time `ramfb` framebuffer. It is
  only attached while the install media is (see "Install media"); a VM built
  before that change needs `launch-vm.sh` regenerated by re-running the build.
* **"This PC doesn't meet the minimum system requirements"** → swtpm not
  attached. `launch-vm.sh` spawns its own swtpm; check its terminal output
  and that `swtpm.sock` appears in the target dir.
* **Guest drops to the UEFI shell** → storage driver missing at setup time;
  check that `.seed/$WinPEDriver$/viostor` exists in the target dir.
* **Setup error `0xD000A000 - 0x40031`** → an unsigned/test-signed driver is in
  `$WinPEDriver$`; only the virtio-win drivers belong there.
* **"Let's connect you to a network" / Microsoft-account OOBE** → the
  oobeSystem `Hide*` settings didn't apply; almost always means the answer
  file was not found (wrong seed ISO layout — autounattend.xml must be at the
  ISO root) or a component architecture typo (all must be `arm64`).
* **No sound** → expected via QEMU cocoa output on some hosts; use RDP audio.
* **`'virtio-gpu-gl-pci' is not a valid device model name`** or **`venus`
  unknown** → the QEMU on `PATH` lacks virglrenderer; install
  and link the `qemu-virgl` tap (see [viogpu3d](#what-it-needs-host)).
* **`viogpu3d archive sha256 mismatch`** → a stale `viogpu3d.zip` from an older
  release is in the target dir; delete `viogpu3d.zip` and `viogpu3d/` and rebuild.
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
Override with `--viogpu3d-url` (plus `--viogpu3d-sha256` to pin a build) or point at an
already-extracted tree with `--viogpu3d-drivers`.

### What it needs (host)

| Requirement | Why |
|---|---|
| QEMU with `virtio-gpu-gl-pci` + virglrenderer (`venus`, `blob`) | Venus/Vulkan path. Stock Homebrew `qemu` on macOS is built without virglrenderer; use the [milesbuckton/qemu-virgl](https://github.com/milesbuckton/homebrew-qemu-virgl) tap: `brew tap milesbuckton/qemu-virgl && brew install qemu-virgl && brew link --force qemu-virgl` |
| Vulkan-capable host GPU | Apple Silicon GPU works through the Mesa/Vulkan stack |
| `hostmem` aperture | guest-visible graphics memory; `--hostmem-mb`, default 1024 MB, raise for heavy workloads |

Check the QEMU on your `PATH` supports it:

```bash
qemu-system-aarch64 -device virtio-gpu-gl-pci,help | grep -E 'venus|blob|hostmem'
```

### What it needs (guest)

The driver is test-signed, not Microsoft-signed. The answer file (`autounattend.xml`) automatically configures test-signing
(`bcdedit /set testsigning on`), trusts `VirtIOTestCert.cer` into Root and
TrustedPublisher stores, disables hibernation (`powercfg /hibernate off`), installs
`viogpu3d.inf`, and triggers a reboot on first logon.

If performing the steps manually from an administrator prompt:

1. Secure Boot must be off. The stock Homebrew edk2 firmware does not enforce
   it, so this is already satisfied.
2. Trust the test certificate and enable test-signing mode, then reboot.
   The driver files are on the `UNATTEND` seed drive under `viogpu3d`
   (check its drive letter in Explorer; `D:` below):
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
(a fork of [arehnman/kvm-guest-drivers-windows](https://github.com/arehnman/kvm-guest-drivers-windows),
itself based on [virtio-win PR #943](https://github.com/virtio-win/kvm-guest-drivers-windows/pull/943))
builds the Win11 ARM64 package in GitHub Actions on every push to `master`:
Mesa UMD from [arehnman/virtio-win-mesa](https://github.com/arehnman/virtio-win-mesa)
(`yttrium-experimental`), the kernel driver, test-signing, then a date-stamped
release (`viogpu3d-arm64-<yyyyMMdd.HHmmss>`) marked Latest with a `.sha256` file.
To pin a build instead of following Latest:

```bash
python3 setup_vm.py --iso ~/Downloads/Win11_Arm64.iso \
  --viogpu3d-url https://github.com/milesbuckton/kvm-guest-drivers-windows/releases/download/<tag>/viogpu3d-arm64-win11.zip \
  --viogpu3d-sha256 <sha256 from the release notes>
```

To build it yourself instead, see [CONTRIBUTING.md](CONTRIBUTING.md#building-arm64-viogpu3d-driver-from-source).

### Known limitations

* **Not validated end-to-end on Apple Silicon yet.** The ARM64 package builds
  in CI and installs unattended at first logon, but `viogpu3d` binding and
  rendering 3D in a Windows guest on this stack is still being verified.
* **The Mesa Vulkan driver is a stub.** The CI build uses MSVC, which cannot
  build Mesa's virtio Vulkan driver (it needs C11 atomics; upstream only
  supports mingw-w64-clang), so `libvulkan_virtio_arm64.dll` is a placeholder.
  OpenGL (WGL) and Direct3D 10/11 (d3d10umd) go through virgl.
* **NVIDIA hosts** require the [Yttrium QEMU
  Flatpak](https://github.com/arehnman/yttrium-qemu-flatpak); an unpatched
  QEMU/virglrenderer stack will likely leave the guest display black.
* **Alpha software.** May crash or hang a guest. Not for production work or data
  you cannot afford to lose.

## License

Code: provided as-is. Windows, virtio-win and all Microsoft/Google/Fedora
assets are downloaded by you at runtime and remain under their own licenses.
