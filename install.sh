#!/usr/bin/env bash
# BLOODBOS RADIO — Linux installer
#
# Paste this into a terminal and press Enter:
#
#     curl -fsSL https://raw.githubusercontent.com/korn788/BloodBos-music-request-bot/main/install.sh | bash
#
# It installs VLC (if needed), sets the app up in your home folder, and adds
# it to your app menu. No admin rights are needed except to install VLC.
# Run it again any time to update. Works on Ubuntu, Debian, Mint, Pop!_OS,
# Fedora, Arch, CachyOS, Manjaro, EndeavourOS and openSUSE.
#
# Options:  --uninstall   remove the app (asks before touching your settings)
#           --yes         don't ask questions (answer yes)
#           --no-launch   don't start the app at the end
#           --from DIR    install from a downloaded copy of the source
set -euo pipefail

APP_ID="bloodbos-radio"
APP_NAME="BLOODBOS Radio"
REPO="korn788/BloodBos-music-request-bot"
BRANCH="main"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
PROG="$DATA_HOME/$APP_ID-app"          # program + its private Python
VENV="$PROG/venv"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/$APP_ID"
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"
ICON_FILE="$DATA_HOME/icons/hicolor/256x256/apps/$APP_ID.png"
USER_DATA="$DATA_HOME/$APP_ID"         # settings, logins, queue (the app's own folder)

YES=0; LAUNCH=1; FROM=""; UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) YES=1 ;;
        --no-launch) LAUNCH=0 ;;
        --from) FROM="$2"; shift ;;
        --uninstall) UNINSTALL=1 ;;
        --branch) BRANCH="$2"; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown option: $1"; exit 2 ;;
    esac
    shift
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
say()  { printf '\033[1;31m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx \033[0m %s\n' "$*" >&2; exit 1; }

ask() {   # ask "question" → 0 for yes
    [ "$YES" -eq 1 ] && return 0
    local reply=""
    if [ -r /dev/tty ]; then
        printf '%s [Y/n] ' "$1" > /dev/tty
        read -r reply < /dev/tty || reply=""
    fi
    case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

ask_no() {   # like ask, but Enter means no (for anything that deletes)
    local reply=""
    if [ -r /dev/tty ]; then
        printf '%s [y/N] ' "$1" > /dev/tty
        read -r reply < /dev/tty || reply=""
    fi
    case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"
    elif command -v sudo >/dev/null 2>&1; then sudo "$@"
    else die "need administrator rights to run: $* (install sudo, or run as root)"; fi
}

# ── uninstall ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" -eq 1 ]; then
    say "Removing $APP_NAME"
    rm -rf "$PROG" "$LAUNCHER" "$DESKTOP_FILE" "$ICON_FILE"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$DATA_HOME/applications" || true
    if [ -d "$USER_DATA" ]; then
        if ask_no "Also delete your settings, logins and song history ($USER_DATA)?"; then
            rm -rf "$USER_DATA"; say "Settings deleted."
        else
            say "Kept your settings in $USER_DATA"
        fi
    fi
    say "Done."
    exit 0
fi

[ "$(uname -s)" = "Linux" ] || die "This installer is for Linux. Windows and Mac users: download the installer from https://github.com/$REPO/releases/latest"

bold ""
bold "  $APP_NAME — installer"
bold ""

# ── 1. system packages (VLC, Python, Qt's desktop libraries) ────────────────
PM=""
for m in apt-get dnf pacman zypper; do
    if command -v "$m" >/dev/null 2>&1; then PM="$m"; break; fi
done

pkgs=()
case "$PM" in
    apt-get)
        pkgs=(python3 python3-venv python3-pip vlc-bin vlc-plugin-base vlc-plugin-video-output libvlc5
              curl ca-certificates
              libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1
              libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1
              libegl1 libgl1 libfontconfig1 libdbus-1-3)
        missing=()
        for p in "${pkgs[@]}"; do
            dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
        done ;;
    dnf)
        pkgs=(python3 python3-pip vlc vlc-plugins-base xcb-util-cursor libxkbcommon-x11 mesa-libEGL
              fontconfig curl ca-certificates)
        missing=()
        for p in "${pkgs[@]}"; do rpm -q "$p" >/dev/null 2>&1 || missing+=("$p"); done ;;
    pacman)
        pkgs=(python vlc xcb-util-cursor libxkbcommon-x11 curl ca-certificates)
        # Newer Arch splits VLC's plugins into their own packages.
        if pacman -Si vlc-plugins-all >/dev/null 2>&1; then pkgs+=(vlc-plugins-all); fi
        missing=()
        for p in "${pkgs[@]}"; do pacman -Q "$p" >/dev/null 2>&1 || missing+=("$p"); done ;;
    zypper)
        pkgs=(python3 python3-pip vlc libxcb-cursor0 libxkbcommon-x11-0 curl ca-certificates)
        missing=()
        for p in "${pkgs[@]}"; do rpm -q "$p" >/dev/null 2>&1 || missing+=("$p"); done ;;
    *)
        warn "Couldn't recognise your package manager. Make sure VLC and Python 3.9+ are installed."
        missing=() ;;
esac

if [ "${#missing[@]}" -gt 0 ]; then
    say "Needs these system packages: ${missing[*]}"
    if ask "Install them now (asks for your password)?"; then
        case "$PM" in
            apt-get) as_root apt-get update -q || true
                     as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${missing[@]}" ;;
            dnf)     as_root dnf install -y "${missing[@]}" || {
                         # vlc-plugins-base only exists on newer Fedora; plain vlc is enough elsewhere
                         rest=(); for p in "${missing[@]}"; do [ "$p" = vlc-plugins-base ] || rest+=("$p"); done
                         as_root dnf install -y "${rest[@]}"; } ;;
            pacman)  as_root pacman -S --needed --noconfirm "${missing[@]}" ;;
            zypper)  as_root zypper --non-interactive install "${missing[@]}" ;;
        esac
    else
        warn "Skipped. If the app can't play, install VLC with your software manager."
    fi
fi

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
        PY="$(command -v "$c")"; break
    fi
done
[ -n "$PY" ] || die "Python 3.9 or newer is needed. Install 'python3' with your software manager and run this again."

# ── 2. the program files ─────────────────────────────────────────────────────
say "Getting $APP_NAME"
NEW="$(mktemp -d)"
if [ -n "$FROM" ]; then
    [ -f "$FROM/src/main.py" ] || die "--from: $FROM doesn't look like the $APP_NAME source folder"
    cp -a "$FROM/src" "$FROM/requirements.txt" "$NEW/"
    [ -f "$FROM/LICENSE" ] && cp "$FROM/LICENSE" "$NEW/" || true
else
    url="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$url" | tar -xz -C "$NEW" --strip-components=1
    else wget -qO- "$url" | tar -xz -C "$NEW" --strip-components=1; fi
fi
[ -f "$NEW/src/main.py" ] || die "download failed — check your internet connection and try again"

mkdir -p "$PROG"
rm -rf "$PROG/src.old"
[ -d "$PROG/src" ] && mv "$PROG/src" "$PROG/src.old"
mv "$NEW/src" "$PROG/src"
cp "$NEW/requirements.txt" "$PROG/requirements.txt"
[ -f "$NEW/LICENSE" ] && cp "$NEW/LICENSE" "$PROG/" || true
rm -rf "$NEW" "$PROG/src.old"

# ── 3. a private Python with the app's libraries ─────────────────────────────
if [ ! -x "$VENV/bin/python" ]; then
    say "Setting up the app's own Python (one time, a minute or two)"
    "$PY" -m venv "$VENV" || die "Couldn't create a Python environment. On Debian/Ubuntu install python3-venv and try again."
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip wheel >/dev/null 2>&1 || true
say "Installing the app's libraries"
"$VENV/bin/python" -m pip install --quiet --upgrade -r "$PROG/requirements.txt" \
    || die "Couldn't install the app's libraries (see the messages above)."

# ── 4. launcher, menu entry, icon ────────────────────────────────────────────
mkdir -p "$BIN_DIR" "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_FILE")"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
exec "$VENV/bin/python" "$PROG/src/main.py" "\$@"
EOF
chmod 755 "$LAUNCHER"
cp "$PROG/src/assets/icon.png" "$ICON_FILE"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
GenericName=Stream song requests
Comment=Song-request radio for Twitch, Kick and Owncast streams
Exec="$LAUNCHER"
Icon=$APP_ID
Terminal=false
Categories=AudioVideo;Audio;Player;
Keywords=music;stream;twitch;kick;owncast;obs;youtube;soundcloud;
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$DATA_HOME/applications" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true

# ── 5. check it works ────────────────────────────────────────────────────────
say "Checking the install"
if "$LAUNCHER" --selftest >/tmp/$APP_ID-selftest.log 2>&1; then
    say "Everything works."
else
    cat /tmp/$APP_ID-selftest.log
    warn "The self-check found a problem (details above). The most common fix is"
    warn "installing VLC with your software manager, then running this installer again."
fi

bold ""
bold "  $APP_NAME is installed."
echo "  Open it from your app menu, or type:  $APP_ID"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "  (log out and back in once so the '$APP_ID' command is found)";; esac
echo "  Update later by running this installer again. Remove with:  bash install.sh --uninstall"
bold ""

if [ "$LAUNCH" -eq 1 ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    nohup "$LAUNCHER" >/dev/null 2>&1 &
fi
