# vlc_loader.py — find VLC before python-vlc goes looking for it
#
# Windows and macOS builds carry their own copy of VLC (libvlc + plugins) in a
# "vlc" folder inside the app, so nobody has to install VLC separately. So does
# the Linux AppImage; the other Linux installs use the system's VLC (the
# installer or the .deb pulls it in).
#
# python-vlc reads two environment variables when it is first imported:
#   PYTHON_VLC_LIB_PATH     the libvlc library to load
#   PYTHON_VLC_MODULE_PATH  the plugins folder
# so they're set here, before anything imports vlc.

import ctypes
import os
import sys

import app_paths

_loaded = None
_error = ""
_extra = []          # options every VLC instance needs (see instance_args)


def instance_args():
    """Options to pass to every vlc.Instance() the app creates."""
    return list(_extra)


def _prepare():
    d = app_paths.bundled_vlc_dir()
    if not d:
        return None
    if sys.platform.startswith("win"):
        lib = os.path.join(d, "libvlc.dll")
        plugins = os.path.join(d, "plugins")
        if not os.path.isfile(lib):
            return None
        try:
            os.add_dll_directory(d)
        except (AttributeError, OSError):
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        os.environ["PYTHON_VLC_LIB_PATH"] = lib
        if os.path.isdir(plugins):
            os.environ["PYTHON_VLC_MODULE_PATH"] = plugins
            os.environ["VLC_PLUGIN_PATH"] = plugins
        return d
    if sys.platform == "darwin":
        libdir = os.path.join(d, "lib")
        lib = os.path.join(libdir, "libvlc.dylib")
        core = os.path.join(libdir, "libvlccore.dylib")
        plugins = os.path.join(d, "plugins")
        if not (os.path.isfile(lib) and os.path.isfile(core)):
            return None
        try:
            ctypes.CDLL(core)          # libvlc needs libvlccore loaded first
        except OSError:
            return None
        os.environ["PYTHON_VLC_LIB_PATH"] = lib
        if os.path.isdir(plugins):
            os.environ["PYTHON_VLC_MODULE_PATH"] = plugins
            os.environ["VLC_PLUGIN_PATH"] = plugins
        return d
    if sys.platform.startswith("linux"):       # the AppImage
        lib = os.path.join(d, "libvlc.so.5")
        core = os.path.join(d, "libvlccore.so.9")
        plugins = os.path.join(d, "plugins")
        if not (os.path.isfile(lib) and os.path.isfile(core) and os.path.isdir(plugins)):
            return None
        os.environ["PYTHON_VLC_LIB_PATH"] = lib
        os.environ["PYTHON_VLC_MODULE_PATH"] = plugins
        os.environ["VLC_PLUGIN_PATH"] = plugins
        certs = os.path.join(d, "certs")
        if os.path.isdir(certs):
            # This VLC's secure-connection library comes from the Linux it was
            # built on and would look for trusted certificates where that
            # Linux keeps them (Fedora and openSUSE keep them elsewhere), so
            # every https stream would fail there. It brings its own list.
            _extra.append(f"--gnutls-dir-trust={certs}")
        return d
    return None


def load():
    """Import python-vlc. Returns the module, or None (see error())."""
    global _loaded, _error
    if _loaded is not None:
        return _loaded
    where = _prepare()
    try:
        import vlc  # noqa: WPS433
        inst = vlc.Instance("--quiet", "--no-video")
        if inst is None:
            raise RuntimeError("libvlc refused to start")
        inst.release()
        _loaded = vlc
        print(f"[VLC] {vlc.libvlc_get_version().decode(errors='replace')}"
              f"{' (bundled)' if where else ''}")
        return vlc
    except BaseException as e:          # python-vlc calls sys.exit() on some failures
        if isinstance(e, KeyboardInterrupt):
            raise
        _error = f"{type(e).__name__}: {e}"
        print(f"[VLC] not available: {_error}")
        return None


def error():
    return _error


def install_hint():
    """Plain-English way to get VLC on this computer."""
    if sys.platform.startswith("win"):
        return ("This copy of the app is missing its built-in VLC. Reinstall it, or install "
                "VLC (64-bit) from videolan.org and start the app again.")
    if sys.platform == "darwin":
        return ("Install VLC from videolan.org (drag it into Applications), then open this "
                "app again.")
    for mgr, cmd in (("apt-get", "sudo apt install vlc"), ("dnf", "sudo dnf install vlc"),
                     ("pacman", "sudo pacman -S vlc"), ("zypper", "sudo zypper install vlc")):
        from shutil import which
        if which(mgr):
            return f"VLC isn't installed. Open a terminal and run:\n\n    {cmd}\n\nthen start the app again."
    return "VLC isn't installed. Install the 'vlc' package for your system, then start the app again."
