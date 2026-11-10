# SPDX-License-Identifier: GPL-3.0-or-later
"""Walk through the GUI off screen and save every page as PNG.

Runs GTK on its Broadway backend (gtk4-broadwayd), so no window appears on the
desktop, and renders each page with GSK's Cairo renderer.

  demo mode (default): fake disks, ISO and progress, for the README
  e2e mode: --iso WINDOWS.iso --test-image FILE runs a real installation into
            an image file, driven by the GUI (no root needed)

Usage: PYTHONPATH=src python3 tools/gui_screenshots.py --out docs/screenshots [--iso X --test-image Y]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

DISPLAY = ":7"


def start_broadway() -> subprocess.Popen:
    proc = subprocess.Popen(["gtk4-broadwayd", DISPLAY], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    os.environ["GDK_BACKEND"] = "broadway"
    os.environ["BROADWAY_DISPLAY"] = DISPLAY
    os.environ["ADW_DISABLE_PORTAL"] = "1"  # default light style, independent of the desktop
    return proc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--iso")
    ap.add_argument("--test-image")
    ap.add_argument("--scheme", choices=("light", "dark"), default="light")
    opts = ap.parse_args()
    out = Path(opts.out)
    out.mkdir(parents=True, exist_ok=True)
    broadway = start_broadway()
    try:
        return drive(out, opts.iso, opts.test_image, opts.scheme)
    finally:
        broadway.terminate()


def drive(out: Path, iso: str | None, test_image: str | None, scheme: str) -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk

    from wvi import gui
    from wvi.cli import WRITE_RATIO
    from wvi.disks import Disk
    from wvi.winiso import Edition, WindowsISO

    e2e = iso is not None and test_image is not None

    class DemoBackend(gui.Backend):
        """Plausible, anonymous data: nothing from the machine taking the screenshots."""

        def problems(self):
            return []

        def inspect_iso(self, path):
            names = ["Home", "Home N", "Education", "Education N", "Pro", "Pro N", "Pro Education",
                     "Pro Education N", "Pro for Workstations", "Pro N for Workstations"]
            eds = [Edition(i, f"Windows 11 {n}", n.replace(" ", ""), "x64", "26300.9457", ["en-US"], "en-US",
                           26_600_000_000) for i, n in enumerate(names, 1)]
            return WindowsISO(Path(path), "sources/install.wim", eds)

        def disks(self):
            linux = Disk("nvme0n1", "/dev/nvme0n1", "/dev/disk/by-id/nvme-Samsung_SSD_990_PRO_1TB_XXXX",
                         1_000_204_886_016, "Samsung SSD 990 PRO 1TB", "XXXX", "nvme",
                         problems=["in use: mounted at /, /boot, /home", "in use by crypt (LUKS/LVM/RAID)"])
            spare = Disk("nvme1n1", "/dev/nvme1n1", "/dev/disk/by-id/nvme-CT500P3SSD8_XXXX",
                         500_107_862_016, "Crucial P3 500GB", "XXXX", "nvme",
                         partitions=[{"name": "nvme1n1p1", "size": 500_090_011_648, "fstype": "ntfs",
                                      "label": "Data", "parttypename": "Microsoft basic data"}],
                         warnings=["contains 1 partition(s): they will be erased"])
            return [linux, spare]

        def has_oem_key(self):
            return False

        def restore_pending(self):
            return False

        def boot_menu_summary(self):
            return "Detected boot manager: GRUB"

        def run(self, args, job):
            total = int(26_600_000_000 * WRITE_RATIO)
            events = [
                {"event": "started", "edition": "Windows 11 Pro", "target": "/dev/nvme1n1", "workdir": "/var/tmp/x"},
                {"event": "progress", "phase": "installing", "elapsed": 214, "written": int(total * 0.47),
                 "total": total},
            ]
            for e in events:
                job.on_event(e)
            job.on_log("Installing Windows (first phase, in a hidden virtual machine)")
            self.finish = lambda: self._finish(job)

        def _finish(self, job):
            for e in ({"event": "first_phase_done", "esp_partition": 1},
                      {"event": "boot_entry", "number": "0003", "label": "Windows 11 Pro (Crucial P3 500GB)"},
                      {"event": "bootloader", "kind": "grub", "summary": "", "error": None,
                       "result": "added 'Windows 11 Pro' to the GRUB menu (regenerated with update-grub)"},
                      {"event": "done", "next_steps": [
                          "Reboot: Windows finishes its setup on the real hardware (devices, account, "
                          "activation). It may restart a couple of times; it keeps booting Windows until "
                          "you are done.",
                          "Back in Linux (in Windows: Settings > System > Recovery > Advanced startup > Use a "
                          "device), make your Linux boot loader the default again with 'restore-boot'."]}):
                job.on_event(e)
            job.on_exit(0, "")

    backend = gui.Backend(test_image) if e2e else DemoBackend()
    app = gui.App(backend)
    state = {"step": 0, "status": 0}

    def shot(name: str) -> None:
        # A toplevel cannot paint itself into a texture: paint its content (header bars
        # included) over the window background, which the toplevel would draw
        content = app.window.get_content()
        w, h = content.get_width(), content.get_height()
        found, bg = content.get_style_context().lookup_color("window_bg_color")
        if not found:
            bg = Gdk.RGBA()
            bg.parse("#222226" if scheme == "dark" else "#fafafb")
        snap = Gtk.Snapshot()
        snap.append_color(bg, Graphene.Rect().init(0, 0, w, h))
        Gtk.WidgetPaintable.new(content).snapshot(snap, w, h)
        node = snap.to_node()
        if node is None:
            raise RuntimeError(f"nothing to render for {name} ({w}x{h})")
        renderer = Gsk.CairoRenderer.new()
        renderer.realize(None)
        name = name.replace(".png", f"-{scheme}.png")
        renderer.render_texture(node, Graphene.Rect().init(0, 0, w, h)).save_to_png(str(out / name))
        renderer.unrealize()
        print("saved", out / name, flush=True)

    def visible_page_name() -> str:
        return app.window.nav.get_visible_page().get_title()

    def page_ready() -> bool:
        child = app.window.nav.get_visible_page().get_child()
        return child is not None and child.get_height() > 0 and child.get_mapped()

    def step() -> bool:
        win = app.window
        s = state["step"]
        if s in (1, 3, 5, 7, 8, 9) and not page_ready() and state.setdefault("waits", 0) < 10:
            state["waits"] += 1  # the page just pushed has no size yet: try again later
            win.queue_resize()
            return True
        state["waits"] = 0
        state["step"] += 1
        if s == 0:
            win.set_iso(iso or "/home/user/Downloads/Win11_26H2_English_x64.iso")
        elif s == 1:
            shot("gui-1-iso.png")
            win.iso_next.emit("clicked")
        elif s == 2:
            if not e2e:
                win.disk_checks["nvme1n1"].set_active(True)
        elif s == 3:
            shot("gui-2-disk.png")
            _click_next(win)
        elif s == 4:
            combo = win.options_widgets[0]
            names = [combo.get_model().get_string(i) for i in range(combo.get_model().get_n_items())]
            combo.set_selected(names.index("Windows 11 Pro"))
        elif s == 5:
            shot("gui-3-options.png")
            _click_next(win)
        elif s == 6:
            accept, confirm = win.summary_widgets
            accept.set_active(True)
            if confirm is not None:
                confirm.set_text("nvme1n1")
        elif s == 7:
            shot("gui-4-summary.png")
            win.start_install()
        elif s == 8:
            if e2e and visible_page_name() == "Installing" and win.progress.bar.get_fraction() < 0.4:
                state["step"] -= 1  # wait for real progress
                return True
            shot("gui-5-progress.png")
            if not e2e:
                backend.finish()
        elif s == 9:
            if e2e and visible_page_name() == "Installing":
                state["step"] -= 1  # wait for the real installation to end
                return True
            shot("gui-6-done.png" if visible_page_name() == "Done" else "gui-6-result.png")
            state["status"] = 0 if visible_page_name() == "Done" else 1
            app.quit()
            return False
        return True

    def _click_next(win) -> None:
        # The "Next" pill lives in the bottom bar of the visible page
        for widget in _walk(win.nav.get_visible_page()):
            if isinstance(widget, Gtk.Button) and widget.get_label() == "Next":
                widget.emit("clicked")
                return
        raise RuntimeError("Next button not found")

    def _walk(widget):
        child = widget.get_first_child()
        while child is not None:
            yield child
            yield from _walk(child)
            child = child.get_next_sibling()

    def on_activate(_app):
        # Without a browser attached, Broadway barely advances the frame clock: animations
        # (page slides, check marks) would be captured halfway
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if scheme == "dark" else Adw.ColorScheme.FORCE_LIGHT)
        GLib.timeout_add(900, step)

    app.connect_after("activate", on_activate)
    app.run([sys.argv[0]])
    return state["status"]


if __name__ == "__main__":
    sys.exit(main())
