# SPDX-License-Identifier: GPL-3.0-or-later
"""Write a tiny ISO 9660 image with Joliet names, holding a few small files.

Used to hand autounattend.xml to Windows Setup on a virtual CD-ROM: a CD is not
a disk, so the target disk stays the only disk the installer can see. Joliet is
needed because plain ISO 9660 level 1 would turn the name into AUTOUNAT.XML.
"""

from __future__ import annotations

import datetime as dt
import struct
from pathlib import Path

SECTOR = 2048


def _both16(v: int) -> bytes:
    return struct.pack("<H", v) + struct.pack(">H", v)


def _both32(v: int) -> bytes:
    return struct.pack("<I", v) + struct.pack(">I", v)


def _dir_date(t: dt.datetime) -> bytes:
    return bytes([t.year - 1900, t.month, t.day, t.hour, t.minute, t.second, 0])


def _vol_date(t: dt.datetime) -> bytes:
    return t.strftime("%Y%m%d%H%M%S00").encode() + b"\0"


def _dir_record(ident: bytes, extent: int, size: int, is_dir: bool, t: dt.datetime) -> bytes:
    length = 33 + len(ident) + (1 if len(ident) % 2 == 0 else 0)
    rec = bytes([length, 0]) + _both32(extent) + _both32(size) + _dir_date(t)
    rec += bytes([2 if is_dir else 0, 0, 0]) + _both16(1) + bytes([len(ident)]) + ident
    return rec.ljust(length, b"\0")


def _path_table(root_extent: int, big_endian: bool) -> bytes:
    fmt = ">IH" if big_endian else "<IH"
    return bytes([1, 0]) + struct.pack(fmt, root_extent, 1) + b"\0\0"


def _volume_descriptor(kind: int, volume_id: str, total: int, root: bytes,
                       pt_size: int, l_pt: int, m_pt: int, t: dt.datetime) -> bytes:
    joliet = kind == 2
    vd = bytearray(SECTOR)
    vd[0:7] = bytes([kind]) + b"CD001\x01"
    if joliet:  # UCS-2 big endian, padded with UCS-2 spaces
        vol = volume_id.encode("utf-16-be")[:32]
        vol += b"\0 " * ((32 - len(vol)) // 2)
        vd[8:40] = b"\0 " * 16
    else:
        vol = volume_id.upper().encode("ascii")[:32].ljust(32)
        vd[8:40] = b" " * 32
    vd[40:72] = vol
    vd[80:88] = _both32(total)
    if joliet:
        vd[88:91] = b"%/E"  # UCS-2 level 3
    vd[120:124] = _both16(1)
    vd[124:128] = _both16(1)
    vd[128:132] = _both16(SECTOR)
    vd[132:140] = _both32(pt_size)
    vd[140:144] = struct.pack("<I", l_pt)
    vd[148:152] = struct.pack(">I", m_pt)
    vd[156:190] = root
    pad = b"\0 " * 64 if joliet else b" " * 128
    for off in (190, 318, 446, 574):
        vd[off:off + 128] = pad
    stamp = _vol_date(t)
    vd[813:830] = stamp
    vd[830:847] = stamp
    vd[847:864] = b"0" * 16 + b"\0"
    vd[864:881] = stamp
    vd[881] = 1
    return bytes(vd)


def write_iso(dest: Path, files: dict[str, bytes], volume_id: str = "UNATTEND") -> None:
    """Write an ISO with the given files in its root directory."""
    t = dt.datetime.now(dt.timezone.utc)  # the dates carry a GMT offset of 0
    names = sorted(files)
    # Sector layout: 16 system, PVD, SVD, terminator, 4 path tables, 2 root dirs, data
    pvd_l, pvd_m, jol_l, jol_m = 19, 20, 21, 22
    pvd_root, jol_root = 23, 24
    data_start = 25
    extents = {}
    nxt = data_start
    for name in names:
        extents[name] = nxt
        nxt += max(1, -(-len(files[name]) // SECTOR))
    total = nxt

    def root_dir(joliet: bool, own_extent: int) -> bytes:
        recs = _dir_record(b"\0", own_extent, SECTOR, True, t)
        recs += _dir_record(b"\1", own_extent, SECTOR, True, t)
        for name in names:
            ident = (name + ";1").encode("utf-16-be") if joliet else (name.upper() + ";1").encode("ascii")
            recs += _dir_record(ident, extents[name], len(files[name]), False, t)
        if len(recs) > SECTOR:
            raise ValueError("too many files for a single directory sector")
        return recs.ljust(SECTOR, b"\0")

    pt_size = len(_path_table(0, False))
    image = bytearray(SECTOR * 16)
    image += _volume_descriptor(1, volume_id, total, _dir_record(b"\0", pvd_root, SECTOR, True, t),
                                pt_size, pvd_l, pvd_m, t)
    image += _volume_descriptor(2, volume_id, total, _dir_record(b"\0", jol_root, SECTOR, True, t),
                                pt_size, jol_l, jol_m, t)
    image += (b"\xffCD001\x01").ljust(SECTOR, b"\0")
    for root, big in ((pvd_root, False), (pvd_root, True), (jol_root, False), (jol_root, True)):
        image += _path_table(root, big).ljust(SECTOR, b"\0")
    image += root_dir(False, pvd_root)
    image += root_dir(True, jol_root)
    for name in names:
        data = files[name]
        image += data.ljust(max(1, -(-len(data) // SECTOR)) * SECTOR, b"\0")
    assert len(image) == total * SECTOR
    dest.write_bytes(bytes(image))
