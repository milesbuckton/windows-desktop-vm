# Contributing to windows-desktop-vm

This project is a personal automation framework for building and running
unattended Windows 11 ARM64 virtual machines on macOS Apple Silicon hosts
using QEMU/HVF.

While this repository is primarily a personal artifact (external pull requests
are automatically closed per the project policy in `docs/CONTRIBUTING.md`),
forks and adaptations are encouraged under the terms of the MIT license.

If you are maintaining a fork or building customized artifacts (such as
experimental 3D drivers), this document outlines the project's development
workflow, coding standards, and build instructions.

---

## Development Environment & Testing

### Host Requirements (macOS Apple Silicon)

- **Python 3.9+** with the packages in `requirements.txt`
  (`python3 -m pip install -r requirements.txt`: Jinja2, pylint)
- **QEMU (`qemu-system-aarch64`, `qemu-img`)**: `brew tap milesbuckton/qemu-virgl && brew install qemu-virgl && brew link --force qemu-virgl` (stock Homebrew `qemu` lacks virglrenderer/Venus; if it is installed, run `brew unlink qemu && brew link --force qemu-virgl` — Gate 0 fails when `qemu-system-aarch64` has no `virtio-gpu-gl-pci`)
- **swtpm**: `brew install swtpm`
- **xorriso**: `brew install xorriso`
- **wimlib**: `brew install wimlib`

### Running the Test Suite

Before making changes, verify that the existing unit tests pass:

```bash
python3 scripts/run-tests.py        # or: python3 -m unittest discover -s tests
```

The test suite covers:
- Answer file (`autounattend.xml`) template rendering and XML well-formedness
- Launcher script generation (`virtio-gpu-gl-pci` + Venus, the only display path)
- Driver ISO verification and directory layouts
- viogpu3d package download, SHA-256 sidecar check and layout discovery
- WIM index metadata inspection
- Host port allocation and probe logic
- QEMU Guest Agent (QGA) protocol client simulation

### Code Style & Linters

Python source code adheres to PEP 8 standards with strict formatting:

```bash
python3 scripts/lint-python.py
```

Or invoke `pylint` directly using the repository configuration:

```bash
pylint --rcfile=.pylintrc windows_vm tests setup_vm.py
```

---

## Architecture & Code Conventions

1. **Seven-Gate Orchestrator:**
   The provisioning pipeline is partitioned into ordered gates
   (`windows_vm/orchestrator.py`), each failing fast with a `GateError`:
   - Gate 0: Host tools present (detect, don't install)
   - Gate 1: Windows ISO validation & WIM index resolution
   - Gate 2: virtio-win ISO verification (ARM64 driver paths), then
     fetch + verify the viogpu3d package
   - Gate 3: Autounattend XML rendering & seed ISO creation (`scripts/make-seed-iso.sh`)
   - Gate 4: No-prompt boot copy of the Windows ISO (drivers go into `$WinPEDriver$` on the seed ISO in gate 3, `scripts/inject-drivers.sh`)
   - Gate 5: Platform assets (qcow2 disk, per-VM OVMF UEFI variables, launcher script)
   - Gate 6: Single-source credential and socket metadata (`install-info.json`)
   - Gate 7 (`--start` only): post-install verification via QGA ping, marker
     file and RDP forward

2. **Template Isolation:**
   All shell and XML assets are rendered with Jinja2 via `windows_vm/templates.py`.
   Logic should live in Python helper functions rather than complex template tags.

3. **Single Display Path:**
   3D via `viogpu3d` (alpha, test-signed) on `virtio-gpu-gl-pci` is the only
   display path; there is no display-only fallback. Building and running a VM
   requires QEMU from the `milesbuckton/qemu-virgl` tap.

---

## Building ARM64 viogpu3d Driver from Source

The [kvm-guest-drivers-windows](https://github.com/milesbuckton/kvm-guest-drivers-windows)
fork builds and publishes this package automatically
(`.github/workflows/build-arm64.yml` on branch `master`): every push
produces a test-signed `viogpu3d-arm64-win11.zip` + `.sha256` in a release
marked Latest, which `setup_vm.py` downloads by default. That
workflow is the reference for the steps below (it builds Mesa with MSVC
`x64_arm64`, stubs the Vulkan ICD, and disables Mesa's d3d10 test). Build by
hand only if you need to change the driver or Mesa.

### Prerequisites (Windows build host or VM)

1. **Visual Studio 2022 (or Build Tools)** with the C++ ARM64 build tools, plus the
   **Windows Driver Kit** (CI: `choco install windowsdriverkit10`). The 24H2 EWDK also works.
2. **Python 3** with `pip install meson ninja mako pyyaml packaging`.
3. **win_flex / win_bison** (`choco install winflexbison3`).
4. Clones of:
   - Kernel driver: [milesbuckton/kvm-guest-drivers-windows](https://github.com/milesbuckton/kvm-guest-drivers-windows) (branch `master`)
   - Mesa user-mode driver: [arehnman/virtio-win-mesa](https://github.com/arehnman/virtio-win-mesa) (branch `yttrium-experimental`)

### Step 1: Build the Mesa ARM64 user-mode drivers

From a PowerShell with the MSVC **x64_arm64** environment loaded (the
`d3d10umddi.h` / `d3dkmddi.h` WDK headers must be on `INCLUDE`), in the
`virtio-win-mesa` checkout. `cross_arm64.txt` is the Meson cross file from the
workflow (`cl`/`lib`/`rc`, `cpu_family = 'aarch64'`, `system = 'windows'`):

```powershell
$env:MESA_PREFIX_ARM64 = "$PWD\..\mesa_prefix_arm64"

meson setup build-arm64 `
  --cross-file cross_arm64.txt `
  --prefix $env:MESA_PREFIX_ARM64 `
  -Dbuildtype=release -Db_ndebug=true -Dbuild-tests=false `
  -Dc_args="/wd4189" -Dcpp_args="/wd4189" `
  -Dgallium-drivers=virgl -Dgallium-d3d10umd=true -Dgallium-yttrium=true `
  -Dgallium-wgl-dll-name=viogpu_wgl -Dgallium-d3d10-dll-name=viogpu_d3d10 `
  -Dvulkan-drivers="" `
  -Dzlib=disabled -Dc_std=c11 -Db_vscrt=mt

ninja -C build-arm64 install
```

Also disable the `d3d10` test in `src/gallium/targets/d3d10umd/meson.build`
(it does not build for ARM64). MSVC cannot build Mesa's virtio Vulkan driver,
so the workflow then compiles a stub `libvulkan_virtio.dll` and writes a stub
`share\vulkan\icd.d\virtio_icd.aarch64.json` into the prefix — the driver
project requires both to exist. `$env:MESA_PREFIX_ARM64\bin` must end up with
`viogpu_d3d10.dll`, `viogpu_wgl.dll`, `libvulkan_virtio.dll`, `libEGL.dll`,
`libGLESv1_CM.dll`, `libGLESv2.dll` and `opengl32.dll`.

### Step 2: Build the viogpu3d kernel-mode driver (KMD)

From the same environment, in the `kvm-guest-drivers-windows` checkout:

```powershell
cd viogpu
msbuild viogpu3d.sln /t:Build /p:Configuration="Win11 Release" `
  /p:Platform=ARM64 /p:MESA_PREFIX_ARM64="$env:MESA_PREFIX_ARM64"
```

The packaged driver tree lands in `viogpu\Install\Win11\ARM64`
(`viogpu3d.inf`, `viogpu3d.sys`, `viogpu3d.cat`, `vgpusrv.exe`, `viogpuap.exe`
and the `*_arm64.dll` Mesa binaries). Test-sign `viogpu3d.sys` and
`viogpu3d.cat` with `build\VirtIOTestCert.pfx`
(`signtool sign /fd SHA256 /f build\VirtIOTestCert.pfx <file>`) and copy
`build\VirtIOTestCert.cer` alongside — the guest needs it to trust the driver.

### Step 3: Supplying the Driver to windows-desktop-vm

Transfer the output folder to your macOS host and pass it during VM generation:

```bash
python3 setup_vm.py \
  --viogpu3d-drivers /path/to/extracted/Install/Win11/ARM64 \
  --iso ~/Downloads/Win11_24H2_English_Arm64.iso \
  --target-dir ~/VMs/windows-11
```
