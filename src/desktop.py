# desktop.py — open web pages and folders with the computer's own apps.
#
# The downloadable builds carry their own copies of some libraries and point
# the app at them through environment variables (LD_LIBRARY_PATH on Linux,
# Qt plugin paths, ...). Anything the app starts inherits those, so a browser
# or file manager launched from here could load the app's copies instead of
# its own and crash or fail to load pages — KDE's own helpers especially, since
# they're Qt programs too. So every page and folder is opened with the
# environment the desktop gave the app, before any of that was changed.

import os
import pathlib
import shutil
import subprocess
import sys
import webbrowser

_BASE = None

# Set by the packaged app's startup code; never meaningful to other programs.
_BUNDLE_VARS = ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                "QTWEBENGINEPROCESS_PATH", "PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY",
                "TK_LIBRARY", "GIO_MODULE_DIR", "GDK_PIXBUF_MODULE_FILE", "GTK_PATH",
                "GTK_DATA_PREFIX", "GTK_EXE_PREFIX", "FONTCONFIG_FILE", "FONTCONFIG_PATH")


def remember_environment():
    """Call first thing at startup, before the app changes any variable."""
    global _BASE
    if _BASE is None:
        _BASE = dict(os.environ)


def clean_env():
    env = dict(_BASE if _BASE is not None else os.environ)
    if getattr(sys, "frozen", False):
        # PyInstaller points these at the bundle and keeps the originals in *_ORIG.
        for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "LIBPATH"):
            orig = env.pop(var + "_ORIG", None)
            if orig:
                env[var] = orig
            else:
                env.pop(var, None)
        for var in _BUNDLE_VARS:
            env.pop(var, None)
    return env


def _launch(cmd):
    env = clean_env()
    exe = shutil.which(cmd[0], path=env.get("PATH")) or shutil.which(cmd[0])
    if not exe:
        return False
    try:
        subprocess.Popen([exe] + cmd[1:], env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        return True
    except OSError:
        return False


def open_url(url):
    """Open a web page (or file:// address) in the user's default app."""
    if sys.platform.startswith("linux"):
        for cmd in (["xdg-open", url], ["gio", "open", url], ["kde-open", url],
                    ["kde-open5", url], ["exo-open", url]):
            if _launch(cmd):
                return True
    elif sys.platform == "darwin":
        if _launch(["open", url]):
            return True
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def open_path(path):
    """Show a folder (or file) in the file manager."""
    path = str(path)
    if sys.platform.startswith("win"):
        try:
            os.startfile(path)        # noqa — Windows only
            return True
        except OSError:
            return False
    return open_url(pathlib.Path(path).resolve().as_uri())


# ── AppImage: show up in the app menu ───────────────────────────────────────
# An AppImage is just a file; nothing adds it to the app menu. People who've
# never used a terminal expect a downloaded app to be there after opening it
# once, so the app adds itself (and moves the entry along if the file moves).
# TryExec hides the entry again if the file is deleted.

APP_ID = "bloodbos-radio"
_APPIMAGE_MARK = "X-BLOODBOS-AppImage=true"


def _desktop_exec(path):
    """A path as a desktop-entry Exec argument (quoting + escaping rules)."""
    quoted = "".join("\\" + c if c in '"`$\\' else c for c in path)
    return '"' + quoted.replace("\\", "\\\\").replace("%", "%%") + '"'


def integrate_appimage(icon_png=None):
    """Running from an AppImage: add it to the app menu, or point the menu
    entry at this copy. Leaves alone an entry made by the install line or the
    .deb. Returns a line for the log ("" when nothing changed)."""
    image = os.environ.get("APPIMAGE", "")
    if not sys.platform.startswith("linux") or not image or not os.path.isfile(image):
        return ""
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    entry = os.path.join(data, "applications", APP_ID + ".desktop")
    for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if d and os.path.isfile(os.path.join(d, "applications", APP_ID + ".desktop")):
            return ""                       # installed with the .deb
    try:
        with open(entry, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        old = None
    if old is not None and _APPIMAGE_MARK not in old:
        return ""                           # installed with the install line
    icon = os.path.join(data, "icons", "hicolor", "256x256", "apps", APP_ID + ".png")
    text = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=BLOODBOS Radio",
        "GenericName=Stream song requests",
        "Comment=Song-request radio for Twitch, Kick and Owncast streams",
        "Exec=" + _desktop_exec(image),
        "TryExec=" + image.replace("\\", "\\\\"),
        "Icon=" + APP_ID,
        "Terminal=false",
        "Categories=AudioVideo;Audio;Player;",
        "Keywords=music;stream;twitch;kick;owncast;obs;youtube;soundcloud;",
        _APPIMAGE_MARK,
        ""])
    if old == text and os.path.isfile(icon):
        return ""
    try:
        if icon_png and os.path.isfile(icon_png):
            os.makedirs(os.path.dirname(icon), exist_ok=True)
            shutil.copyfile(icon_png, icon)
        os.makedirs(os.path.dirname(entry), exist_ok=True)
        tmp = entry + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, entry)
    except OSError as e:
        return f"Couldn't add the app to the app menu: {e}"
    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(["update-desktop-database", "-q", os.path.dirname(entry)],
                           env=clean_env(), timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    return ("Added BLOODBOS Radio to your app menu" if old is None
            else "The app menu now opens this copy of BLOODBOS Radio")

