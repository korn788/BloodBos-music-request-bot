# tools.py — keeps the helper programs YouTube playback needs
#
#   yt-dlp  finds the stream for a YouTube / SoundCloud link or song name.
#           YouTube changes constantly, so an old yt-dlp stops working within
#           weeks. The app keeps its own copy in the data folder and updates it
#           once a day — nobody has to touch a terminal.
#   deno    a JavaScript runtime. Since late 2025 yt-dlp needs one to solve
#           YouTube's player challenges. If Deno (or Node 20+) is already
#           installed it's used; otherwise the app downloads Deno once.
#
# Everything here runs in the background. Song lookups wait (briefly) for it
# the very first time; after that the copies are already on disk.

import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

import app_paths

YTDLP_BASE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
DENO_BASE = "https://github.com/denoland/deno/releases/latest/download/"
UPDATE_EVERY = 24 * 3600
UA = "BLOODBOS-Radio (+https://github.com/%s)" % app_paths.REPO

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

_HIDE = {}
if IS_WIN:
    _si = subprocess.STARTUPINFO()
    _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _si.wShowWindow = 0
    _HIDE = {"startupinfo": _si}


def _arch():
    m = platform.machine().lower()
    if IS_MAC:
        try:     # an Intel build running under Rosetta still wants arm64 helpers
            r = subprocess.run(["sysctl", "-n", "hw.optional.arm64"],
                               capture_output=True, text=True, timeout=5)
            if r.stdout.strip() == "1":
                return "arm64"
        except Exception:
            pass
    if m in ("arm64", "aarch64", "armv8l"):
        return "arm64"
    return "x86_64"


def ytdlp_asset():
    if IS_WIN:
        return "yt-dlp.exe"
    if IS_MAC:
        return "yt-dlp_macos"
    return "yt-dlp_linux_aarch64" if _arch() == "arm64" else "yt-dlp_linux"


def deno_asset():
    a = _arch()
    if IS_WIN:
        return "deno-x86_64-pc-windows-msvc.zip"
    if IS_MAC:
        return "deno-aarch64-apple-darwin.zip" if a == "arm64" else "deno-x86_64-apple-darwin.zip"
    return ("deno-aarch64-unknown-linux-gnu.zip" if a == "arm64"
            else "deno-x86_64-unknown-linux-gnu.zip")


def _exe(name):
    return name + (".exe" if IS_WIN else "")


def _download(url, dest, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 16)
    os.replace(tmp, dest)


def _make_executable(path):
    if not IS_WIN:
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _version_of(cmd, flag="--version"):
    try:
        r = subprocess.run(cmd + [flag], capture_output=True, text=True, timeout=20, **_HIDE)
        if r.returncode == 0:
            return (r.stdout or r.stderr).strip().splitlines()[0]
    except Exception:
        pass
    return ""


class Tools:
    def __init__(self, settings=None, notice=None):
        self.settings = settings
        self._notice = notice or (lambda m: print(f"[Tools] {m}"))
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._ytdlp = None          # command list
        self._js = []               # extra yt-dlp args for the JS runtime
        self.status = "starting"
        self._state_file = os.path.join(app_paths.bin_dir(), "state.json")

    # ── public ──

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="tools").start()

    def wait_ready(self, timeout=120):
        return self._ready.wait(timeout)

    def ytdlp_cmd(self):
        with self._lock:
            if self._ytdlp:
                return list(self._ytdlp)
        return [self._configured_ytdlp() or shutil.which("yt-dlp") or "yt-dlp"]

    def js_runtime_args(self):
        with self._lock:
            return list(self._js)

    # ── internals ──

    def notice(self, msg):
        try:
            self._notice(msg)
        except Exception:
            print(f"[Tools] {msg}")

    def _load_state(self):
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, st):
        try:
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(st, f)
        except Exception:
            pass

    def _configured_ytdlp(self):
        p = ""
        if self.settings is not None:
            p = (self.settings.get("player", "ytdlp_path") or "").strip()
        if not p:
            return ""
        return shutil.which(p) or (p if os.path.isfile(p) else "")

    def _run(self):
        tries = 0
        while True:
            try:
                ok = self._setup()
            except Exception as e:
                ok = False
                self.notice(f"helper setup error: {type(e).__name__}: {e}")
            if ok:
                self.status = "ready"
                self._ready.set()
                return
            tries += 1
            # Let song lookups try whatever exists rather than wait forever.
            if tries >= 2:
                self._ready.set()
            time.sleep(min(600, 30 * tries))

    def _setup(self):
        # ── yt-dlp ──
        custom = self._configured_ytdlp()
        if custom:
            ytdlp = [custom]
            managed = False
        else:
            path = os.path.join(app_paths.bin_dir(), _exe("yt-dlp"))
            managed = True
            if not os.path.isfile(path):
                self.status = "downloading yt-dlp"
                self.notice("Getting YouTube support ready (one-time download)…")
                try:
                    _download(YTDLP_BASE + ytdlp_asset(), path)
                    _make_executable(path)
                    st = self._load_state()
                    st["ytdlp_updated"] = time.time()
                    self._save_state(st)
                except Exception as e:
                    sysw = shutil.which("yt-dlp")
                    if sysw:
                        self.notice(f"couldn't download yt-dlp ({e}); using the one on this computer")
                        path, managed = sysw, False
                    else:
                        self.notice(f"couldn't download yt-dlp — check the internet connection "
                                    f"({type(e).__name__}). Retrying shortly.")
                        return False
            ytdlp = [path]
        with self._lock:
            self._ytdlp = ytdlp

        # ── JavaScript runtime ──
        js = self._find_js_runtime()
        if js is None:
            js = self._download_deno()
        with self._lock:
            self._js = js or []
        if js is None:
            self.notice("couldn't get a JavaScript runtime for YouTube — some videos may "
                        "not play. Retrying in the background.")
            threading.Thread(target=self._retry_js, daemon=True, name="deno-retry").start()

        ver = _version_of(ytdlp)
        self.notice(f"YouTube support ready (yt-dlp {ver or '?'}"
                    f"{', ' + self._js_label() if self._js else ''})")

        if managed:
            threading.Thread(target=self._update_loop, args=(ytdlp,), daemon=True,
                             name="ytdlp-update").start()
        return True

    def _retry_js(self):
        delay = 120
        while True:
            time.sleep(delay)
            js = self._find_js_runtime() or self._download_deno()
            if js:
                with self._lock:
                    self._js = js
                self.notice("YouTube challenge solver ready")
                return
            delay = min(3600, delay * 2)

    def _js_label(self):
        try:
            name = self._js[1].split(":", 1)[0]
            return f"{name} for YouTube challenges"
        except Exception:
            return ""

    def _find_js_runtime(self):
        """--js-runtimes args for an existing runtime, or None."""
        mine = os.path.join(app_paths.bin_dir(), _exe("deno"))
        if os.path.isfile(mine):
            return ["--js-runtimes", f"deno:{mine}"]
        deno = shutil.which("deno")
        if deno:
            return ["--js-runtimes", f"deno:{deno}"]
        node = shutil.which("node")
        if node:
            v = _version_of([node])          # "v22.3.0"
            try:
                major = int(v.lstrip("v").split(".")[0])
            except ValueError:
                major = 0
            if major >= 22:
                return ["--js-runtimes", f"node:{node}"]
        return None

    def _download_deno(self):
        dest = os.path.join(app_paths.bin_dir(), _exe("deno"))
        self.status = "downloading deno"
        self.notice("Downloading the small JavaScript helper YouTube needs (one time)…")
        try:
            req = urllib.request.Request(DENO_BASE + deno_asset(), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r:
                blob = r.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                member = next(n for n in z.namelist()
                              if os.path.basename(n) == _exe("deno"))
                tmp = dest + ".part"
                with z.open(member) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
            os.replace(tmp, dest)
            _make_executable(dest)
            return ["--js-runtimes", f"deno:{dest}"]
        except Exception as e:
            self.notice(f"Deno download failed: {type(e).__name__}: {e}")
            return None

    def _update_loop(self, ytdlp):
        while True:
            st = self._load_state()
            due = time.time() - float(st.get("ytdlp_updated") or 0) > UPDATE_EVERY
            if due:
                try:
                    r = subprocess.run(ytdlp + ["-U", "--update-to", "stable"],
                                       capture_output=True, text=True, timeout=300, **_HIDE)
                    out = (r.stdout + r.stderr).strip().splitlines()
                    last = out[-1] if out else ""
                    if "Updated yt-dlp to" in (r.stdout + r.stderr):
                        self.notice(f"yt-dlp updated — {last}")
                    st["ytdlp_updated"] = time.time()
                    self._save_state(st)
                except Exception as e:
                    print(f"[Tools] yt-dlp update check failed: {e}")
            time.sleep(3600)
