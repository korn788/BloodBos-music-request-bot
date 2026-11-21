# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for BLOODBOS RADIO (Windows, macOS, Linux).
#
#   pyinstaller packaging/bloodbos_radio.spec --noconfirm
#
# VLC is NOT collected here. The release workflow copies a trimmed VLC into
# the finished app afterwards (Windows: next to the .exe, macOS: into
# Contents/Frameworks/vlc), so PyInstaller never rewrites VLC's own library
# paths. Linux builds use the system's VLC.

import os
import sys

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")
ASSETS = os.path.join(SRC, "assets")
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

APP = "BLOODBOS Radio"
EXE_NAME = "bloodbos-radio" if not (IS_WIN or IS_MAC) else APP

sys.path.insert(0, SRC)
from version import __version__  # noqa: E402

datas = [
    (os.path.join(SRC, "widget.html"), "."),
    (ASSETS, "assets"),
]
binaries = []
hiddenimports = [
    "chat", "chat.commands", "chat.http_util", "chat.kick", "chat.manager",
    "chat.oauth_local", "chat.owncast", "chat.twitch",
    "setup_dialog", "selftest", "youtube_api",
]
if os.path.isfile(os.path.join(SRC, "_build_keys.py")):
    hiddenimports.append("_build_keys")
if IS_WIN:   # media keys (pynput is only installed on Windows)
    hiddenimports += ["pynput.keyboard._win32", "pynput.mouse._win32", "pynput._util.win32"]

for pkg in ("curl_cffi", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

excludes = ["tkinter", "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtQml",
            "PyQt6.QtQuick", "PyQt6.QtMultimedia", "PyQt6.Qt3DCore", "PyQt6.QtBluetooth",
            "PyQt6.QtPositioning", "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtPdf",
            "PyQt6.QtDesigner", "PyQt6.QtHelp", "PyQt6.QtOpenGLWidgets",
            # never used; only here if the build machine happens to have them
            "numpy", "scipy", "lxml", "yaml", "pydantic", "pydantic_core", "mypy", "psutil",
            "setuptools", "chardet", "PIL", "matplotlib", "pandas", "IPython", "jedi"]

a = Analysis(
    [os.path.join(SRC, "main.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)

if not (IS_WIN or IS_MAC):
    # Leave the core system libraries to the system. The app loads the
    # distro's own libvlc and its plugins, which were built against the
    # distro's glib, zlib, libstdc++, … — older copies from the build machine
    # loaded first would break them.
    SYSTEM_LIBS = ("libvlc", "libglib-2.0", "libgobject-2.0", "libgio-2.0", "libgmodule-2.0",
                   "libgthread-2.0", "libdbus-1", "libfontconfig", "libfreetype", "libz.",
                   "libexpat", "libstdc++", "libgcc_s", "libuuid", "libpcre", "libbz2",
                   "liblzma", "libzstd", "libxkbcommon", "libsystemd", "libselinux",
                   "libmount", "libblkid", "libgpg-error", "libgcrypt", "liblz4",
                   "libcap", "libX", "libxcb", "libEGL", "libGL", "libdrm", "libgbm",
                   "libwayland", "libasound", "libpulse", "libsndfile", "libharfbuzz",
                   "libpng16", "libjpeg", "libbrotli", "libgtk-3", "libgdk-3",
                   "libgdk_pixbuf", "libatk", "libatspi", "libcairo", "libpango",
                   "libpixman", "libepoxy", "libfribidi", "libthai", "libdatrie",
                   "libgraphite2", "libkrb5", "libgssapi", "libk5crypto", "libkeyutils",
                   "libcom_err", "libidn", "libbsd", "libmd.", "libgfortran", "libquadmath")
    a.binaries = [b for b in a.binaries
                  if not os.path.basename(b[0]).startswith(SYSTEM_LIBS)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(ASSETS, "icon.ico" if IS_WIN else "icon.icns"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP if (IS_WIN or IS_MAC) else EXE_NAME,
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{APP}.app",
        icon=os.path.join(ASSETS, "icon.icns"),
        bundle_identifier="io.github.korn788.bloodbos-radio",
        version=__version__,
        info_plist={
            "CFBundleName": APP,
            "CFBundleDisplayName": APP,
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.music",
        },
    )
