# SPDX-License-Identifier: GPL-3.0-or-later
"""Inspect a Windows installation ISO: editions, language, build, architecture.

The image list lives in the XML metadata of sources/install.wim (or install.esd).
The XML resource is stored uncompressed, so reading the WIM header is enough.
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .udf import UDFError, UDFImage

WIM_MAGIC = b"MSWIM\0\0\0"
ARCH_NAMES = {0: "x86", 5: "arm", 9: "x64", 12: "arm64"}
INSTALL_IMAGES = ("sources/install.wim", "sources/install.esd")


class NotWindowsISO(Exception):
    """The file is not a Windows installation ISO."""


@dataclass
class Edition:
    index: int
    name: str  # value for /IMAGE/NAME in the answer file, e.g. "Windows 11 Pro"
    edition_id: str  # e.g. "Professional"
    arch: str
    build: str
    languages: list[str]
    default_language: str
    total_bytes: int  # size of the expanded image, used to estimate progress


@dataclass
class WindowsISO:
    path: Path
    image_path: str
    editions: list[Edition]

    @property
    def default_language(self) -> str:
        return self.editions[0].default_language if self.editions else "en-US"


def _text(node: ET.Element | None, tag: str) -> str:
    found = node.find(tag) if node is not None else None
    return (found.text or "").strip() if found is not None else ""


def parse_wim_xml(xml_text: str) -> list[Edition]:
    root = ET.fromstring(xml_text)
    editions = []
    for image in root.findall("IMAGE"):
        windows = image.find("WINDOWS")
        version = windows.find("VERSION") if windows is not None else None
        langs = windows.find("LANGUAGES") if windows is not None else None
        arch = _text(windows, "ARCH")
        editions.append(Edition(
            index=int(image.get("INDEX", "0")),
            name=_text(image, "NAME"),
            edition_id=_text(windows, "EDITIONID"),
            arch=ARCH_NAMES.get(int(arch), arch) if arch.isdigit() else arch,
            build=".".join(x for x in (_text(version, "BUILD"), _text(version, "SPBUILD")) if x),
            languages=[(lang.text or "").strip() for lang in langs.findall("LANGUAGE")] if langs is not None else [],
            default_language=_text(langs, "DEFAULT"),
            total_bytes=int(_text(image, "TOTALBYTES") or 0),
        ))
    return editions


def inspect_iso(path: str | Path) -> WindowsISO:
    path = Path(path)
    with path.open("rb") as f:
        try:
            udf = UDFImage(f)
        except UDFError as e:
            raise NotWindowsISO(f"{path}: not a UDF image ({e})") from None
        for image_path in INSTALL_IMAGES:
            try:
                entry = udf.lookup(image_path)
            except FileNotFoundError:
                continue
            header = udf.read(entry, 0, 208)
            if header[:8] != WIM_MAGIC:
                raise NotWindowsISO(f"{image_path} is not a WIM file")
            # rhXmlData: 8-byte size (low 56 bits) + flags, 8-byte offset, 8-byte original size
            size_flags, offset, _ = struct.unpack_from("<QQQ", header, 72)
            xml_raw = udf.read(entry, offset, size_flags & 0x00FFFFFFFFFFFFFF)
            xml_text = xml_raw.decode("utf-16-le").lstrip("﻿")
            return WindowsISO(path, image_path, parse_wim_xml(xml_text))
    raise NotWindowsISO(f"{path}: no sources/install.wim or install.esd")
