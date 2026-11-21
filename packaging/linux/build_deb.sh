#!/usr/bin/env bash
# build_deb.sh — wrap the PyInstaller build in a .deb for Debian, Ubuntu, Mint, Pop!_OS
#
#   packaging/linux/build_deb.sh dist/bloodbos-radio 1.0.0 dist/bloodbos-radio_amd64.deb
#
# Double-clicking the .deb opens the Software app, which also installs VLC and
# the few desktop libraries Qt needs.
set -euo pipefail

BUILD="$1"
VERSION="$2"
OUT="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

PKG="$(mktemp -d)"
install -d "$PKG/DEBIAN" "$PKG/opt/bloodbos-radio" "$PKG/usr/bin" \
           "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/256x256/apps" \
           "$PKG/usr/share/icons/hicolor/512x512/apps" "$PKG/usr/share/doc/bloodbos-radio"
cp -a "$BUILD/." "$PKG/opt/bloodbos-radio/"
ln -s /opt/bloodbos-radio/bloodbos-radio "$PKG/usr/bin/bloodbos-radio"
install -m 644 "$HERE/bloodbos-radio.desktop" "$PKG/usr/share/applications/"
install -m 644 "$ROOT/src/assets/icon.png" "$PKG/usr/share/icons/hicolor/256x256/apps/bloodbos-radio.png"
install -m 644 "$ROOT/src/assets/icon-512.png" "$PKG/usr/share/icons/hicolor/512x512/apps/bloodbos-radio.png"
install -m 644 "$ROOT/LICENSE" "$PKG/usr/share/doc/bloodbos-radio/copyright"

SIZE=$(du -sk "$PKG" | cut -f1)
cat > "$PKG/DEBIAN/control" <<EOF
Package: bloodbos-radio
Version: $VERSION
Section: sound
Priority: optional
Architecture: amd64
Installed-Size: $SIZE
Depends: libvlc5, vlc-plugin-base, vlc-plugin-video-output, libxcb-cursor0, libxkbcommon-x11-0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, libxcb-render-util0, libxcb-shape0, libxcb-xinerama0, libxcb-xkb1, libegl1, libgl1, libfontconfig1, libdbus-1-3, ca-certificates
Maintainer: korn788 <10628831+korn788@users.noreply.github.com>
Homepage: https://github.com/korn788/BloodBos-music-request-bot
Description: Song-request radio for Twitch, Kick and Owncast streams
 Viewers type !sr and a song name or link in chat; the song is found on
 YouTube or SoundCloud, queued and played, with music videos and a
 now-playing card for OBS. Sign in to Twitch, Kick and Owncast with
 buttons in the app.
EOF
cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
exit 0
EOF
chmod 755 "$PKG/DEBIAN/postinst"
mkdir -p "$(dirname "$OUT")"
dpkg-deb --build --root-owner-group "$PKG" "$OUT"
rm -rf "$PKG"
echo "built $OUT"
