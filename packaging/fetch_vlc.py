# fetch_vlc.py — download VLC and keep only what the player needs
#
#   python packaging/fetch_vlc.py windows vendor/vlc
#   python packaging/fetch_vlc.py macos   vendor/vlc
#
# Windows: the official 64-bit zip → libvlc.dll, libvlccore.dll, plugins/
# macOS:   the official universal .dmg → lib/libvlc*.dylib, plugins/
# The VLC interface, Lua scripts, skins, visualisations and translations are
# left out; playback needs none of them. VLC's license comes along.

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

VERSIONS = ["3.0.23", "3.0.22", "3.0.21"]
MIRRORS = ["https://download.videolan.org/pub/videolan/vlc/{v}/{sub}/{name}",
           "https://get.videolan.org/vlc/{v}/{sub}/{name}",
           "https://mirror.csclub.uwaterloo.ca/vlc/vlc/{v}/{sub}/{name}",
           "https://ftp.osuosl.org/pub/videolan/vlc/{v}/{sub}/{name}"]

# Plugin folders (Windows) / name fragments (macOS) the player never uses.
DROP_DIRS = {"gui", "visualization", "services_discovery", "control", "stream_out",
             "access_output", "mux", "video_splitter", "lua", "skins2"}
DROP_MAC = ("libmacosx_plugin", "liblua_plugin", "libgoom_plugin", "libprojectm_plugin",
            "libvisual_plugin", "libglspectrum_plugin", "libskins2_plugin",
            "libstream_out_", "libaccess_output_", "libmux_", "libhotkeys_plugin",
            "libsap_plugin", "libupnp_plugin", "libmicrodns_plugin", "libbonjour_plugin")


def fetch(sub, pattern):
    last = None
    for v in VERSIONS:
        name = pattern.format(v=v)
        for m in MIRRORS:
            url = m.format(v=v, sub=sub, name=name)
            try:
                print("trying", url, flush=True)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(name)[1])
                with urllib.request.urlopen(req, timeout=180) as r:
                    if "text/html" in (r.headers.get("Content-Type") or ""):
                        raise IOError("got a web page instead of the file")
                    shutil.copyfileobj(r, tmp, 1 << 20)
                tmp.close()
                if os.path.getsize(tmp.name) < 20 << 20:
                    raise IOError("download too small")
                print("got", name, os.path.getsize(tmp.name) >> 20, "MB", flush=True)
                return v, tmp.name
            except Exception as e:
                last = e
                print("  failed:", e, flush=True)
    raise SystemExit(f"couldn't download VLC: {last}")


def windows(dest):
    v, path = fetch("win64", "vlc-{v}-win64.zip")
    tmpdir = tempfile.mkdtemp()
    with zipfile.ZipFile(path) as z:
        z.extractall(tmpdir)
    root = os.path.join(tmpdir, f"vlc-{v}")
    os.makedirs(dest, exist_ok=True)
    for f in ("libvlc.dll", "libvlccore.dll", "COPYING.txt", "vlc-cache-gen.exe"):
        if os.path.exists(os.path.join(root, f)):
            shutil.copy2(os.path.join(root, f), dest)
    src_plugins = os.path.join(root, "plugins")
    shutil.copytree(src_plugins, os.path.join(dest, "plugins"),
                    ignore=lambda d, names: [n for n in names
                                             if d == src_plugins and n in DROP_DIRS])
    cache = os.path.join(dest, "plugins", "plugins.dat")
    if os.path.exists(cache):
        os.remove(cache)
    gen = os.path.join(dest, "vlc-cache-gen.exe")
    if os.path.exists(gen):        # rebuild the plugin cache for the trimmed set
        subprocess.run([gen, os.path.join(dest, "plugins")], check=False)
        os.remove(gen)
    _note(dest, v)


def macos(dest):
    v, path = fetch("macosx", "vlc-{v}-universal.dmg")
    mnt = tempfile.mkdtemp()
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mnt, path],
                   check=True)
    try:
        app = os.path.join(mnt, "VLC.app", "Contents", "MacOS")
        os.makedirs(dest, exist_ok=True)
        subprocess.run(["ditto", os.path.join(app, "lib"), os.path.join(dest, "lib")], check=True)
        subprocess.run(["ditto", os.path.join(app, "plugins"), os.path.join(dest, "plugins")],
                       check=True)
        lic = os.path.join(mnt, "VLC.app", "Contents", "Resources", "COPYING")
        for cand in (lic, os.path.join(mnt, "COPYING.txt"), os.path.join(mnt, "COPYING")):
            if os.path.exists(cand):
                shutil.copy2(cand, os.path.join(dest, "COPYING.txt"))
                break
    finally:
        subprocess.run(["hdiutil", "detach", mnt, "-force"], check=False)
    pdir = os.path.join(dest, "plugins")
    for n in os.listdir(pdir):
        if n.startswith(DROP_MAC) or n == "plugins.dat":
            os.remove(os.path.join(pdir, n))
    _note(dest, v)


def _note(dest, v):
    with open(os.path.join(dest, "README-VLC.txt"), "w") as f:
        f.write(f"This folder holds parts of VLC media player {v} by VideoLAN, used\n"
                f"under the GNU LGPL / GPL (see COPYING.txt). Source code:\n"
                f"https://download.videolan.org/pub/videolan/vlc/{v}/\n")
    total = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(dest) for f in fs)
    print(f"VLC {v} ready in {dest} ({total >> 20} MB)")


if __name__ == "__main__":
    which, dest = sys.argv[1], sys.argv[2]
    if os.path.exists(dest):
        shutil.rmtree(dest)
    {"windows": windows, "macos": macos}[which](dest)
