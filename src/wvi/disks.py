# SPDX-License-Identifier: GPL-3.0-or-later
"""Find candidate target disks and explain why a disk cannot be used."""

from __future__ import annotations

import errno
import glob
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MIN_SIZE = 64 * 10**9  # Windows 11 requirement
CONTROLLERS = {"nvme": "nvme", "sata": "sata", "ata": "sata"}
# Signatures of storage pools: the member may be in use without being mounted
POOL_MEMBERS = {"LVM2_member": "LVM", "linux_raid_member": "software RAID", "zfs_member": "ZFS",
                "bcache": "bcache", "crypto_LUKS": "LUKS"}


class DiskError(Exception):
    """The disks could not be listed."""


@dataclass
class Disk:
    name: str  # kernel name, e.g. nvme1n1 (not stable across boots: use by_id to refer to the disk)
    path: str  # /dev/nvme1n1
    by_id: str | None  # stable /dev/disk/by-id/... path
    size: int
    model: str
    serial: str
    transport: str
    partitions: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # reasons the disk must not be used
    warnings: list[str] = field(default_factory=list)
    sector_size: int = 512  # logical
    physical_sector_size: int = 512

    @property
    def controller(self) -> str | None:
        return CONTROLLERS.get(self.transport)

    @property
    def usable(self) -> bool:
        return not self.problems


def _lsblk() -> list[dict]:
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,MOUNTPOINTS,FSTYPE,LABEL,PARTTYPENAME,PARTUUID,"
             "LOG-SEC,PHY-SEC"],
            check=True, capture_output=True, text=True).stdout
        return json.loads(out)["blockdevices"]
    except FileNotFoundError:
        raise DiskError("lsblk (util-linux) is not installed") from None
    except subprocess.CalledProcessError as e:
        raise DiskError(f"lsblk failed (util-linux 2.37 or newer is needed): {e.stderr.strip()}") from None
    except (ValueError, KeyError) as e:
        raise DiskError(f"unexpected lsblk output: {e}") from None


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


def _btrfs_members() -> set[str]:
    """Kernel names of every device of the mounted btrfs filesystems.

    lsblk shows the mount point on one device only, so the other members of a
    multi-device filesystem would look unused.
    """
    return {os.path.basename(p) for p in glob.glob("/sys/fs/btrfs/*/devices/*")}


def _holders(name: str) -> list[str]:
    """Devices stacked on this one (device mapper, md, bcache)."""
    try:
        return sorted(os.listdir(f"/sys/class/block/{name}/holders"))
    except OSError:
        return []


def _behind_vmd(name: str) -> bool:
    """True when the disk sits behind an Intel VMD controller (RST "RAID" mode)."""
    path = os.path.realpath(f"/sys/block/{name}/device")
    while path not in ("/", "/sys"):
        driver = os.path.join(path, "driver")
        if os.path.islink(driver) and os.path.basename(os.readlink(driver)) == "vmd":
            return True
        path = os.path.dirname(path)
    return False


def _boot_esp_guid() -> str | None:
    """Partition GUID of the EFI partition this system was started from."""
    from .bootloaders import parse_current
    try:
        out = subprocess.run(["efibootmgr", "-v"], check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return parse_current(out)[1]


def kernel_busy(path: str) -> bool | None:
    """Ask the kernel whether the disk or one of its partitions is claimed.

    An exclusive open fails with EBUSY when a filesystem is mounted, or when
    swap, device mapper, md, btrfs, ZFS or bcache hold the device. Returns None
    when it cannot be checked (not root).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_EXCL)
    except OSError as e:
        if e.errno == errno.EBUSY:
            return True
        return None
    os.close(fd)
    return False


def list_disks() -> list[Disk]:
    swaps = _swaps()
    btrfs = _btrfs_members()
    boot_esp = _boot_esp_guid()
    zfs_loaded = os.path.isdir("/sys/module/zfs")
    disks = []
    for dev in _lsblk():
        if dev.get("type") != "disk" or dev["name"].startswith(("zram", "loop", "ram", "sr")):
            continue
        disk = Disk(name=dev["name"], path=dev["path"], by_id=_by_id(dev["name"]),
                    size=int(dev["size"]), model=(dev.get("model") or "").strip(),
                    serial=(dev.get("serial") or "").strip(), transport=(dev.get("tran") or "").lower(),
                    sector_size=int(dev.get("log-sec") or 512),
                    physical_sector_size=int(dev.get("phy-sec") or 512))
        children = list(_walk(dev))
        nodes = [dev, *children]
        disk.partitions = [c for c in children if c.get("type") == "part"]

        mounted = sorted({m for c in nodes for m in (c.get("mountpoints") or []) if m})
        if mounted:
            disk.problems.append("in use: mounted at " + ", ".join(mounted))
        if any(c["path"] in swaps for c in nodes):
            disk.problems.append("in use as swap")
        stacked = sorted({c.get("type") for c in children if c.get("type") not in ("part", None)})
        holders = sorted({h for c in nodes for h in _holders(c["name"])})
        if stacked or holders:
            disk.problems.append("in use by " + ", ".join(stacked or holders) + " (LUKS/LVM/RAID)")
        if any(c["name"] in btrfs for c in nodes):
            disk.problems.append("part of a mounted btrfs filesystem")
        fstypes = {c.get("fstype") for c in nodes}
        if "zfs_member" in fstypes and zfs_loaded:
            disk.problems.append("member of a ZFS pool")
        if boot_esp and any((c.get("partuuid") or "").lower() == boot_esp for c in children):
            disk.problems.append("holds the EFI partition this system boots from")
        if not any(p.startswith(("in use", "part of", "member of")) for p in disk.problems) \
                and kernel_busy(disk.path):
            disk.problems.append("in use (the kernel reports the device as busy)")
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
        if not disk.problems:
            pools = sorted({POOL_MEMBERS[t] for t in fstypes if t in POOL_MEMBERS})
            if pools:
                disk.warnings.append(f"holds {', '.join(pools)} data that is not active now: "
                                     "make sure no storage pool you use needs this disk")
            if disk.partitions:
                disk.warnings.append(f"contains {len(disk.partitions)} partition(s): they will be erased")
        disks.append(disk)
    return disks
