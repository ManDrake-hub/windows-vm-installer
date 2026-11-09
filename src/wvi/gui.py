# SPDX-License-Identifier: GPL-3.0-or-later
"""Graphical installer: GTK 4 + libadwaita (1.5 or newer).

The window runs as the user: reading the ISO and listing disks need no
privileges. The installation itself runs `windows-vm-installer install --json`
through pkexec, and the window follows its JSON events. GUI programs started as
root work badly on Wayland, so the GUI is never elevated itself.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import bootloaders, efi  # noqa: E402
from .cli import EULA_URL, host_problems  # noqa: E402
from .disks import Disk, list_disks  # noqa: E402
from .winiso import Edition, NotWindowsISO, WindowsISO, inspect_iso  # noqa: E402

APP_ID = "io.github.mandrake_hub.WindowsVmInstaller"
PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)  # makes "-m wvi" work under pkexec
PHASES = {
    "firmware": "Starting the hidden virtual machine",
    "loading": "Loading Windows Setup",
    "installing": "Installing Windows",
}


def gb(n: int) -> str:
    return f"{n / 10**9:.0f} GB"


def gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


# --- what the window needs from the system ---------------------------------

@dataclass
class Choices:
    iso: WindowsISO | None = None
    disk: Disk | None = None
    edition: Edition | None = None
    utc_clock: bool = True
    disable_fast_startup: bool = True
    boot_entry: bool = True
    boot_menu: bool = True

    def install_args(self, test_image: str | None) -> list[str]:
        assert self.iso and self.edition
        args = ["install", "--iso", str(self.iso.path), "--edition", self.edition.name, "--accept-eula", "--yes"]
        args += ["--test-image", test_image] if test_image else ["--disk", self.disk.by_id or self.disk.path]
        for flag, off in (("--no-utc-clock", not self.utc_clock),
                          ("--keep-fast-startup", not self.disable_fast_startup),
                          ("--no-boot-entry", not self.boot_entry),
                          ("--no-bootloader", not self.boot_menu)):
            if off:
                args.append(flag)
        return args


@dataclass
class Job:
    """Callbacks a running backend command reports to."""

    on_event: Callable[[dict], None]
    on_log: Callable[[str], None]
    on_exit: Callable[[int, str], None]  # exit status, stderr tail
    stderr_tail: list[str] = field(default_factory=list)


class Backend:
    """Talks to the real system. The screenshot tool swaps in a fake one."""

    def __init__(self, test_image: str | None = None):
        self.test_image = test_image

    def problems(self) -> list[str]:
        found = host_problems(test_mode=True, boot_entry=False)  # root comes from pkexec
        if not self.test_image and shutil.which("pkexec") is None:
            found.append("pkexec (polkit) is not installed")
        return found

    def inspect_iso(self, path: str) -> WindowsISO:
        return inspect_iso(path)

    def disks(self) -> list[Disk]:
        return list_disks()

    def has_oem_key(self) -> bool:
        return efi.has_oem_windows_key()

    def restore_pending(self) -> bool:
        return efi.STATE_FILE.exists()

    def boot_menu_summary(self) -> str:
        try:
            bm = bootloaders.detect()
            plan = bootloaders.plan(bm, bootloaders.WindowsTarget("Windows", "", ""))
            return f"{bm.kind}: {plan.summary}"
        except Exception:  # detection is best effort without root
            return "detected when the installation runs"

    def argv(self, args: list[str], elevated: bool) -> list[str]:
        cmd = [sys.executable, "-m", "wvi", *args]
        if elevated:
            return [shutil.which("pkexec") or "pkexec", shutil.which("env") or "/usr/bin/env",
                    f"PYTHONPATH={PACKAGE_PARENT}", *cmd]
        return cmd

    def terminal_command(self, args: list[str]) -> str:
        return shlex.join(["sudo", "env", f"PYTHONPATH={PACKAGE_PARENT}", sys.executable, "-m", "wvi", *args])

    def run(self, args: list[str], job: Job) -> None:
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        launcher.setenv("PYTHONPATH", PACKAGE_PARENT, True)
        proc = launcher.spawnv(self.argv([*args, "--json"], elevated=not self.test_image))

        def on_stdout(line: str) -> None:
            try:
                job.on_event(json.loads(line))
            except ValueError:
                job.on_log(line)

        def on_stderr(line: str) -> None:
            job.stderr_tail[:] = (job.stderr_tail + [line])[-20:]
            job.on_log(line)

        def on_finished(p, res):
            p.wait_finish(res)
            job.on_exit(p.get_exit_status(), "\n".join(job.stderr_tail))

        _read_lines(proc.get_stdout_pipe(), on_stdout)
        _read_lines(proc.get_stderr_pipe(), on_stderr)
        proc.wait_async(None, on_finished)


def _read_lines(stream: Gio.InputStream, on_line: Callable[[str], None]) -> None:
    data = Gio.DataInputStream.new(stream)

    def next_line(src, res):
        line, _ = src.read_line_finish_utf8(res)
        if line is None:
            return
        on_line(line)
        src.read_line_async(GLib.PRIORITY_DEFAULT, None, next_line)

    data.read_line_async(GLib.PRIORITY_DEFAULT, None, next_line)


# --- widgets helpers --------------------------------------------------------

def page(title: str, content: Gtk.Widget, button: Gtk.Button | None = None) -> Adw.NavigationPage:
    view = Adw.ToolbarView()
    view.add_top_bar(Adw.HeaderBar())
    view.set_content(content)
    if button is not None:
        bar = Gtk.Box(halign=Gtk.Align.END, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        bar.append(button)
        view.add_bottom_bar(bar)
    return Adw.NavigationPage.new(view, title)


def pill(label: str, style: str = "suggested-action") -> Gtk.Button:
    b = Gtk.Button(label=label)
    b.add_css_class("pill")
    b.add_css_class(style)
    return b


def info_row(title: str, subtitle: str = "", icon: str | None = None) -> Adw.ActionRow:
    row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle))
    if icon:
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
    return row


# --- the window -------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, backend: Backend):
        super().__init__(application=app, title="Windows VM Installer", default_width=760, default_height=720)
        self.backend = backend
        self.choices = Choices()
        self.nav = Adw.NavigationView()
        self.toasts = Adw.ToastOverlay(child=self.nav)
        self.set_content(self.toasts)
        self.nav.push(self._iso_page())

    # 1. ISO ---------------------------------------------------------------

    def _iso_page(self) -> Adw.NavigationPage:
        prefs = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup(
            title="Install Windows on another disk",
            description="No USB stick needed. The Windows installer runs in a hidden virtual machine that "
                        "can only see the disk you choose; then Windows finishes its setup on your PC.")
        prefs.add(intro)

        problems = self.backend.problems()
        if problems:
            group = Adw.PreferencesGroup(title="Missing requirements")
            for p in problems:
                group.add(info_row(p, icon="dialog-warning-symbolic"))
            prefs.add(group)

        group = Adw.PreferencesGroup(title="Windows ISO",
                                     description="Download it from microsoft.com/software-download")
        self.iso_row = info_row("No file selected", "Windows 10 or 11, x64", "media-optical-symbolic")
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._choose_iso)
        self.iso_row.add_suffix(choose)
        group.add(self.iso_row)
        prefs.add(group)

        self.iso_next = pill("Next")
        self.iso_next.set_sensitive(False)
        self.iso_next.connect("clicked", lambda *_: self.nav.push(self._disk_page()))
        self._can_continue = not problems

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        if self.backend.restore_pending():
            banner = Adw.Banner(title="A Windows installation left Windows first in the boot order",
                                button_label="Restore boot order", revealed=True)
            banner.connect("button-clicked", self._restore_boot)
            content.append(banner)
        prefs.set_vexpand(True)
        content.append(prefs)
        return page("Windows VM Installer", content, self.iso_next)

    def _choose_iso(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Windows ISO")
        filt = Gtk.FileFilter(name="Disk images (*.iso)")
        filt.add_pattern("*.iso")
        filt.add_pattern("*.ISO")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        dialog.set_filters(filters)

        def done(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return  # cancelled
            if f is not None and f.get_path():
                self.set_iso(f.get_path())

        dialog.open(self, None, done)

    def set_iso(self, path: str) -> None:
        try:
            iso = self.backend.inspect_iso(path)
            editions = [e for e in iso.editions if e.arch == "x64"]
            if not editions:
                raise NotWindowsISO("the ISO has no x64 edition")
        except (NotWindowsISO, OSError) as e:
            self.iso_row.set_title(GLib.markup_escape_text(Path(path).name))
            self.iso_row.set_subtitle(GLib.markup_escape_text(f"Not usable: {e}"))
            self.iso_next.set_sensitive(False)
            return
        self.choices.iso = iso
        first = editions[0]
        self.iso_row.set_title(GLib.markup_escape_text(Path(path).name))
        self.iso_row.set_subtitle(GLib.markup_escape_text(
            f"Windows build {first.build} · {iso.default_language} · {len(editions)} editions"))
        self.iso_next.set_sensitive(self._can_continue)

    # 2. disk --------------------------------------------------------------

    def _disk_page(self) -> Adw.NavigationPage:
        prefs = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Target disk",
            description="Everything on the chosen disk will be erased. Disks in use by Linux cannot be chosen.")
        prefs.add(group)
        nxt = pill("Next")
        nxt.set_sensitive(False)
        nxt.connect("clicked", lambda *_: self.nav.push(self._options_page()))

        if self.backend.test_image:
            row = info_row("Test image", self.backend.test_image, "drive-harddisk-symbolic")
            group.add(row)
            nxt.set_sensitive(True)
            return page("Target disk", prefs, nxt)

        first_check = None
        self.disk_checks: dict[str, Gtk.CheckButton] = {}  # used by the screenshot tool
        for disk in self.backend.disks():
            details = [disk.path, disk.transport.upper()] + disk.problems + disk.warnings
            row = info_row(f"{disk.model or disk.name}  ·  {gb(disk.size)}", "\n".join(details))
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first_check is None:
                first_check = check
            else:
                check.set_group(first_check)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            if disk.problems:
                row.set_sensitive(False)
                row.add_suffix(Gtk.Image.new_from_icon_name("system-lock-screen-symbolic"))

            def toggled(btn, d=disk):
                if btn.get_active():
                    self.choices.disk = d
                    nxt.set_sensitive(True)

            check.connect("toggled", toggled)
            self.disk_checks[disk.name] = check
            group.add(row)
        return page("Target disk", prefs, nxt)

    # 3. edition and options -----------------------------------------------

    def _options_page(self) -> Adw.NavigationPage:
        assert self.choices.iso
        editions = [e for e in self.choices.iso.editions if e.arch == "x64"]
        prefs = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(title="Edition")
        combo = Adw.ComboRow(title="Windows edition", model=Gtk.StringList.new([e.name for e in editions]))
        group.add(combo)
        if self.backend.has_oem_key():
            group.add(info_row("This PC's firmware carries an OEM key (often for Home)",
                               "Choose the edition you have a license for: Windows activates on its first boot.",
                               "dialog-information-symbolic"))
        prefs.add(group)

        group = Adw.PreferencesGroup(title="Dual boot")
        utc = Adw.SwitchRow(title="Keep the clock in UTC, like Linux",
                            subtitle="Otherwise the time shifts by an hour or two at every switch", active=True)
        fast = Adw.SwitchRow(title="Turn off Fast Startup",
                             subtitle="Leaves the Windows disk clean and readable from Linux", active=True)
        group.add(utc)
        group.add(fast)
        prefs.add(group)

        group = Adw.PreferencesGroup(title="Starting Windows")
        entry = Adw.SwitchRow(title="Create a UEFI boot entry",
                              subtitle="First in the boot order until Windows finishes its setup", active=True)
        menu = Adw.SwitchRow(title="Add Windows to your boot menu",
                             subtitle=self.backend.boot_menu_summary(), active=True)
        group.add(entry)
        group.add(menu)
        prefs.add(group)

        nxt = pill("Next")

        def go(*_):
            self.choices.edition = editions[combo.get_selected()]
            self.choices.utc_clock = utc.get_active()
            self.choices.disable_fast_startup = fast.get_active()
            self.choices.boot_entry = entry.get_active()
            self.choices.boot_menu = menu.get_active()
            self.nav.push(self._summary_page())

        nxt.connect("clicked", go)
        self.options_widgets = (combo, utc, fast, entry, menu)  # used by the screenshot tool
        return page("Edition and options", prefs, nxt)

    # 4. summary and consent -----------------------------------------------

    def _summary_page(self) -> Adw.NavigationPage:
        c = self.choices
        assert c.iso and c.edition
        prefs = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(title="Summary")
        group.add(info_row("Windows", f"{c.edition.name} · build {c.edition.build} · {c.iso.default_language}",
                           "computer-symbolic"))
        if self.backend.test_image:
            group.add(info_row("Target", f"Test image {self.backend.test_image}", "drive-harddisk-symbolic"))
        else:
            assert c.disk
            group.add(info_row("Target disk", f"{c.disk.model} · {gb(c.disk.size)} · {c.disk.path}",
                               "drive-harddisk-symbolic"))
        tweaks = [t for t, on in (("clock in UTC", c.utc_clock), ("Fast Startup off", c.disable_fast_startup),
                                  ("UEFI boot entry", c.boot_entry), ("boot menu entry", c.boot_menu)) if on]
        group.add(info_row("Options", ", ".join(tweaks) or "none", "emblem-system-symbolic"))
        prefs.add(group)

        if c.disk and c.disk.partitions:
            group = Adw.PreferencesGroup(title="Will be erased")
            for p in c.disk.partitions:
                what = " ".join(x for x in (p.get("fstype"), p.get("label"), p.get("parttypename")) if x)
                group.add(info_row(f"{p['name']}  ·  {gib(int(p['size']))}", what or "unknown content",
                                   "user-trash-symbolic"))
            prefs.add(group)

        group = Adw.PreferencesGroup(title="Confirm")
        accept = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        eula = Adw.ActionRow(title="I accept the Microsoft Software License Terms",
                             subtitle="Windows Setup runs unattended, so it needs your acceptance now")
        eula.add_prefix(accept)
        eula.set_activatable_widget(accept)
        link = Gtk.LinkButton(uri=EULA_URL, label="Read", valign=Gtk.Align.CENTER)
        eula.add_suffix(link)
        group.add(eula)
        confirm = None
        if c.disk and not self.backend.test_image:
            confirm = Adw.EntryRow(title=f"Type {c.disk.name} to erase this disk")
            group.add(confirm)
        prefs.add(group)

        install = pill("Install", "destructive-action")
        install.set_sensitive(False)

        def update(*_):
            ok = accept.get_active() and (confirm is None or confirm.get_text().strip() == c.disk.name)
            install.set_sensitive(ok)

        accept.connect("toggled", update)
        if confirm is not None:
            confirm.connect("changed", update)
        install.connect("clicked", lambda *_: self.start_install())
        self.summary_widgets = (accept, confirm)  # used by the screenshot tool
        return page("Summary", prefs, install)

    # 5. progress ----------------------------------------------------------

    def start_install(self) -> None:
        args = self.choices.install_args(self.backend.test_image)
        self.progress = ProgressView(on_toast=lambda t: self.toasts.add_toast(Adw.Toast(title=t)))
        p = page("Installing", self.progress)
        p.set_can_pop(False)
        self.nav.push(p)
        self.backend.run(args, Job(self.progress.event, self.progress.log,
                                   lambda status, err: self.finished(status, err, args)))

    def finished(self, status: int, stderr_tail: str, args: list[str]) -> None:
        result = self.progress.result
        if status == 0 and result is not None:
            self.nav.push(self._done_page(result))
            return
        if status in (126, 127) and self.progress.error is None:
            self._elevation_failed(status, stderr_tail, args)
            return
        self.nav.push(self._error_page(self.progress.error or {"message": f"exit status {status}"}))

    def _done_page(self, result: dict) -> Adw.NavigationPage:
        status = Adw.StatusPage(icon_name="object-select-symbolic", title="Windows is ready for its first boot",
                                description="The first phase is done. Windows completes its setup on your PC.")
        status.add_css_class("compact")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        group = Adw.PreferencesGroup(title="Next steps")
        for i, step in enumerate(result.get("next_steps", []), 1):
            row = info_row(f"Step {i}", step)
            row.set_subtitle_lines(0)
            group.add(row)
        box.append(group)

        group = Adw.PreferencesGroup(title="Starting Windows")
        if self.progress.boot_entry:
            e = self.progress.boot_entry
            group.add(info_row("UEFI boot entry", f"{e.get('label')} (Boot{e.get('number')}), first in the boot order",
                               "object-select-symbolic"))
        if self.progress.bootloader:
            b = self.progress.bootloader
            row = info_row(f"Boot menu ({b.get('kind', '')})",
                           b.get("error") or b.get("result") or b.get("summary", ""),
                           "dialog-warning-symbolic" if b.get("error") else "object-select-symbolic")
            row.set_subtitle_lines(0)
            group.add(row)
        if self.progress.boot_entry or self.progress.bootloader:
            box.append(group)
        clamp = Adw.Clamp(child=box, maximum_size=600)
        status.set_child(clamp)
        p = page("Done", Gtk.ScrolledWindow(child=status, vexpand=True))
        p.set_can_pop(False)
        return p

    def _error_page(self, error: dict) -> Adw.NavigationPage:
        status = Adw.StatusPage(icon_name="dialog-error-symbolic", title="The installation stopped",
                                description=GLib.markup_escape_text(error.get("message", "")))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        shot = error.get("screenshot")
        if shot and os.access(shot, os.R_OK):
            pic = Gtk.Picture.new_for_filename(shot)
            pic.set_size_request(-1, 300)
            box.append(Gtk.Label(label="Last screen of the hidden virtual machine:", xalign=0))
            box.append(pic)
        if error.get("log"):
            box.append(Gtk.Label(label=f"Log: {error['log']}", selectable=True, xalign=0, wrap=True))
        status.set_child(Adw.Clamp(child=box, maximum_size=640))
        p = page("Error", Gtk.ScrolledWindow(child=status, vexpand=True))
        p.set_can_pop(False)
        return p

    def _elevation_failed(self, status: int, stderr_tail: str, args: list[str]) -> None:
        self.nav.pop()
        if status == 126:
            self.toasts.add_toast(Adw.Toast(title="Authentication cancelled: nothing was changed"))
            return
        self._show_terminal_fallback(stderr_tail, args)

    def _show_terminal_fallback(self, stderr_tail: str, args: list[str]) -> None:
        no_agent = "agent" in stderr_tail.lower()
        body = ("No polkit authentication agent is running, so the password cannot be asked here. "
                "This is common on minimal Wayland compositors (niri, sway, Hyprland): start an agent "
                "such as polkit-gnome, lxqt-policykit or soteria, or run this command in a terminal."
                if no_agent else
                f"Administrator rights could not be obtained ({stderr_tail.strip() or 'pkexec failed'}). "
                "You can run the same installation from a terminal:")
        command = self.backend.terminal_command(args)
        dialog = Adw.AlertDialog(heading="Administrator rights needed", body=body)
        label = Gtk.Label(label=command, selectable=True, wrap=True, xalign=0)
        label.add_css_class("monospace")
        dialog.set_extra_child(label)
        dialog.add_response("copy", "Copy command")
        dialog.add_response("close", "Close")
        dialog.set_default_response("copy")

        def response(_d, rid):
            if rid == "copy":
                value = GObject.Value(GObject.TYPE_STRING, command)
                self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(value))
                self.toasts.add_toast(Adw.Toast(title="Command copied"))

        dialog.connect("response", response)
        dialog.present(self)

    # restore boot order ---------------------------------------------------

    def _restore_boot(self, banner: Adw.Banner) -> None:
        def on_exit(status: int, err: str) -> None:
            if status == 0:
                banner.set_revealed(False)
                self.toasts.add_toast(Adw.Toast(title="Boot order restored"))
            elif status == 126:
                self.toasts.add_toast(Adw.Toast(title="Authentication cancelled"))
            elif "agent" in err.lower():
                self._show_terminal_fallback(err, ["restore-boot"])
            else:
                self.toasts.add_toast(Adw.Toast(title=f"Could not restore the boot order ({status})"))

        self.backend.run(["restore-boot"], Job(lambda e: None, lambda line: None, on_exit))


class ProgressView(Gtk.Box):
    """Follows the JSON events of `install --json`."""

    def __init__(self, on_toast: Callable[[str], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=36, margin_bottom=24,
                         margin_start=36, margin_end=36, valign=Gtk.Align.START)
        self.on_toast = on_toast
        self.result: dict | None = None
        self.error: dict | None = None
        self.boot_entry: dict | None = None
        self.bootloader: dict | None = None

        self.spinner = Gtk.Spinner(spinning=True, width_request=48, height_request=48)
        self.title = Gtk.Label(label="Waiting for administrator rights…")
        self.title.add_css_class("title-2")
        self.detail = Gtk.Label(label="Your password is asked by the system, not by this program.")
        self.detail.add_css_class("dim-label")
        self.bar = Gtk.ProgressBar(show_text=False)
        self.numbers = Gtk.Label(label="")
        self.numbers.add_css_class("numeric")
        note = Gtk.Label(label="Your Linux disk is invisible to the installer: you can keep using the PC.",
                         wrap=True)
        note.add_css_class("dim-label")
        self.logbuf = Gtk.TextBuffer()
        logview = Gtk.TextView(buffer=self.logbuf, editable=False, monospace=True, cursor_visible=False)
        expander = Gtk.Expander(label="Details",
                                child=Gtk.ScrolledWindow(child=logview, min_content_height=160))
        for w in (self.spinner, self.title, self.detail, self.bar, self.numbers, note, expander):
            self.append(w)

    def log(self, line: str) -> None:
        self.logbuf.insert(self.logbuf.get_end_iter(), line + "\n")

    def event(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "started":
            self.title.set_label("Installing " + ev.get("edition", "Windows"))
            self.detail.set_label("First phase, in a hidden virtual machine")
        elif kind == "progress":
            self._progress(ev)
        elif kind == "first_phase_done":
            self.bar.set_fraction(1.0)
            self.detail.set_label("First phase done, preparing the boot entries")
        elif kind == "boot_entry":
            self.boot_entry = ev
        elif kind == "bootloader":
            self.bootloader = ev
        elif kind == "done":
            self.result = ev
        elif kind == "error":
            self.error = ev

    def _progress(self, ev: dict) -> None:
        phase, written, total = ev.get("phase"), ev.get("written", 0), ev.get("total", 0)
        self.detail.set_label(PHASES.get(phase, phase or ""))
        minutes, seconds = divmod(int(ev.get("elapsed", 0)), 60)
        if phase == "installing" and total:
            pct = min(0.99, written / total)
            self.bar.set_fraction(pct)
            self.numbers.set_label(f"~{pct:.0%}  ·  {gib(written)} written  ·  {minutes:02}:{seconds:02}")
        else:
            self.bar.pulse()
            self.numbers.set_label(f"{minutes:02}:{seconds:02}")


class App(Adw.Application):
    def __init__(self, backend: Backend):
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        super().__init__(application_id=APP_ID, flags=flags)
        self.backend = backend
        self.window: Window | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = Window(self, self.backend)
        self.window.present()


def run(test_image: str | None = None) -> None:
    if os.geteuid() == 0:
        print("Run the GUI as your normal user: it asks for administrator rights itself.", file=sys.stderr)
    App(Backend(test_image)).run([sys.argv[0]])
