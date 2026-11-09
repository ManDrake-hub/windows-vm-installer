# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, bootloaders, efi, unattend
from .disks import Disk, list_disks
from .fat import FAT32, FATError
from .gpt import GPTError, read_gpt
from .iso9660 import write_iso
from .qemu import InstallerVM, Progress, VMError, find_firmware
from .winiso import Edition, NotWindowsISO, inspect_iso

EULA_URL = "https://www.microsoft.com/en-us/useterms"
TEST_IMAGE_SIZE = 80 * 2**30
# The first phase writes about 57% of the image's TOTALBYTES (measured: 13.9 of
# 24.8 GiB for Windows 11 Pro 26H2): duplicates and hard links count only once
WRITE_RATIO = 0.58
TTY = sys.stdout.isatty()
# In --json mode human-readable output goes to stderr and machine-readable events,
# one JSON object per line, go to the original stdout (used by the GUI)
EVENTS = None


class Abort(Exception):
    """Stop with a message, no traceback."""

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


# --- output helpers -------------------------------------------------------

def emit(event: str, **data) -> None:
    if EVENTS is not None:
        EVENTS.write(json.dumps({"event": event, **data}) + "\n")
        EVENTS.flush()



def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


def bold(t: str) -> str:
    return _c("1", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def green(t: str) -> str:
    return _c("32", t)


def gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


def gb(n: int) -> str:
    return f"{n / 10**9:.0f} GB"


def ask(prompt: str) -> str:
    if EVENTS is not None:
        raise Abort(f"a question was needed in --json mode ({prompt.strip()})")
    try:
        return input(prompt).strip()
    except EOFError:
        raise Abort("no input available: use the command line options") from None


def choose(title: str, items: list[str], selectable: list[bool] | None = None) -> int:
    print(bold(title))
    for i, item in enumerate(items, 1):
        print(f"  {i:2}) {item}")
    while True:
        answer = ask("Choose a number: ")
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            idx = int(answer) - 1
            if selectable is None or selectable[idx]:
                return idx
            print(red("That one cannot be used."))


# --- checks ---------------------------------------------------------------

def host_problems(test_mode: bool, boot_entry: bool) -> list[str]:
    problems = []
    if not test_mode and os.geteuid() != 0:
        problems.append("run as root (sudo): the target disk and the UEFI boot entries need it")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        problems.append("/dev/kvm is not available: enable virtualization (VT-x/AMD-V) in the firmware")
    for tool in ("qemu-system-x86_64", "swtpm"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} is not installed")
    if find_firmware() is None:
        problems.append("no UEFI firmware with Secure Boot for QEMU (install edk2-ovmf / ovmf)")
    if boot_entry and not test_mode:
        if not efi.is_uefi_host():
            problems.append("this PC did not boot in UEFI mode: use --no-boot-entry and add Windows to your boot menu manually")
        elif shutil.which("efibootmgr") is None:
            problems.append("efibootmgr is not installed (or use --no-boot-entry)")
    return problems


def describe_disk(d: Disk) -> str:
    text = f"{d.model or d.name}  {gb(d.size)}  {d.transport.upper()}  {d.path}"
    if d.problems:
        text += red("  [" + "; ".join(d.problems) + "]")
    return text


def pick_disk(path: str | None) -> Disk:
    disks = list_disks()
    if path:
        real = os.path.realpath(path)
        disk = next((d for d in disks if d.path == real), None)
        if disk is None:
            raise Abort(f"{path} is not a whole disk")
    else:
        if not disks:
            raise Abort("no disks found")
        idx = choose("Target disk (everything on it will be erased):",
                     [describe_disk(d) for d in disks], [d.usable for d in disks])
        disk = disks[idx]
    if disk.problems:
        raise Abort(f"{disk.path} cannot be used: " + "; ".join(disk.problems))
    return disk


def pick_edition(editions: list[Edition], name: str | None) -> Edition:
    if name:
        found = next((e for e in editions if e.name.lower() == name.lower()), None)
        if found is None:
            raise Abort(f"edition {name!r} is not in this ISO: " + ", ".join(e.name for e in editions))
        return found
    if efi.has_oem_windows_key():
        print(yellow("This PC's firmware carries an OEM Windows key (often for Home). Pick the "
                     "edition your license is for: Windows activates on the first native boot."))
    return editions[choose("Windows edition:", [e.name for e in editions])]


# --- install --------------------------------------------------------------

def show_progress(p: Progress, total: int) -> None:
    if EVENTS is not None:
        if int(p.elapsed) != show_progress.last_second:  # at most one event per second
            show_progress.last_second = int(p.elapsed)
            emit("progress", phase=p.phase, elapsed=int(p.elapsed), written=p.written, total=total)
        return
    if p.phase == "firmware":
        status = "starting the VM, booting from the ISO"
    elif p.phase == "loading":
        status = "loading Windows Setup"
    else:
        pct = min(99, int(p.written * 100 / total)) if total else 0
        bar = "#" * (pct // 4) + "-" * (25 - pct // 4)
        status = f"[{bar}] ~{pct:2}%  {gib(p.written)} written"
    minutes, seconds = divmod(int(p.elapsed), 60)
    line = f"  {minutes:02}:{seconds:02}  {status}"
    if TTY:
        print("\r" + line.ljust(78), end="", flush=True)
    elif int(p.elapsed) // 60 != show_progress.last_minute:  # one line per minute in logs
        show_progress.last_minute = int(p.elapsed) // 60
        print(line, flush=True)


show_progress.last_minute = -1  # type: ignore[attr-defined]
show_progress.last_second = -1  # type: ignore[attr-defined]


def verify_target(target: str) -> tuple[int, str, str]:
    """Check a disk with Windows on it: (ESP number, ESP partition GUID, FAT volume id)."""
    try:
        table = read_gpt(target)
    except (GPTError, OSError) as e:
        raise Abort(f"{target} has no valid GPT: {e}") from None
    esp = table.esp()
    if esp is None:
        raise Abort(f"{target} has no EFI system partition")
    with open(target, "rb") as f:
        try:
            fs = FAT32(f, esp.offset(table.sector_size))
            missing = [p for p in ("EFI/Microsoft/Boot/bootmgfw.efi", "EFI/Microsoft/Boot/BCD")
                       if fs.stat(p) is None]
        except FATError as e:
            raise Abort(f"cannot read the EFI system partition: {e}") from None
    if missing:
        raise Abort("the Windows boot files are missing: " + ", ".join(missing))
    return esp.number, str(esp.guid), fs.volume_id


def add_to_boot_manager(target: bootloaders.WindowsTarget, assume_yes: bool) -> None:
    """Final phase: put Windows in the menu of the boot manager in use."""
    bm = bootloaders.detect()
    p = bootloaders.plan(bm, target)
    print(bold(f"Boot menu ({bm.kind}{', entry ' + repr(bm.entry) if bm.entry else ''})"))
    print(f"  {p.summary}")
    if p.apply is None:
        emit("bootloader", kind=bm.kind, summary=p.summary, result=None, error=None)
        return
    for change in p.changes:
        print(f"  - {change}")
    if not assume_yes and ask("Apply these changes? [y/N] ").lower() not in ("y", "yes"):
        print("  Skipped: you can run 'add-to-bootloader' later.")
        return
    try:
        result = p.apply()
        print(green(f"  {result}"))
        emit("bootloader", kind=bm.kind, summary=p.summary, result=result, error=None)
    except (OSError, RuntimeError) as e:
        print(red(f"  Could not update the boot menu: {e}"))
        print("  Windows can still be started from the firmware boot menu.")
        emit("bootloader", kind=bm.kind, summary=p.summary, result=None, error=str(e))


def cmd_install(args: argparse.Namespace) -> None:
    test_mode = args.test_image is not None
    if EVENTS is not None:
        missing = [opt for opt, value in (("--iso", args.iso), ("--edition", args.edition),
                                          ("--disk or --test-image", args.disk or args.test_image),
                                          ("--accept-eula", args.accept_eula), ("--yes", args.yes or test_mode))
                   if not value]
        if missing:
            raise Abort("--json needs " + ", ".join(missing))
    problems = host_problems(test_mode, not args.no_boot_entry)
    if problems:
        raise Abort("cannot continue:\n  - " + "\n  - ".join(problems))

    iso_path = Path(args.iso or ask("Path of the Windows ISO: ")).expanduser().resolve()
    try:
        iso = inspect_iso(iso_path)
    except (NotWindowsISO, OSError) as e:
        raise Abort(str(e)) from None
    editions = [e for e in iso.editions if e.arch == "x64"]
    if not editions:
        raise Abort("the ISO has no x64 edition")

    if test_mode:
        target = Path(args.test_image).expanduser().resolve()
        disk = None
        target_desc = f"test image {target}"
    else:
        disk = pick_disk(args.disk)
        target = Path(disk.path)
        target_desc = describe_disk(disk)

    edition = pick_edition(editions, args.edition)
    language = iso.default_language

    print()
    print(bold("Summary"))
    print(f"  ISO:       {iso_path.name} (build {edition.build}, {language})")
    print(f"  Edition:   {edition.name}")
    print(f"  Target:    {target_desc}")
    if disk:
        for p in disk.partitions:
            print(f"             - {p['name']}  {gib(int(p['size']))}  {p.get('fstype') or ''} "
                  f"{p.get('label') or ''} {p.get('parttypename') or ''}".rstrip())
        for w in disk.warnings:
            print(yellow(f"  Warning:   {w}"))
    tweaks = [t for t, on in (("clock in UTC like Linux", not args.no_utc_clock),
                              ("Fast Startup off", not args.keep_fast_startup)) if on]
    print(f"  Dual boot: {', '.join(tweaks) or 'no changes'}")
    print()

    if not args.accept_eula:
        print(f"Windows is licensed under the Microsoft Software License Terms ({EULA_URL}).")
        print("The installer runs unattended, so it needs your acceptance now.")
        if ask("Type 'accept' to accept the license terms: ").lower() != "accept":
            raise Abort("license terms not accepted")

    if disk and not args.yes:
        print(red(bold(f"ALL DATA ON {disk.path} ({disk.model}, {gb(disk.size)}) WILL BE ERASED.")))
        if ask(f"Type the disk name ({disk.name}) to continue: ") != disk.name:
            raise Abort("confirmation did not match, nothing was changed")

    answer = unattend.build(unattend.AnswerOptions(
        edition=edition.name, language=language, product_key=args.product_key or "",
        accept_eula=True, utc_clock=not args.no_utc_clock,
        disable_fast_startup=not args.keep_fast_startup))

    workdir = Path(tempfile.mkdtemp(prefix="windows-vm-installer-", dir="/var/tmp"))
    workdir.chmod(0o755)  # the GUI runs as the user and shows the log and the last screen
    answer_iso = workdir / "answer.iso"
    write_iso(answer_iso, {"autounattend.xml": answer.encode()})
    if test_mode and not target.exists():
        with open(target, "wb") as f:
            f.truncate(TEST_IMAGE_SIZE)

    firmware = find_firmware()
    assert firmware
    vm = InstallerVM(workdir, iso_path, answer_iso, target, disk.controller if disk else "nvme",
                     firmware, memory_mb=args.memory, show=args.show)
    print(bold("Installing Windows (first phase, in a hidden virtual machine)"))
    emit("started", edition=edition.name, target=str(target), workdir=str(workdir))
    ok = False
    try:
        vm.start()
        vm.run(lambda p: show_progress(p, int(edition.total_bytes * WRITE_RATIO)))
        ok = True
    except VMError as e:
        shot = workdir / "last-screen.png"
        shot_path = str(shot) if shot.exists() else None
        raise Abort(f"{e}\n  Log: {vm.log_path}" + (f"\n  Last screen: {shot}" if shot_path else ""),
                    log=str(vm.log_path), screenshot=shot_path) from None
    except KeyboardInterrupt:
        raise Abort(f"interrupted; the target disk may be partially written. Log: {vm.log_path}",
                    log=str(vm.log_path)) from None
    finally:
        vm.stop()
        if TTY:
            print()
        if ok:
            shutil.rmtree(workdir, ignore_errors=True)

    esp_number, esp_guid, esp_volume_id = verify_target(str(target))
    print(green(f"First phase done: Windows boot files are on partition {esp_number}."))
    emit("first_phase_done", esp_partition=esp_number)

    if disk is None:
        print("Test image ready: boot it in a VM to continue the installation.")
        emit("done", next_steps=["Boot the test image in a VM to continue the installation."])
        return
    subprocess.run(["partx", "-u", disk.path], capture_output=True)  # refresh the kernel's view
    subprocess.run(["udevadm", "settle"], capture_output=True)

    if not args.no_boot_entry:
        label = f"{edition.name} ({disk.model or disk.name})"[:60]
        num = efi.create_windows_entry(disk.path, esp_number, label)
        efi.put_first(num)
        print(green(f"Boot entry Boot{num} '{label}' created and put first."))
        emit("boot_entry", number=num, label=label)
    if not args.no_bootloader:
        print()
        add_to_boot_manager(bootloaders.WindowsTarget(edition.name, esp_guid, esp_volume_id), args.yes)

    if args.no_boot_entry:
        steps = ["Reboot and start Windows from your boot menu: it finishes its setup on the real hardware."]
    else:
        steps = [
            "Reboot: Windows finishes its setup on the real hardware (devices, account, activation). "
            "It may restart a couple of times; it keeps booting Windows until you are done.",
            "Back in Linux (in Windows: Settings > System > Recovery > Advanced startup > Use a device), "
            "make your Linux boot loader the default again with 'restore-boot'.",
        ]
    print()
    print(bold("Next steps"))
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")
    emit("done", next_steps=steps)


def cmd_list_disks(args: argparse.Namespace) -> None:
    for d in list_disks():
        print(("  " if d.usable else red("✗ ")) + describe_disk(d))
        if d.by_id:
            print(f"      {d.by_id}")
        for w in d.warnings:
            print(yellow(f"      {w}"))


def cmd_inspect_iso(args: argparse.Namespace) -> None:
    try:
        iso = inspect_iso(args.iso)
    except (NotWindowsISO, OSError) as e:
        raise Abort(str(e)) from None
    print(f"{iso.image_path}, default language {iso.default_language}")
    for e in iso.editions:
        print(f"  {e.index:2}  {e.name:<40} {e.arch:<6} build {e.build}  {gib(e.total_bytes)}")


def cmd_add_to_bootloader(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise Abort("run as root (sudo)")
    disk = os.path.realpath(args.disk)
    _, esp_guid, esp_volume_id = verify_target(disk)
    add_to_boot_manager(bootloaders.WindowsTarget(args.title, esp_guid, esp_volume_id), args.yes)


def cmd_restore_boot(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise Abort("run as root (sudo)")
    try:
        order = efi.restore_order()
    except efi.EFIError as e:
        raise Abort(str(e)) from None
    entries = efi.read_state().entries
    print(green("Boot order restored:"))
    for n in order:
        print(f"  Boot{n}  {entries.get(n, '')}")
    emit("done", order=[{"number": n, "label": entries.get(n, "")} for n in order])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="windows-vm-installer",
        description="Install Windows onto a physical disk from Linux, through a hidden virtual machine.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install", help="install Windows onto a disk")
    p.add_argument("--iso", help="Windows installation ISO")
    p.add_argument("--disk", help="target disk, e.g. /dev/disk/by-id/nvme-...")
    p.add_argument("--edition", help='edition name as listed by inspect-iso, e.g. "Windows 11 Pro"')
    p.add_argument("--product-key", help="product key (default: none, activation happens later)")
    p.add_argument("--accept-eula", action="store_true", help="accept the Microsoft license terms")
    p.add_argument("--yes", action="store_true",
                   help="do not ask for confirmations (erasing the disk, boot menu changes)")
    p.add_argument("--no-utc-clock", action="store_true", help="leave the Windows clock in local time")
    p.add_argument("--keep-fast-startup", action="store_true", help="leave Windows Fast Startup on")
    p.add_argument("--no-boot-entry", action="store_true", help="do not create a UEFI boot entry")
    p.add_argument("--no-bootloader", action="store_true",
                   help="do not add Windows to the menu of your boot manager (Limine, GRUB, rEFInd)")
    p.add_argument("--memory", type=int, default=4096, help="VM memory in MiB (default 4096)")
    p.add_argument("--show", action="store_true", help="show the VM window (debugging)")
    p.add_argument("--test-image", metavar="FILE", help="install into an image file instead of a disk (testing)")
    p.add_argument("--json", action="store_true", help="machine-readable events on stdout (used by the GUI)")
    p.set_defaults(func=cmd_install)

    sub.add_parser("list-disks", help="show disks and whether they can be used").set_defaults(func=cmd_list_disks)
    p = sub.add_parser("inspect-iso", help="show the editions in a Windows ISO")
    p.add_argument("iso")
    p.set_defaults(func=cmd_inspect_iso)
    p = sub.add_parser("restore-boot", help="make the previous boot loader the default again")
    p.add_argument("--json", action="store_true", help="machine-readable events on stdout")
    p.set_defaults(func=cmd_restore_boot)
    p = sub.add_parser("gui", help="open the graphical installer")
    p.add_argument("--test-image", metavar="FILE", help="install into an image file instead of a disk (testing)")
    p.set_defaults(func=cmd_gui)
    p = sub.add_parser("add-to-bootloader", help="add a Windows already on a disk to your boot menu")
    p.add_argument("--disk", required=True, help="disk holding Windows, e.g. /dev/disk/by-id/nvme-...")
    p.add_argument("--title", default="Windows", help="menu title (default: Windows)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_add_to_bootloader)

    args = parser.parse_args(argv)
    global EVENTS, TTY
    if getattr(args, "json", False):
        EVENTS, sys.stdout, TTY = sys.stdout, sys.stderr, False
    try:
        args.func(args)
    except Abort as e:
        print(red(f"Error: {e}"), file=sys.stderr)
        emit("error", message=str(e).split("\n")[0], **e.details)
        return 1
    except KeyboardInterrupt:
        print(red("\nInterrupted."), file=sys.stderr)
        emit("error", message="interrupted")
        return 130
    except Exception as e:  # report unexpected failures to the GUI too
        emit("error", message=f"unexpected error: {e!r}")
        raise
    return 0


def cmd_gui(args: argparse.Namespace) -> None:
    try:
        from .gui import run
    except (ImportError, ValueError) as e:
        raise Abort(f"the GUI needs GTK 4 and libadwaita through PyGObject: {e}") from None
    run(test_image=args.test_image)
