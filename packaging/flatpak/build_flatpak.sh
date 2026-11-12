#!/bin/sh
# Build the Flatpak from this checkout.
#
# Usage: packaging/flatpak/build_flatpak.sh
#
# Needs: flatpak-builder, and the runtimes installed:
#   flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
#   flatpak install flathub org.gnome.Platform//50 org.gnome.Sdk//50
#
# The Flatpak is the graphical front end only: the installation itself runs
# through the windows-vm-installer CLI installed on the host (distribution
# package), via flatpak-spawn --host pkexec.
#
# The result is build/io.github.mandrake_hub.WindowsVmInstaller.flatpak

set -eu

here=$(cd "$(dirname "$0")" && pwd)
manifest=$(basename "$here/io.github.mandrake_hub.WindowsVmInstaller.json")

cd "$here"
rm -rf build repo
# FLATPAK_BUILDER_OPTS: extra flags, e.g. "--user --disable-rofiles-fuse" in CI
flatpak-builder --force-clean ${FLATPAK_BUILDER_OPTS:-} --repo=repo build "$manifest"
flatpak build-bundle repo build/io.github.mandrake_hub.WindowsVmInstaller.flatpak \
    io.github.mandrake_hub.WindowsVmInstaller

echo "done: build/io.github.mandrake_hub.WindowsVmInstaller.flatpak"
echo "install with: flatpak install --user build/io.github.mandrake_hub.WindowsVmInstaller.flatpak"