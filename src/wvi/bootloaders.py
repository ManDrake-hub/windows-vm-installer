# SPDX-License-Identifier: GPL-3.0-or-later
"""Add the new Windows to the boot manager the PC actually boots with.

The boot manager is identified from the UEFI entry the system was started from
(BootCurrent) and, for the generic fallback path, from the loader's contents.
Every file is backed up before it is changed, and every change is reversible.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MARK_BEGIN = "# BEGIN windows-vm-installer"
MARK_END = "# END windows-vm-installer"
GRUB_SCRIPT_DIR = Path("/etc/grub.d")
GRUB_SCRIPT_PREFIX = "41_windows-vm-installer"
BACKUP_DIR = Path("/var/lib/windows-vm-installer/backups")
WINDOWS_LOADER = "/EFI/Microsoft/Boot/bootmgfw.efi"
LIMINE_HASH_MARKER = b"++CONFIG_B2SUM_SIGNATURE++"
# The loader path ends at ".efi": firmware-made entries append optional data right after it
DEVICE_PATH_RE = re.compile(r"HD\(\d+,GPT,([0-9a-fA-F-]+),[^)]*\)/(?:File\()?(\\[^)\t]*?\.efi)", re.IGNORECASE)


@dataclass
class WindowsTarget:
    title: str  # menu title, e.g. "Windows 11 Pro"
    esp_guid: str  # GPT partition GUID of the Windows EFI system partition
    esp_volume_id: str  # FAT serial as shown by blkid, e.g. 8441-18C4


@dataclass
class BootManager:
    kind: str  # limine, grub, refind, systemd-boot, uki, windows, unknown
    entry: str | None = None  # label of the UEFI entry in use
    esp_guid: str | None = None
    loader: str | None = None  # path inside the ESP, e.g. \EFI\LIMINE\LIMINE_X64.EFI
    esp_mount: Path | None = None

    def file(self, rel: str) -> Path | None:
        """A file on the boot manager's ESP (vfat lookups are case-insensitive)."""
        if self.esp_mount is None:
            return None
        return self.esp_mount / rel.replace("\\", "/").lstrip("/")


@dataclass
class Plan:
    summary: str
    changes: list[str] = field(default_factory=list)
    apply: Callable[[], str] | None = None  # None: nothing to do automatically


# --- detection ------------------------------------------------------------

def _mountpoint_of(partuuid: str) -> Path | None:
    out = subprocess.run(["lsblk", "-J", "-o", "PARTUUID,MOUNTPOINTS"], capture_output=True, text=True)
    if out.returncode != 0:
        return None

    def walk(nodes):
        for n in nodes:
            yield n
            yield from walk(n.get("children") or [])

    for node in walk(json.loads(out.stdout)["blockdevices"]):
        if (node.get("partuuid") or "").lower() == partuuid.lower():
            mounts = [m for m in node.get("mountpoints") or [] if m]
            return Path(mounts[0]) if mounts else None
    return None


def kind_from_loader(loader: str, contents: bytes | None = None) -> str:
    p = loader.lower()
    for needle, kind in (("limine", "limine"), ("systemd", "systemd-boot"), ("refind", "refind"),
                         ("grub", "grub"), ("shim", "grub"), ("\\microsoft\\", "windows")):
        if needle in p:
            return kind
    if p.startswith("\\efi\\linux\\") or "vmlinuz" in p:
        return "uki"
    if contents:  # generic name such as \EFI\BOOT\BOOTX64.EFI: look inside
        for needle, kind in ((b"LoaderInfo: systemd-stub", "uki"), (b"LoaderInfo: systemd-boot", "systemd-boot"),
                             (LIMINE_HASH_MARKER, "limine"), (b"rEFInd", "refind"), (b"GRUB", "grub")):
            if needle in contents:
                return kind
    return "unknown"


def parse_current(efibootmgr_verbose: str) -> tuple[str | None, str | None, str | None]:
    """(label, ESP partition GUID, loader path) of the BootCurrent entry."""
    current = None
    for line in efibootmgr_verbose.splitlines():
        if line.startswith("BootCurrent:"):
            current = line.split(":", 1)[1].strip().upper()
        elif current and line.upper().startswith(f"BOOT{current}"):
            label = re.sub(r"^Boot[0-9A-Fa-f]{4}\*?\s+", "", line).split("\t")[0].strip()
            m = DEVICE_PATH_RE.search(line)
            return label, (m.group(1).lower() if m else None), (m.group(2) if m else None)
    return None, None, None


def detect() -> BootManager:
    try:
        out = subprocess.run(["efibootmgr", "-v"], check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return BootManager("unknown")
    label, guid, loader = parse_current(out)
    if not loader:
        return BootManager("unknown", label)
    bm = BootManager("unknown", label, guid, loader, _mountpoint_of(guid) if guid else None)
    contents = None
    path = bm.file(loader)
    if path is not None:
        try:
            contents = path.read_bytes()
        except OSError:
            pass
    bm.kind = kind_from_loader(loader, contents)
    return bm


# --- helpers ----------------------------------------------------------------

def backup(path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, dest)
    return dest


def _run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {(res.stderr or res.stdout).strip()[-400:]}")
    return res.stdout


def limine_config_enrolled(binary: bytes) -> bool:
    """True when the Limine binary carries a config hash: editing limine.conf would break boot."""
    i = binary.find(LIMINE_HASH_MARKER)
    if i < 0:
        return False
    digest = binary[i + len(LIMINE_HASH_MARKER): i + len(LIMINE_HASH_MARKER) + 128]
    return digest.strip(b"0") != b""


def marked(key: str, body: str) -> str:
    """Wrap an entry in comment markers; the key (the Windows ESP GUID) identifies it."""
    return f"{MARK_BEGIN} {key}\n{body.rstrip(chr(10))}\n{MARK_END} {key}\n"


def limine_block(t: WindowsTarget) -> str:
    return marked(t.esp_guid, f"/{t.title}\n    protocol: efi\n    path: guid({t.esp_guid}):{WINDOWS_LOADER}")


def replace_block(text: str, key: str, block: str | None) -> str:
    """Remove the block previously added for this key (if any) and append the new one (if given).

    Blocks of other Windows installations are left alone. A begin marker
    without its end marker is not trusted: nothing is removed in that case.
    """
    begin, end = f"{MARK_BEGIN} {key}", f"{MARK_END} {key}"
    lines = text.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        if lines[i].rstrip() == begin:
            close = next((j for j in range(i + 1, len(lines)) if lines[j].rstrip() == end), None)
            if close is not None:
                if out and not out[-1].strip():
                    out.pop()  # the blank line that was added before the block
                i = close + 1
                continue
        out.append(lines[i])
        i += 1
    result = "".join(out).rstrip("\n") + "\n"
    if block:
        result += "\n" + block
    return result


def grub_script(t: WindowsTarget) -> str:
    title = t.title.replace("'", "")
    return f"""#!/bin/sh
# Added by windows-vm-installer: starts Windows from its own EFI system partition.
cat <<'EOF'
menuentry '{title}' --class windows --class os {{
	insmod part_gpt
	insmod fat
	insmod chain
	search --no-floppy --fs-uuid --set=root {t.esp_volume_id}
	chainloader {WINDOWS_LOADER}
}}
EOF
"""


def grub_regenerate_cmd() -> list[str] | None:
    if shutil.which("update-grub"):
        return ["update-grub"]
    for tool, cfg in (("grub-mkconfig", "/boot/grub/grub.cfg"), ("grub2-mkconfig", "/boot/grub2/grub.cfg")):
        if shutil.which(tool) and Path(cfg).exists():
            return [tool, "-o", cfg]
    return None


def refind_scans_internal(conf: str) -> bool:
    for line in conf.splitlines():
        words = line.split("#", 1)[0].split()
        if words and words[0] == "scanfor":
            return "internal" in ",".join(words[1:]).replace(" ", "").split(",")
    return True  # default: internal,external,optical,manual


# --- plans ------------------------------------------------------------------

def plan(bm: BootManager, t: WindowsTarget) -> Plan:
    if bm.kind == "limine":
        return _plan_limine(bm, t)
    if bm.kind == "grub":
        return _plan_grub(t)
    if bm.kind == "refind":
        return _plan_refind(bm, t)
    if bm.kind == "systemd-boot":
        return Plan("systemd-boot can only start programs on its own EFI partition, and Windows has its "
                    "own: choose Windows from the firmware boot menu.")
    if bm.kind == "uki":
        return Plan("Linux starts straight from the firmware (no boot menu): choose Windows from the "
                    "firmware boot menu.")
    return Plan(f"Boot manager not recognised ({bm.loader or 'unknown'}): choose Windows from the firmware "
                "boot menu.")


def _plan_limine(bm: BootManager, t: WindowsTarget) -> Plan:
    if shutil.which("limine-update") and Path("/etc/limine-entry-tool.conf").exists():
        def run_update() -> str:
            _run(["limine-update"])
            conf = _find_limine_conf(bm)
            if conf and t.esp_guid.lower() in conf.read_text(errors="replace").lower():
                return f"limine-update added '{t.title}' to the Limine menu"
            return ("limine-update ran, but Windows is not in limine.conf: it lists the systems that have a "
                    "UEFI boot entry, so create one for Windows and run limine-update again")
        return Plan("Limine is managed by limine-entry-tool: regenerate its menu",
                    ["run limine-update (it adds the UEFI entries of other systems to limine.conf)"], run_update)

    conf = _find_limine_conf(bm)
    if conf is None:
        return Plan("Limine configuration not found: add Windows by hand with "
                    f"'path: guid({t.esp_guid}):{WINDOWS_LOADER}'")
    if conf.suffix == ".cfg":
        return Plan(f"{conf} uses the pre-8.0 Limine syntax: update Limine, or add Windows by hand")
    binary = bm.file(bm.loader or "")
    if binary is not None and binary.exists() and limine_config_enrolled(binary.read_bytes()):
        return Plan("The Limine configuration is enrolled (hash inside the EFI binary): editing it would "
                    "stop Limine from booting. Add Windows by hand and re-enroll the configuration.")

    def apply() -> str:
        saved = backup(conf)
        conf.write_text(replace_block(conf.read_text(), t.esp_guid, limine_block(t)))
        return f"added '{t.title}' to {conf} (backup: {saved})"
    return Plan("Limine: add a Windows entry", [f"append an entry to {conf}"], apply)


def _find_limine_conf(bm: BootManager) -> Path | None:
    if bm.esp_mount is None:
        return None
    loader_dir = Path((bm.loader or "").replace("\\", "/")).parent.as_posix().lstrip("/")
    for rel in (f"{loader_dir}/limine.conf", "limine.conf", "limine/limine.conf", "boot/limine.conf",
                "boot/limine/limine.conf", f"{loader_dir}/limine.cfg", "limine.cfg", "boot/limine.cfg"):
        p = bm.esp_mount / rel
        if p.is_file():
            return p
    return None


def _plan_grub(t: WindowsTarget) -> Plan:
    cmd = grub_regenerate_cmd()
    if not GRUB_SCRIPT_DIR.is_dir() or cmd is None:
        return Plan("GRUB found, but not /etc/grub.d or grub-mkconfig: add Windows by hand "
                    f"(chainloader {WINDOWS_LOADER} on the FAT volume {t.esp_volume_id})")
    script = GRUB_SCRIPT_DIR / f"{GRUB_SCRIPT_PREFIX}-{t.esp_volume_id}"

    def apply() -> str:
        script.write_text(grub_script(t))
        script.chmod(0o755)
        _run(cmd)
        return f"added '{t.title}' to the GRUB menu ({script}, regenerated with {' '.join(cmd)})"
    return Plan("GRUB: add a Windows entry", [f"create {script}", f"run {' '.join(cmd)}"], apply)


def _plan_refind(bm: BootManager, t: WindowsTarget) -> Plan:
    conf = bm.file(Path((bm.loader or "").replace("\\", "/")).parent.as_posix() + "/refind.conf")
    if conf is None or not conf.is_file():
        return Plan("rEFInd scans every disk by default: Windows should appear in its menu automatically.")
    if refind_scans_internal(conf.read_text(errors="replace")):
        return Plan("rEFInd scans internal disks: Windows will appear in its menu automatically.")
    title = t.title.replace('"', "")
    block = marked(t.esp_guid, f"menuentry \"{title}\" {{\n    volume {t.esp_guid}\n"
                               f"    loader {WINDOWS_LOADER.replace('/', chr(92))}\n}}")

    def apply() -> str:
        saved = backup(conf)
        conf.write_text(replace_block(conf.read_text(), t.esp_guid, block))
        return f"added '{t.title}' to {conf} (backup: {saved})"
    return Plan("rEFInd does not scan internal disks: add a manual Windows entry",
                [f"append a menuentry to {conf}"], apply)
