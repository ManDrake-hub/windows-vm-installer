# SPDX-License-Identifier: GPL-3.0-or-later
"""The --json protocol between the GUI and the command line installer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")


def run_json(*args: str) -> tuple[int, list[dict]]:
    res = subprocess.run([sys.executable, "-m", "wvi", *args], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": SRC})
    return res.returncode, [json.loads(line) for line in res.stdout.splitlines() if line.strip()]


class JSONProtocolTests(unittest.TestCase):
    def test_missing_options_are_one_error_event(self):
        status, events = run_json("install", "--json", "--iso", "x.iso")
        self.assertEqual(status, 1)
        self.assertEqual([e["event"] for e in events], ["error"])
        self.assertIn("--edition", events[0]["message"])
        self.assertIn("--accept-eula", events[0]["message"])

    def test_stdout_carries_only_events(self):
        status, events = run_json("install", "--json", "--iso", "/nonexistent.iso", "--edition", "Windows 11 Pro",
                                  "--accept-eula", "--test-image", "/nonexistent/img")
        self.assertEqual(status, 1)
        self.assertTrue(all("event" in e for e in events))


class GUIChoicesTests(unittest.TestCase):
    def setUp(self):
        try:
            from wvi import gui
        except (ImportError, ValueError) as e:
            self.skipTest(f"GTK 4 / libadwaita not available: {e}")
        self.gui = gui

    def test_install_args(self):
        from wvi.disks import Disk
        from wvi.winiso import Edition, WindowsISO

        ed = Edition(5, "Windows 11 Pro", "Professional", "x64", "26300.9457", ["it-IT"], "it-IT", 1)
        disk = Disk("nvme1n1", "/dev/nvme1n1", "/dev/disk/by-id/nvme-X", 500 * 10**9, "X", "S", "nvme")
        c = self.gui.Choices(iso=WindowsISO(Path("/isos/w.iso"), "sources/install.wim", [ed]), disk=disk,
                             edition=ed, utc_clock=False, boot_menu=False)
        args = c.install_args(None)
        self.assertEqual(args[:5], ["install", "--iso", "/isos/w.iso", "--edition", "Windows 11 Pro"])
        self.assertIn("--accept-eula", args)
        self.assertIn("--yes", args)
        self.assertEqual(args[args.index("--disk") + 1], "/dev/disk/by-id/nvme-X")  # stable name
        self.assertIn("--no-utc-clock", args)
        self.assertIn("--no-bootloader", args)
        self.assertNotIn("--keep-fast-startup", args)
        test_args = c.install_args("/tmp/t.img")
        self.assertEqual(test_args[test_args.index("--test-image") + 1], "/tmp/t.img")
        self.assertNotIn("--disk", test_args)

    def test_elevated_command_passes_the_package_path(self):
        argv = self.gui.Backend().argv(["restore-boot"], elevated=True)
        self.assertTrue(argv[0].endswith("pkexec"))
        self.assertTrue(any(a.startswith("PYTHONPATH=") for a in argv))
        self.assertEqual(argv[-3:], ["-m", "wvi", "restore-boot"])


if __name__ == "__main__":
    unittest.main()
