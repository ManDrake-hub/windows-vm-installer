#!/bin/sh
# Build a self-contained AppImage: CPython + wvi + PyGObject + GTK 4 / libadwaita.
# The host still must provide qemu-system-x86_64, OVMF, swtpm, efibootmgr and
# util-linux: the installer manages real disks and firmware.
#
# Usage: packaging/appimage/build_appimage.sh
# Build needs: curl, gobject-introspection + cairo + pycairo headers (for
# PyGObject; Arch: python-cairo), glib2 (glib-compile-schemas),
# gdk-pixbuf2 (query-loaders).

set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
A="$work/AppDir"
PYVER=3.12.7
PBS=20241016

mkdir -p "$A/usr"
curl -fL "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS/cpython-$PYVER+$PBS-x86_64-unknown-linux-gnu-install_only.tar.gz" \
  | tar -xz -C "$A/usr"
PY="$A/usr/python/bin/python3"
export PATH="$A/usr/python/bin:$PATH"
"$PY" -m pip install --quiet --upgrade pip setuptools wheel meson-python meson ninja
"$PY" -m pip install --no-build-isolation --no-warn-script-location --prefix="$A/usr" \
  "$root" "PyGObject>=3.42"

# GTK 4 / libadwaita stack: ldd closure of the seed libraries.
# gdk-pixbuf 2.44+ loads images through glycin: include its loaders too.
mkdir -p "$A/usr/lib"
{
  for l in libgtk-4.so.1 libadwaita-1.so.0 libgdk_pixbuf-2.0.so.0 \
           libpangocairo-1.0.so.0 libcairo-gobject.so.2 libgirepository-1.0.so.1; do
    ldd "/usr/lib/$l"
  done
  if [ -d /usr/lib/glycin-loaders ]; then
    for l in /usr/lib/glycin-loaders/2+/*; do ldd "$l"; done
  fi
} | awk '/=> \//{print $3} /^\//{print $1}' | sort -u \
  | grep -Ev 'ld-linux|libc\.so|libm\.so|libpthread|libdl\.so|librt\.so|libresolv' \
  | while read -r f; do cp -Ln "$f" "$A/usr/lib/"; done
cp -a /usr/lib/girepository-1.0 "$A/usr/lib/"
# legacy pixbuf loaders: only exist if some package still installs them
[ ! -d /usr/lib/gdk-pixbuf-2.0 ] || cp -a /usr/lib/gdk-pixbuf-2.0 "$A/usr/lib/"
if [ -d /usr/lib/glycin-loaders ]; then
  cp -a /usr/lib/glycin-loaders "$A/usr/lib/"
  mkdir -p "$A/usr/share"
  cp -a /usr/share/glycin-loaders "$A/usr/share/"
fi
mkdir -p "$A/usr/bin"
cp /usr/bin/gdk-pixbuf-query-loaders "$A/usr/bin/"

mkdir -p "$A/usr/share/glib-2.0" "$A/usr/share/icons" "$A/usr/share/metainfo"
cp -a /usr/share/glib-2.0/schemas "$A/usr/share/glib-2.0/"
glib-compile-schemas "$A/usr/share/glib-2.0/schemas"
cp -a /usr/share/icons/Adwaita /usr/share/icons/hicolor "$A/usr/share/icons/"
cp "$root/data/io.github.mandrake_hub.WindowsVmInstaller.metainfo.xml" \
  "$A/usr/share/metainfo/io.github.mandrake_hub.WindowsVmInstaller.appdata.xml"
cp "$root/data/icons/hicolor/scalable/apps/io.github.mandrake_hub.WindowsVmInstaller.svg" \
  "$A/usr/share/icons/hicolor/scalable/apps/"
mkdir -p "$A/usr/share/icons/hicolor/256x256/apps"
cp "$root/data/icons/hicolor/256x256/apps/io.github.mandrake_hub.WindowsVmInstaller.png" \
  "$A/usr/share/icons/hicolor/256x256/apps/"
cp "$root/data/io.github.mandrake_hub.WindowsVmInstaller.desktop" "$A/"
# also in the standard location: appstreamcli resolves the launchable desktop-id from there
mkdir -p "$A/usr/share/applications"
cp "$root/data/io.github.mandrake_hub.WindowsVmInstaller.desktop" "$A/usr/share/applications/"
# appimagetool builds .DirIcon from a top-level PNG named after Icon=
cp "$root/data/icons/hicolor/256x256/apps/io.github.mandrake_hub.WindowsVmInstaller.png" "$A/"

cat > "$A/AppRun" <<'EOF'
#!/bin/sh
HERE=$(dirname "$(readlink -f "$0")")
export LD_LIBRARY_PATH="$HERE/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="$HERE/usr/lib/girepository-1.0"
export XDG_DATA_DIRS="$HERE/usr/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export GSETTINGS_SCHEMA_DIR="$HERE/usr/share/glib-2.0/schemas"
export GDK_PIXBUF_MODULEDIR="$HERE/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders"
export GDK_PIXBUF_MODULE_FILE="$HERE/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
"$HERE/usr/bin/gdk-pixbuf-query-loaders" > "$GDK_PIXBUF_MODULE_FILE" 2>/dev/null || true
export PYTHONPATH="$HERE/usr/lib/python3.12/site-packages"
# glycin's .conf files hardcode /usr/lib: rewrite them into a writable copy
if [ -d "$HERE/usr/share/glycin-loaders" ]; then
  GLYCIN_DATA_DIR=$(mktemp -d)
  export GLYCIN_DATA_DIR
  mkdir -p "$GLYCIN_DATA_DIR/glycin-loaders"
  cp -a "$HERE/usr/share/glycin-loaders/." "$GLYCIN_DATA_DIR/glycin-loaders/"
  sed -i "s|=/usr/lib/glycin-loaders|=$HERE/usr/lib/glycin-loaders|" \
    "$GLYCIN_DATA_DIR"/glycin-loaders/*/conf.d/*.conf
fi
[ $# -gt 0 ] || set -- gui
"$HERE/usr/python/bin/python3" -m wvi "$@"
status=$?
[ -z "${GLYCIN_DATA_DIR:-}" ] || rm -rf "$GLYCIN_DATA_DIR"
exit $status
EOF
chmod 755 "$A/AppRun"

curl -fL -o "$work/appimagetool" \
  "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
chmod 755 "$work/appimagetool"
ARCH=x86_64 "$work/appimagetool" --appimage-extract-and-run "$A" \
  "$here/windows-vm-installer-x86_64.AppImage"
echo "done: $here/windows-vm-installer-x86_64.AppImage"