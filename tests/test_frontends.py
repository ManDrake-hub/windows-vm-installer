# SPDX-License-Identifier: GPL-3.0-or-later
"""The --json protocol between the GUI and the command line installer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

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


class GUIBackendTests(unittest.TestCase):
    """Backend.run must deliver every event before it reports the exit."""

    def setUp(self):
        try:
            from wvi import gui
        except (ImportError, ValueError) as e:
            self.skipTest(f"GTK 4 / libadwaita not available: {e}")
        self.gui = gui

    def test_events_arrive_before_exit(self):
        from gi.repository import GLib

        for _ in range(15):  # the old code lost this race now and then
            seen: list = []
            loop = GLib.MainLoop()

            def on_exit(status, stderr_tail):
                seen.append(("exit", status))
                loop.quit()

            job = self.gui.Job(lambda ev: seen.append(("event", ev["event"])), lambda line: None, on_exit)
            # test-image mode runs the installer unprivileged; the missing ISO makes it fail at once
            self.gui.Backend(test_image="/nonexistent/t.img").run(
                ["install", "--iso", "/nonexistent.iso", "--edition", "x", "--accept-eula",
                 "--test-image", "/nonexistent/t.img"], job)
            GLib.timeout_add_seconds(20, loop.quit)
            loop.run()
            self.assertEqual(seen, [("event", "error"), ("exit", 1)])


class FlatpakBackendTests(unittest.TestCase):
    """Inside a Flatpak the elevated command goes to the host's CLI."""

    def setUp(self):
        try:
            from wvi import gui
        except (ImportError, ValueError) as e:
            self.skipTest(f"GTK 4 / libadwaita not available: {e}")
        self.gui = gui
        self.env = mock.patch.dict(os.environ, {"FLATPAK_ID": "io.github.mandrake_hub.WindowsVmInstaller"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_elevated_argv_uses_host_cli(self):
        argv = self.gui.Backend().argv(["install", "--json"], elevated=True)
        self.assertEqual(argv[:4], ["flatpak-spawn", "--host", "pkexec", "windows-vm-installer"])
        self.assertEqual(argv[4:], ["install", "--json"])

    def test_unprivileged_argv_stays_in_sandbox(self):
        argv = self.gui.Backend().argv(["list-disks", "--json"], elevated=False)
        self.assertEqual(argv[1:3], ["-m", "wvi"])
        self.assertNotIn("flatpak-spawn", argv)

    def test_terminal_command_uses_host_cli(self):
        cmd = self.gui.Backend().terminal_command(["install"])
        self.assertTrue(cmd.startswith("flatpak-spawn --host pkexec windows-vm-installer"))


if __name__ == "__main__":
    unittest.main()
