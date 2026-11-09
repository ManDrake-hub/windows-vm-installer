# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal read-only FAT32 reader to check files inside an EFI system partition.

Supports long file names, because Windows creates EFI/Microsoft (9 characters,
so only reachable as MICROS~1 through the short name).
"""

from __future__ import annotations

import struct
from typing import BinaryIO


class FATError(Exception):
    pass


class FAT32:
    def __init__(self, f: BinaryIO, offset: int):
        self.f, self.base = f, offset
        f.seek(offset)
        bs = f.read(512)
        if bs[510:512] != b"\x55\xaa":
            raise FATError("no FAT boot sector signature")
        self.bps, self.spc, reserved, nfats = struct.unpack_from("<HBHB", bs, 11)
        fat16_size = struct.unpack_from("<H", bs, 22)[0]
        fat_size, self.root_cluster = struct.unpack_from("<I4xI", bs, 36)
        if fat16_size != 0 or self.bps not in (512, 1024, 2048, 4096) or self.spc == 0:
            raise FATError("not a FAT32 filesystem")
        self.fat_start = offset + reserved * self.bps
        self.data_start = self.fat_start + nfats * fat_size * self.bps
        self.cluster_bytes = self.bps * self.spc
        serial = struct.unpack_from("<I", bs, 67)[0]
        # The "UUID" Linux and GRUB show for FAT volumes, e.g. 8441-18C4
        self.volume_id = f"{serial >> 16:04X}-{serial & 0xFFFF:04X}"

    def _next(self, cluster: int) -> int:
        self.f.seek(self.fat_start + cluster * 4)
        return struct.unpack("<I", self.f.read(4))[0] & 0x0FFFFFFF

    def _chain(self, cluster: int) -> bytes:
        data, seen = bytearray(), set()
        while 2 <= cluster < 0x0FFFFFF8 and cluster not in seen:
            seen.add(cluster)
            self.f.seek(self.data_start + (cluster - 2) * self.cluster_bytes)
            data += self.f.read(self.cluster_bytes)
            cluster = self._next(cluster)
        return bytes(data)

    def listdir(self, cluster: int) -> dict[str, tuple[bool, int, int]]:
        """Map lower-case name -> (is_dir, first cluster, size)."""
        raw = self._chain(cluster)
        out: dict[str, tuple[bool, int, int]] = {}
        lfn: list[str] = []
        for off in range(0, len(raw), 32):
            e = raw[off:off + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = []
                continue
            attr = e[11]
            if attr == 0x0F:  # long file name fragment, stored last-first
                part = (e[1:11] + e[14:26] + e[28:32]).decode("utf-16-le", "ignore")
                lfn.insert(0, part.split("\0")[0].rstrip("￿"))
                continue
            if attr & 0x08:  # volume label
                lfn = []
                continue
            short = e[0:8].decode("latin-1").rstrip()
            ext = e[8:11].decode("latin-1").rstrip()
            name = "".join(lfn) or (short + ("." + ext if ext else ""))
            lfn = []
            if name in (".", ".."):
                continue
            hi, lo = struct.unpack_from("<H", e, 20)[0], struct.unpack_from("<H", e, 26)[0]
            size = struct.unpack_from("<I", e, 28)[0]
            out[name.lower()] = (bool(attr & 0x10), (hi << 16) | lo, size)
        return out

    def stat(self, path: str) -> tuple[bool, int] | None:
        """(is_dir, size) for a path like EFI/Microsoft/Boot/BCD, or None."""
        cluster = self.root_cluster
        info: tuple[bool, int, int] = (True, cluster, 0)
        for part in path.strip("/").split("/"):
            if not info[0]:
                return None
            info = self.listdir(info[1]).get(part.lower())  # type: ignore[assignment]
            if info is None:
                return None
        return info[0], info[2]
