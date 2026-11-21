#!/usr/bin/env bash
# BLOODBOS RADIO — Linux installer
#
# Paste this into a terminal and press Enter:
#
#     curl -fsSL https://raw.githubusercontent.com/korn788/BloodBos-music-request-bot/main/install.sh | bash
#
# It installs VLC (if needed), puts the app in your home folder, and adds it
# to your app menu. No admin rights are needed except to install VLC.
# Run it again any time to update. Works on Ubuntu, Debian, Mint, Pop!_OS,
# Fedora, Arch, CachyOS, Manjaro, EndeavourOS and openSUSE.
#
# It installs the same ready-made app as the Windows/Mac/.deb downloads
# (Kick sign-in built in). Where that can't run (a non-x86 computer, a very
# old system, or before the first release), it sets the app up from its
# source code instead.
#
# Options:  --uninstall    remove the app (asks before touching your settings)
#           --yes          don't ask questions (answer yes)
#           --no-launch    don't start the app at the end
#           --source       set it up from the source code, not the ready-made app
#           --from DIR     install from a downloaded copy of the source
#           --bundle FILE  install a downloaded BLOODBOS-Radio-Linux-x86_64.tar.gz
#           --testing      install the test build (the newest code, before it's
#                          released); run without it to go back to the release
set -euo pipefail

APP_ID="bloodbos-radio"
APP_NAME="BLOODBOS Radio"
REPO="korn788/BloodBos-music-request-bot"
BRANCH="main"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
PROG="$DATA_HOME/$APP_ID-app"          # the program (ready-made app, or source + private Python)
VENV="$PROG/venv"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/$APP_ID"
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"
ICON_FILE="$DATA_HOME/icons/hicolor/256x256/apps/$APP_ID.png"
USER_DATA="$DATA_HOME/$APP_ID"         # settings, logins, queue (the app's own folder)
TARBALL_NAME="BLOODBOS-Radio-Linux-x86_64.tar.gz"
LOG="${TMPDIR:-/tmp}/$APP_ID-selftest.log"

YES=0; LAUNCH=1; FROM=""; UNINSTALL=0; SOURCE=0; BUNDLE=""; TESTING=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) YES=1 ;;
        --no-launch) LAUNCH=0 ;;
        --from) FROM="$2"; SOURCE=1; shift ;;
        --source) SOURCE=1 ;;
        --bundle) BUNDLE="$2"; shift ;;
        --uninstall) UNINSTALL=1 ;;
        --branch) BRANCH="$2"; shift ;;
        --testing) TESTING=1 ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
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

fetch() {   # fetch URL FILE
    if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 2 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
    else return 1; fi
}

PM=""
for m in apt-get dnf pacman zypper; do
    if command -v "$m" >/dev/null 2>&1; then PM="$m"; break; fi
done

# ── system packages (VLC and Qt's desktop libraries; Python for a source setup)
system_packages() {   # system_packages bundle|source
    local mode="$1" pkgs=() missing=() p
    case "$PM" in
        apt-get)
            pkgs=(vlc-plugin-base vlc-plugin-video-output libvlc5 ca-certificates
                  libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1
                  libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1
                  libegl1 libgl1 libfontconfig1 libdbus-1-3)
            [ "$mode" = source ] && pkgs+=(python3 python3-venv python3-pip vlc-bin curl)
            for p in "${pkgs[@]}"; do
                dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
            done ;;
        dnf)
            pkgs=(vlc vlc-plugins-base xcb-util-cursor libxkbcommon-x11 mesa-libEGL fontconfig ca-certificates)
            [ "$mode" = source ] && pkgs+=(python3 python3-pip curl)
            for p in "${pkgs[@]}"; do rpm -q "$p" >/dev/null 2>&1 || missing+=("$p"); done ;;
        pacman)
            pkgs=(vlc xcb-util-cursor libxkbcommon-x11 ca-certificates)
            # Newer Arch splits VLC's plugins into their own packages.
            if pacman -Si vlc-plugins-all >/dev/null 2>&1; then pkgs+=(vlc-plugins-all); fi
            [ "$mode" = source ] && pkgs+=(python curl)
            for p in "${pkgs[@]}"; do pacman -Q "$p" >/dev/null 2>&1 || missing+=("$p"); done ;;
        zypper)
            pkgs=(vlc libxcb-cursor0 libxkbcommon-x11-0 ca-certificates)
            [ "$mode" = source ] && pkgs+=(python3 python3-pip curl)
            for p in "${pkgs[@]}"; do rpm -q "$p" >/dev/null 2>&1 || missing+=("$p"); done ;;
        *)
            warn "Couldn't recognise your package manager. Make sure VLC is installed."
            return 0 ;;
    esac
    [ "${#missing[@]}" -gt 0 ] || return 0
    say "Needs these system packages: ${missing[*]}"
    if ask "Install them now (asks for your password)?"; then
        case "$PM" in
            apt-get) as_root apt-get update -q || true
                     as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${missing[@]}" ;;
            dnf)     as_root dnf install -y "${missing[@]}" || {
                         # vlc-plugins-base only exists on newer Fedora; plain vlc is enough elsewhere
                         local rest=(); for p in "${missing[@]}"; do [ "$p" = vlc-plugins-base ] || rest+=("$p"); done
                         as_root dnf install -y "${rest[@]}"; } ;;
            pacman)  as_root pacman -S --needed --noconfirm "${missing[@]}" ;;
            zypper)  as_root zypper --non-interactive install "${missing[@]}" ;;
        esac
    else
        warn "Skipped. If the app can't play, install VLC with your software manager."
    fi
}

# ── the ready-made app ────────────────────────────────────────────────────────
install_bundle() {   # install_bundle TARBALL
    say "Installing $APP_NAME"
    local new; new="$(mktemp -d)"
    tar -xzf "$1" -C "$new" 2>/dev/null && [ -x "$new/bloodbos-radio/bloodbos-radio" ] \
        || { rm -rf "$new"; die "the download was damaged — run this installer again"; }
    mkdir -p "$PROG"
    rm -rf "$PROG/app.old"
    [ -d "$PROG/app" ] && mv "$PROG/app" "$PROG/app.old"
    mv "$new/bloodbos-radio" "$PROG/app"
    # tidy up an older setup (source copy and its private Python)
    rm -rf "$new" "$PROG/app.old" "$PROG/src" "$PROG/venv" "$PROG/requirements.txt"
    write_launcher "\"$PROG/app/bloodbos-radio\""
    ICON_SRC="$(find "$PROG/app" -path '*assets/icon.png' 2>/dev/null | head -n 1)" || ICON_SRC=""
}

# ── set up from the source code, with a private Python ─────────────────────────
install_source() {
    local py="" c new url
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
            py="$(command -v "$c")"; break
        fi
    done
    [ -n "$py" ] || die "Python 3.9 or newer is needed. Install 'python3' with your software manager and run this again."

    say "Getting $APP_NAME"
    new="$(mktemp -d)"
    if [ -n "$FROM" ]; then
        [ -f "$FROM/src/main.py" ] || die "--from: $FROM doesn't look like the $APP_NAME source folder"
        cp -a "$FROM/src" "$FROM/requirements.txt" "$new/"
        [ -f "$FROM/LICENSE" ] && cp "$FROM/LICENSE" "$new/" || true
    else
        url="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
        if command -v curl >/dev/null 2>&1; then curl -fsSL "$url" | tar -xz -C "$new" --strip-components=1
        else wget -qO- "$url" | tar -xz -C "$new" --strip-components=1; fi
    fi
    [ -f "$new/src/main.py" ] || die "download failed — check your internet connection and try again"

    mkdir -p "$PROG"
    rm -rf "$PROG/src.old" "$PROG/app"            # (the ready-made app, if it was there)
    [ -d "$PROG/src" ] && mv "$PROG/src" "$PROG/src.old"
    mv "$new/src" "$PROG/src"
    cp "$new/requirements.txt" "$PROG/requirements.txt"
    [ -f "$new/LICENSE" ] && cp "$new/LICENSE" "$PROG/" || true
    rm -rf "$new" "$PROG/src.old"

    if [ ! -x "$VENV/bin/python" ]; then
        say "Setting up the app's own Python (one time, a minute or two)"
        "$py" -m venv "$VENV" || die "Couldn't create a Python environment. On Debian/Ubuntu install python3-venv and try again."
    fi
    "$VENV/bin/python" -m pip install --quiet --upgrade pip wheel >/dev/null 2>&1 || true
    say "Installing the app's libraries"
    # If a library has no ready-made download for this Python yet (brand-new
    # Python versions), build its plain-Python version — no compiler needed.
    export AIOHTTP_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 YARL_NO_EXTENSIONS=1 \
           FROZENLIST_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1
    "$VENV/bin/python" -m pip install --quiet --upgrade -r "$PROG/requirements.txt" \
        || die "Couldn't install the app's libraries (see the messages above)."
    write_launcher "\"$VENV/bin/python\" \"$PROG/src/main.py\""
    ICON_SRC="$PROG/src/assets/icon.png"
}

write_launcher() {   # write_launcher 'COMMAND' (already quoted)
    mkdir -p "$BIN_DIR"
    printf '#!/bin/sh\nexec %s "$@"\n' "$1" > "$LAUNCHER"
    chmod 755 "$LAUNCHER"
}

# ── menu entry, icon ───────────────────────────────────────────────────────────
menu_entry() {
    mkdir -p "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_FILE")"
    [ -n "${ICON_SRC:-}" ] && [ -f "$ICON_SRC" ] && cp "$ICON_SRC" "$ICON_FILE"
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
}

# ── 1. pick: the ready-made app, or a source setup ────────────────────────────
MODE=source
TARBALL=""
if [ -n "$BUNDLE" ]; then
    [ -f "$BUNDLE" ] || die "--bundle: can't find $BUNDLE"
    TARBALL="$BUNDLE"; MODE=bundle
elif [ "$SOURCE" -eq 0 ] && [ "$(uname -m)" = "x86_64" ]; then
    if [ "$TESTING" -eq 1 ]; then
        say "Downloading $APP_NAME (test build — the newest code, not a release)"
        DOWNLOADS="https://github.com/$REPO/releases/download/testing"
    else
        say "Downloading $APP_NAME"
        DOWNLOADS="https://github.com/$REPO/releases/latest/download"
    fi
    TARBALL="$(mktemp)"
    if fetch "$DOWNLOADS/$TARBALL_NAME" "$TARBALL" \
            && tar -tzf "$TARBALL" bloodbos-radio/bloodbos-radio >/dev/null 2>&1; then
        MODE=bundle
    else
        rm -f "$TARBALL"; TARBALL=""
        warn "No ready-made download for this computer yet — setting it up from the source code."
    fi
fi

# ── 2. system packages, 3. the program, 4. launcher + menu entry ─────────────
system_packages "$MODE"
if [ "$MODE" = bundle ]; then
    install_bundle "$TARBALL"
    [ "$TARBALL" = "$BUNDLE" ] || rm -f "$TARBALL"
else
    install_source
fi
menu_entry

# ── 5. check it works ────────────────────────────────────────────────────────
say "Checking the install"
if "$LAUNCHER" --selftest >"$LOG" 2>&1; then
    say "Everything works."
elif [ "$MODE" = bundle ] && grep -qE "GLIBC(XX)?_[0-9.]+' not found|version .GLIBC" "$LOG"; then
    warn "This Linux is older than the ready-made app needs — setting it up from the source code instead."
    MODE=source
    system_packages source
    install_source
    menu_entry
    if "$LAUNCHER" --selftest >"$LOG" 2>&1; then say "Everything works."
    else cat "$LOG"; warn "The self-check found a problem (details above)."; fi
else
    cat "$LOG"
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
