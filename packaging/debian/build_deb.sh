#!/bin/sh
# Build a .deb from this checkout, without touching the source tree.
#
# Usage: packaging/debian/build_deb.sh [output-dir]
#
# Needs: dpkg-buildpackage, debhelper, dh-python, python3-all,
#        python3-build, pybuild-plugin-pyproject
#        (Debian/Ubuntu: apt install dpkg-dev build-essential debhelper
#         dh-python python3-all python3-build python3-pybuild-plugin-pyproject)

set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
version=$(sed -n "s/^__version__ = ['\"]\(.*\)['\"]/\1/p" "$root/src/wvi/__init__.py")
out=${1:-"$here/debs"}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
src="$work/windows-vm-installer_$version"

# a clean copy of the tracked source, named as a native source package expects
# (git archive: no untracked files like backup/ or test images end up in it)
# -c safe.directory: in CI the checkout is owned by another uid
mkdir -p "$src"
git -C "$root" -c safe.directory="$root" archive HEAD | tar -C "$src" -xf -
cp -r "$here" "$src/debian"

cd "$src"
dpkg-buildpackage -b -us -uc

mkdir -p "$out"
mv -v "$work"/windows-vm-installer_*.deb "$out"/
echo "done: $(ls "$out")"
