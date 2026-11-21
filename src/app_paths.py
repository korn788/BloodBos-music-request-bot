# app_paths.py — where the app keeps its files
#
# Two kinds of files:
#   resources  read-only things that ship with the app (widget.html, icons).
#              Inside the installed app bundle, or next to these .py files
#              when running from source.
#   data       everything the app writes: settings, logins, the queue, the
#              song log, the blocklist, downloaded helper programs. Installed
#              apps can't write next to themselves (Program Files, a signed
#              macOS .app, /opt), so this lives in the user's own folders:
#                Windows  %APPDATA%\BLOODBOS Radio
#                macOS    ~/Library/Application Support/BLOODBOS Radio
#                Linux    ~/.local/share/bloodbos-radio  (or $XDG_DATA_HOME)
#
# Override with the environment variable BLOODBOS_DATA_DIR, or put an empty
# file called "portable.txt" next to the program to keep everything beside it.

import os
import sys

APP_NAME = "BLOODBOS RADIO"
APP_FOLDER = "BLOODBOS Radio"      # Windows / macOS folder name
APP_ID = "bloodbos-radio"          # Linux / CLI name
REPO = "korn788/BloodBos-music-request-bot"    # GitHub owner/name (update checks, links)

FROZEN = bool(getattr(sys, "frozen", False))


def resource_dir():
    if FROZEN and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def resource(*parts):
    return os.path.join(resource_dir(), *parts)


def program_dir():
    """Folder holding the executable (frozen) or these source files."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _default_data_dir():
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return os.path.join(base, APP_FOLDER)
    if sys.platform == "darwin":
        return os.path.expanduser(os.path.join("~/Library/Application Support", APP_FOLDER))
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_ID)


_DATA_DIR = None


def data_dir():
    global _DATA_DIR
    if _DATA_DIR:
        return _DATA_DIR
    d = os.environ.get("BLOODBOS_DATA_DIR", "").strip()
    if not d and os.path.isfile(os.path.join(program_dir(), "portable.txt")):
        d = os.path.join(program_dir(), "data")
    if not d:
        d = _default_data_dir()
    d = os.path.abspath(os.path.expanduser(d))
    os.makedirs(d, exist_ok=True)
    _DATA_DIR = d
    return d


def set_data_dir(path):
    """Used by --data-dir on the command line (before anything else runs)."""
    global _DATA_DIR
    _DATA_DIR = None
    os.environ["BLOODBOS_DATA_DIR"] = path
    return data_dir()


def data_path(*parts):
    p = os.path.join(data_dir(), *parts)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return p


def bin_dir():
    """Helper programs the app downloads and keeps up to date (yt-dlp, deno)."""
    d = os.path.join(data_dir(), "bin")
    os.makedirs(d, exist_ok=True)
    return d


def logs_dir():
    d = os.path.join(data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def bundled_vlc_dir():
    """VLC shipped inside the Windows / macOS app, or None (system VLC)."""
    prog = program_dir()
    for d in (os.path.join(resource_dir(), "vlc"),
              os.path.join(prog, "vlc"),
              os.path.join(prog, "..", "Frameworks", "vlc"),     # macOS .app bundle
              os.path.join(prog, "..", "Resources", "vlc")):
        if os.path.isdir(d):
            return os.path.abspath(d)
    return None


def write_private(path, text):
    """Write a file only this user can read (logins live in these)."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
