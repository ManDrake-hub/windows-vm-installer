#!/bin/sh
# Build the Flatpak from this checkout.
#
# Usage: packaging/flatpak/build_flatpak.sh
#
# Needs: flatpak-builder, and the runtimes installed:
#   flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
#   flatpak install flathub org.freedesktop.Platform//24.08
#   flatpak install --sdk flathub org.freedesktop.Sdk//24.08
#
# The result is build/io.github.mandrake_hub.WindowsVmInstaller-1.flatpak

set -eu

here=$(cd "$(dirname "$0")" && pwd)
manifest=$(basename "$here/io.github.mandrake_hub.WindowsVmInstaller.json")

cd "$here"
rm -rf build
flatpak-builder --force-clean --user --install build "$manifest"

echo "done: $(ls build/*.flatpak 2>/dev/null || echo 'see build/')"
