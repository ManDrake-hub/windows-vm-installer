# Distribution packages

Packaging for the major Linux distributions, plus AppImage and Flatpak.
The project is in early development (`0.1.0`), so these are starting
points, but every package is built and tested by CI
(`.github/workflows/packages.yml`: Arch, Debian and Fedora in containers,
AppImage in an Arch container, Flatpak on the Ubuntu runner).

```
packaging/
├── arch/       PKGBUILD (Arch / CachyOS)
├── debian/     debian/ source package + build_deb.sh (Debian / Ubuntu)
├── fedora/     windows-vm-installer.spec (Fedora)
├── appimage/   build_appimage.sh (AppImage, bundles Python + GTK)
└── flatpak/    manifest + build_flatpak.sh (Flatpak, GUI front end)
```

The package installs the `wvi` Python module and the two entry points
(`windows-vm-installer`, `windows-vm-installer-gui`), the `.desktop` file, an
AppStream metainfo and an icon for the graphical installer.

## Runtime dependencies

Every distro package declares the same core dependencies (names differ per
distro):

| What            | Arch              | Debian/Ubuntu     | Fedora            |
|-----------------|-------------------|-------------------|-------------------|
| Python 3        | `python`          | `python3`         | `python3`         |
| QEMU (x86_64)   | `qemu-system-x86` | `qemu-system-x86` | `qemu-system-x86` |
| OVMF firmware   | `edk2-ovmf`       | `ovmf`            | `edk2-ovmf`       |
| Software TPM    | `swtpm`           | `swtpm`           | `swtpm`           |
| UEFI boot mgmt  | `efibootmgr`      | `efibootmgr`      | `efibootmgr`      |
| Block tools     | `util-linux`      | `util-linux`      | `util-linux`      |

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
see the comment at the top for switching to a tagged tarball. `check()` runs
the unit tests and validates the `.desktop` and metainfo files.

## Debian / Ubuntu

```sh
packaging/debian/build_deb.sh [output-dir]
sudo dpkg -i packaging/debian/debs/windows-vm-installer_*.deb
```

`build_deb.sh` copies the **tracked** source (`git archive HEAD`, so untracked
files like personal backups never end up in the package) into a temp directory
named as a native source package expects, drops in `packaging/debian`, and runs
`dpkg-buildpackage -b`. Build dependencies: `dpkg-dev build-essential debhelper
dh-python python3-all python3-build python3-setuptools python3-wheel
pybuild-plugin-pyproject`.

## Fedora

```sh
cd packaging/fedora
spectool -g -R windows-vm-installer.spec    # downloads Source0 into ~/rpmbuild/SOURCES
rpmbuild -bb windows-vm-installer.spec
sudo rpm -i ~/rpmbuild/RPMS/noarch/windows-vm-installer-*.rpm
```

`spectool` comes from `rpmdevtools`; alternatively download the `Source0` URL
into `~/rpmbuild/SOURCES/` by hand. The spec builds a `noarch` package from the
repository snapshot (no release tags yet); `%pyproject_wheel` builds without
isolation, so `python3-setuptools` and `python3-wheel` are build requirements.
`%check` runs the unit tests and validates the `.desktop` and metainfo files.

## AppImage

```sh
packaging/appimage/build_appimage.sh
```

Bundles a standalone CPython, the `wvi` package, PyGObject and the GTK 4 /
libadwaita stack, so the graphical installer runs on any x86-64 distro without
Python or GTK installed. It does **not** bundle the system tools the installer
needs at run time; the host must provide `qemu-system-x86_64`, OVMF firmware,
`swtpm`, `efibootmgr` and util-linux, plus a polkit authentication agent for
the GUI. Build needs: curl, `gobject-introspection`, cairo and pycairo headers
(`python-cairo` on Arch, to build PyGObject for the bundled Python),
`glib-compile-schemas` and `gdk-pixbuf-query-loaders`. The result is ~100 MB:
that is the price of bundling GTK.

## Flatpak

```sh
packaging/flatpak/build_flatpak.sh
```

The Flatpak is the **graphical front end only**. A sandbox can never get root,
and the installation writes raw disks and UEFI boot entries, so the elevated
command goes through `flatpak-spawn --host pkexec windows-vm-installer …` to
the CLI installed on the host: **the distribution package must be installed**
for the Flatpak to do anything beyond browsing ISOs and disks. The GUI detects
the sandbox (`FLATPAK_ID`) and switches to that path automatically, with a
clear message when the host CLI is missing.

The manifest builds on `org.gnome.Platform` 50 (GTK 4, libadwaita and PyGObject
are in the runtime) and bundles `lsblk` and `efibootmgr` for the unprivileged
parts (disk list, boot-manager detection). `finish-args` grant all devices
(`--device=all`: raw disks and `/dev/kvm`), `/sys/firmware/efi` read-only and
the Flatpak spawn portal; polkit is not talked to from inside the sandbox.

## Releasing

When a version is tagged, update `pkgver`/`Version` and the sources in each
packaging file to point at the tag tarball (the comments in `arch/PKGBUILD` and
`fedora/windows-vm-installer.spec` show where), bump the Debian revision in
`debian/changelog`, and add a `<release>` to
`data/io.github.mandrake_hub.WindowsVmInstaller.metainfo.xml`.