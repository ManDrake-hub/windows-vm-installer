# SPDX-License-Identifier: GPL-3.0-or-later
"""Find candidate target disks and explain why a disk cannot be used."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MIN_SIZE = 64 * 10**9  # Windows 11 requirement
CONTROLLERS = {"nvme": "nvme", "sata": "sata", "ata": "sata"}


@dataclass
class Disk:
    name: str  # kernel name, e.g. nvme1n1
    path: str  # /dev/nvme1n1
    by_id: str | None  # stable /dev/disk/by-id/... path
    size: int
    model: str
    serial: str
    transport: str
    partitions: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # reasons the disk must not be used
    warnings: list[str] = field(default_factory=list)

    @property
    def controller(self) -> str | None:
        return CONTROLLERS.get(self.transport)

    @property
    def usable(self) -> bool:
        return not self.problems


def _lsblk() -> list[dict]:
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o",
         "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,MOUNTPOINTS,FSTYPE,LABEL,PARTTYPENAME"],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)["blockdevices"]


def _by_id(name: str) -> str | None:
    """Prefer the model+serial symlink (nvme-..., ata-...) over wwn/eui ones."""
    links = []
    base = Path("/dev/disk/by-id")
    if base.is_dir():
        for link in base.iterdir():
            if os.path.basename(os.path.realpath(link)) == name:
                links.append(link.name)
    for prefix in ("nvme-", "ata-", "scsi-", ""):
        best = sorted(link for link in links if link.startswith(prefix)
                      and not link.startswith(("nvme-eui.", "nvme-nvme.")) and not link.endswith("_1"))
        if best:
            return str(base / best[0])
    return str(base / sorted(links)[0]) if links else None


def _walk(node: dict):
    for child in node.get("children", []) or []:
        yield child
        yield from _walk(child)


def _swaps() -> set[str]:
    try:
        with open("/proc/swaps") as f:
            return {line.split()[0] for line in f.readlines()[1:] if line.strip()}
    except OSError:
        return set()


def _behind_vmd(name: str) -> bool:
    """True when the disk sits behind an Intel VMD controller (RST "RAID" mode)."""
    path = os.path.realpath(f"/sys/block/{name}/device")
    while path not in ("/", "/sys"):
        driver = os.path.join(path, "driver")
        if os.path.islink(driver) and os.path.basename(os.readlink(driver)) == "vmd":
            return True
        path = os.path.dirname(path)
    return False


def list_disks() -> list[Disk]:
    swaps = _swaps()
    disks = []
    for dev in _lsblk():
        if dev.get("type") != "disk" or dev["name"].startswith(("zram", "loop", "ram", "sr")):
            continue
        disk = Disk(name=dev["name"], path=dev["path"], by_id=_by_id(dev["name"]),
                    size=int(dev["size"]), model=(dev.get("model") or "").strip(),
                    serial=(dev.get("serial") or "").strip(), transport=(dev.get("tran") or "").lower())
        children = list(_walk(dev))
        disk.partitions = [c for c in children if c.get("type") == "part"]

        mounted = sorted({m for c in [dev, *children] for m in (c.get("mountpoints") or []) if m})
        if mounted:
            disk.problems.append("in use: mounted at " + ", ".join(mounted))
        if any(c["path"] in swaps for c in [dev, *children]):
            disk.problems.append("in use as swap")
        stacked = sorted({c.get("type") for c in children if c.get("type") not in ("part", None)})
        if stacked:
            disk.problems.append("in use by " + ", ".join(stacked) + " (LUKS/LVM/RAID)")
        if dev.get("ro"):
            disk.problems.append("read-only")
        if disk.transport == "usb" or dev.get("rm"):
            disk.problems.append("removable/USB disks are not supported")
        elif disk.controller is None:
            disk.problems.append(f"unsupported connection {disk.transport or 'unknown'} (only NVMe and SATA)")
        if disk.size < MIN_SIZE:
            disk.problems.append(f"too small: Windows 11 needs at least {MIN_SIZE // 10**9} GB")
        if disk.by_id is None:
            disk.problems.append("no stable /dev/disk/by-id name")
        if _behind_vmd(disk.name):
            disk.warnings.append("behind Intel VMD/RST: Windows will need the Intel RST driver to boot")
        if disk.partitions and not disk.problems:
            disk.warnings.append(f"contains {len(disk.partitions)} partition(s): they will be erased")
        disks.append(disk)
    return disks
