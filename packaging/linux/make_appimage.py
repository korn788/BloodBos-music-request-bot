#!/usr/bin/env python3
# make_appimage.py — the Linux AppImage: one file, VLC inside, nothing to install.
#
#   python3 packaging/linux/make_appimage.py dist/bloodbos-radio \
#       dist/BLOODBOS-Radio-x86_64.AppImage --appimagetool ./appimagetool.AppImage
#
# Takes the PyInstaller app (dist/bloodbos-radio) and adds:
#   vlc/                 this computer's libvlc + the plugins the player uses
#   vlc/certs/           trusted certificates for VLC's https (see vlc_loader)
#   _internal/...        every library the app or VLC needs that a desktop
#                        Linux can't be counted on to have
# then packs it with appimagetool. What a desktop Linux CAN be counted on to
# have is the AppImage project's list (EXCLUDE below: glibc, OpenGL, X11,
# ALSA, fonts, ...): those must come from the computer it runs on.
#
# Build on the oldest Linux it should run on (CI: ubuntu-22.04), with VLC
# installed (vlc-plugin-base vlc-plugin-video-output libvlc-bin), the X11
# libraries Qt's display plugin uses (the libxcb-* list in install.sh), and
# patchelf. Every library they need gets packed, so they must be installed.

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys

LIBDIR = "/usr/lib/x86_64-linux-gnu"
VLC_PLUGINS = os.path.join(LIBDIR, "vlc", "plugins")
CACHE_GEN = os.path.join(LIBDIR, "vlc", "vlc-cache-gen")
APP_ID = "bloodbos-radio"

# Libraries taken from the computer it runs on, never packed (see the file).
HERE = os.path.dirname(os.path.abspath(__file__))
EXCLUDE = {line.strip() for line in open(os.path.join(HERE, "appimage-excludelist.txt"))
           if line.strip() and not line.startswith("#")}

# Qt plugins the PyInstaller app carries that the player never uses and that
# would drag whole toolkits in: the GTK theme (all of GTK 3), VNC remote
# display, TUIO touch input.
DROP_QT = ("platformthemes/libqgtk3.so", "platforms/libqvnc.so",
           "generic/libqtuiotouchplugin.so")

# VLC plugin folders the player never uses (VLC's own interfaces, network
# services, recording and streaming out, hardware-decoding glue).
DROP_DIRS = {"gui", "control", "services_discovery", "stream_out", "access_output", "mux",
             "visualization", "video_splitter", "lua", "meta_engine", "stream_extractor",
             "vaapi", "vdpau", "notify"}
# Single plugins the player never uses — discs, TV cards, cameras, encoders,
# Wayland-only outputs, ... — most of them with big libraries of their own.
# (ffmpeg's demuxer "avformat" and "avio" too: VLC's own demuxers read
# everything YouTube and SoundCloud send, and libavformat alone would drag in
# Blu-ray, SSH, SRT, ZeroMQ... libraries.)
DROP = {
    "access": "access_alsa access_mms access_mtp access_realrtsp avio cdda dc1394 dtv dv1394 "
              "dvb dvdnav dvdread libbluray linsys_hdsdi linsys_sdi nfs pulsesrc rist rtp "
              "satip sdp sftp shm smb srt timecode v4l2 vcd vdr vnc rdp xcb_screen jack "
              "decklink",
    "codec": "aribsub kate libass svgdec x264 x26410b x265 shine twolame zvbi oggspots "
             "fluidsynth gstdecode crystalhd telx aom daala schroedinger qsv",
    "demux": "avformat demux_chromecast sid mod gme",
    "misc": "addonsfsstorage addonsvorepository audioscrobbler dbus_screensaver export "
            "fingerprinter stats vod_rtsp xdg_screensaver",
    "keystore": "kwallet secret",
    "stream_filter": "decomp record",
    "text_renderer": "svg",
    "video_output": "aa caca fb flaschen egl_wl glconv_vaapi_drm glconv_vaapi_wl "
                    "glconv_vaapi_x11 glconv_vdpau wl_shell wl_shm xdg_shell kms evas",
    "video_filter": "opencv_example opencv_wrapper remoteosd",
}
DROP = {k: set(v.split()) for k, v in DROP.items()}

APPRUN = """#!/bin/sh
# AppImage entry point: start the app that's inside this AppImage.
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/bloodbos-radio/bloodbos-radio" "$@"
"""

DESKTOP = """[Desktop Entry]
Type=Application
Name=BLOODBOS Radio
GenericName=Stream song requests
Comment=Song-request radio for Twitch, Kick and Owncast streams
Exec=bloodbos-radio
Icon=bloodbos-radio
Terminal=false
Categories=AudioVideo;Audio;Player;
Keywords=music;stream;twitch;kick;owncast;obs;youtube;soundcloud;
"""


def run(cmd, **kw):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def is_elf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def elf_files(top):
    for root, _dirs, files in os.walk(top):
        for name in files:
            p = os.path.join(root, name)
            if not os.path.islink(p) and is_elf(p):
                yield p


def plugin_name(filename):
    m = re.match(r"lib(.+)_plugin\.so$", filename)
    return m.group(1) if m else None


def copy_vlc(vlc_dir):
    os.makedirs(vlc_dir)
    for so in ("libvlc.so.5", "libvlccore.so.9"):
        shutil.copy2(os.path.realpath(os.path.join(LIBDIR, so)), os.path.join(vlc_dir, so))
    kept = dropped = 0
    for cat in sorted(os.listdir(VLC_PLUGINS)):
        src = os.path.join(VLC_PLUGINS, cat)
        if not os.path.isdir(src):
            continue
        for name in sorted(os.listdir(src)):
            plug = plugin_name(name)
            if not plug:
                continue
            if cat in DROP_DIRS or plug in DROP.get(cat, ()):
                dropped += 1
                continue
            os.makedirs(os.path.join(vlc_dir, "plugins", cat), exist_ok=True)
            shutil.copy2(os.path.join(src, name), os.path.join(vlc_dir, "plugins", cat, name))
            kept += 1
    print(f"VLC: kept {kept} plugins, left out {dropped}")


def ldd(path, search):
    """{soname: resolved path or None} for everything `path` loads."""
    env = dict(os.environ, LD_LIBRARY_PATH=":".join(search))
    out = subprocess.run(["ldd", path], capture_output=True, text=True, env=env).stdout
    deps = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\S+)\s+=>\s+(not found|\S+)", line)
        if m:
            deps[m.group(1)] = None if m.group(2) == "not found" else m.group(2)
    return deps


def bundle_libraries(app, internal, vlc_dir):
    """Copy into _internal every library some file in the app needs that is
    neither in the app already nor on the always-there list."""
    search = [internal, vlc_dir, os.path.join(LIBDIR, "vlc")]
    have = {name for name in os.listdir(internal)}
    have |= set(os.listdir(vlc_dir))
    added, missing = [], {}
    todo = list(elf_files(app))
    seen = set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for soname, found in ldd(path, search).items():
            if soname in EXCLUDE or soname in have:
                continue
            if found is None:
                missing.setdefault(soname, []).append(os.path.relpath(path, app))
                continue
            if os.path.realpath(found).startswith(os.path.realpath(app) + os.sep):
                continue
            dest = os.path.join(internal, soname)
            shutil.copy2(os.path.realpath(found), dest)
            have.add(soname)
            added.append(soname)
            todo.append(dest)
    fatal = False
    for soname, users in sorted(missing.items()):
        # VLC and Qt's X11 display plugin must be complete. The PyInstaller
        # app has always carried a few optional Qt plugins for things this
        # build machine doesn't have (they're never loaded): only reported.
        vlc_user = any(u.startswith("vlc" + os.sep) or "xcb" in os.path.basename(u).lower()
                       for u in users)      # VLC, or Qt's X11 display plugin
        fatal |= vlc_user
        print(f"{'MISSING' if vlc_user else 'note: not found'} {soname} "
              f"(needed by {', '.join(users[:3])})", file=sys.stderr)
    if fatal:
        raise SystemExit("VLC needs libraries this build machine doesn't have")
    # For the AppImage test in CI: these are known to be missing (optional Qt
    # plugins), anything else missing on a test computer is a packing mistake.
    with open(os.path.join(app, "appimage-optional-missing.txt"), "w") as f:
        f.write("".join(name + "\n" for name in sorted(missing)))
    print(f"added {len(added)} libraries: {' '.join(sorted(added))}")
    return added


def set_runpaths(internal, vlc_dir, added):
    # The PyInstaller launcher points LD_LIBRARY_PATH at _internal anyway, but
    # VLC's files shouldn't depend on that: give them their own search paths.
    for so in added:
        run(["patchelf", "--set-rpath", "$ORIGIN", os.path.join(internal, so)])
    for so in ("libvlc.so.5", "libvlccore.so.9"):
        run(["patchelf", "--set-rpath", "$ORIGIN:$ORIGIN/../_internal", os.path.join(vlc_dir, so)])
    plugins = os.path.join(vlc_dir, "plugins")
    for path in elf_files(plugins):
        subprocess.run(["patchelf", "--set-rpath", "$ORIGIN/../..:$ORIGIN/../../../_internal",
                        path], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("app", help="the PyInstaller folder (dist/bloodbos-radio)")
    ap.add_argument("out", help="the .AppImage to write")
    ap.add_argument("--appimagetool", default="appimagetool")
    ap.add_argument("--icon", default="src/assets/icon.png")
    ap.add_argument("--workdir", default="build/AppDir")
    ap.add_argument("--appdir-only", action="store_true",
                    help="stop before packing: build/AppDir/AppRun runs the app as-is")
    a = ap.parse_args()

    appdir = os.path.abspath(a.workdir)
    if os.path.exists(appdir):
        shutil.rmtree(appdir)
    app = os.path.join(appdir, "usr", "lib", APP_ID)
    shutil.copytree(a.app, app, symlinks=True)
    internal = os.path.join(app, "_internal")
    vlc_dir = os.path.join(app, "vlc")
    for rel in DROP_QT:
        p = os.path.join(internal, "PyQt6", "Qt6", "plugins", rel)
        if os.path.exists(p):
            os.remove(p)

    copy_vlc(vlc_dir)
    certs = os.path.join(vlc_dir, "certs")
    os.makedirs(certs)
    cacert = os.path.join(internal, "certifi", "cacert.pem")
    if not os.path.isfile(cacert):
        raise SystemExit("certifi's cacert.pem isn't in the app")
    shutil.copy2(cacert, os.path.join(certs, "ca-certificates.crt"))

    added = bundle_libraries(app, internal, vlc_dir)
    set_runpaths(internal, vlc_dir, added)
    # VLC reads its plugin list from this cache; an AppImage is read-only, so
    # without it every start would scan all the plugins again.
    run([CACHE_GEN, os.path.join(vlc_dir, "plugins")])

    with open(os.path.join(appdir, "AppRun"), "w") as f:
        f.write(APPRUN)
    os.chmod(os.path.join(appdir, "AppRun"), 0o755)
    with open(os.path.join(appdir, APP_ID + ".desktop"), "w") as f:
        f.write(DESKTOP)
    shutil.copy2(a.icon, os.path.join(appdir, APP_ID + ".png"))
    os.symlink(APP_ID + ".png", os.path.join(appdir, ".DirIcon"))

    if a.appdir_only:
        print(f"AppDir ready: {appdir} (run {appdir}/AppRun)")
        return
    tool = a.appimagetool
    if os.path.isfile(tool):
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IXUSR)
    env = dict(os.environ, ARCH="x86_64", APPIMAGE_EXTRACT_AND_RUN="1")
    run([tool, "--no-appstream", appdir, a.out], env=env)
    print(f"made {a.out} ({os.path.getsize(a.out) >> 20} MB)")


if __name__ == "__main__":
    main()
