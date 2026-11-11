# Distribution packages

Packaging for the major Linux distributions, plus AppImage and Flatpak.
The project is in early development (`0.1.0.dev0`), so these are starting
points: the distro packages are the primary, most complete deliverables, while
AppImage and Flatpak are experimental and need more work (see the notes below).

```
packaging/
├── arch/       PKGBUILD (Arch / CachyOS)
├── debian/     debian/ source package + build_deb.sh (Debian / Ubuntu)
├── fedora/     windows-vm-installer.spec (Fedora)
├── appimage/   build_appimage.sh (AppImage, experimental)
└── flatpak/    manifest + build_flatpak.sh (Flatpak, experimental)
```

The package installs the `wvi` Python module and the two entry points
(`windows-vm-installer`, `windows-vm-installer-gui`), plus the `.desktop` file
for the graphical installer.

## Runtime dependencies

Every package declares the same core dependencies (names differ per distro):

| What            | Arch            | Debian/Ubuntu   | Fedora          |
|-----------------|-----------------|-----------------|-----------------|
| Python 3        | `python`        | `python3`       | `python3`       |
| QEMU (x86_64)   | `qemu`          | `qemu-system-x86` | `qemu-system-x86` |
| OVMF firmware   | `edk2-ovmf`     | `ovmf`          | `edk2-ovmf`     |
| Software TPM    | `swtpm`         | `swtpm`         | `swtpm`         |
| UEFI boot mgmt  | `efibootmgr`    | `efibootmgr`    | `efibootmgr`    |
| Block tools     | `util-linux`    | `util-linux`    | `util-linux`    |

The graphical installer additionally recommends `gtk4`, `libadwaita` and the
PyGObject bindings (`python-gobject` / `python3-gi` + `gir1.2-gtk-4.0` +
`gir1.2-libadwaita-1.0`). Boot-manager integration optionally uses
`limine-entry-tool` (CachyOS) or `grub`.

## Arch / CachyOS

```sh
cd packaging/arch
makepkg -s            # builds windows-vm-installer-<ver>-1-x86_64.pkg.tar.zst
sudo pacman -U windows-vm-installer-*.pkg.tar.zst
```

The PKGBUILD builds from the git repository (there are no release tags yet);
see the comment at the top for switching to a tagged tarball.

## Debian / Ubuntu

```sh
packaging/debian/build_deb.sh [output-dir]
sudo dpkg -i packaging/debian/debs/windows-vm-installer_*.deb
```

`build_deb.sh` copies the source (minus `.git` and build artifacts) into a
temp directory named as a native source package expects, drops in
`packaging/debian`, and runs `dpkg-buildpackage -b`. Build dependencies:
`dpkg-dev build-essential debhelper dh-python python3-all python3-build
python3-setuptools python3-wheel pybuild-plugin-pyproject`.

## Fedora

```sh
cd packaging/fedora
spectool -g -R windows-vm-installer.spec    # downloads Source0 into ~/rpmbuild/SOURCES
rpmbuild -bb windows-vm-installer.spec
sudo rpm -i ~/rpmbuild/RPMS/noarch/windows-vm-installer-*.rpm
```

`spectool` comes from `rpmdevtools`; alternatively download the `Source0` URL
into `~/rpmbuild/SOURCES/` by hand.

The spec builds a `noarch` package from the repository snapshot (no release
tags yet); `%pyproject_wheel` builds without isolation, so `python3-setuptools`
and `python3-wheel` are build requirements.

## AppImage (experimental)

```sh
packaging/appimage/build_appimage.sh
```

Bundles the Python runtime and the `wvi` package via `appimagecraft`. It does
**not** bundle the system tools (QEMU, OVMF, swtpm, efibootmgr) or access to
`/dev/kvm` — the host must provide them, so an AppImage is mostly a
convenience for the GUI, not a self-contained installer. Bundling GTK 4 /
libadwaita makes it ~100 MB heavier. Treat this as a proof of concept.

## Flatpak (experimental)

```sh
packaging/flatpak/build_flatpak.sh
```

The manifest builds the `wvi` app plus `efibootmgr` and `swtpm` from source on
`org.freedesktop.Platform` 24.08. It is a starting point, not a finished
package:

- **QEMU and OVMF are not bundled yet** — they must be added as modules (or an
  extension) before the app can run.
- The `finish-args` grant broad access (`/dev`, `/sys/firmware/efi`, the
  polkit bus name) because the installer manages raw disks and firmware; this
  is inherent to what the app does, but it is a wide sandbox grant to review.
- `pkexec`/polkit inside the sandbox is the trickiest part and needs manual
  verification on a real desktop.

## Releasing

When a version is tagged, update `pkgver`/`Version` and the sources in each
packaging file to point at the tag tarball (the comments in `arch/PKGBUILD` and
`fedora/windows-vm-installer.spec` show where), and bump the Debian revision in
`debian/changelog`.
