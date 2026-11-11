#!/bin/sh
# Build an AppImage for windows-vm-installer (experimental starting point).
#
# Usage: packaging/appimage/build_appimage.sh
#
# The AppImage bundles the Python runtime and the wvi package. It does NOT
# bundle the system tools the installer needs at run time; the host must
# provide:
#   - qemu-system-x86_64   (QEMU, with KVM support: /dev/kvm)
#   - OVMF/edk2 firmware    (Secure Boot + SMM)
#   - swtpm
#   - efibootmgr
#   - util-linux (lsblk, partx, udevadm)
#   - a polkit authentication agent (for the GUI)
#
# Needs: appimagecraft  (pip install appimagecraft)
#
# Note: appimagecraft flags change between versions; if the invocation below
# does not match your version, run `appimagecraft --help` and adjust. The key
# points are: run it from src/ so the wvi package is importable, and point the
# entrypoint at wvi.cli:main.

set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)

if ! command -v appimagecraft >/dev/null 2>&1; then
  echo "appimagecraft is not installed. Install it with: pip install appimagecraft" >&2
  exit 1
fi

cd "$root/src"

appimagecraft \
  --appname windows-vm-installer \
  --appversion "$(sed -n "s/^__version__ = ['\"]\(.*\)['\"]/\1/p" wvi/__init__.py)" \
  --appdescription "Install Windows onto a physical disk from Linux, through a hidden virtual machine" \
  --author "Geraldino Mandragora" \
  --copyright "GPL-3.0-or-later" \
  --license GPL-3.0-or-later \
  --entrypoint "wvi.cli:main" \
  --python "python3" \
  --output-dir "$here"

echo "done: $(ls "$here"/*.AppImage 2>/dev/null || echo 'see the output above')"
