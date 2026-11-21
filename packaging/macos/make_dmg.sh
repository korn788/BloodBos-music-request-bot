#!/usr/bin/env bash
# make_dmg.sh — finish the macOS app and wrap it in a drag-to-install .dmg
#
#   packaging/macos/make_dmg.sh "dist/BLOODBOS Radio.app" vendor/vlc dist/BLOODBOS-Radio-macOS.dmg
#
# 1. copies the trimmed VLC into the app (Contents/Frameworks/vlc)
# 2. signs the whole app "ad hoc" (free; Apple Silicon refuses unsigned code)
# 3. builds a compressed .dmg with an Applications shortcut to drag onto
set -euo pipefail

APP="$1"
VLC="$2"
DMG="$3"
NAME="BLOODBOS Radio"

[ -d "$APP" ] || { echo "no app at $APP"; exit 1; }
[ -d "$VLC" ] || { echo "no VLC at $VLC"; exit 1; }

rm -rf "$APP/Contents/Frameworks/vlc"
ditto "$VLC" "$APP/Contents/Frameworks/vlc"

# VLC ships "universal" libraries (Intel + Apple chip code in every file).
# Keep only this build's half — the download gets roughly 40 MB smaller.
ARCH="$(uname -m)"
find "$APP/Contents/Frameworks/vlc" -type f -name "*.dylib" -print0 | while IFS= read -r -d '' f; do
    if lipo -archs "$f" 2>/dev/null | grep -qw "$ARCH" && [ "$(lipo -archs "$f" | wc -w)" -gt 1 ]; then
        lipo "$f" -thin "$ARCH" -output "$f.thin" && mv "$f.thin" "$f"
    fi
done

# Strip download quarantine flags picked up on the build machine, then sign
# every library inside-out, then the app itself.
xattr -cr "$APP" || true
find "$APP/Contents/Frameworks/vlc" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 \
  | xargs -0 -n 50 codesign --force --sign - --timestamp=none
codesign --force --deep --sign - --timestamp=none "$APP"
codesign --verify --deep --strict "$APP" && echo "signature ok"

STAGE="$(mktemp -d)"
ditto "$APP" "$STAGE/$NAME.app"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/READ ME FIRST.txt" <<'EOF'
Install:  drag "BLOODBOS Radio" onto "Applications".

First start: macOS doesn't know this free app yet, so it may say it
"can't be opened" or "can't check it for malicious software".
  1. Open System Settings > Privacy & Security.
  2. Scroll down — next to the message about BLOODBOS Radio, click "Open Anyway".
  3. Confirm with your password or Touch ID.
You only have to do this once.
EOF
rm -f "$DMG"
hdiutil create -volname "$NAME" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$DMG"
rm -rf "$STAGE"
echo "built $DMG"
