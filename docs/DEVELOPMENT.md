# Development notes

Technical findings behind the design choices. Checked with Windows 11 26H2,
QEMU 11.1, edk2-ovmf 202608 and swtpm 0.10.

## Unattended first phase

- An answer file limited to the **windowsPE pass** drives the whole first phase
  with no input: language, disk layout, edition, license.
- **No product key is needed**: `ProductKey/Key` empty plus `InstallFrom` with
  `/IMAGE/NAME` selects the edition. No generic keys are shipped.
- The answer file is found on a **second CD-ROM** with a Joliet name
  (`autounattend.xml`). Plain ISO 9660 level 1 would expose `AUTOUNAT.XML`.
  Being a CD, it is not a disk, so `DiskID 0` is always the target.
- The answer file is cached in `C:\Windows\Panther`, so `specialize`
  commands (UTC clock, Fast Startup off) run later on the real hardware.

## Booting the installer headless

- The ISO's `cdboot.efi` shows *Press any key to boot from CD or DVD* for about
  5 seconds, and when it appears varies (with two CD drives it can take more
  than 10 seconds). Pressing keys for a fixed time is not reliable: `qemu.py`
  sends a key every 0.5 s **until the ISO has been read for 32 MiB**, i.e. until
  the boot really started.
- Phases are detected from block statistics (`query-blockstats`): ISO reads
  mean the installer is loading, target writes mean it is installing. No screen
  scraping is needed; screenshots are only kept for diagnostics.
- `-no-reboot` turns the installer's first reboot into a QEMU exit (code 0).
- Paths in QEMU option lists need commas doubled (`file=/a,,b.iso`): a comma
  in an ISO name would otherwise be parsed as the next option.

## Progress

- The first phase writes about **57% of the image's `TOTALBYTES`** (about 14 of
  25 GiB for Pro): files shared between editions and hard links are counted
  more than once in the WIM metadata. `cli.WRITE_RATIO` uses 0.58.
- With the target on an NVMe SSD the first phase takes a few minutes.

## Target disk safety

- `lsblk` shows a mount point on one device only: the other members of a
  multi-device btrfs filesystem look unused. Members of mounted btrfs
  filesystems are listed in `/sys/fs/btrfs/*/devices/`.
- The authoritative check is the kernel's: opening the whole disk with
  `O_EXCL` fails with `EBUSY` when the disk or any partition is claimed (mounted
  filesystem, swap, device mapper, md, btrfs, ZFS, bcache). It needs root, so the
  GUI list relies on the sysfs checks and the installer repeats everything as
  root right before starting the VM.
- **4K-native disks**: the virtual disk must have the real logical sector size
  (`logical_block_size=4096` on the QEMU device), or Setup writes a GPT laid out
  for 512-byte sectors that the real disk cannot be read with.
- Kernel names (`nvme0n1`…) change between boots: disks are referred to by
  `/dev/disk/by-id`.

## Verifying the result

- After the VM exits, read the GPT and the ESP **from the disk itself**
  (`gpt.py`, `fat.py`). The kernel's partition view can be stale: a new
  partition that starts at the same sector as an old one may keep the old size.
- The installer's BCD references the Windows partition by GPT disk and
  partition GUIDs (`11000001`/`21000001` device elements), so it does not depend
  on the controller or on the VM.

## Boot managers

- **Which one is in use**: the `BootCurrent` entry of `efibootmgr -v` gives the
  ESP and the loader path. Names usually tell (`LIMINE_X64.EFI`, `grubx64.efi`,
  `shimx64.efi`, `systemd-bootx64.efi`, `refind_x64.efi`, `\EFI\Linux\*.efi`); for
  the fallback `\EFI\BOOT\BOOTX64.EFI` the binary is searched for
  `LoaderInfo: systemd-boot`, `LoaderInfo: systemd-stub`, the Limine hash
  marker, `rEFInd` or `GRUB`. efibootmgr before version 18 prints the file as
  `File(...)`; both forms are parsed.
- **Limine config enrollment**: the Limine EFI binary contains
  `++CONFIG_B2SUM_SIGNATURE++` followed by 128 characters: all `0` when no
  config is enrolled, a BLAKE2b hash otherwise. With a hash, any edit of
  `limine.conf` makes Limine refuse to boot, and re-enrolling would break a
  Secure Boot signature: the tool leaves the file alone and explains.
- **`limine-entry-tool`** (CachyOS) generates Limine entries for the other UEFI
  boot entries (`path: uuid(<ESP GUID>):/EFI/Microsoft/Boot/bootmgfw.efi`), so
  running `limine-update` after creating the UEFI entry is enough. Its scripts
  call `sudo` even for `--help`: never run them from a non-interactive context.
- **GRUB**: os-prober is disabled by default on several distros, so the tool
  installs its own `/etc/grub.d/41_windows-vm-installer-<FAT serial>` script that
  finds the Windows ESP by FAT serial (`search --fs-uuid`) and chainloads
  `bootmgfw.efi`.
- **rEFInd** scans every ESP for `bootmgfw.efi` unless `scanfor` lacks
  `internal`; only then a `menuentry` with `volume <partition GUID>` is added.
- **systemd-boot** only starts loaders on its own ESP. Copying Windows' boot
  files into the Linux ESP would work but would go stale after Windows updates;
  the tool does not do it.

## Graphical installer

- GTK 4 + libadwaita through PyGObject, using only widgets available since
  libadwaita 1.5 (Ubuntu 24.04); Tkinter would need XWayland and looks dated.
- The GUI never runs as root (GTK programs started as root work badly on
  Wayland). It reads the ISO, the disks and the boot manager as the user, then
  runs `pkexec env PYTHONPATH=… python3 -m wvi install --json …` and follows
  the JSON events on stdout; stderr becomes the "Details" log. The work
  directory is made world-readable so the GUI can show the last VM screen.
- pkexec exit codes: **126** = the user dismissed the password dialog, **127** =
  not authorised or **no polkit authentication agent** (stderr mentions
  "agent"). Minimal Wayland compositors (niri, sway, Hyprland) do not start an
  agent: the GUI then shows the equivalent `sudo` command, ready to copy.
- Screenshots are taken off screen (`tools/gui_screenshots.py`) with the GTK
  Broadway backend and GSK's Cairo renderer. Pitfalls:
  - a toplevel window cannot paint itself into a texture: paint its content
    and draw the window background first (otherwise text in the dark style is
    white on a transparent background);
  - without a browser attached, Broadway rarely runs the frame clock, and both
    the restyling after a state change and the layout of a new page wait for
    it. Captures then come out empty, or show a stale state (a radio just
    selected still unselected). The tool disables `gtk-enable-animations`, and
    before each capture emits the frame clock's `update` and `layout` signals
    (the window restyles and lays out on `layout`) and allocates the content;
  - never trust a capture blindly: the tool checks that the visible page is the
    expected one and that the picture is not a single colour, retries, and
    fails loudly otherwise. The rendering is byte-for-byte deterministic, so
    two runs must produce identical files;
  - `emblem-ok-symbolic` is not in the Adwaita icon theme any more:
    `object-select-symbolic` is.
- Reading the installer's output with `Gio.DataInputStream`: in PyGObject
  `read_line_finish()` returns `b""` both for an empty line and at the end of the
  stream, so the end cannot be detected with it; `read_line_finish_utf8()`
  returns `None` at the end. The process can exit before its last lines are
  delivered: report the exit only after both pipes reached their end.
- Closing the window closes the pipes; the installer then stops the VM at its
  next event. The window asks for confirmation while an installation runs.
- efibootmgr prints firmware-made entries with optional data glued to the
  loader path (`\EFI\BOOT\BOOTX64.EFI0000424f`): cut the path at `.efi`.

## Testing the second phase in a VM

1. `python3 -m wvi install --test-image IMG --iso ISO --edition "Windows 11 Pro" --accept-eula`
2. Boot `IMG` in a VM **without a TPM device** (see below): Windows completes
   its setup and stops at the OOBE.
3. At the OOBE, Shift+F10 opens a console: `reg query` shows
   `RealTimeIsUniversal = 1` and `HiberbootEnabled = 0` under
   `HKLM\SYSTEM\CurrentControlSet\Control\...`, and the chosen `EditionID`.

Typing into the guest through QMP needs care: the guest keyboard follows the
ISO language, and keys sent faster than about one every 150 ms get dropped or
merged. The OOBE window keeps the focus after Shift+F10 until the console is
clicked.

## Test harness pitfalls

- **A second VM with a fresh swtpm cannot boot the installed system**: Windows
  Boot Manager stalls for about two minutes, then fails with `0xc000000d`.
  Without a TPM the same image boots and continues the setup. This concerns
  test VMs only: on real hardware Windows boots with the machine's own TPM.
- `cache=none` needs `O_DIRECT`, which tmpfs lacks: keep test images on a real
  filesystem.
- Do not start two QMP clients at once: QEMU serves one connection at a time.
