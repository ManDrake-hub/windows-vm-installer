# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the Windows installer in a headless QEMU/KVM virtual machine.

The VM sees only the target disk, attached with the same kind of controller it
has on the real machine (NVMe or SATA/AHCI), so the boot-critical storage driver
configured by Setup is the right one. The Windows ISO and the answer file are
CD-ROMs. QEMU runs with -no-reboot: the installer's first reboot ends the
windowsPE pass and makes QEMU exit.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

FIRMWARE_DESCRIPTORS = ("/usr/share/qemu/firmware", "/etc/qemu/firmware")
# Fallbacks for distros that ship OVMF without QEMU firmware descriptors
KNOWN_FIRMWARE = (
    ("/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd", "/usr/share/edk2/x64/OVMF_VARS.4m.fd"),  # Arch
    ("/usr/share/OVMF/OVMF_CODE_4M.secboot.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),  # Debian, Ubuntu
    ("/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd", "/usr/share/edk2/ovmf/OVMF_VARS.fd"),  # Fedora
    ("/usr/share/qemu/ovmf-x86_64-smm-code.bin", "/usr/share/qemu/ovmf-x86_64-smm-vars.bin"),  # openSUSE
)
BOOT_READ_THRESHOLD = 32 * 2**20  # bytes read from the ISO once the CD boot prompt is passed


class VMError(Exception):
    pass


@dataclass
class Firmware:
    code: str
    vars_template: str
    fmt: str = "raw"


def find_firmware() -> Firmware | None:
    """UEFI x64 firmware with Secure Boot and SMM, as Windows 11 requires."""
    for directory in FIRMWARE_DESCRIPTORS:
        for path in sorted(glob.glob(f"{directory}/*.json")):
            try:
                with open(path) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            mapping = d.get("mapping", {})
            features = set(d.get("features", []))
            machines = [m for t in d.get("targets", []) if t.get("architecture") == "x86_64"
                        for m in t.get("machines", [])]
            code = mapping.get("executable", {}).get("filename")
            nvram = mapping.get("nvram-template", {}).get("filename")
            if ("uefi" in d.get("interface-types", []) and {"secure-boot", "requires-smm"} <= features
                    and "ia32" not in d.get("description", "").lower()
                    and mapping.get("device") == "flash" and mapping.get("mode", "split") == "split"
                    and any(m.startswith("pc-q35") for m in machines)
                    and code and nvram and os.path.exists(code) and os.path.exists(nvram)):
                return Firmware(code, nvram, mapping["executable"].get("format", "raw"))
    for code, nvram in KNOWN_FIRMWARE:
        if os.path.exists(code) and os.path.exists(nvram):
            return Firmware(code, nvram)
    return None


class QMP:
    """Small QEMU Machine Protocol client."""

    def __init__(self, path: Path, timeout: float = 10):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.f = self.sock.makefile("rwb")
        self._read()  # greeting
        self.cmd("qmp_capabilities")

    def _read(self) -> dict:
        while True:
            line = self.f.readline()
            if not line:
                raise VMError("QEMU closed the QMP connection")
            msg = json.loads(line)
            if "event" not in msg:
                return msg

    def cmd(self, name: str, **args):
        payload = {"execute": name}
        if args:
            payload["arguments"] = args
        self.f.write(json.dumps(payload).encode() + b"\n")
        self.f.flush()
        reply = self._read()
        if "error" in reply:
            raise VMError(f"QMP {name}: {reply['error'].get('desc')}")
        return reply.get("return")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


@dataclass
class Progress:
    elapsed: float
    written: int  # bytes written to the target disk
    phase: str  # "firmware", "loading", "installing"


class InstallerVM:
    def __init__(self, workdir: Path, iso: Path, answer_iso: Path, target: Path, controller: str,
                 firmware: Firmware, memory_mb: int = 4096, cpus: int = 4, show: bool = False):
        if controller not in ("nvme", "sata"):
            raise ValueError(f"unsupported controller {controller!r}")
        self.workdir, self.iso, self.answer_iso, self.target = workdir, iso, answer_iso, target
        self.controller, self.firmware = controller, firmware
        self.memory_mb, self.cpus, self.show = memory_mb, cpus, show
        self.qemu: subprocess.Popen | None = None
        self.swtpm: subprocess.Popen | None = None
        self.qmp: QMP | None = None
        self.log_path = workdir / "qemu.log"

    # --- lifecycle ---------------------------------------------------------

    def command(self) -> list[str]:
        w = self.workdir
        disk_dev = ("nvme,drive=target,serial=WVI-TARGET" if self.controller == "nvme"
                    else "ide-hd,drive=target,bus=ide.0")
        return [
            "qemu-system-x86_64", "-name", "windows-vm-installer",
            "-enable-kvm", "-machine", "q35,smm=on", "-cpu", "host",
            "-smp", str(self.cpus), "-m", str(self.memory_mb),
            "-global", "driver=cfi.pflash01,property=secure,value=on",
            "-drive", f"if=pflash,format={self.firmware.fmt},unit=0,readonly=on,file={self.firmware.code}",
            "-drive", f"if=pflash,format={self.firmware.fmt},unit=1,file={w / 'OVMF_VARS.fd'}",
            "-chardev", f"socket,id=chrtpm,path={w / 'swtpm.sock'}",
            "-tpmdev", "emulator,id=tpm0,chardev=chrtpm", "-device", "tpm-tis,tpmdev=tpm0",
            "-drive", f"file={self.target},format=raw,if=none,id=target,cache=none,discard=unmap",
            "-device", f"{disk_dev},bootindex=1",
            "-drive", f"file={self.iso},format=raw,media=cdrom,readonly=on,if=none,id=iso",
            "-device", "ide-cd,drive=iso,bus=ide.2,bootindex=0",
            "-drive", f"file={self.answer_iso},format=raw,media=cdrom,readonly=on,if=none,id=answer",
            "-device", "ide-cd,drive=answer,bus=ide.3",
            "-device", "qemu-xhci", "-device", "usb-tablet",
            "-nic", "none", "-vga", "std",
            "-display", "gtk" if self.show else "none",
            "-qmp", f"unix:{w / 'qmp.sock'},server,nowait",
            "-no-reboot",
        ]

    def start(self) -> None:
        w = self.workdir
        shutil.copyfile(self.firmware.vars_template, w / "OVMF_VARS.fd")
        (w / "tpm").mkdir(exist_ok=True)
        log = self.log_path.open("ab")
        self.swtpm = subprocess.Popen(
            ["swtpm", "socket", "--tpm2", "--tpmstate", f"dir={w / 'tpm'}",
             "--ctrl", f"type=unixio,path={w / 'swtpm.sock'}", "--terminate"],
            stdout=log, stderr=log)
        self._wait_for(w / "swtpm.sock", self.swtpm, "swtpm")
        self.qemu = subprocess.Popen(self.command(), stdout=log, stderr=log)
        self._wait_for(w / "qmp.sock", self.qemu, "QEMU")
        self.qmp = QMP(w / "qmp.sock")

    def _wait_for(self, path: Path, proc: subprocess.Popen, what: str, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists():
            if proc.poll() is not None:
                raise VMError(f"{what} exited with code {proc.returncode}, see {self.log_path}")
            if time.monotonic() > deadline:
                raise VMError(f"{what} did not start, see {self.log_path}")
            time.sleep(0.1)

    def stop(self) -> None:
        """Shut everything down; safe to call more than once."""
        if self.qemu and self.qemu.poll() is None:
            try:
                if self.qmp:
                    self.qmp.cmd("quit")
            except (VMError, OSError):
                pass
            try:
                self.qemu.wait(10)
            except subprocess.TimeoutExpired:
                self.qemu.kill()
        if self.qmp:
            self.qmp.close()
            self.qmp = None
        if self.swtpm and self.swtpm.poll() is None:
            self.swtpm.terminate()
            try:
                self.swtpm.wait(5)
            except subprocess.TimeoutExpired:
                self.swtpm.kill()

    # --- monitoring --------------------------------------------------------

    def _stats(self) -> dict[str, dict]:
        assert self.qmp
        return {d.get("device", ""): d["stats"] for d in self.qmp.cmd("query-blockstats")}

    def screenshot(self, path: Path) -> Path | None:
        try:
            if self.qmp:
                self.qmp.cmd("screendump", filename=str(path), format="png")
                return path
        except (VMError, OSError):
            pass
        return None

    def run(self, on_progress: Callable[[Progress], None], boot_timeout: float = 240,
            stall_timeout: float = 900, total_timeout: float = 5400) -> None:
        """Drive the VM until the installer's first reboot. Raises VMError on problems."""
        assert self.qemu and self.qmp
        start = time.monotonic()
        last_written, last_change = 0, start
        last_shot = start
        phase = "firmware"
        try:
            for now, stats in self._poll(start):
                if now - last_shot >= 60:  # keep a recent view of the hidden screen
                    self.screenshot(self.workdir / "screen.png")
                    last_shot = now
                iso_read = stats.get("iso", {}).get("rd_bytes", 0)
                written = stats.get("target", {}).get("wr_bytes", 0)
                if phase == "firmware":
                    if iso_read >= BOOT_READ_THRESHOLD:
                        phase = "loading"
                    else:
                        # Answer "Press any key to boot from CD or DVD" until the ISO is really read
                        self.qmp.cmd("send-key", keys=[{"type": "qcode", "data": "spc"}])
                        if now - start > boot_timeout:
                            raise VMError("the VM did not boot from the Windows ISO")
                if phase == "loading" and written > 0:
                    phase = "installing"
                if written != last_written:
                    last_written, last_change = written, now
                elif phase == "installing" and now - last_change > stall_timeout:
                    raise VMError("the installer stopped writing to the disk: it may be waiting for input")
                if phase == "loading" and now - start > boot_timeout + stall_timeout:
                    raise VMError("the installer never started writing to the disk")
                if now - start > total_timeout:
                    raise VMError("the installation took too long")
                on_progress(Progress(now - start, written, phase))
        finally:
            if self.qemu.poll() is None:
                self.screenshot(self.workdir / "last-screen.png")

        if self.qemu.returncode != 0:
            raise VMError(f"QEMU exited with code {self.qemu.returncode}, see {self.log_path}")

    def _poll(self, start: float) -> Iterator[tuple[float, dict]]:
        assert self.qemu
        while self.qemu.poll() is None:
            try:
                stats = self._stats()
            except (VMError, OSError):
                # At the first reboot QEMU closes QMP slightly before the process ends
                try:
                    self.qemu.wait(10)
                except subprocess.TimeoutExpired:
                    raise VMError("lost the connection to QEMU") from None
                break
            yield time.monotonic(), stats
            time.sleep(0.5)
