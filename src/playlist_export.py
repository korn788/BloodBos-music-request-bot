# playlist_export.py — keep a log of every song a stream played, and optionally
# publish it to a self-hosted playlist web page
#
# Two parts:
#   SessionLog      Every song that actually starts playing is written to
#                   stream_session.json the moment it plays. Unlike the queue
#                   history (capped at 100, used by the Back button), this keeps
#                   the whole stream in order, survives crashes/restarts, and
#                   resets when you choose "Start new session".
#   export_session  Sends that log to the playlist site. Pressing Export again
#                   for the same session updates the same playlist.
#
# Publishing is optional and only for people who run their own playlist site.
# It's switched on by a playlist_site.json file in the data folder:
#   {"endpoint": "https://your.server/playlist/api/export",
#    "token": "<PLAYLIST_TOKEN from your server>",
#    "host_name": ""}      <- name shown for songs you add yourself ("You");
#                            blank = your Kick or Twitch channel name
# Without that file the Export button is hidden.

import datetime
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

import app_paths

HERE = app_paths.data_dir()
LOG_FILE = os.path.join(HERE, "stream_session.json")
SETTINGS_FILE = os.path.join(HERE, "playlist_site.json")
DEFAULT_ENDPOINT = ""
DEFAULT_SETTINGS = {"endpoint": DEFAULT_ENDPOINT, "token": "", "host_name": ""}


def site_configured():
    """True when a playlist site has been set up (shows the Export button)."""
    if not os.path.isfile(SETTINGS_FILE):
        return False
    return bool((load_settings().get("endpoint") or "").strip())


# ─────────────────────────────── settings ───────────────────────────────

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if v is not None})
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[Playlist] {SETTINGS_FILE} unreadable, using defaults: {e}")
    return s


def save_settings(s):
    try:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        print(f"[Playlist] could not save settings: {e}")


def channel_name():
    """The streamer's channel name from the app settings, if any."""
    try:
        with open(os.path.join(HERE, "settings.json"), "r", encoding="utf-8") as f:
            d = json.load(f)
        for sec in ("kick", "twitch"):
            ch = ((d.get(sec) or {}).get("channel") or "").strip()
            if ch:
                return ch
    except Exception:
        pass
    return ""


# ─────────────────────────────── session log ───────────────────────────────

def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class SessionLog:
    """Ordered list of every song that reached PLAYING in this stream session."""

    def __init__(self, path=LOG_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load() or self._fresh()

    @staticmethod
    def _fresh():
        return {"session_id": f"{datetime.date.today().isoformat()}-{uuid.uuid4().hex[:8]}",
                "started_at": _now_iso(), "title": "", "exported_url": "", "tracks": [],
                "failed": [],      # URLs that were queued but never started playing
                "removed": []}     # URLs you removed — kept off the playlist

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("session_id") and isinstance(d.get("tracks"), list):
                return d
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[Playlist] session log unreadable, starting a new one: {e}")
        return None

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[Playlist] could not save session log: {e}")

    # public
    @property
    def session_id(self):
        return self._data["session_id"]

    @property
    def started_at(self):
        return self._data["started_at"]

    @property
    def title(self):
        return self._data.get("title") or ""

    @property
    def exported_url(self):
        return self._data.get("exported_url") or ""

    def count(self):
        with self._lock:
            return len(self._data["tracks"])

    def reset(self):
        """Start a new stream. The old log is archived to sessions/ first, so
        picking "Start new session" by mistake never destroys a playlist."""
        with self._lock:
            if self._data.get("tracks"):
                try:
                    arch = os.path.join(HERE, "sessions")
                    os.makedirs(arch, exist_ok=True)
                    with open(os.path.join(arch, f"{self._data['session_id']}.json"), "w",
                              encoding="utf-8") as f:
                        json.dump(self._data, f, indent=1)
                except Exception as e:
                    print(f"[Playlist] could not archive old session: {e}")
            self._data = self._fresh()
            self._save()
        print(f"[Playlist] new stream session {self.session_id}")

    def last_activity(self):
        """Unix time of the last logged play (or file write), 0 if unknown."""
        with self._lock:
            t = [int(e.get("played_at") or 0) for e in self._data["tracks"]]
        try:
            return max(t + [int(os.path.getmtime(self.path))])
        except Exception:
            return max(t + [0])

    def reset_if_stale(self, hours=6):
        """Used when the player starts with no saved session at all (first run,
        or the saved queue file was lost/corrupted in a crash). A log that saw
        a song in the last few hours is the stream you're still doing — keep it."""
        if not self.count() or time.time() - self.last_activity() > hours * 3600:
            self.reset()
            return True
        print(f"[Playlist] kept stream session {self.session_id} "
              f"({self.count()} songs) — last song was under {hours}h ago")
        return False

    def remove(self, url):
        """Take a song off this stream's playlist (every time it played)."""
        if not url:
            return 0
        with self._lock:
            before = len(self._data["tracks"])
            self._data["tracks"] = [e for e in self._data["tracks"] if e["url"] != url]
            rem = self._data.setdefault("removed", [])
            if url not in rem:
                rem.append(url)
            self._save()
            return before - len(self._data["tracks"])

    def mark_failed(self, track):
        """A song that was queued but never started playing. It's left out of
        the export even if it sits in the history list."""
        url = getattr(track, "url", "")
        if not url:
            return
        with self._lock:
            self._data.setdefault("failed", []).append(url)
            self._save()

    def set_title(self, title):
        with self._lock:
            self._data["title"] = title
            self._save()

    def set_exported(self, url):
        with self._lock:
            self._data["exported_url"] = url
            self._save()

    def record(self, track):
        """Log a track that just started playing. Pause/resume of the same
        track, or the Back button replaying it, doesn't add a second row."""
        if track is None or not getattr(track, "url", ""):
            return False
        if getattr(track, "_session_logged", None) == self.session_id:
            return False
        entry = track_entry(track)
        now = int(time.time())
        with self._lock:
            # Played again after being removed (someone re-requested it and you
            # let it play) — it counts again.
            rem = self._data.get("removed") or []
            if entry["url"] in rem:
                rem.remove(entry["url"])
        entry["played_at"] = now
        with self._lock:
            last = self._data["tracks"][-1] if self._data["tracks"] else None
            # Same song restarting right away = crash-resume or a retry, not a
            # second play. (A real repeat request comes after other songs.)
            if last and last["url"] == entry["url"] and \
                    now - int(last.get("played_at") or 0) < max(600, entry["duration"] + 120):
                try:
                    track._session_logged = self.session_id
                except Exception:
                    pass
                return False
            self._data["tracks"].append(entry)
            self._save()
        try:
            track._session_logged = self.session_id
            track._play_failed = False      # it did play after all (requeued)
        except Exception:
            pass
        return True

    def refresh_durations(self, track):
        """VLC often learns the real length after playback starts."""
        if track is None:
            return
        dur = int(getattr(track, "duration_seconds", 0) or 0)
        with self._lock:
            for e in reversed(self._data["tracks"][-5:]):
                if e["url"] == track.url and dur and e.get("duration") != dur:
                    e["duration"] = dur
                    self._save()
                    break

    def tracks(self):
        with self._lock:
            return [dict(t) for t in self._data["tracks"]]

    def earlier_tracks(self, history):
        """Songs in the player's history that the log doesn't have yet — ones
        that played before the log existed (e.g. right after updating), or
        before a resume. Returned oldest-first, as entries.

        A history track counts as logged if it's marked with this session, or
        if the log still has an unmatched entry for the same URL (restored
        history objects are fresh copies without the mark)."""
        from collections import Counter
        with self._lock:
            left = Counter(e["url"] for e in self._data["tracks"])
            failed = Counter(self._data.get("failed") or [])
            removed = set(self._data.get("removed") or [])
        earlier = []
        for t in history:
            url = getattr(t, "url", "")
            if not url or getattr(t, "pending", False):
                continue
            if getattr(t, "_session_logged", None) == self.session_id:
                left[url] -= 1
                continue
            if left[url] > 0:
                left[url] -= 1
                continue
            if getattr(t, "_play_failed", False) or url in removed:
                continue
            if failed[url] > 0:          # restored copy of a song that never played
                failed[url] -= 1
                continue
            earlier.append(t)
        return earlier

    def adopt_earlier(self, tracks):
        """Put earlier-played tracks at the front of the log (after a
        successful export) so they stay in the playlist even once they
        scroll out of the 100-song history."""
        if not tracks:
            return
        entries = [track_entry(t) for t in tracks]
        with self._lock:
            self._data["tracks"] = entries + self._data["tracks"]
            self._save()
        for t in tracks:
            try:
                t._session_logged = self.session_id
            except Exception:
                pass


def track_entry(track):
    source = getattr(track.source, "value", track.source) or "youtube"
    return {
        "url": track.url,
        "video_id": getattr(track, "video_id", "") or "",
        "source": source,
        "title": track.title or "",
        "artist": track.artist if (track.artist or "").lower() not in ("unknown", "…", "") else "",
        "artwork_url": getattr(track, "artwork_url", "") or "",
        "duration": int(getattr(track, "duration_seconds", 0) or 0),
        "requested_by": getattr(track, "requested_by", "") or "",
        "platform": getattr(track, "platform", "") or "",
    }


# ─────────────────────────────── export ───────────────────────────────

class ExportError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _post(endpoint, payload, token, timeout=150):
    req = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                 "User-Agent": "BLOODBOS-Radio/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or e.reason
        except Exception:
            msg = e.reason
        if e.code == 401:
            raise ExportError("the site rejected the token", 401)
        if e.code == 403:
            raise ExportError("the site is blocking exports (HTTP 403) — check the "
                              "server's export settings", 403)
        raise ExportError(f"HTTP {e.code}: {msg}", e.code)


def export_session(entries, settings, title, session_id, started_at, host_name=""):
    """POST the tracks. Returns the server result (has 'url'). Raises ExportError."""
    token = (settings.get("token") or "").strip()
    if not token:
        raise ExportError("no token set", 401)

    tracks = []
    for e in entries:
        t = dict(e)
        if host_name and (t.get("requested_by") or "").strip().lower() in ("you", ""):
            t["requested_by"] = host_name
        tracks.append(t)
    if not tracks:
        raise ExportError("no songs to export")

    payload = {"session_id": session_id, "title": title,
               "date": (started_at or _now_iso())[:10],
               "started_at": started_at, "ended_at": _now_iso(), "tracks": tracks}

    endpoint = (settings.get("endpoint") or DEFAULT_ENDPOINT).strip()
    if not endpoint:
        raise ExportError("no playlist site set up (playlist_site.json)")
    try:
        return _post(endpoint, payload, token)
    except urllib.error.URLError as e:
        # https not set up on the VPS? fall back to http once and remember it.
        if endpoint.startswith("https://"):
            alt = "http://" + endpoint[len("https://"):]
            try:
                res = _post(alt, payload, token)
            except urllib.error.URLError:
                raise ExportError(f"can't reach {endpoint} ({e.reason})")
            print("[Playlist] https unavailable — using http (token is sent unencrypted; "
                  "add a TLS certificate to the site to fix)")
            settings["endpoint"] = alt
            save_settings(settings)
            return res
        raise ExportError(f"can't reach {endpoint} ({e.reason})")
    except (TimeoutError, OSError) as e:
        raise ExportError(f"connection problem: {e}")


# ─────────────────────────────── blocklist ───────────────────────────────

BLOCK_FILE = os.path.join(HERE, "blocked_songs.json")


class Blocklist:
    """Songs that can never be requested again. Matched by URL and by YouTube
    video ID, so a different link to the same video is caught too.
    Stored in blocked_songs.json — delete an entry there to unblock it."""

    def __init__(self, path=BLOCK_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._items = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self._items = [i for i in (d.get("songs") if isinstance(d, dict) else d) or []
                           if isinstance(i, dict)]
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[Blocklist] {path} unreadable: {e}")

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"songs": self._items}, f, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[Blocklist] could not save: {e}")

    def add(self, track, reason=""):
        url = getattr(track, "url", "") or ""
        vid = getattr(track, "video_id", "") or ""
        with self._lock:
            if any((url and i.get("url") == url) or (vid and i.get("video_id") == vid) for i in self._items):
                return False
            self._items.append({"url": url, "video_id": vid, "title": getattr(track, "title", ""),
                                "artist": getattr(track, "artist", ""),
                                "requested_by": getattr(track, "requested_by", ""),
                                "blocked_at": _now_iso(), "reason": reason})
            self._save()
        return True

    def matches(self, url="", video_id=""):
        url = (url or "").split("?")[0] if "soundcloud.com" in (url or "") else (url or "")
        with self._lock:
            for i in self._items:
                if video_id and i.get("video_id") == video_id:
                    return i
                if url and i.get("url") and i["url"].split("?")[0] == url.split("?")[0]:
                    return i
        return None

    def count(self):
        with self._lock:
            return len(self._items)
