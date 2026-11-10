# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for boot manager detection and menu changes (no root, no real ESP)."""

from __future__ import annotations

import unittest

from wvi import bootloaders as b

TARGET = b.WindowsTarget("Windows 11 Pro", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "8441-18C4")

EFIBOOTMGR_V = """BootCurrent: 0001
Timeout: 0 seconds
BootOrder: 0001,0000,0006
Boot0000* Windows 11 Pro\tHD(1,GPT,bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb,0x800,0x64000)/\\EFI\\MICROSOFT\\BOOT\\BOOTMGFW.EFI
Boot0001* Limine\tHD(1,GPT,aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa,0x1000,0x800000)/\\EFI\\LIMINE\\LIMINE_X64.EFI
Boot0006* UEFI OS\tHD(1,GPT,aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa,0x1000,0x800000)/\\EFI\\BOOT\\BOOTX64.EFI0000424f
"""

OLD_EFIBOOTMGR_V = """BootCurrent: 0003
BootOrder: 0003
Boot0003* ubuntu\tHD(1,GPT,0a1b2c3d-0000-0000-0000-000000000001,0x800,0x100000)/File(\\EFI\\ubuntu\\shimx64.efi)
"""


class DetectionTests(unittest.TestCase):
    def test_parse_current(self):
        self.assertEqual(b.parse_current(EFIBOOTMGR_V),
                         ("Limine", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "\\EFI\\LIMINE\\LIMINE_X64.EFI"))

    def test_parse_current_old_format(self):
        label, guid, loader = b.parse_current(OLD_EFIBOOTMGR_V)
        self.assertEqual((label, loader), ("ubuntu", "\\EFI\\ubuntu\\shimx64.efi"))
        self.assertEqual(b.kind_from_loader(loader), "grub")

    def test_loader_path_ends_at_efi(self):
        # Firmware-made entries carry optional data right after the file name
        line = ("BootCurrent: 0006\nBoot0006* UEFI OS\tHD(1,GPT,aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa,0x800,0x800000)"
                "/\\EFI\\BOOT\\BOOTX64.EFI0000424f\n")
        self.assertEqual(b.parse_current(line)[2], "\\EFI\\BOOT\\BOOTX64.EFI")
        spaced = ("BootCurrent: 0001\nBoot0001* X\tHD(1,GPT,aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa,0x800,0x800000)"
                  "/\\EFI\\My Dir\\grubx64.efi\n")
        self.assertEqual(b.parse_current(spaced)[2], "\\EFI\\My Dir\\grubx64.efi")

    def test_kind_from_name(self):
        cases = {
            "\\EFI\\LIMINE\\LIMINE_X64.EFI": "limine",
            "\\EFI\\systemd\\systemd-bootx64.efi": "systemd-boot",
            "\\EFI\\refind\\refind_x64.efi": "refind",
            "\\EFI\\fedora\\grubx64.efi": "grub",
            "\\EFI\\Linux\\arch-linux.efi": "uki",
            "\\EFI\\Microsoft\\Boot\\bootmgfw.efi": "windows",
            "\\EFI\\BOOT\\BOOTX64.EFI": "unknown",
        }
        for loader, kind in cases.items():
            self.assertEqual(b.kind_from_loader(loader), kind, loader)

    def test_kind_from_contents(self):
        fallback = "\\EFI\\BOOT\\BOOTX64.EFI"
        self.assertEqual(b.kind_from_loader(fallback, b"..++CONFIG_B2SUM_SIGNATURE++000.."), "limine")
        self.assertEqual(b.kind_from_loader(fallback, b"#### LoaderInfo: systemd-boot 257 ####"), "systemd-boot")
        self.assertEqual(b.kind_from_loader(fallback, b"#### LoaderInfo: systemd-stub 257 ####"), "uki")
        self.assertEqual(b.kind_from_loader(fallback, b"rEFInd 0.14"), "refind")


class LimineTests(unittest.TestCase):
    def test_enrolled(self):
        marker = b.LIMINE_HASH_MARKER
        self.assertFalse(b.limine_config_enrolled(b"x" + marker + b"0" * 128 + b"y"))
        self.assertTrue(b.limine_config_enrolled(b"x" + marker + b"a1" * 64 + b"y"))
        self.assertFalse(b.limine_config_enrolled(b"no marker"))

    def test_block_is_replaced_not_duplicated(self):
        conf = "timeout: 5\n\n/Linux\n    protocol: linux\n"
        once = b.replace_block(conf, TARGET.esp_guid, b.limine_block(TARGET))
        twice = b.replace_block(once, TARGET.esp_guid, b.limine_block(TARGET))
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("/Windows 11 Pro"), 1)
        self.assertIn(f"path: guid({TARGET.esp_guid}):/EFI/Microsoft/Boot/bootmgfw.efi", once)
        self.assertTrue(once.startswith(conf.rstrip("\n")))

    def test_block_removal(self):
        conf = "timeout: 5\n"
        added = b.replace_block(conf, TARGET.esp_guid, b.limine_block(TARGET))
        self.assertEqual(b.replace_block(added, TARGET.esp_guid, None), conf)

    def test_other_windows_entries_are_kept(self):
        other = b.WindowsTarget("Windows 10", "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "1111-2222")
        conf = b.replace_block("timeout: 5\n", TARGET.esp_guid, b.limine_block(TARGET))
        conf = b.replace_block(conf, other.esp_guid, b.limine_block(other))
        for _ in range(3):  # re-adding one entry must not touch the other, nor pile up blank lines
            conf = b.replace_block(conf, TARGET.esp_guid, b.limine_block(TARGET))
        self.assertEqual(conf.count("/Windows 11 Pro"), 1)
        self.assertEqual(conf.count("/Windows 10"), 1)
        self.assertNotIn("\n\n\n", conf)

    def test_unterminated_block_removes_nothing(self):
        conf = b.replace_block("timeout: 5\n", TARGET.esp_guid, b.limine_block(TARGET))
        broken = conf.replace(f"{b.MARK_END} {TARGET.esp_guid}\n", "") + "/Linux\n    protocol: linux\n"
        self.assertIn("/Linux", b.replace_block(broken, TARGET.esp_guid, None))


class GrubTests(unittest.TestCase):
    def test_script(self):
        script = b.grub_script(b.WindowsTarget("Windows 11 Pro's", TARGET.esp_guid, TARGET.esp_volume_id))
        self.assertTrue(script.startswith("#!/bin/sh\n"))
        self.assertIn("menuentry 'Windows 11 Pros'", script)
        self.assertIn("search --no-floppy --fs-uuid --set=root 8441-18C4", script)
        self.assertIn("chainloader /EFI/Microsoft/Boot/bootmgfw.efi", script)


class RefindTests(unittest.TestCase):
    def test_scanfor(self):
        self.assertTrue(b.refind_scans_internal("timeout 20\n#scanfor manual\n"))
        self.assertTrue(b.refind_scans_internal("scanfor internal,external"))
        self.assertTrue(b.refind_scans_internal("scanfor manual internal # comment"))
        self.assertFalse(b.refind_scans_internal("scanfor manual,external"))


if __name__ == "__main__":
    unittest.main()
