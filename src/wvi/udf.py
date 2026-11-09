# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal read-only UDF reader, enough to locate files on a Windows install ISO.

Microsoft ISOs are mastered as UDF with an ISO 9660 bridge that only contains a
README, so the installation image (sources/install.wim) is reachable through UDF
only. This module implements the small subset of ECMA-167 / UDF 1.02 needed to
walk the directory tree and read a file's data: anchor, volume descriptors,
file set descriptor, file entries and file identifier descriptors.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO

SECTOR = 2048
ANCHOR_SECTOR = 256

TAG_PARTITION = 5
TAG_LOGICAL_VOLUME = 6
TAG_TERMINATING = 8
TAG_FILE_SET = 256
TAG_FILE_ID = 257
TAG_FILE_ENTRY = 261
TAG_EXT_FILE_ENTRY = 266
TAG_ANCHOR = 2

FID_DIRECTORY = 0x02
FID_DELETED = 0x04
FID_PARENT = 0x08


class UDFError(Exception):
    """The image is not a UDF volume this reader understands."""


@dataclass
class Extent:
    """A run of file data: absolute byte offset in the image and length."""

    offset: int
    length: int


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int
    extents: list[Extent]
    embedded: bytes | None = None


class UDFImage:
    def __init__(self, f: BinaryIO):
        self.f = f
        self.partition_start = 0  # in sectors
        self.block_size = SECTOR
        self._read_volume()

    # --- low level -------------------------------------------------------

    def _sector(self, n: int, count: int = 1) -> bytes:
        self.f.seek(n * SECTOR)
        data = self.f.read(SECTOR * count)
        if len(data) < SECTOR * count:
            raise UDFError(f"short read at sector {n}")
        return data

    @staticmethod
    def _tag_id(buf: bytes) -> int:
        return struct.unpack_from("<H", buf, 0)[0]

    def _block(self, lbn: int) -> bytes:
        """Read one logical block of the (single) partition."""
        return self._sector(self.partition_start + lbn)

    # --- volume structure ------------------------------------------------

    def _read_volume(self) -> None:
        anchor = self._sector(ANCHOR_SECTOR)
        if self._tag_id(anchor) != TAG_ANCHOR:
            raise UDFError("no UDF anchor volume descriptor at sector 256")
        vds_len, vds_loc = struct.unpack_from("<II", anchor, 16)

        fsd_lbn = None
        for i in range(max(1, vds_len // SECTOR)):
            desc = self._sector(vds_loc + i)
            tag = self._tag_id(desc)
            if tag == TAG_PARTITION:
                self.partition_start = struct.unpack_from("<I", desc, 188)[0]
            elif tag == TAG_LOGICAL_VOLUME:
                self.block_size = struct.unpack_from("<I", desc, 212)[0]
                # LogicalVolumeContentsUse holds a long_ad pointing at the file set descriptor
                fsd_lbn = struct.unpack_from("<I", desc, 248 + 4)[0]
            elif tag == TAG_TERMINATING:
                break
        if fsd_lbn is None or self.block_size != SECTOR:
            raise UDFError("unsupported UDF volume layout")

        fsd = self._block(fsd_lbn)
        if self._tag_id(fsd) != TAG_FILE_SET:
            raise UDFError("file set descriptor not found")
        root_lbn = struct.unpack_from("<I", fsd, 400 + 4)[0]
        self.root = self._file_entry(root_lbn, "")

    def _file_entry(self, lbn: int, name: str) -> Entry:
        fe = self._block(lbn)
        tag = self._tag_id(fe)
        if tag == TAG_FILE_ENTRY:
            size = struct.unpack_from("<Q", fe, 56)[0]
            l_ea, l_ad = struct.unpack_from("<II", fe, 168)
            ad_start = 176 + l_ea
        elif tag == TAG_EXT_FILE_ENTRY:
            size = struct.unpack_from("<Q", fe, 56)[0]
            l_ea, l_ad = struct.unpack_from("<II", fe, 208)
            ad_start = 216 + l_ea
        else:
            raise UDFError(f"expected a file entry at block {lbn}, found tag {tag}")

        file_type = fe[16 + 11]  # ICB tag: file type (4 = directory)
        ad_type = struct.unpack_from("<H", fe, 16 + 18)[0] & 0x7
        ads = fe[ad_start:ad_start + l_ad]
        extents: list[Extent] = []
        embedded = None
        if ad_type == 3:  # data stored inside the file entry
            embedded = ads[:size]
        elif ad_type in (0, 1):
            step = 8 if ad_type == 0 else 16
            for off in range(0, len(ads) - step + 1, step):
                length, pos = struct.unpack_from("<II", ads, off)
                kind, length = length >> 30, length & 0x3FFFFFFF
                if length == 0:
                    break
                if kind == 3:
                    raise UDFError("chained allocation descriptors are not supported")
                if kind == 0:  # recorded and allocated
                    extents.append(Extent((self.partition_start + pos) * SECTOR, length))
        else:
            raise UDFError(f"unsupported allocation descriptor type {ad_type}")
        return Entry(name, file_type == 4, size, extents, embedded)

    # --- files -----------------------------------------------------------

    def read(self, entry: Entry, offset: int, length: int) -> bytes:
        if entry.embedded is not None:
            return entry.embedded[offset:offset + length]
        out = bytearray()
        pos = 0
        for ext in entry.extents:
            if offset < pos + ext.length and len(out) < length:
                start = max(offset - pos, 0)
                take = min(ext.length - start, length - len(out))
                self.f.seek(ext.offset + start)
                out += self.f.read(take)
            pos += ext.length
        return bytes(out)

    def listdir(self, directory: Entry) -> list[Entry]:
        data = self.read(directory, 0, directory.size)
        entries = []
        off = 0
        while off + 38 <= len(data):
            if struct.unpack_from("<H", data, off)[0] != TAG_FILE_ID:
                break
            chars = data[off + 18]
            l_fi = data[off + 19]
            icb_lbn = struct.unpack_from("<I", data, off + 20 + 4)[0]
            l_iu = struct.unpack_from("<H", data, off + 36)[0]
            raw = data[off + 38 + l_iu: off + 38 + l_iu + l_fi]
            off += (38 + l_iu + l_fi + 3) & ~3
            if chars & (FID_PARENT | FID_DELETED) or not raw:
                continue
            name = raw[1:].decode("latin-1") if raw[0] == 8 else raw[1:].decode("utf-16-be")
            entries.append(self._file_entry(icb_lbn, name))
        return entries

    def lookup(self, path: str) -> Entry:
        """Find a file by path, case-insensitively (like Windows does)."""
        node = self.root
        for part in path.strip("/").split("/"):
            matches = [e for e in self.listdir(node) if e.name.lower() == part.lower()]
            if not matches:
                raise FileNotFoundError(path)
            node = matches[0]
        return node
