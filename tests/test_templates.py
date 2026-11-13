import unittest

from windows_vm import templates


VARS = {
    "wim_index": "6",
    "product_key": "W269N-WFGWX-YVC9B-4J6C9-T83GX",
    "hostname": "windows-vm",
    "username": "windows",
    "user_password": "windows",
}


class TestAutounattend(unittest.TestCase):
    def test_render_validates(self):
        xml = templates.render_autounattend(dict(VARS))
        templates.validate_autounattend(xml)  # must not raise

    def test_all_components_arm64(self):
        xml = templates.render_autounattend(dict(VARS))
        self.assertIn('processorArchitecture="arm64"', xml)
        self.assertNotIn('processorArchitecture="amd64"', xml)

    def test_labconfig_bypass_present(self):
        xml = templates.render_autounattend(dict(VARS))
        for key in ("BypassTPMCheck", "BypassSecureBootCheck",
                    "BypassRAMCheck", "BypassCPUCheck"):
            self.assertIn(key, xml)

    def test_empty_wim_index_rejected(self):
        bad = dict(VARS, wim_index="")
        xml = templates.render_autounattend(bad)
        with self.assertRaises(templates.ValidationError):
            templates.validate_autounattend(xml)

    def test_malformed_xml_rejected(self):
        with self.assertRaises(templates.ValidationError):
            templates.validate_autounattend("<unattend><broken>")

    def test_render_no_product_key_omits_key_and_validates(self):
        vars_no_key = dict(VARS)
        vars_no_key["product_key"] = ""
        xml = templates.render_autounattend(vars_no_key)
        self.assertNotIn("<ProductKey>", xml)
        templates.validate_autounattend(xml)

    def test_all_generic_kms_keys_validate(self):
        for edition, key in templates.GENERIC_KMS_KEYS.items():
            vars_with_key = dict(VARS, product_key=key)
            xml = templates.render_autounattend(vars_with_key)
            self.assertIn(f"<Key>{key}</Key>", xml)
            templates.validate_autounattend(xml)

    def test_rdp_firewall_sets_private_profile(self):
        xml = templates.render_autounattend(dict(VARS))
        self.assertIn("Set-NetConnectionProfile -NetworkCategory Private", xml)

    def test_oobe_international_core_configured(self):
        vars_lang = dict(VARS, ui_language="en-GB")
        xml = templates.render_autounattend(vars_lang)
        self.assertIn("<UILanguage>en-GB</UILanguage>", xml)
        self.assertIn('pass="oobeSystem"', xml)
        templates.validate_autounattend(xml)

    def test_first_logon_viogpu3d(self):
        xml = templates.render_autounattend(dict(VARS, gpu="3d"))
        self.assertIn("viogpu3d-install", xml)
        self.assertIn("install.cmd", xml)
        self.assertIn("bcdedit /set testsigning on", xml)
        templates.validate_autounattend(xml)


    def test_first_logon_2d_default(self):
        xml = templates.render_autounattend(VARS)
        templates.validate_autounattend(xml)
        self.assertNotIn("viogpu3d", xml)
        self.assertNotIn("testsigning", xml)
        self.assertIn("powercfg /hibernate off", xml)


class TestLauncher(unittest.TestCase):
    def test_render_launcher_2d_default(self):
        out = templates.render_launcher({
            "vm_name": "windows-11", "vcpus": 4, "memory_mb": 8192,
            "xres": 1920, "yres": 1080, "rdp_port": 3389,
            "target_dir": "/tmp/vm", "ovmf_code": "/c", "ovmf_vars": "/v",
            "windows_iso": "/w", "virtio_iso": "/vi", "seed_iso": "/s",
            "disk_qcow2": "/d", "hostmem_mb": 1024,
        })
        self.assertIn("-device virtio-gpu-pci,xres=1920,yres=1080 ", out)
        self.assertNotIn("virtio-gpu-gl-pci", out)
        self.assertNotIn("venus", out)
        self.assertIn("-display cocoa \\", out)

    def test_render_contains_essentials(self):
        out = templates.render_launcher({
            "vm_name": "windows-11", "vcpus": 4, "memory_mb": 8192,
            "xres": 3456, "yres": 2234, "rdp_port": 3389,
            "target_dir": "/tmp/vm", "ovmf_code": "/c", "ovmf_vars": "/v",
            "windows_iso": "/w", "virtio_iso": "/vi", "seed_iso": "/s",
            "disk_qcow2": "/d", "hostmem_mb": 1024, "gpu": "3d",
        })
        for needle in ("tpm-tis-device", "hostfwd=tcp:127.0.0.1:3389",
                       "virtio-gpu-gl-pci", "localtime"):
            self.assertIn(needle, out)
        self.assertNotIn("virtio-gpu-pci", out)
        self.assertNotIn("viogpudo", out)

    def test_render_launcher_no_tpm(self):
        out = templates.render_launcher({
            "vm_name": "windows-11", "vcpus": 4, "memory_mb": 8192,
            "xres": 3456, "yres": 2234, "rdp_port": 3389,
            "target_dir": "/tmp/vm", "ovmf_code": "/c", "ovmf_vars": "/v",
            "windows_iso": "/w", "virtio_iso": "/vi", "seed_iso": "/s",
            "disk_qcow2": "/d", "no_tpm": True,
            "hostmem_mb": 1024, "gpu": "3d",
        })
        self.assertNotIn("tpm-tis-device", out)
        self.assertNotIn("swtpm", out)
        self.assertNotIn("chrtpm", out)
        self.assertIn("virtio-gpu-gl-pci", out)
        self.assertIn("qemu-system-aarch64", out)

    def test_render_launcher_gpu(self):
        out = templates.render_launcher({
            "vm_name": "windows-11", "vcpus": 4, "memory_mb": 8192,
            "xres": 3456, "yres": 2234, "rdp_port": 3389,
            "target_dir": "/tmp/vm", "ovmf_code": "/c", "ovmf_vars": "/v",
            "windows_iso": "/w", "virtio_iso": "/vi", "seed_iso": "/s",
            "disk_qcow2": "/d", "hostmem_mb": 2048, "gpu": "3d",
        })
        self.assertIn("virtio-gpu-gl-pci", out)
        self.assertIn("venus=on", out)
        self.assertIn("blob=on", out)
        self.assertIn("hostmem=2048M", out)
        self.assertIn("-display cocoa,gl=es", out)
        self.assertIn("DYLD_LIBRARY_PATH", out)
        self.assertIn("VK_ICD_FILENAMES", out)
        self.assertNotIn("virtio-gpu-pci", out)
        self.assertNotIn("viogpudo", out)

    def test_render_launcher_no_exec(self):
        out = templates.render_launcher({
            "vm_name": "windows-11", "vcpus": 4, "memory_mb": 8192,
            "xres": 3456, "yres": 2234, "rdp_port": 3389,
            "target_dir": "/tmp/vm", "ovmf_code": "/c", "ovmf_vars": "/v",
            "windows_iso": "/w", "virtio_iso": "/vi", "seed_iso": "/s",
            "disk_qcow2": "/d",
        })
        self.assertNotIn("exec qemu-system-aarch64", out)
        self.assertIn('"$TARGET_DIR/.install-complete"', out)
        self.assertIn('"${INSTALL_MEDIA_ARGS[@]+"${INSTALL_MEDIA_ARGS[@]}"}"', out)
        self.assertIn("RAMFB_ARGS=(-device ramfb)", out)
        self.assertNotIn("  -device ramfb \\", out)
        self.assertIn("qemu-system-aarch64", out)
        self.assertIn('"$@"', out)
        self.assertIn("trap cleanup EXIT", out)


if __name__ == "__main__":
    unittest.main()
