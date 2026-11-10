# SPDX-License-Identifier: GPL-3.0-or-later
"""Read a GPT partition table straight from a disk or image file.

Reading the table from the disk itself matters: right after the VM exits, the
kernel's view of the partitions can be stale (a partition that starts at the
same sector as an old one may keep its old size).
"""

from __future__ import annotations

import struct
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path

ESP_TYPE = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
MSR_TYPE = uuid.UUID("e3c9e316-0b5c-4db8-817d-f92df00215ae")
BASIC_DATA_TYPE = uuid.UUID("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7")


class GPTError(Exception):
    pass


@dataclass
class Partition:
    number: int
    type_guid: uuid.UUID
    guid: uuid.UUID
    first_lba: int
    last_lba: int
    name: str

    def offset(self, sector_size: int) -> int:
        return self.first_lba * sector_size

    def size(self, sector_size: int) -> int:
        return (self.last_lba - self.first_lba + 1) * sector_size


@dataclass
class GPT:
    disk_guid: uuid.UUID
    sector_size: int
    partitions: list[Partition]

    def esp(self) -> Partition | None:
        return next((p for p in self.partitions if p.type_guid == ESP_TYPE), None)


def read_gpt(path: str | Path, sector_size: int | None = None) -> GPT:
    with open(path, "rb") as f:
        for ss in ([sector_size] if sector_size else [512, 4096]):
            f.seek(ss)
            header = f.read(92)
            if header[:8] == b"EFI PART":
                break
        else:
            raise GPTError(f"{path}: no GPT header")

        hsize, hcrc = struct.unpack_from("<II", header, 12)
        if not 92 <= hsize <= ss:
            raise GPTError(f"{path}: invalid GPT header size")
        f.seek(ss)
        check = bytearray(f.read(hsize))
        check[16:20] = b"\0\0\0\0"
        if zlib.crc32(check) != hcrc:
            raise GPTError(f"{path}: GPT header checksum mismatch")
        disk_guid = uuid.UUID(bytes_le=header[56:72])
        entries_lba, count, esize, ecrc = struct.unpack_from("<QIII", header, 72)
        f.seek(entries_lba * ss)
        table = f.read(count * esize)
        if zlib.crc32(table) != ecrc:
            raise GPTError(f"{path}: GPT partition table checksum mismatch")

    parts = []
    for i in range(count):
        e = table[i * esize:(i + 1) * esize]
        type_guid = uuid.UUID(bytes_le=e[:16])
        if type_guid.int == 0:
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        name = e[56:128].decode("utf-16-le").rstrip("\0")
        parts.append(Partition(i + 1, type_guid, uuid.UUID(bytes_le=e[16:32]), first, last, name))
    return GPT(disk_guid, ss, parts)
