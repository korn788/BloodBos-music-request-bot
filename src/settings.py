# settings.py — the app's settings and saved logins
#
#   settings.json  choices you make in the app (channels, song length limit…)
#   tokens.json    logins and keys — readable only by your user account
#
# Both live in the data folder (see app_paths.py). Nothing secret is ever
# written to settings.json, so it's safe to share when asking for help.

import copy
import json
import os
import threading

import app_paths

DEFAULTS = {
    "first_run_done": False,
    "twitch": {"enabled": True, "channel": ""},
    "kick": {
        "enabled": True,
        "channel": "",
        # Filled in automatically the first time the channel is looked up, so
        # later starts don't depend on kick.com answering that lookup.
        "chatroom_id": 0,
        "broadcaster_user_id": 0,
        "channel_id": 0,
        # Advanced: read chat through a self-hosted webhook relay instead of
        # Kick's live chat feed (see extras/install_kick_relay.sh).
        "relay_url": "",
        "relay_verify": True,
    },
    "owncast": {"enabled": True, "server_url": ""},
    "player": {
        "port": 9111,
        "max_song_minutes": 10,
        # Song requests (!sr) are always open to everyone. These decide who
        # may use !skip, !back, !pause, !play, !volume and !clear.
        "anyone_can_control": False,
        # Use a browser's YouTube login for age-restricted videos:
        # "none", "firefox", "chrome", "edge", "brave", "opera", "chromium",
        # "vivaldi" or "safari".
        "cookies_browser": "none",
        # "" = the app keeps its own up-to-date copy of yt-dlp. Set a path
        # (or just "yt-dlp") to use one you manage yourself.
        "ytdlp_path": "",
        "video_window": True,
        "check_updates": True,
    },
    # Optional branding. theme.json in the data folder still works too.
    "app": {"name": "", "accent": "", "accent_2": ""},
}

SECRET_DEFAULTS = {
    "twitch": {},     # access_token, login, user_id, expires_at, scopes
    "kick": {},       # access_token, refresh_token, expires_at, scope, username,
                      # user_id, client_id/client_secret (your own Kick app)
    "owncast": {},    # access_token
    "kick_relay": {}, # relay_key (advanced)
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, path=None, secrets_path=None):
        self.path = path or app_paths.data_path("settings.json")
        self.secrets_path = secrets_path or app_paths.data_path("tokens.json")
        self._lock = threading.RLock()
        self._data = _merge(DEFAULTS, self._read(self.path))
        self._secrets = _merge(SECRET_DEFAULTS, self._read(self.secrets_path))
        self._listeners = []

    # ── io ──

    @staticmethod
    def _read(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            print(f"[Settings] {os.path.basename(path)} unreadable, using defaults: {e}")
            try:
                os.replace(path, path + ".broken")
            except OSError:
                pass
            return {}

    def save(self):
        with self._lock:
            data = json.dumps(self._data, indent=2)
            secrets = json.dumps(self._secrets, indent=2)
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
            app_paths.write_private(self.secrets_path, secrets)
        except Exception as e:
            print(f"[Settings] could not save: {e}")

    # ── change notifications ──

    def on_change(self, fn):
        self._listeners.append(fn)

    def _changed(self, section):
        for fn in list(self._listeners):
            try:
                fn(section)
            except Exception as e:
                print(f"[Settings] listener error: {e}")

    def notify(self, section):
        """Tell listeners a section changed (after a batch of notify=False edits)."""
        self._changed(section)

    # ── plain settings ──

    def get(self, section, key=None, default=None):
        with self._lock:
            sec = self._data.get(section)
            if key is None:
                return copy.deepcopy(sec) if sec is not None else default
            if isinstance(sec, dict):
                return copy.deepcopy(sec.get(key, default))
            return default

    def section(self, name):
        return self.get(name) or {}

    def update(self, section, values, notify=True):
        with self._lock:
            cur = self._data.get(section)
            if isinstance(cur, dict) and isinstance(values, dict):
                changed = any(cur.get(k) != v for k, v in values.items())
                cur.update(copy.deepcopy(values))
            else:
                changed = cur != values
                self._data[section] = copy.deepcopy(values)
        if changed:
            self.save()
            if notify:
                self._changed(section)
        return changed

    def set(self, section, key, value, notify=True):
        return self.update(section, {key: value}, notify=notify)

    # ── secrets ──

    def secret(self, section, key=None, default=None):
        with self._lock:
            sec = self._secrets.get(section) or {}
            if key is None:
                return copy.deepcopy(sec)
            return copy.deepcopy(sec.get(key, default))

    def set_secret(self, section, values, replace=False, notify=True):
        """values=None clears the section (sign out)."""
        with self._lock:
            if values is None:
                self._secrets[section] = {}
            elif replace:
                self._secrets[section] = copy.deepcopy(values)
            else:
                self._secrets.setdefault(section, {}).update(copy.deepcopy(values))
        self.save()
        if notify:
            self._changed(section)


# ═══════════════════════════════════════════════════════════════════════════
#  One-time import from the old folder layout (everything next to the .py
#  files, configs per bot). Only non-secret choices and your song data are
#  brought over; you sign in again with the Connect buttons.
# ═══════════════════════════════════════════════════════════════════════════

LEGACY_DATA_FILES = ("queue_state.json", "stream_session.json", "blocked_songs.json",
                     "theme.json", "youtube.json", "playlist_site.json")


def import_legacy(settings, folder):
    """Copy what's useful from an old install folder. Returns a list of notes."""
    import shutil
    notes = []
    if not folder or not os.path.isdir(folder):
        return notes
    if os.path.abspath(folder) == os.path.abspath(app_paths.data_dir()):
        return notes

    def load(*parts):
        p = os.path.join(folder, *parts)
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    for name in LEGACY_DATA_FILES:
        src = os.path.join(folder, name)
        dst = app_paths.data_path(name)
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
                notes.append(f"brought over {name}")
            except Exception:
                pass
    old_sessions = os.path.join(folder, "sessions")
    if os.path.isdir(old_sessions):
        dst_dir = app_paths.data_path("sessions", ".keep")
        dst_dir = os.path.dirname(dst_dir)
        for n in os.listdir(old_sessions):
            s, d = os.path.join(old_sessions, n), os.path.join(dst_dir, n)
            if n.endswith(".json") and not os.path.exists(d):
                try:
                    shutil.copy2(s, d)
                except Exception:
                    pass

    cc = load("channel_config.json") or {}
    if cc.get("channel") and not settings.get("kick", "channel"):
        settings.set("kick", "channel", str(cc["channel"]).strip().lower(), notify=False)
        notes.append("Kick channel")
    ks = load("chatbot", ".kick-settings.json") or {}
    if ks.get("chatroom_id") and not settings.get("kick", "chatroom_id"):
        settings.update("kick", {"chatroom_id": int(ks.get("chatroom_id") or 0),
                                 "broadcaster_user_id": int(ks.get("broadcaster_user_id") or 0)},
                        notify=False)
    oc = load("owncast_config.json") or load("chatbot", "owncast_config.json") or {}
    url = (oc.get("server_url") or "").strip()
    if url and "example.com" not in url and not settings.get("owncast", "server_url"):
        settings.set("owncast", "server_url", url, notify=False)
        tok = (oc.get("access_token") or "").strip()
        if tok and not tok.startswith("YOUR"):
            settings.set_secret("owncast", {"access_token": tok}, notify=False)
        notes.append("Owncast server")
    tc = load("twitch_config.json") or load("twitch", "twitch_config.json") or {}
    ch = (tc.get("channel_name") or "").strip()
    if ch and not ch.startswith("YOUR") and not settings.get("twitch", "channel"):
        settings.set("twitch", "channel", ch.lower(), notify=False)
        notes.append("Twitch channel")
    ka = load("kick_app.json") or {}
    if ka.get("relay_url") and ka.get("relay_key") and not settings.get("kick", "relay_url"):
        settings.set("kick", "relay_url", ka["relay_url"].strip(), notify=False)
        settings.set_secret("kick_relay", {"relay_key": ka["relay_key"].strip()}, notify=False)
        notes.append("Kick relay")
    return notes
