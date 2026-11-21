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
