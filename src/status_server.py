# status_server.py — HTTP server for OBS widget + video proxy + chat bot API

import http.server
import json
import os
import tempfile
import time
import threading
import urllib.request
import urllib.error
from urllib.parse import urlparse
from typing import Optional, Callable

# The spool fetches googlevideo URLs resolved by audio_engine's yt-dlp, and a
# GVS URL is bound to the user-agent it was resolved under. This used to be an
# unrelated hardcoded Chrome string, which meant the spool was fetching with a
# UA that didn't match the resolve — a 403 waiting to happen. Import the one
# the engine uses so the two can't drift apart.
try:
    from audio_engine import HTTP_USER_AGENT as UA
except Exception:
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/15.5 Safari/605.1.15")

_SPOOL_DIR = os.path.join(tempfile.gettempdir(), "bloodbos_video_spool")

# How long POST /api/addsong holds the connection waiting for the real result.
# A name search that falls through several bad hits can take a while; bots
# must use an HTTP timeout LONGER than this (they use 120s).
ADDSONG_WAIT_SECONDS = 100


class _Spool:
    """One video, one upstream download, any number of local readers.

    A single thread pulls the googlevideo URL into a temp file; the /stream/
    handlers serve byte ranges out of that file, waiting for bytes that
    haven't arrived yet. The OBS widget and the local VLC window both read
    from here, so YouTube only ever sees ONE connection per video instead of
    two competing readers fighting the same throttle budget.
    """

    def __init__(self, video_id, resolver, upstream_get, drop_upstream):
        self.video_id = video_id
        self._resolver = resolver          # fn(video_id, force=False) -> url
        self._upstream_get = upstream_get
        self._drop_upstream = drop_upstream
        os.makedirs(_SPOOL_DIR, exist_ok=True)
        self.path = os.path.join(_SPOOL_DIR, f"{video_id}.bin")
        self.have = 0
        self.total = None                  # unknown until first response
        self.ctype = "video/mp4"
        self.done = False
        self.failed = False
        self.stopped = False
        self.cond = threading.Condition()
        self.last_access = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ── downloader ──

    # Download in ranged chunks, the way browsers and yt-dlp do. googlevideo
    # SPEED-CAPS a single continuous connection after the first ~10MB — a
    # plain open-ended GET crawls below realtime, so the spool's leading edge
    # fell behind playback near the end of a song (stutter) and the players
    # lagged behind the audio (delay). Fresh Range requests over one warm
    # keep-alive connection reset that cap each chunk and keep full speed.
    CHUNK = 10 * 1024 * 1024

    def _run(self):
        force = False
        errors = 0
        t0 = time.time()
        try:
            f = open(self.path, "wb")
        except Exception:
            self._fail()
            return
        with f:
            while not self.stopped:
                if self.total is not None and self.have >= self.total:
                    self._finish(t0)
                    return
                try:
                    url = self._resolver(self.video_id, force)
                except TypeError:
                    url = self._resolver(self.video_id)
                force = False
                if not url:
                    print(f"[Spool] {self.video_id}: resolver returned no URL "
                          f"(force={force})")
                    self._fail()
                    return
                try:
                    headers = {
                        "User-Agent": UA,
                        "Accept": "*/*",
                        "Referer": "https://www.youtube.com/",
                        "Origin": "https://www.youtube.com",
                    }
                    chunk_end = self.have + self.CHUNK - 1
                    if self.total is not None:
                        chunk_end = min(chunk_end, self.total - 1)
                    headers["Range"] = f"bytes={self.have}-{chunk_end}"
                    resp = self._upstream_get(url, headers)

                    if resp.status in (403, 410, 404):
                        print(f"[Spool] {self.video_id}: HTTP {resp.status} on "
                              f"Range {headers.get('Range')} "
                              f"(error {errors + 1}/5) — re-resolving")
                        try:
                            resp.read()
                        except Exception:
                            self._drop_upstream()
                        errors += 1
                        if errors > 4:
                            self._fail()
                            return
                        force = True       # URL is dead — fresh yt-dlp resolve
                        time.sleep(1.0)
                        continue
                    if resp.status == 416 and self.total is not None \
                            and self.have >= self.total:
                        self._finish()
                        return
                    if resp.status not in (200, 206):
                        print(f"[Spool] {self.video_id}: unexpected HTTP "
                              f"{resp.status} on Range {headers.get('Range')} "
                              f"(error {errors + 1}/5)")
                        try:
                            resp.read()
                        except Exception:
                            self._drop_upstream()
                        errors += 1
                        if errors > 4:
                            self._fail()
                            return
                        time.sleep(1.0)
                        continue

                    # Server ignored our resume Range and restarted from 0.
                    if resp.status == 200 and self.have > 0:
                        f.seek(0)
                        f.truncate()
                        with self.cond:
                            self.have = 0
                            self.cond.notify_all()

                    # Learn the full size.
                    if self.total is None:
                        cr = resp.headers.get("Content-Range", "")
                        if "/" in cr:
                            try:
                                self.total = int(cr.rsplit("/", 1)[1])
                            except Exception:
                                pass
                        if self.total is None:
                            cl = resp.headers.get("Content-Length")
                            if cl:
                                try:
                                    self.total = self.have + int(cl)
                                except Exception:
                                    pass
                        ct = resp.headers.get("Content-Type")
                        if ct and ct.startswith(("video/", "audio/",
                                                 "application/")):
                            self.ctype = ct
                        with self.cond:
                            self.cond.notify_all()

                    while not self.stopped:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        f.flush()
                        with self.cond:
                            self.have += len(chunk)
                            self.cond.notify_all()
                    if self.stopped:
                        try:
                            resp.close()
                        except Exception:
                            pass
                        self._drop_upstream()
                        return
                    if self.total is not None and self.have >= self.total:
                        self._finish(t0)
                        return
                    if self.total is None:
                        # No length info at all: EOF is the whole file.
                        with self.cond:
                            self.total = self.have
                        self._finish(t0)
                        return
                    if self.have > chunk_end:
                        errors = 0    # got a full chunk — healthy, next chunk
                        continue
                    # Short read — connection dropped mid-chunk. The next pass
                    # resumes from the exact byte offset.
                    errors += 1
                    print(f"[Spool] {self.video_id}: short read, have "
                          f"{self.have} of {self.total} "
                          f"(error {errors}/9) — resuming")
                    if errors > 8:
                        self._fail()
                        return
                    self._drop_upstream()
                    time.sleep(0.5)
                except Exception as e:
                    self._drop_upstream()
                    errors += 1
                    print(f"[Spool] {self.video_id}: {type(e).__name__}: {e} "
                          f"(error {errors}/9, have {self.have})")
                    if errors > 8:
                        self._fail()
                        return
                    time.sleep(1.0)

    def _finish(self, t0=None):
        with self.cond:
            self.done = True
            self.cond.notify_all()
        if t0:
            dt = max(0.001, time.time() - t0)
            print(f"[Spool] {self.video_id}: complete "
                  f"({self.have // 1024} KB in {dt:.1f}s, "
                  f"{self.have / dt / 1048576:.1f} MB/s)")
        else:
            print(f"[Spool] {self.video_id}: complete "
                  f"({self.have // 1024} KB)")

    def _fail(self):
        with self.cond:
            self.failed = True
            self.cond.notify_all()
        print(f"[Spool] {self.video_id}: download FAILED")

    # ── reader side ──

    def wait_ready(self, timeout=50.0):
        """Block until we know the total size (or failure)."""
        deadline = time.time() + timeout
        with self.cond:
            while self.total is None and not self.failed and not self.stopped:
                left = deadline - time.time()
                if left <= 0:
                    return False
                self.cond.wait(min(left, 1.0))
        return self.total is not None and not self.failed

    def wait_for(self, offset, timeout=60.0):
        """Block until byte `offset` exists on disk. False on stall/fail."""
        deadline = time.time() + timeout
        with self.cond:
            while (self.have <= offset and not self.done
                   and not self.failed and not self.stopped):
                left = deadline - time.time()
                if left <= 0:
                    return False
                self.cond.wait(min(left, 1.0))
            return self.have > offset

    def close(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()
        try:
            os.remove(self.path)
        except Exception:
            pass  # a reader may still hold it open (Windows); startup cleanup


def _clean_spool_dir():
    """Delete leftovers from previous runs."""
    try:
        for name in os.listdir(_SPOOL_DIR):
            try:
                os.remove(os.path.join(_SPOOL_DIR, name))
            except Exception:
                pass
    except Exception:
        pass


class StatusServer:
    """HTTP server: status API, video stream proxy, chat bot API"""

    def __init__(self, player_status_fn, queue_fn, get_video_url_fn,
                 get_theme_fn=None,
                 on_addsong=None, on_skip=None, on_back=None,
                 on_play=None, on_pause=None, on_volume=None,
                 on_song_query=None, on_queue_query=None,
                 port: int = 9111):
        self._port = port
        self._get_status = player_status_fn
        self._get_queue = queue_fn
        self._get_video_url = get_video_url_fn
        self._get_theme = get_theme_fn

        # Chat bot API callbacks
        self.on_addsong = on_addsong
        self.on_skip = on_skip
        self.on_back = on_back
        self.on_play = on_play
        self.on_pause = on_pause
        self.on_volume = on_volume
        self.on_song_query = on_song_query
        self.on_queue_query = on_queue_query

        self._server = None
        self._thread = None
        # Per-thread keep-alive connections to googlevideo. OBS's Chromium
        # issues LOTS of Range requests while playing an mp4; the old code
        # opened a brand-new TCP+TLS connection to YouTube for every single one
        # (urllib does no keep-alive). Each handshake added 100-300ms of dead
        # air right when the player needed the next chunk -> stutter. Reusing
        # one warm connection per handler thread removes that latency.
        self._tls = threading.local()
        # Spool registry: video_id -> _Spool. One upstream download per video,
        # shared by every local reader (OBS widget + local VLC window).
        self._spools = {}
        self._spool_lock = threading.Lock()
        _clean_spool_dir()

    def _get_spool(self, video_id):
        with self._spool_lock:
            sp = self._spools.get(video_id)
            if sp is not None and not sp.failed and not sp.stopped:
                sp.last_access = time.time()
                return sp
            sp = _Spool(video_id, self._get_video_url,
                        self._upstream_get, self._drop_upstream)
            self._spools[video_id] = sp
            # Keep current + previous (covers prefetch / quick Back presses);
            # evict anything older so disk use stays bounded.
            if len(self._spools) > 2:
                oldest = sorted(self._spools.items(),
                                key=lambda kv: kv[1].last_access)
                for vid, old in oldest[:len(self._spools) - 2]:
                    if vid != video_id:
                        old.close()
                        self._spools.pop(vid, None)
            return sp

    def _drop_upstream(self):
        """Close and forget this thread's upstream connection."""
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._tls.conn = None
        self._tls.host = None

    def _upstream_get(self, url: str, headers: dict, max_redirects: int = 5):
        """GET over a per-thread keep-alive connection, following redirects.

        Returns an http.client.HTTPResponse (streamable via .read(n)).
        Raises on connection-level failure so the caller's retry logic runs.
        """
        import http.client
        for _ in range(max_redirects + 1):
            u = urlparse(url)
            host = u.netloc
            conn = getattr(self._tls, "conn", None)
            if conn is None or getattr(self._tls, "host", None) != host:
                self._drop_upstream()
                if u.scheme == "https":
                    conn = http.client.HTTPSConnection(host, timeout=15)
                else:
                    conn = http.client.HTTPConnection(host, timeout=15)
                self._tls.conn = conn
                self._tls.host = host
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
            except Exception:
                # Stale keep-alive connection (server closed it) — one clean
                # rebuild before giving up to the caller.
                self._drop_upstream()
                if u.scheme == "https":
                    conn = http.client.HTTPSConnection(host, timeout=15)
                else:
                    conn = http.client.HTTPConnection(host, timeout=15)
                self._tls.conn = conn
                self._tls.host = host
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                try:
                    resp.read()  # drain so the connection stays reusable
                except Exception:
                    self._drop_upstream()
                if not loc:
                    return resp
                url = loc
                continue
            return resp
        return resp

    def start(self):
        handler_factory = self._make_handler()
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self._port), handler_factory
        )
        self._server.handle_error = lambda req, addr: None
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"[StatusServer] http://localhost:{self._port}")

    def stop(self):
        with self._spool_lock:
            for sp in self._spools.values():
                sp.close()
            self._spools.clear()
        if self._server:
            self._server.shutdown()

    def _make_handler(self):
        get_status = self._get_status
        get_queue = self._get_queue
        get_video_url = self._get_video_url
        srv = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Cache-Control", "no-cache")

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):
                path = urlparse(self.path).path

                # ── Status API ──
                if path == "/status":
                    data = get_status()
                    self._json_response(data)
                    return

                # ── Queue API ──
                if path == "/queue":
                    data = get_queue()
                    self._json_response(data)
                    return

                # ── Theme API ──
                if path == "/theme":
                    if srv._get_theme:
                        self._json_response(srv._get_theme())
                    else:
                        self._json_response({})
                    return

                # ── Video stream (served from the shared local spool) ──
                if path.startswith("/stream/"):
                    video_id = path.split("/stream/", 1)[1].strip("/")
                    if not video_id:
                        self._error(400, "Missing video ID")
                        return
                    sp = srv._get_spool(video_id)
                    if not sp.wait_ready():
                        self._error(502, f"Cannot resolve {video_id}")
                        return
                    total = sp.total

                    # Parse Range: bytes=start-end | bytes=start- | bytes=-N
                    start, end = 0, total - 1
                    ranged = False
                    rh = self.headers.get("Range", "")
                    if rh.startswith("bytes="):
                        ranged = True
                        spec = rh[6:].split(",")[0].strip()
                        try:
                            if spec.startswith("-"):
                                n = int(spec[1:])
                                start = max(0, total - n)
                            else:
                                a, _, b = spec.partition("-")
                                start = int(a)
                                if b:
                                    end = min(int(b), total - 1)
                        except ValueError:
                            ranged = False
                            start, end = 0, total - 1
                    if start >= total:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self._cors()
                        self.end_headers()
                        return

                    self.send_response(206 if ranged else 200)
                    self.send_header("Content-Type", sp.ctype)
                    self.send_header("Content-Length", str(end - start + 1))
                    if ranged:
                        self.send_header("Content-Range",
                                         f"bytes {start}-{end}/{total}")
                    self.send_header("Accept-Ranges", "bytes")
                    self._cors()
                    self.end_headers()

                    try:
                        with open(sp.path, "rb") as f:
                            pos = start
                            while pos <= end:
                                if not sp.wait_for(pos):
                                    break   # download stalled/failed
                                f.seek(pos)
                                n = min(262144, end - pos + 1, sp.have - pos)
                                if n <= 0:
                                    continue
                                data = f.read(n)
                                if not data:
                                    break
                                try:
                                    self.wfile.write(data)
                                except (BrokenPipeError, ConnectionResetError,
                                        ConnectionAbortedError, OSError):
                                    return   # client seeked/closed — normal
                                pos += len(data)
                                sp.last_access = time.time()
                    except (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError, OSError):
                        return
                    except Exception as e:
                        print(f"[Spool] serve error: {e}")
                    return


                # ── Widget HTML ──
                # A widget.html in the data folder (your own design) wins over
                # the one that ships with the app.
                if path in ("/", "/widget", "/widget.html"):
                    import app_paths
                    for fp in (os.path.join(app_paths.data_dir(), "widget.html"),
                               app_paths.resource("widget.html")):
                        if os.path.isfile(fp):
                            self.send_response(200)
                            self.send_header("Content-Type", "text/html; charset=utf-8")
                            self._cors()
                            self.end_headers()
                            with open(fp, "rb") as f:
                                self.wfile.write(f.read())
                            return
                    self._error(404, "widget.html not found")
                    return

                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                path = urlparse(self.path).path
                cl = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(cl).decode() if cl > 0 else "{}"

                # Control requests may come from local tools (no Origin header)
                # or from the widget itself — never from some other website the
                # streamer happens to have open in a browser.
                origin = (self.headers.get("Origin") or "").rstrip("/")
                if origin and origin not in (f"http://localhost:{srv._port}",
                                             f"http://127.0.0.1:{srv._port}"):
                    self._json_response({"ok": False, "error": "forbidden origin"}, 403)
                    return

                # ── Chat bot API ──
                if path == "/api/addsong":
                    try:
                        data = json.loads(body)
                        text = data.get("text", "").strip()
                        user = data.get("user", "chat")
                        platform = data.get("platform", "api")
                        if not text:
                            self._json_response({"ok": False, "error": "No text"}, 400)
                            return
                        if not srv.on_addsong:
                            self._json_response({"ok": False, "error": "Not available"}, 503)
                            return

                        # By default the request is held open until the song
                        # is actually queued or has definitely failed, and the
                        # JSON carries a ready-to-post chat "message". This used
                        # to answer "Queued" instantly, before any search ran,
                        # so no bot could ever tell chat what really happened.
                        # Send {"wait": false} for the old fire-and-forget reply.
                        if data.get("wait", True) is False:
                            srv.on_addsong(text, user, platform)
                            self._json_response({"ok": True, "status": "accepted",
                                                 "message": ""})
                            return

                        done = threading.Event()
                        box = {}

                        def on_done(result):
                            box["result"] = result
                            done.set()

                        try:
                            srv.on_addsong(text, user, platform, on_done=on_done)
                        except TypeError:
                            # Player without outcome support — old behaviour.
                            srv.on_addsong(text, user, platform)
                            self._json_response({"ok": True, "status": "accepted",
                                                 "message": ""})
                            return

                        if done.wait(ADDSONG_WAIT_SECONDS):
                            self._json_response(box["result"])
                        else:
                            shown = text if len(text) <= 60 else text[:59] + "…"
                            self._json_response({
                                "ok": False, "status": "pending",
                                "message": f"⏳ Still looking for “{shown}” "
                                           f"— it'll be added if it's found. "
                                           f"Check !queue in a minute.",
                            })
                    except Exception as e:
                        self._json_response({"ok": False, "error": str(e)}, 500)
                    return

                if path == "/api/skip":
                    if srv.on_skip:
                        srv.on_skip()
                    self._json_response({"ok": True})
                    return

                if path == "/api/back":
                    if srv.on_back:
                        srv.on_back()
                    self._json_response({"ok": True})
                    return

                if path == "/api/play":
                    if srv.on_play:
                        srv.on_play()
                    self._json_response({"ok": True})
                    return

                if path == "/api/pause":
                    if srv.on_pause:
                        srv.on_pause()
                    self._json_response({"ok": True})
                    return

                if path == "/api/volume":
                    try:
                        data = json.loads(body)
                        level = int(data.get("level", 50))
                        if srv.on_volume:
                            srv.on_volume(level)
                        self._json_response({"ok": True, "level": level})
                    except Exception as e:
                        self._json_response({"ok": False, "error": str(e)}, 400)
                    return

                # Widget video-ended callback
                if path == "/video-ended":
                    self._json_response({"ok": True})
                    return

                self.send_response(404)
                self.end_headers()

            def _json_response(self, data, code=200):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def _error(self, code, msg):
                self.send_response(code)
                self.send_header("Content-Type", "text/plain")
                self._cors()
                self.end_headers()
                self.wfile.write(msg.encode())

        return Handler
