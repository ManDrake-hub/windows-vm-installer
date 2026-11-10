# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests that need neither root nor a Windows ISO."""

from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from wvi import efi, unattend
from wvi.fat import FAT32
from wvi.gpt import ESP_TYPE, read_gpt
from wvi.iso9660 import write_iso
from wvi.winiso import parse_wim_xml

EFIBOOTMGR_OUTPUT = """BootCurrent: 0001
Timeout: 0 seconds
BootOrder: 0001,0006
Boot0001* Limine\tHD(1,GPT,aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa,0x1000,0x800000)/\\EFI\\LIMINE\\LIMINE_X64.EFI
Boot0006* UEFI OS\tHD(1,GPT,aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa,0x1000,0x800000)/\\EFI\\BOOT\\BOOTX64.EFI
"""

# A fake efibootmgr keeping its NVRAM in a JSON file next to it
FAKE_EFIBOOTMGR = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "nvram.json")
s = json.load(open(state_path))
a = sys.argv[1:]
if "-C" in a:
    num = "%04X" % (max(int(n, 16) for n in s["entries"]) + 1)
    s["entries"][num] = a[a.index("-L") + 1]
if "-o" in a:
    s["order"] = a[a.index("-o") + 1].split(",")
json.dump(s, open(state_path, "w"))
if not any(x in a for x in ("-C", "-o")):
    print("BootCurrent: " + s["current"])
    print("BootOrder: " + ",".join(s["order"]))
    for n, label in sorted(s["entries"].items()):
        print("Boot%s* %s\tHD(1,GPT,x)" % (n, label))
'''


class EFITests(unittest.TestCase):
    def test_parse(self):
        s = efi.parse(EFIBOOTMGR_OUTPUT)
        self.assertEqual(s.current, "0001")
        self.assertEqual(s.order, ["0001", "0006"])
        self.assertEqual(s.entries, {"0001": "Limine", "0006": "UEFI OS"})

    def test_create_put_first_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp)
            tool = bindir / "efibootmgr"
            tool.write_text(FAKE_EFIBOOTMGR)
            tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
            (bindir / "nvram.json").write_text(json.dumps(
                {"current": "0001", "order": ["0001", "0006"], "entries": {"0001": "Limine", "0006": "UEFI OS"}}))
            state_file = bindir / "boot-state.json"
            old_path = os.environ["PATH"]
            os.environ["PATH"] = f"{bindir}:{old_path}"
            try:
                num = efi.create_windows_entry("/dev/nvme1n1", 1, "Windows 11 Pro")
                self.assertEqual(num, "0007")
                self.assertEqual(efi.put_first(num, state_file), ["0007", "0001", "0006"])
                # a second put_first must not overwrite the saved order
                efi.put_first(num, state_file)
                self.assertEqual(json.loads(state_file.read_text())["previous_order"], ["0001", "0006"])
                self.assertEqual(efi.restore_order(state_file), ["0001", "0006", "0007"])
                self.assertFalse(state_file.exists())
            finally:
                os.environ["PATH"] = old_path


class UnattendTests(unittest.TestCase):
    NS = {"u": "urn:schemas-microsoft-com:unattend"}

    def build(self, **kw):
        opts = unattend.AnswerOptions(edition="Windows 11 Pro", language="it-IT", accept_eula=True, **kw)
        return ET.fromstring(unattend.build(opts))

    def test_requires_eula(self):
        with self.assertRaises(ValueError):
            unattend.build(unattend.AnswerOptions(edition="Windows 11 Pro", language="it-IT"))

    def test_edition_and_language(self):
        root = self.build()
        self.assertEqual(root.find(".//u:MetaData/u:Value", self.NS).text, "Windows 11 Pro")
        self.assertEqual(root.find(".//u:SetupUILanguage/u:UILanguage", self.NS).text, "it-IT")
        self.assertEqual(root.find(".//u:AcceptEula", self.NS).text, "true")

    def test_dual_boot_commands(self):
        paths = [p.text for p in self.build().findall(".//u:RunSynchronousCommand/u:Path", self.NS)]
        self.assertEqual(len(paths), 2)
        self.assertTrue(any("RealTimeIsUniversal" in p for p in paths))
        self.assertTrue(any("HiberbootEnabled" in p for p in paths))
        root = self.build(utc_clock=False, disable_fast_startup=False)
        self.assertIsNone(root.find(".//u:settings[@pass='specialize']", self.NS))

    def test_escaping(self):
        root = self.build(product_key="<&>")
        self.assertEqual(root.find(".//u:ProductKey/u:Key", self.NS).text, "<&>")


class ISOTests(unittest.TestCase):
    def test_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp) / "a.iso"
            write_iso(iso, {"autounattend.xml": b"<unattend/>"})
            data = iso.read_bytes()
            self.assertEqual(len(data) % 2048, 0)
            self.assertEqual(data[16 * 2048 + 1:16 * 2048 + 6], b"CD001")
            self.assertEqual(data[17 * 2048], 2)  # Joliet supplementary descriptor
            self.assertEqual(data[17 * 2048 + 88:17 * 2048 + 91], b"%/E")
            self.assertIn("autounattend.xml;1".encode("utf-16-be"), data)
            self.assertIn(b"<unattend/>", data)

    @unittest.skipUnless(shutil.which("bsdtar"), "bsdtar not installed")
    def test_readable_by_libarchive(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp) / "a.iso"
            write_iso(iso, {"autounattend.xml": b"<unattend/>"})
            out = subprocess.run(["bsdtar", "-xOf", str(iso), "autounattend.xml"], capture_output=True)
            self.assertEqual(out.stdout, b"<unattend/>")


def make_gpt_image(path: Path, partitions: list[tuple[uuid.UUID, int, int]], ss: int = 512) -> None:
    """Write a minimal valid GPT (primary header and table only)."""
    entries = bytearray(128 * 128)
    for i, (ptype, first, last) in enumerate(partitions):
        e = ptype.bytes_le + uuid.uuid4().bytes_le + struct.pack("<QQQ", first, last, 0)
        e += "part".encode("utf-16-le").ljust(72, b"\0")
        entries[i * 128:(i + 1) * 128] = e
    header = bytearray(92)
    header[:8] = b"EFI PART"
    struct.pack_into("<IIII", header, 8, 0x00010000, 92, 0, 0)
    struct.pack_into("<QQQQ", header, 24, 1, 0, 34, 1000)
    header[56:72] = uuid.uuid4().bytes_le
    struct.pack_into("<QIII", header, 72, 2, 128, 128, zlib.crc32(entries))
    struct.pack_into("<I", header, 16, zlib.crc32(header))
    with open(path, "wb") as f:
        f.truncate(2 * 2**20)
        f.seek(ss)
        f.write(header)
        f.seek(2 * ss)
        f.write(entries)


class GPTTests(unittest.TestCase):
    def test_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "disk.img"
            make_gpt_image(img, [(ESP_TYPE, 2048, 4095)])
            table = read_gpt(img)
            self.assertEqual(table.sector_size, 512)
            self.assertEqual(table.esp().number, 1)
            self.assertEqual(table.esp().size(512), 2048 * 512)

    def test_read_4k_native(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "disk.img"
            make_gpt_image(img, [(ESP_TYPE, 256, 511)], ss=4096)
            self.assertEqual(read_gpt(img).sector_size, 4096)
            self.assertEqual(read_gpt(img, 4096).esp().offset(4096), 256 * 4096)


@unittest.skipUnless(shutil.which("mkfs.fat") and shutil.which("mmd") and shutil.which("mcopy"),
                     "dosfstools/mtools not installed")
class FATTests(unittest.TestCase):
    def test_long_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "esp.img"
            subprocess.run(["mkfs.fat", "-F", "32", "-C", str(img), str(64 * 1024)], check=True,
                           capture_output=True)
            src = Path(tmp) / "BCD"
            src.write_bytes(b"x" * 24576)
            env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
            for d in ("::/EFI", "::/EFI/Microsoft", "::/EFI/Microsoft/Boot"):
                subprocess.run(["mmd", "-i", str(img), d], check=True, env=env)
            subprocess.run(["mcopy", "-i", str(img), str(src), "::/EFI/Microsoft/Boot/BCD"], check=True, env=env)
            with open(img, "rb") as f:
                fs = FAT32(f, 0)
                self.assertEqual(fs.stat("EFI/Microsoft/Boot/BCD"), (False, 24576))
                self.assertEqual(fs.stat("efi/microsoft/boot"), (True, 0))
                self.assertIsNone(fs.stat("EFI/Microsoft/Boot/bootmgfw.efi"))


class QEMUCommandTests(unittest.TestCase):
    def vm(self, **kw):
        from wvi.qemu import Firmware, InstallerVM
        return InstallerVM(Path("/var/tmp/w"), Path("/isos/Win11, Italian.iso"), Path("/var/tmp/w/answer.iso"),
                           Path("/dev/nvme1n1"), kw.pop("controller", "nvme"), Firmware("/fw/code.fd", "/fw/vars.fd"),
                           **kw)

    def test_commas_in_paths_are_escaped(self):
        cmd = self.vm().command()
        iso = next(a for a in cmd if "id=iso" in a)
        self.assertTrue(iso.startswith("file=/isos/Win11,, Italian.iso,format=raw"))

    def test_sector_size_reaches_the_device(self):
        plain = next(a for a in self.vm().command() if a.startswith("nvme,"))
        self.assertNotIn("logical_block_size", plain)
        native = next(a for a in self.vm(sector_size=4096, physical_sector_size=4096).command()
                      if a.startswith("nvme,"))
        self.assertIn("logical_block_size=4096,physical_block_size=4096", native)
        sata = next(a for a in self.vm(controller="sata", sector_size=4096).command() if a.startswith("ide-hd,"))
        self.assertIn("logical_block_size=4096,physical_block_size=4096", sata)


class DiskSafetyTests(unittest.TestCase):
    """list_disks() with a fake lsblk: every reason to refuse a disk must be found."""

    def disks(self, devices, btrfs=(), boot_esp=None, busy=None, holders=None):
        from unittest import mock

        from wvi import disks
        with mock.patch.object(disks, "_lsblk", return_value=devices), \
                mock.patch.object(disks, "_by_id", side_effect=lambda n: f"/dev/disk/by-id/x-{n}"), \
                mock.patch.object(disks, "_swaps", return_value=set()), \
                mock.patch.object(disks, "_btrfs_members", return_value=set(btrfs)), \
                mock.patch.object(disks, "_boot_esp_guid", return_value=boot_esp), \
                mock.patch.object(disks, "_holders", side_effect=lambda n: (holders or {}).get(n, [])), \
                mock.patch.object(disks, "_behind_vmd", return_value=False), \
                mock.patch.object(disks, "kernel_busy", return_value=busy):
            return disks.list_disks()

    @staticmethod
    def dev(name="sda", parts=(), **kw):
        d = {"name": name, "path": f"/dev/{name}", "type": "disk", "size": 500 * 10**9, "model": "M",
             "serial": "S", "tran": "sata", "rm": False, "ro": False, "mountpoints": [None], "fstype": None,
             "log-sec": 512, "phy-sec": 512}
        d.update(kw)
        d["children"] = [{"name": f"{name}{i}", "path": f"/dev/{name}{i}", "type": "part", "size": 10**9,
                          "mountpoints": [None], "fstype": None, "partuuid": None, **p}
                         for i, p in enumerate(parts, 1)]
        return d

    def test_free_disk_is_usable(self):
        (d,) = self.disks([self.dev(parts=[{"fstype": "ntfs"}])], busy=False)
        self.assertTrue(d.usable)
        self.assertTrue(any("will be erased" in w for w in d.warnings))

    def test_mounted_partition(self):
        (d,) = self.disks([self.dev(parts=[{"mountpoints": ["/home"]}])])
        self.assertTrue(any("mounted at /home" in p for p in d.problems))

    def test_second_device_of_mounted_btrfs(self):
        # lsblk shows no mount point on the other members of a multi-device btrfs
        (d,) = self.disks([self.dev(parts=[{"fstype": "btrfs"}])], btrfs={"sda1"}, busy=None)
        self.assertEqual(d.problems, ["part of a mounted btrfs filesystem"])

    def test_holders(self):
        (d,) = self.disks([self.dev(parts=[{"fstype": "LVM2_member"}])], holders={"sda1": ["dm-3"]})
        self.assertTrue(any("in use by dm-3" in p for p in d.problems))

    def test_kernel_says_busy(self):
        (d,) = self.disks([self.dev(parts=[{"fstype": "zfs_member"}])], busy=True)
        self.assertEqual(d.problems, ["in use (the kernel reports the device as busy)"])

    def test_boot_esp(self):
        guid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        (d,) = self.disks([self.dev(parts=[{"fstype": "vfat", "partuuid": guid.upper()}])], boot_esp=guid)
        self.assertEqual(d.problems, ["holds the EFI partition this system boots from"])

    def test_inactive_pool_member_warns(self):
        (d,) = self.disks([self.dev(parts=[{"fstype": "LVM2_member"}])], busy=False)
        self.assertTrue(d.usable)
        self.assertTrue(any("LVM" in w for w in d.warnings))

    def test_sector_sizes(self):
        (d,) = self.disks([self.dev(**{"log-sec": 4096, "phy-sec": 4096})], busy=False)
        self.assertEqual((d.sector_size, d.physical_sector_size), (4096, 4096))


class WIMXMLTests(unittest.TestCase):
    def test_parse(self):
        xml_text = """<WIM><IMAGE INDEX="5"><TOTALBYTES>26600000000</TOTALBYTES>
          <WINDOWS><ARCH>9</ARCH><EDITIONID>Professional</EDITIONID>
          <LANGUAGES><LANGUAGE>it-IT</LANGUAGE><DEFAULT>it-IT</DEFAULT></LANGUAGES>
          <VERSION><BUILD>26300</BUILD><SPBUILD>9457</SPBUILD></VERSION></WINDOWS>
          <NAME>Windows 11 Pro</NAME></IMAGE></WIM>"""
        (e,) = parse_wim_xml(xml_text)
        self.assertEqual((e.index, e.name, e.edition_id, e.arch, e.build), (5, "Windows 11 Pro", "Professional", "x64", "26300.9457"))
        self.assertEqual(e.default_language, "it-IT")
        self.assertEqual(e.total_bytes, 26600000000)


if __name__ == "__main__":
    unittest.main()
