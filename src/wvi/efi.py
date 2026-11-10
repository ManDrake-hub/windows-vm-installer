# SPDX-License-Identifier: GPL-3.0-or-later
"""UEFI boot entries through efibootmgr, plus the saved boot order.

During Windows' first native boot the installer reboots more than once, so
BootNext alone is not enough: the Windows entry goes first until the user is back
in Linux and runs `restore-boot`, which puts the previous order back.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

STATE_FILE = Path("/var/lib/windows-vm-installer/boot-state.json")
WINDOWS_LOADER = r"\EFI\Microsoft\Boot\bootmgfw.efi"
ENTRY_RE = re.compile(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+(.*)$")


class EFIError(Exception):
    pass


@dataclass
class BootState:
    current: str | None
    order: list[str]
    entries: dict[str, str]  # number -> label


def is_uefi_host() -> bool:
    return os.path.isdir("/sys/firmware/efi")


def has_oem_windows_key() -> bool:
    """The firmware carries an OEM Windows product key (ACPI MSDM table)."""
    return os.path.exists("/sys/firmware/acpi/tables/MSDM")


def _efibootmgr(*args: str) -> str:
    try:
        res = subprocess.run(["efibootmgr", *args], check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise EFIError("efibootmgr is not installed") from None
    except subprocess.CalledProcessError as e:
        raise EFIError(f"efibootmgr {' '.join(args)}: {e.stderr.strip() or e.returncode}") from None
    return res.stdout


def parse(output: str) -> BootState:
    current, order, entries = None, [], {}
    for line in output.splitlines():
        if line.startswith("BootCurrent:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("BootOrder:"):
            order = [x for x in line.split(":", 1)[1].strip().split(",") if x]
        else:
            m = ENTRY_RE.match(line)
            if m:
                entries[m.group(1).upper()] = m.group(3).split("\t")[0].strip()
    return BootState(current, [o.upper() for o in order], entries)


def read_state() -> BootState:
    return parse(_efibootmgr())


def create_windows_entry(disk: str, partition: int, label: str) -> str:
    """Create a boot entry for bootmgfw.efi without touching BootOrder; return its number."""
    before = read_state()
    _efibootmgr("-q", "-C", "-d", disk, "-p", str(partition), "-L", label, "-l", WINDOWS_LOADER)
    new = sorted(set(read_state().entries) - set(before.entries))
    if len(new) != 1:
        raise EFIError("could not identify the new boot entry")
    return new[0]


def set_order(order: list[str]) -> None:
    _efibootmgr("-q", "-o", ",".join(order))


def put_first(num: str, state_file: Path = STATE_FILE) -> list[str]:
    """Put an entry first, saving the previous order for restore_order()."""
    state = read_state()
    if not state_file.exists():
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "previous_order": state.order,
            "windows_entry": num,
        }, indent=2) + "\n")
    order = [num] + [n for n in state.order if n != num]
    set_order(order)
    return order


def restore_order(state_file: Path = STATE_FILE) -> list[str]:
    """Previous order back (entries that still exist), Windows entry last."""
    try:
        saved = json.loads(state_file.read_text())
    except FileNotFoundError:
        raise EFIError(f"no saved boot order ({state_file}): nothing to restore") from None
    state = read_state()
    windows = saved.get("windows_entry")
    order = [n for n in saved["previous_order"] if n in state.entries and n != windows]
    order += [n for n in state.order if n not in order and n != windows]
    if windows in state.entries:
        order.append(windows)
    if not order:
        raise EFIError("no boot entry is left to put in the boot order")
    set_order(order)
    state_file.unlink()
    return order
