# player_qt.py — the main window (PyQt6)
# A clean, resizable "control deck" UI for the live stream radio queue.
# Works with the audio engine + queue + status server + chat bots.
# Started by main.py.
#
# ──────────────────────────────────────────────────────────────────────────
#  What changed vs the old Winamp-skinned build
# ──────────────────────────────────────────────────────────────────────────
#  • The .wal/.wsz skin layer is gone. The window is a normal, resizable Qt
#    window with a hand-built dark theme — no skin files required.
#  • Queue handling was rewritten so a requested song can never be silently
#    dropped. A single advance authority (_advance_if_idle) starts playback,
#    and a _pending_start guard moves a song that fails to start into history
#    instead of letting the next request appear to "replace" it. Requests from
#    the search box and from chat (Twitch / Kick / website) go through the
#    exact same path, so neither can clobber the other.

import sys
import os
import json
import platform
import threading
import urllib.request
import time
from pathlib import Path

import app_paths

# VLC already backs the audio engine. We use a second, independent VLC player
# here for the music-video window; if python-vlc can't be imported the player
# still runs and simply won't show video.
try:
    import vlc
except Exception:
    vlc = None

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QLineEdit, QSlider, QMenu, QFrame, QSizePolicy,
    QVBoxLayout, QHBoxLayout, QGridLayout, QDialog, QInputDialog, QCheckBox,
    QDialogButtonBox,
)
from PyQt6.QtCore import Qt, QTimer, QSize, pyqtSignal, QObject
from PyQt6.QtGui import (
    QPixmap, QIcon, QColor, QFont, QPainter, QPainterPath, QBrush, QPen,
    QLinearGradient, QFontMetrics, QImage, QKeySequence, QShortcut,
)

from audio_engine import AudioEngine, Track, TrackSource, PlayerState
import vlc_loader
from queue_manager import QueueManager
from request_text import clean_request
from status_server import StatusServer
from playlist_export import (SessionLog, load_settings, save_settings,
                             export_session, ExportError, channel_name, track_entry,
                             Blocklist, site_configured)

try:
    from youtube_api import YouTubeAPI
except Exception:
    YouTubeAPI = None


# ═══════════════════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════════════════

APP_NAME = app_paths.APP_NAME        # wordmark shown top-left (Setup > theme.json can change it)
THEME_FILE = "theme.json"
QUEUE_STATE_FILE = "queue_state.json"
STATUS_PORT = 9111                   # from settings: player.port
MAX_SONG_MINUTES = 10                # from settings: player.max_song_minutes
PREFETCH_AHEAD = 2                   # pre-resolve this many upcoming tracks while one plays
# The in-app video window uses its OWN --no-audio VLC instance and never calls
# audio_set_mute, so it renders video silently without muting the engine's audio
# session in the Windows Volume Mixer (which was the earlier no-audio bug).
ENABLE_VIDEO_WINDOW = True           # from settings: player.video_window

# ── Video sync tuning ──────────────────────────────────────────────────────
SYNC_ENGAGE    = 0.25   # s — start correcting once drift exceeds this
SYNC_RELEASE   = 0.08   # s — stop correcting once drift is back under this
SYNC_TRIM      = 0.04   # ±4% playback-rate trim used to close small drift
SYNC_HARD      = 1.50   # s — beyond this, trimming is hopeless: seek instead
SYNC_SEEK_GAP  = 4.0    # s — minimum time between hard seeks
SYNC_JUMP      = 2.50   # s — audio-clock discontinuity (user seek) => seek now
SYNC_SETTLE    = 1.50   # s — grace period after a start/seek before judging

# Colour system. Spend the boldness on one place: the ember accent (the round
# play button + the seek line). Everything else stays quiet and dark.
THEME = {
    "bg":       "#0e0e12",   # window
    "panel":    "#16161d",   # cards
    "panel2":   "#1e1e27",   # inputs / list
    "stroke":   "#2a2a35",   # hairlines
    "text":     "#f3f3f6",
    "muted":    "#9b9bab",
    "faint":    "#6a6a78",
    "accent":   "#ff2e4d",   # ember / blood red
    "accent_2": "#ff5a3c",   # gradient partner for the accent
    "yt":       "#ff4b3e",   # YouTube chip text
    "sc":       "#ff9500",   # SoundCloud chip text
    "ok":       "#46d39a",   # "live" dot
    "warn":     "#ffb648",   # "paused/loading" dot
}


def configure(settings):
    """Pick up the user's settings (called by main.py, and again on changes)."""
    global STATUS_PORT, MAX_SONG_MINUTES, ENABLE_VIDEO_WINDOW, APP_NAME
    p = settings.section("player")
    try:
        STATUS_PORT = int(p.get("port") or 9111)
    except (TypeError, ValueError):
        STATUS_PORT = 9111
    try:
        MAX_SONG_MINUTES = max(1, int(p.get("max_song_minutes") or 10))
    except (TypeError, ValueError):
        MAX_SONG_MINUTES = 10
    ENABLE_VIDEO_WINDOW = bool(p.get("video_window", True))
    a = settings.section("app")
    if a.get("name"):
        APP_NAME = str(a["name"])
    for k in ("accent", "accent_2"):
        if a.get(k):
            THEME[k] = str(a[k])


def load_config():
    """Optional theme.json override: {"app": {"name": "...", "accent": "#..."}}"""
    global APP_NAME
    path = os.path.join(app_paths.data_dir(), THEME_FILE)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f).get("app", {})
            if cfg.get("name"):
                APP_NAME = str(cfg["name"])
            if cfg.get("accent"):
                THEME["accent"] = str(cfg["accent"])
            if cfg.get("accent_2"):
                THEME["accent_2"] = str(cfg["accent_2"])
        except Exception as e:
            print(f"[Config] theme.json ignored: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  Queue-state persistence (crash recovery) — unchanged behaviour
# ═══════════════════════════════════════════════════════════════════════════

def _state_path():
    return os.path.join(app_paths.data_dir(), QUEUE_STATE_FILE)


def save_queue_state(engine, queue):
    try:
        state = {"current": None, "current_elapsed": 0, "queue": [], "history": []}
        if engine.current_track:
            state["current"] = engine.current_track.to_dict()
            state["current_elapsed"] = engine.elapsed_seconds
        for t in queue.get_list():
            if getattr(t, "pending", False):
                continue      # still resolving; would restore as a dead row
            state["queue"].append(t.to_dict())
        for t in queue.get_history(limit=50):
            state["history"].append(t.to_dict())
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[QueueState] Save error: {e}")


def load_queue_state():
    try:
        p = _state_path()
        if not os.path.isfile(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[QueueState] Load error: {e}")
        return None


def dict_to_track(d):
    if not d:
        return None
    return Track(
        title=d.get("title", "Unknown"),
        artist=d.get("artist", "Unknown"),
        url=d.get("url", ""),
        stream_url="",            # stream URLs expire — force re-resolve
        video_stream_url="",
        source=TrackSource(d.get("source", "youtube")),
        video_id=d.get("video_id", ""),
        artwork_url=d.get("artwork_url", ""),
        duration_seconds=d.get("duration_seconds", 0),
        requested_by=d.get("requested_by", ""),
        is_music_video=d.get("is_music_video", False),
        album=d.get("album", ""),
        platform=d.get("platform", ""),
    )


def _relative_time(dt):
    """A short, human phrase like 'just now' / '12 minutes ago' / 'Jun 26 at 3:42 PM'."""
    try:
        import datetime
        s = (datetime.datetime.now() - dt).total_seconds()
        if s < 0:
            s = 0
        if s < 90:
            return "just now"
        if s < 3600:
            m = int(round(s / 60))
            return f"{m} minute{'s' if m != 1 else ''} ago"
        if s < 86400:
            h = int(s // 3600)
            return f"{h} hour{'s' if h != 1 else ''} ago"
        try:
            return dt.strftime("%b %d at %I:%M %p").replace(" 0", " ")
        except Exception:
            return dt.strftime("%b %d")
    except Exception:
        return "earlier"


def saved_session_info():
    """Summarise a resumable session, or return None when there's nothing to resume."""
    state = load_queue_state()
    if not state:
        return None
    queued = len(state.get("queue", []) or [])
    history = len(state.get("history", []) or [])
    cur = state.get("current")
    if not cur and queued == 0 and history == 0:
        return None
    now_playing = None
    if cur:
        title = (cur.get("title") or "Unknown").strip()
        artist = (cur.get("artist") or "").strip()
        now_playing = (f"{artist} \u2013 {title}"
                       if artist and artist.lower() != "unknown" else title)
    when = None
    try:
        import datetime
        when = datetime.datetime.fromtimestamp(os.path.getmtime(_state_path()))
    except Exception:
        when = None
    return {"queued": queued, "history": history,
            "now_playing": now_playing, "when": when}


# ═══════════════════════════════════════════════════════════════════════════
#  Cross-thread signal bridge
# ═══════════════════════════════════════════════════════════════════════════

class SignalBridge(QObject):
    track_ended   = pyqtSignal()
    state_changed = pyqtSignal(str)
    error         = pyqtSignal(str)
    queue_changed = pyqtSignal()
    status_msg    = pyqtSignal(str)
    artwork_ready = pyqtSignal(int, object)   # (token, QImage|None)
    site_export_done = pyqtSignal(object)     # playlist-site export result dict
    chat_status   = pyqtSignal(str, str, str) # (platform, level, text) from the chat bots
    run_on_gui    = pyqtSignal(object)        # any callable, run on the Qt thread
    update_available = pyqtSignal(str, str)   # (version, url)


# ═══════════════════════════════════════════════════════════════════════════
#  Small painted assets (chips, placeholder art, status dot)
# ═══════════════════════════════════════════════════════════════════════════

def _device_ratio():
    app = QApplication.instance()
    try:
        return max(1.0, float(app.devicePixelRatio())) if app else 1.0
    except Exception:
        return 1.0


def source_chip(source: TrackSource) -> QIcon:
    """A small rounded chip: 'YT' or 'SC' in the source colour."""
    label = "SC" if source == TrackSource.SOUNDCLOUD else "YT"
    colour = THEME["sc"] if source == TrackSource.SOUNDCLOUD else THEME["yt"]
    w, h = 30, 18
    r = _device_ratio()
    pm = QPixmap(int(w * r), int(h * r))
    pm.setDevicePixelRatio(r)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0.5, 0.5, w - 1, h - 1, 5, 5)
    p.fillPath(path, QColor(255, 255, 255, 16))
    p.setPen(QPen(QColor(colour), 1))
    p.drawPath(path)
    f = QFont("Arial", 9)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor(colour))
    p.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter, label)
    p.end()
    return QIcon(pm)


def placeholder_art(size: int) -> QPixmap:
    """A dark gradient tile with a music glyph, used when no artwork loads."""
    r = _device_ratio()
    pm = QPixmap(int(size * r), int(size * r))
    pm.setDevicePixelRatio(r)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, 14, 14)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor(THEME["panel2"]))
    grad.setColorAt(1.0, QColor("#26121a"))
    p.fillPath(path, QBrush(grad))
    p.setPen(QColor(THEME["accent"]))
    f = QFont("Arial", int(size * 0.38))
    p.setFont(f)
    p.setOpacity(0.85)
    p.drawText(0, 0, size, size, Qt.AlignmentFlag.AlignCenter, "\u266B")  # ♫
    p.end()
    return pm


def rounded_pixmap(src: QPixmap, size: int, radius: int = 14) -> QPixmap:
    """Center-crop `src` to a square and round the corners."""
    r = _device_ratio()
    out = QPixmap(int(size * r), int(size * r))
    out.setDevicePixelRatio(r)
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, radius, radius)
    p.setClipPath(path)
    scaled = src.scaled(int(size * r), int(size * r),
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    x = (scaled.width() - int(size * r)) // 2
    y = (scaled.height() - int(size * r)) // 2
    p.drawPixmap(-x, -y, scaled)
    p.end()
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  Eliding label — clips long titles with an ellipsis instead of overflowing
# ═══════════════════════════════════════════════════════════════════════════

class ElidingLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text
        self.setText(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text):
        self._full = text or ""
        self._apply()

    def fullText(self):
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply()

    def _apply(self):
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(self._full, Qt.TextElideMode.ElideRight,
                               max(0, self.width()))
        super().setText(elided)


# ═══════════════════════════════════════════════════════════════════════════
#  Startup session prompt
# ═══════════════════════════════════════════════════════════════════════════

class SessionDialog(QDialog):
    """Asked once at launch: recover the previous session, or start fresh.

    Only the two buttons dismiss it (no stray window-close or Escape), so the
    app always gets a deliberate answer before deciding whether to restore.
    """

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.choice = "new"
        self.setWindowTitle("Stream session")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setFixedWidth(450)
        self.setStyleSheet(self._qss())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(6)

        eyebrow = QLabel("STREAM SESSION")
        eyebrow.setObjectName("eyebrow")
        lay.addWidget(eyebrow)

        head = QLabel("Pick up where you left off?")
        head.setObjectName("head")
        lay.addWidget(head)

        bits = []
        when = info.get("when")
        if when is not None:
            bits.append(f"There's a session saved {_relative_time(when)}.")
        else:
            bits.append("There's a saved session from last time.")
        np = info.get("now_playing")
        if np:
            bits.append(f"Last playing:  {np}")
        q = info.get("queued", 0)
        bits.append(f"{q} song{'s' if q != 1 else ''} still in the queue.")
        body = QLabel("\n".join(bits))
        body.setObjectName("body")
        body.setWordWrap(True)
        lay.addWidget(body)

        lay.addSpacing(16)

        row = QHBoxLayout()
        row.setSpacing(10)

        btn_new = QPushButton("Start new session")
        btn_new.setObjectName("ghostbtn")
        btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_new.clicked.connect(lambda: self._pick("new"))
        row.addWidget(btn_new)

        row.addStretch(1)

        btn_resume = QPushButton("Resume session")
        btn_resume.setObjectName("primarybtn")
        btn_resume.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_resume.setDefault(True)
        btn_resume.clicked.connect(lambda: self._pick("resume"))
        row.addWidget(btn_resume)

        lay.addLayout(row)

    def _pick(self, choice):
        self.choice = choice
        self.accept()

    # Make a choice mandatory: ignore Escape and the window close button.
    def reject(self):
        pass

    def closeEvent(self, e):
        e.ignore()

    @staticmethod
    def ask(info, parent=None):
        dlg = SessionDialog(info, parent)
        dlg.exec()
        return dlg.choice

    def _qss(self):
        t = THEME
        return f"""
        QDialog {{ background: {t['panel']}; }}
        QLabel {{ color: {t['text']};
                  font-family: 'Segoe UI','Helvetica Neue',Arial; }}
        QLabel#eyebrow {{ color: {t['accent']}; font-size: 10px;
                          font-weight: 800; letter-spacing: 2px; }}
        QLabel#head {{ font-size: 19px; font-weight: 800; padding-top: 2px; }}
        QLabel#body {{ color: {t['muted']}; font-size: 13px; padding-top: 4px; }}
        QPushButton#primarybtn {{
            background: {t['accent']}; color: white; border: none;
            border-radius: 11px; padding: 10px 20px;
            font-size: 13px; font-weight: 700;
        }}
        QPushButton#primarybtn:hover {{ background: {t['accent_2']}; }}
        QPushButton#ghostbtn {{
            background: transparent; color: {t['muted']};
            border: 1px solid {t['stroke']}; border-radius: 11px;
            padding: 10px 18px; font-size: 13px; font-weight: 600;
        }}
        QPushButton#ghostbtn:hover {{ color: {t['text']};
                                      border-color: {t['accent']}; }}
        """


# ═══════════════════════════════════════════════════════════════════════════
#  Music-video window (independent, muted VLC player synced to the audio clock)
# ═══════════════════════════════════════════════════════════════════════════

class VideoSurface(QWidget):
    """A native widget that VLC paints video into."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QColor("#000000"))
        self.setPalette(pal)
        self.setStyleSheet("background:#000;")
        self.winId()  # realise the native handle up front so VLC can bind to it

    def handle(self):
        return int(self.winId())


class VideoWindow(QWidget):
    """A separate, freely resizable window for the music video.

    Size it however you like — drag it bigger, maximize it, double-click for
    fullscreen, or pin it on top / on a second monitor. Closing it just hides
    it (the video keeps playing for your OBS viewers) and tells the owner so it
    won't pop back up until the next video.
    """

    def __init__(self, on_close=None):
        super().__init__()
        self._on_close = on_close
        self.setWindowTitle(f"{APP_NAME} \u2014 Video")
        self.setMinimumSize(480, 300)
        self.resize(960, 600)
        self.setStyleSheet(self._qss())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("vbar")
        bar.setFixedHeight(36)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 0, 8, 0)
        h.setSpacing(8)
        self.title = ElidingLabel("Music video")
        self.title.setObjectName("vtitle")
        h.addWidget(self.title, 1)
        self.btn_pin = QPushButton("Pin")
        self.btn_pin.setObjectName("vbtn")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pin.setToolTip("Keep this window on top")
        self.btn_pin.clicked.connect(self._toggle_pin)
        h.addWidget(self.btn_pin)
        self.btn_full = QPushButton("Fullscreen")
        self.btn_full.setObjectName("vbtn")
        self.btn_full.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_full.setToolTip("Fullscreen (double-click the video, or Esc to exit)")
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        h.addWidget(self.btn_full)
        lay.addWidget(bar)

        self.surface = VideoSurface(self)
        lay.addWidget(self.surface, 1)

    def set_title(self, text):
        self.title.setText(text or "Music video")

    def _toggle_pin(self):
        on = self.btn_pin.isChecked()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()  # re-applying window flags requires a re-show

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def mouseDoubleClickEvent(self, e):
        self.toggle_fullscreen()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        # Hide rather than destroy so the native handle stays valid for VLC.
        e.ignore()
        if self.isFullScreen():
            self.showNormal()
        self.hide()
        if self._on_close:
            self._on_close()

    def _qss(self):
        t = THEME
        return f"""
        QWidget {{ background: {t['bg']}; }}
        QFrame#vbar {{ background: {t['panel']};
                       border-bottom: 1px solid {t['stroke']}; }}
        QLabel#vtitle {{ color: {t['text']}; font-size: 12px; font-weight: 700;
                         font-family: 'Segoe UI','Helvetica Neue',Arial; }}
        QPushButton#vbtn {{
            background: transparent; color: {t['muted']};
            border: 1px solid {t['stroke']}; border-radius: 8px;
            padding: 5px 12px; font-size: 11px; font-weight: 600;
        }}
        QPushButton#vbtn:hover {{ color: {t['text']}; border-color: {t['accent']}; }}
        QPushButton#vbtn:checked {{ color: white; background: {t['accent']};
                                    border-color: {t['accent']}; }}
        """


class VideoController:
    """Owns an independent, muted VLC player bound to a VideoSurface.

    The video stream YouTube hands us is muxed (it carries its own audio), so
    this player is always muted — the audio you hear and stream comes from the
    engine. Position is nudged to match the engine's clock so it stays roughly
    in sync without fighting it frame by frame.
    """

    # End of the music video. The audio-only stream is often a little longer
    # than the video, so the audio clock keeps running after the last frame.
    # The old code saw VLC's "Ended" state as a dead player and restarted the
    # video from the top every 3 seconds (then the sync seeked it back to the
    # end, where it ended again) — the loop right before a song finished.
    # Now the video freezes on its last frame and stays there for the rest of
    # the song. Only a real rewind of the song (the audio clock jumping back)
    # brings it back — not "the audio is still short of the video's end": a
    # video running a little ahead of the audio, or ending a little early,
    # used to jump back and replay its last seconds right before the song ended.
    END_FREEZE = 0.45   # s before the video's end: pause on this frame
    END_SLACK = 2.0     # s: the rewound audio must land at least this far inside

    def __init__(self, surface, engine=None):
        self.available = vlc is not None
        self._surface = surface
        self._instance = None
        self._owns_instance = False
        self._player = None
        self._bound = False
        self._url = None
        self._key = None             # identity of the track the media belongs to
        self._want_pause = False
        self._held = False           # frozen on the last frame (see END_FREEZE)
        self._hold_at = None         # furthest audio position seen while frozen
        self._stall_hold = False     # paused because the audio is stuck (see sync_to)
        self._length = 0.0           # video length in seconds, once VLC knows it
        self._vt_mark = (None, 0.0)  # (get_time() value, monotonic when it changed)
        # Sync state (see sync_to)
        self._rate = 1.0
        self._last_seek = 0.0
        self._last_restart = 0.0
        self._settle = 0.0
        self._resync = False
        self._audio_mark = None      # (elapsed, monotonic) of the last sample
        self._ready = False          # armed on transition back into playback
        self._pos_mark = None        # (video pos, monotonic) for stall detect
        self.last_drift = 0.0        # exposed for the debug readout
        if not self.available:
            return
        try:
            # Always create our OWN instance with audio output DISABLED
            # (--no-audio). Critically, we never call audio_set_mute() here: on
            # Windows that mute lands on the shared per-app audio session in the
            # Volume Mixer, which silences the engine's audio too (and Windows
            # remembers it). A --no-audio instance has no audio output at all, so
            # it renders video silently without ever touching a mute switch.
            self._instance = (vlc.Instance("--quiet", "--no-audio",
                                           "--no-video-title-show",
                                           *vlc_loader.instance_args())
                              or vlc.Instance("--no-audio", *vlc_loader.instance_args()))
            self._owns_instance = True
            self._player = self._instance.media_player_new()
            try:
                self._player.video_set_mouse_input(False)
                self._player.video_set_key_input(False)
            except Exception:
                pass
        except Exception as e:
            print(f"[Video] init failed: {e}")
            self.available = False

    def _bind(self):
        if not self.available or self._bound:
            return self._bound
        try:
            wid = int(self._surface.winId())
            system = platform.system()
            if system == "Windows":
                self._player.set_hwnd(wid)
            elif system == "Darwin":
                self._player.set_nsobject(wid)
            else:
                self._player.set_xwindow(wid)
            self._bound = True
        except Exception as e:
            print(f"[Video] bind failed: {e}")
        return self._bound

    def _state(self):
        try:
            return self._player.get_state()
        except Exception:
            return None

    def _is_dead(self):
        """True when VLC has given up on the current media (error, or a stop
        we didn't ask for). In these states the video surface stays black
        while the audio engine keeps playing.

        "Ended" is deliberately NOT dead: a video that simply reached its last
        frame before the audio finished is done, not broken. Restarting it is
        exactly what used to loop the start of the video at the end of songs.
        """
        return self._state() in (vlc.State.Error, vlc.State.Stopped,
                                 vlc.State.NothingSpecial)

    def _video_length(self):
        if self._length <= 0:
            try:
                ms = self._player.get_length()
                if ms and ms > 0:
                    self._length = ms / 1000.0
            except Exception:
                pass
        return self._length

    def play(self, url, key=None):
        """Show `url`. `key` identifies the track: the same song requested
        twice in a row has the same URL but must start over."""
        if not self.available or not url:
            return
        if url == self._url and key == self._key:
            # Finished (or frozen on its last frame) while the audio carries
            # on: leave it alone. sync_to() brings it back if the song is
            # seeked back into the video.
            if self._held or self._state() == vlc.State.Ended:
                return
            # Same track — but if VLC errored out mid-stream (throttled URL,
            # network blip), the old guard made this a permanent no-op: the
            # tick kept calling play(), play() kept returning, and the window
            # stayed black for the rest of the song. Instead, detect the dead
            # state and restart, rate-limited so a truly broken stream doesn't
            # get hammered every 400ms tick.
            if not self._is_dead():
                return
            now = time.monotonic()
            if now - self._last_restart < 3.0:
                return
            self._last_restart = now
            print("[Video] player died mid-stream — restarting")
        # Bind first; without an embed target VLC would open its own window.
        if not self._bind():
            return
        try:
            media = self._instance.media_new(url)
            media.add_option(":no-audio")            # video only; never open audio out
            # Local spool playback needs far less buffer than raw YouTube did —
            # big caching here just delays the first frame (your "video is
            # delayed" symptom). Keep the big buffer only for direct URLs.
            local = url.startswith("http://127.0.0.1")
            media.add_option(f":network-caching={1500 if local else 5000}")
            self._player.set_media(media)
            self._want_pause = False
            self._held = False
            self._hold_at = None
            self._stall_hold = False
            self._length = 0.0
            self._vt_mark = (None, 0.0)
            self._set_rate(1.0, force=True)
            self._player.play()
            self._url = url
            self._key = key
            # Fresh stream: snap to the audio clock once, then hold with rate
            # trim. Clear the audio mark so the first sample can't look like a
            # jump.
            self._resync = True
            self._audio_mark = None
            self._ready = False
            self._pos_mark = None
            self._settle = time.monotonic()
        except Exception as e:
            print(f"[Video] play failed: {e}")

    def set_paused(self, paused):
        if not self.available or not self._player or paused == self._want_pause:
            return
        self._want_pause = paused
        if self._held:
            return      # frozen on the last frame: a pause/resume changes nothing
        try:
            self._player.set_pause(1 if paused else 0)
        except Exception:
            pass
        if not paused:
            # Audio and video don't come back from a pause with the same
            # latency, so re-anchor on resume instead of inheriting whatever
            # offset the pause introduced.
            self._resync = True
            self._audio_mark = None
            self._ready = False
            self._pos_mark = None
            self._settle = time.monotonic()

    # ── Sync ───────────────────────────────────────────────────────────────

    def _set_rate(self, rate, force=False):
        """Trim playback speed. This is the mechanism that actually removes
        drift: a seek flushes VLC's buffer and re-requests the stream (the old
        stutter loop), a ±4% rate change is invisible and pulls the picture
        back into step over a few seconds."""
        if not force and abs(rate - self._rate) < 0.001:
            return
        try:
            self._player.set_rate(rate)
            self._rate = rate
        except Exception:
            pass

    def _hard_seek(self, seconds, now):
        try:
            self._set_rate(1.0)
            self._player.set_time(int(max(0.0, seconds) * 1000))
            self._last_seek = now
            self._settle = now
            self._pos_mark = None
        except Exception:
            pass

    def sync_to(self, seconds, paused=False, running=True):
        """Lock this player to the audio engine's clock.

        `running` is False while the song's position isn't moving (the audio is
        buffering, or a song went quiet at its end and the watchdog hasn't ended
        it yet). The picture waits on its frame until it moves again: playing on
        against a frozen clock and being pulled back every few seconds replayed
        the same stretch of video over and over — right before songs ended.

        Three tiers:

          drift < RELEASE   →  run at 1.0x, leave it alone
          drift < HARD      →  trim playback rate ±4% until it closes
          drift >= HARD     →  one rate-limited hard seek

        Hysteresis (ENGAGE > RELEASE) stops it hunting on the jitter in
        libvlc's get_time(), which is only accurate to a few hundred ms.

        Two failure modes learned the hard way:

        1. The settle timer must never be reset by a condition that repeats.
           The first version did `self._settle = now` on every Buffering tick.
           A stream that flaps in and out of Buffering (which a spooling
           stream does constantly) therefore reset the grace period faster
           than the 1.5s grace could ever expire — so the corrector never ran
           ONCE and the video free-ran exactly like it had no sync at all.
           Now the grace is armed on the *transition* back to playing only.

        2. The HARD tier must not sit behind the settle gate. It's the
           backstop that bounds worst-case drift; gating it means a stalling
           stream can drift arbitrarily far while "settling".
        """
        if not self.available or not self._player:
            return
        now = time.monotonic()
        try:
            # ── Discontinuity check on the AUDIO clock ──
            jumped = False
            if self._audio_mark is not None and not paused:
                last_s, last_w = self._audio_mark
                if abs(seconds - (last_s + (now - last_w))) > SYNC_JUMP:
                    jumped = True
            self._audio_mark = (seconds, now)

            if paused:
                self._ready = False     # re-arm grace on resume
                return

            st = self._player.get_state()

            # ── The audio is stuck: wait on this frame ──
            if not running:
                if not self._held and st in (vlc.State.Playing, vlc.State.Buffering):
                    self._player.set_pause(1)
                self._stall_hold = True
                self._set_rate(1.0)
                self._ready = False
                return
            if self._stall_hold:
                self._stall_hold = False
                if not self._held:
                    if not self._want_pause and st == vlc.State.Paused:
                        self._player.set_pause(0)
                    # Snap to the audio once playing again (it may have moved
                    # on, or the picture may have run a little ahead first).
                    self._resync = True
                    self._audio_mark = None
                    self._pos_mark = None
                self._ready = False
                return

            # ── End of the video (see END_FREEZE) ──
            length = self._video_length()
            if self._held or st == vlc.State.Ended:
                self._hold_at = seconds if self._hold_at is None else max(self._hold_at, seconds)
                if (length > 0 and seconds < length - self.END_SLACK
                        and seconds < self._hold_at - SYNC_JUMP):
                    self._back_inside(seconds, now)     # the song was rewound
                return                                  # hold the last frame
            if length > 0 and st == vlc.State.Playing:
                # libvlc's get_time() only moves every few hundred ms, so
                # project it forward from the moment it last changed.
                t_now = self._player.get_time()
                if t_now != self._vt_mark[0]:
                    self._vt_mark = (t_now, now)
                vnow = 0.0
                if t_now and t_now > 0:
                    vnow = t_now / 1000.0 + (now - self._vt_mark[1]) * self._rate
                if max(vnow, seconds) >= length - self.END_FREEZE:
                    # Freeze on this frame instead of letting VLC reach Ended
                    # (which blanks the window) — and never rewind to 0.
                    self._held = True
                    self._hold_at = seconds
                    self._set_rate(1.0)
                    self._player.set_pause(1)
                    return

            ready = (st != vlc.State.Buffering) and self._player.is_playing()
            if not ready:
                self._ready = False
                return
            if not self._ready:
                # Transition back into playback: arm the grace period ONCE.
                self._ready = True
                self._settle = now
                self._pos_mark = None

            t = self._player.get_time()
            if t < 0:
                return
            vpos = t / 1000.0

            # engine.elapsed_seconds falls back to 0 whenever the engine isnt
            # strictly playing (track changes, loading). Treat a bogus zero as
            # no reading rather than seeking the video back to the start.
            if seconds <= 0.05 and vpos > 5.0:
                return

            # ── Stall detection ──
            # VLC doesn't always report Buffering when a spooling source
            # starves it; it just quietly stops advancing. That silently
            # drops the video further behind every second, and rate trim has
            # no authority to fix it — only a seek does.
            stalled = False
            if self._pos_mark is None:
                self._pos_mark = (vpos, now)
            else:
                mark_p, mark_w = self._pos_mark
                elapsed_w = now - mark_w
                if elapsed_w >= 0.6:
                    if (vpos - mark_p) < 0.05:
                        stalled = True
                    self._pos_mark = (vpos, now)

            drift = vpos - seconds          # positive = video ahead of audio
            mag = abs(drift)
            self.last_drift = drift         # for the debug readout

            if self._resync:
                self._resync = False
                self._hard_seek(seconds, now)
                return
            if jumped:
                self._hard_seek(seconds, now)
                return

            # Backstop first — deliberately NOT behind the settle gate.
            if mag >= SYNC_HARD or (stalled and mag > SYNC_ENGAGE):
                if now - self._last_seek >= SYNC_SEEK_GAP:
                    self._hard_seek(seconds, now)
                return

            if now - self._settle < SYNC_SETTLE:
                return

            if self._rate == 1.0:
                if mag > SYNC_ENGAGE:
                    self._set_rate(1.0 - SYNC_TRIM if drift > 0
                                   else 1.0 + SYNC_TRIM)
            else:
                if mag < SYNC_RELEASE:
                    self._set_rate(1.0)
                elif (drift > 0) != (self._rate < 1.0):
                    # Overshot past the target — flip the correction.
                    self._set_rate(1.0 - SYNC_TRIM if drift > 0
                                   else 1.0 + SYNC_TRIM)
        except Exception:
            pass

    def _back_inside(self, seconds, now):
        """The audio clock moved back inside a video that had finished: play
        on from the audio's position."""
        self._hold_at = None
        try:
            if self._held:
                self._held = False
                if not self._want_pause:
                    self._player.set_pause(0)
                self._hard_seek(seconds, now)
            else:                               # VLC reached Ended: reopen it
                self._player.stop()
                self._player.play()
                self._resync = True             # snaps to the clock once playing
            self._audio_mark = None
            self._ready = False
            self._pos_mark = None
        except Exception:
            pass

    def stop(self):
        self._url = None
        self._key = None
        self._held = False
        self._hold_at = None
        self._stall_hold = False
        self._length = 0.0
        self._want_pause = False
        self._resync = False
        self._audio_mark = None
        self._ready = False
        self._pos_mark = None
        if self.available and self._player:
            try:
                self._set_rate(1.0)
            except Exception:
                pass
            try:
                self._player.stop()
            except Exception:
                pass

    def release(self):
        try:
            if self._player:
                self._player.stop()
                self._player.release()
            # Never release the engine's shared instance — only one we created.
            if self._instance and self._owns_instance:
                self._instance.release()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
#  Player window
# ═══════════════════════════════════════════════════════════════════════════

class PlayerWindow(QMainWindow):
    def __init__(self, engine, queue, signals, yt_api=None, settings=None):
        super().__init__()
        self.engine = engine
        self.queue = queue
        self.signals = signals
        self.yt_api = yt_api
        self.settings = settings
        self.chat = None                       # ChatManager, attached by run()
        self._setup_dlg = None

        self._seeking = False
        self._last_track_key = object()       # forces first refresh
        self._art_token = 0                   # invalidates stale artwork loads
        self._art_pixmap = None               # last good artwork (QPixmap)
        self._row_map = []                    # list index -> (section, idx)

        # Music-video window (created lazily on the first video).
        self._video_ready = False
        self.video = None                     # VideoController
        self.video_win = None                 # VideoWindow
        self._video_dismissed = False         # user closed it for this track

        # Background pre-resolution of upcoming tracks.
        self._prefetching = set()             # id(track) currently being resolved
        self._prefetch_lock = threading.Lock()
        self._prefetch_retry_at = {}          # id(track) -> earliest retry time
        self._prefetch_fails = {}             # id(track) -> consecutive failures

        # One lock serialises every start/skip/advance decision.
        # Reentrant: play_track may, on a synchronous failure, invoke
        # on_track_ended on this same thread while we still hold the lock.
        # A plain Lock would self-deadlock there; cross-thread mutual
        # exclusion is identical either way.
        self._playback_lock = threading.RLock()
        # The track we just handed to the engine but haven't seen reach PLAYING.
        # If it ends before playing, it failed — we archive it instead of
        # dropping it, so a later request can never appear to replace it.
        self._pending_start = None

        # Duplicate-request guard for api_addsong: (user, text) -> monotonic ts
        self._recent_addsong = {}
        self._addsong_lock = threading.Lock()

        # Every song that really plays this stream, for the playlist site.
        self.session_log = SessionLog()
        self._site_exporting = False
        self.blocklist = Blocklist()

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(430, 660)
        self.resize(490, 840)

        self._build_ui()
        self._wire_signals()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_ui)
        self._timer.start(400)

        # Video sync gets its own, faster timer. Sampling the audio clock only
        # 2.5x/second was never enough to hold a sub-second lock, and it tied
        # correction to the full UI repaint.
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._sync_video_tick)
        self._sync_timer.start(150)

    # ──────────────────────────────────────────────────────────────────────
    #  UI construction
    # ──────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setStyleSheet(self._app_qss())

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(14)

        outer.addLayout(self._build_header())
        outer.addWidget(self._build_now_playing())
        outer.addLayout(self._build_add_bar())
        outer.addWidget(self._build_queue_panel(), 1)   # queue takes the slack
        outer.addWidget(self._build_statusbar())

    def _build_header(self):
        row = QHBoxLayout()
        row.setSpacing(5)

        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        self._set_dot(THEME["faint"])
        row.addWidget(self.dot)

        wordmark = ElidingLabel(APP_NAME)
        wordmark.setObjectName("wordmark")
        wordmark.setMinimumWidth(60)
        row.addWidget(wordmark, 1)

        # One pill per chat platform: green = reading chat, amber = working on
        # it / can't reply yet, red = needs attention, grey = not set up.
        # Clicking any of them opens Setup.
        self.pills = {}
        for key, name in (("twitch", "Twitch"), ("kick", "Kick"), ("owncast", "Owncast")):
            b = QPushButton(f"\u25CF {name}")
            b.setObjectName("pill")
            b.setProperty("state", "off")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(f"{name}: not set up — click to open Setup")
            b.clicked.connect(self.open_setup)
            self.pills[key] = b
            row.addWidget(b)

        self.btn_setup = QPushButton("\u2699")
        self.btn_setup.setObjectName("ghostbtn")
        self.btn_setup.setFixedSize(32, 28)
        self.btn_setup.setToolTip("Setup: chat connections, song length, OBS link")
        self.btn_setup.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_setup.clicked.connect(self.open_setup)
        row.addWidget(self.btn_setup)
        return row

    def _build_now_playing(self):
        card = QFrame()
        card.setObjectName("card")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(16)

        # Album art. Was a hard 128px square: it could not give ground as the
        # window shrank, so every pixel of compression landed on the queue
        # list instead. Now it scales between these bounds with the window.
        self.ART_MIN, self.ART_MAX = 84, 160
        self.art_size = 128
        self.art = QLabel()
        self.art.setFixedSize(self.art_size, self.art_size)
        self.art.setPixmap(placeholder_art(self.art_size))
        lay.addWidget(self.art, 0, Qt.AlignmentFlag.AlignTop)

        # Right column: text + chips + seek + transport
        right = QVBoxLayout()
        right.setSpacing(8)

        self.lbl_title = ElidingLabel("Nothing playing")
        self.lbl_title.setObjectName("title")
        right.addWidget(self.lbl_title)

        self.lbl_artist = ElidingLabel("Add a song to get started")
        self.lbl_artist.setObjectName("artist")
        right.addWidget(self.lbl_artist)

        # Chips row (source / MV / requester)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        self.chips.setContentsMargins(0, 2, 0, 2)
        chips_host = QWidget()
        chips_host.setLayout(self.chips)
        right.addWidget(chips_host)

        right.addStretch(1)

        # Seek line + times
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setObjectName("seek")
        self.seek.setRange(0, 1000)
        self.seek.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek.sliderReleased.connect(self._on_seek)
        right.addWidget(self.seek)

        times = QHBoxLayout()
        self.lbl_elapsed = QLabel("0:00")
        self.lbl_elapsed.setObjectName("time")
        self.lbl_duration = QLabel("0:00")
        self.lbl_duration.setObjectName("time")
        times.addWidget(self.lbl_elapsed)
        times.addStretch(1)
        times.addWidget(self.lbl_duration)
        right.addLayout(times)

        # Transport row
        right.addLayout(self._build_transport())

        lay.addLayout(right, 1)
        return card

    def _build_transport(self):
        # Two rows in a vertical wrapper. At 470px the old single row needed
        # ~57px more than it had, so the volume slider was clipped off the
        # window edge and the Video button was crushed to 36px against a 47px
        # minimum. Secondary controls now drop to their own line when the
        # window is narrow and rejoin the main line when there's room —
        # see _reflow_transport, driven from resizeEvent.
        wrap = QVBoxLayout()
        wrap.setSpacing(8)

        self._row_main = QHBoxLayout()
        self._row_main.setSpacing(10)
        self._row_extra = QHBoxLayout()
        self._row_extra.setSpacing(10)

        self.btn_prev = QPushButton("\u23EE")
        self.btn_prev.setObjectName("ghostbtn")
        self.btn_prev.setFixedSize(44, 44)
        self.btn_prev.clicked.connect(self._on_back)
        self.btn_prev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_prev.setToolTip("Previous (from history)")

        self.btn_play = QPushButton("\u25B6")
        self.btn_play.setObjectName("playbtn")
        self.btn_play.setFixedSize(58, 58)
        self.btn_play.clicked.connect(self._on_play_pause)
        self.btn_play.setCursor(Qt.CursorShape.PointingHandCursor)

        self.btn_next = QPushButton("\u23ED")
        self.btn_next.setObjectName("ghostbtn")
        self.btn_next.setFixedSize(44, 44)
        self.btn_next.clicked.connect(self._on_skip)
        self.btn_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_next.setToolTip("Skip to next")

        self._row_main.addWidget(self.btn_prev)
        self._row_main.addWidget(self.btn_play)
        self._row_main.addWidget(self.btn_next)
        self._row_main.addStretch(1)

        # Pops out the resizable video window. Enabled only while a music
        # video is playing; lets you reopen it if you closed it mid-song.
        self.btn_video = QPushButton("Video")
        self.btn_video.setObjectName("ghostbtn")
        self.btn_video.setFixedHeight(36)
        self.btn_video.setMinimumWidth(56)   # was crushed below its 47px hint
        self.btn_video.setToolTip("Open the music-video window")
        self.btn_video.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_video.setEnabled(False)
        self.btn_video.clicked.connect(self._open_video_window)

        # Publishes this stream's songs to your own playlist site. Only shown
        # when one is set up (playlist_site.json in the data folder).
        self.btn_site = QPushButton("Export")
        self.btn_site.setObjectName("ghostbtn")
        self.btn_site.setFixedHeight(36)
        self.btn_site.setMinimumWidth(66)
        self.btn_site.setToolTip("Publish this stream's songs to the playlist site")
        self.btn_site.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_site.clicked.connect(self._site_export)

        # Emergency: stop this song NOW, take it off the stream playlist and
        # block it from being requested again.
        self.btn_kill = QPushButton("\U0001F6AB")
        self.btn_kill.setObjectName("ghostbtn")
        self.btn_kill.setFixedSize(44, 44)
        self.btn_kill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_kill.setToolTip("KILL: stop this song now, remove it from the stream "
                                 "playlist and block it  (Ctrl+Shift+Delete)\n"
                                 "Remove without blocking: Ctrl+Delete")
        self.btn_kill.clicked.connect(lambda: self._remove_current(block=True))
        self._row_main.insertWidget(3, self.btn_kill)

        # Everything below moves between the two rows on resize.
        self._secondary = [self.btn_video]
        if site_configured():
            self._secondary.append(self.btn_site)
        else:
            self.btn_site.hide()

        # YouTube actions (only when the API is configured)
        if self._yt_on():
            for glyph, tip, cb in (
                ("\U0001F44D", "Like on YouTube", self._yt_like_current),
                ("\U0001F44E", "Dislike on YouTube", self._yt_dislike_current),
                ("\u2191",     "Export history as a YouTube playlist", self._yt_export_playlist),
            ):
                b = QPushButton(glyph)
                b.setObjectName("ghostbtn")
                b.setFixedSize(36, 36)
                b.setToolTip(tip)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(cb)
                self._secondary.append(b)

        # Volume
        self.vol_lbl = QLabel("VOL")
        self.vol_lbl.setObjectName("eyebrow")
        self._secondary.append(self.vol_lbl)

        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setObjectName("vol")
        self.vol.setRange(0, 100)
        self.vol.setValue(self.engine.volume)
        # Was setFixedWidth(110): it could not give ground, so the overflow
        # landed past the window edge instead of shrinking the slider.
        self.vol.setMinimumWidth(56)    # 80 -> 56 to make room for Export
        self.vol.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Fixed)
        self.vol.valueChanged.connect(self.engine.set_volume)
        self._secondary.append(self.vol)

        wrap.addLayout(self._row_main)
        wrap.addLayout(self._row_extra)
        self._transport_stacked = None      # forces the first reflow to apply
        self._reflow_transport(self.width() or 470)
        return wrap

    # Width below which the secondary controls need their own line. Derived
    # from the measured need: 146px of transport buttons + 56 video + 3x36
    # YouTube buttons + VOL label + an 80px minimum slider, plus spacing.
    # (+76 for the playlist-site Export button)
    TRANSPORT_WRAP_WIDTH = 700

    def _rescale_art(self):
        """Size the album art to the window, redrawing the current image."""
        target = max(self.ART_MIN, min(self.ART_MAX, int(self.height() * 0.17)))
        if target == self.art_size:
            return
        self.art_size = target
        self.art.setFixedSize(target, target)
        pm = self.art.pixmap()
        if pm is not None and not pm.isNull():
            self.art.setPixmap(pm.scaled(
                target, target,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self.art.setPixmap(placeholder_art(target))

    def _reflow_transport(self, width):
        """Move secondary controls between the main and extra rows."""
        stacked = width < self.TRANSPORT_WRAP_WIDTH
        if stacked == self._transport_stacked:
            return                       # nothing to do; avoids relayout churn
        self._transport_stacked = stacked

        for w in self._secondary:
            self._row_main.removeWidget(w)
            self._row_extra.removeWidget(w)

        target = self._row_extra if stacked else self._row_main
        if stacked:
            # Left-align the wrapped line so it reads as its own control strip.
            for w in self._secondary:
                target.addWidget(w)
        else:
            for w in self._secondary:
                target.addWidget(w)

        for w in self._secondary:
            w.setVisible(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self._reflow_transport(self.width())
            self._rescale_art()
        except Exception:
            pass

    def _build_add_bar(self):
        row = QHBoxLayout()
        row.setSpacing(8)

        self.entry = QLineEdit()
        self.entry.setObjectName("entry")
        self.entry.setPlaceholderText("Paste a YouTube or SoundCloud link, or search a song")
        self.entry.setClearButtonEnabled(True)
        self.entry.returnPressed.connect(self._on_add_song)
        row.addWidget(self.entry, 1)

        self.btn_add = QPushButton("Add to queue")
        self.btn_add.setObjectName("primarybtn")
        self.btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add.clicked.connect(self._on_add_song)
        row.addWidget(self.btn_add)
        return row

    def _build_queue_panel(self):
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        self.lbl_queue_head = QLabel("UP NEXT")
        self.lbl_queue_head.setObjectName("eyebrow")
        head.addWidget(self.lbl_queue_head)
        head.addStretch(1)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setObjectName("linkbtn")
        self.btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear.clicked.connect(self.queue.clear)
        head.addWidget(self.btn_clear)
        lay.addLayout(head)

        self.list = QListWidget()
        self.list.setObjectName("queue")
        self.list.setIconSize(QSize(30, 18))
        self.list.setUniformItemSizes(False)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_menu)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        lay.addWidget(self.list, 1)
        return card

    def _build_statusbar(self):
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)

        headrow = QHBoxLayout()
        head = QLabel("ACTIVITY LOG")
        head.setObjectName("eyebrow")
        headrow.addWidget(head)
        headrow.addStretch(1)
        self.btn_obs = QPushButton("COPY OBS LINK")
        self.btn_obs.setObjectName("linkbtn")
        self.btn_obs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_obs.setToolTip(f"Copies http://localhost:{STATUS_PORT}/ — in OBS add a "
                                f"Browser source and paste it (560 x 500)")
        self.btn_obs.clicked.connect(self.copy_obs_link)
        headrow.addWidget(self.btn_obs)
        self.btn_update = QPushButton("")
        self.btn_update.setObjectName("linkbtn")
        self.btn_update.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_update.hide()
        headrow.addWidget(self.btn_update)
        lay.addLayout(headrow)

        self.log_list = QListWidget()
        self.log_list.setObjectName("loglist")
        # Minimum rather than fixed: a fixed 85px competed with the queue for
        # vertical space in a short window.
        self.log_list.setMinimumHeight(64)
        self.log_list.setMaximumHeight(110)
        self.log_list.setWordWrap(True)
        self.log_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.log_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        lay.addWidget(self.log_list)

        self.log_list.addItem("System initialized")
        return card

    # ──────────────────────────────────────────────────────────────────────
    #  Signals / shortcuts
    # ──────────────────────────────────────────────────────────────────────

    def _wire_signals(self):
        s = self.signals
        s.track_ended.connect(self._on_track_ended)
        s.state_changed.connect(self._on_state_changed)
        s.queue_changed.connect(self._refresh_queue)
        s.status_msg.connect(self._set_status)
        s.error.connect(lambda m: self._set_status(f"Error — {m}"))
        s.artwork_ready.connect(self._on_artwork)
        s.site_export_done.connect(self._on_site_export_done)
        s.chat_status.connect(self._on_chat_status)
        s.run_on_gui.connect(lambda fn: fn())
        s.update_available.connect(self._on_update_available)

        QShortcut(QKeySequence("Ctrl+Right"), self, activated=self._on_skip)
        QShortcut(QKeySequence("Ctrl+Left"), self, activated=self._on_back)
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self._on_play_pause)
        QShortcut(QKeySequence("Ctrl+Delete"), self,
                  activated=lambda: self._remove_current(block=False))
        QShortcut(QKeySequence("Ctrl+Shift+Delete"), self,
                  activated=lambda: self._remove_current(block=True))

    # ──────────────────────────────────────────────────────────────────────
    #  Playback control — single source of truth
    # ──────────────────────────────────────────────────────────────────────

    def _advance_if_idle(self):
        """Start the next queued track, but only if the engine is truly idle."""
        with self._playback_lock:
            if self.engine.state != PlayerState.STOPPED:
                return
            nxt = self.queue.peek_next()
            if nxt is not None and getattr(nxt, "pending", False):
                return
            track = self.queue.pop_next()
            if track is None:
                return
            self._pending_start = track
            self.engine.play_track(track)

    def _on_play_pause(self):
        st = self.engine.state
        if st == PlayerState.PLAYING:
            self.engine.pause()
        elif st == PlayerState.PAUSED:
            self.engine.resume()
        else:
            self._advance_if_idle()

    def _on_skip(self):
        with self._playback_lock:
            cur = self.engine.current_track
            if cur:
                self.queue.push_to_history(cur)
            self._pending_start = None
            self.engine.stop()
            nxt = self.queue.pop_next()
            if nxt:
                self._pending_start = nxt
                self.engine.play_track(nxt)
            else:
                self.signals.status_msg.emit("Queue is empty")

    def _on_back(self):
        with self._playback_lock:
            prev = self.queue.pop_from_history()
            if not prev:
                self.signals.status_msg.emit("No previous track")
                return
            if self.engine.current_track:
                self.queue.add_next(self.engine.current_track)
            self._pending_start = prev
            self.engine.stop()
            self.engine.play_track(prev)

    def _on_seek(self):
        self._seeking = False
        dur = self.engine.duration_seconds
        if dur > 0:
            self.engine.seek(dur * self.seek.value() / 1000.0)

    def _on_add_song(self):
        text = self.entry.text().strip()
        if not text:
            return
        self.entry.clear()
        threading.Thread(target=self._enqueue_request,
                         args=(text, "You", None, ""), daemon=True).start()

    def _enqueue_request(self, text, requested_by, on_done=None, platform=""):
        """Queue a placeholder row now; resolve it in the background.

        on_done(result) is called EXACTLY ONCE with the final outcome, from a
        worker thread. It's how chat bots and the website learn whether the
        song really made it — see _report for the result shape.
        """
        # A song shared from a phone app comes with a line of text in front of
        # the link ("Listen to … on #SoundCloud https://…"): keep just the link.
        text = clean_request(text)
        if not text:
            self._report(on_done, False, "empty", "Usage: !sr <song name or link>")
            return

        is_sc = AudioEngine.is_soundcloud_url(text)
        is_yt = AudioEngine.is_youtube_url(text)

        placeholder = Track(
            title=(text if not (is_sc or is_yt) else "Loading\u2026")[:90],
            artist="\u2026",
            url=text if (is_sc or is_yt) else "",
            source=TrackSource.SOUNDCLOUD if is_sc else TrackSource.YOUTUBE,
            video_id=(AudioEngine.extract_video_id(text) or "") if is_yt else "",
            requested_by=requested_by,
            platform=(platform or "").strip().lower()[:20],
        )
        placeholder.pending = True

        if (is_sc or is_yt) and self._blocked(placeholder):
            self.signals.status_msg.emit(f"Blocked song requested by {requested_by} \u2014 ignored")
            self._report(on_done, False, "blocked", "\U0001F6AB That song is blocked on this stream.")
            return

        self.queue.add(placeholder)
        self.signals.status_msg.emit(f"Searching \u2014 {text[:60]}")

        threading.Thread(target=self._resolve_request,
                         args=(placeholder, text, is_sc, is_yt, on_done),
                         daemon=True).start()

    def _drop_placeholder(self, track):
        for i, t in enumerate(self.queue.get_list()):
            if t is track:
                self.queue.remove_at(i)
                return

    # ── Request outcome reporting ─────────────────────────────────────────

    @staticmethod
    def _short(s, n=80):
        s = (s or "").strip()
        return s if len(s) <= n else s[:n - 1].rstrip() + "\u2026"

    def _track_label(self, t):
        artist = (t.artist or "").strip()
        title = self._short(t.title, 90)
        if artist and artist.lower() not in ("unknown", "\u2026", "na") \
                and artist.lower() not in title.lower():
            return f"{self._short(artist, 40)} - {title}"
        return title

    def _report(self, on_done, ok, status, message, track=None, position=None):
        """Deliver a request's final outcome to whoever asked.

        result = {
          "ok":       True only if the song is really in the queue / playing,
          "status":   queued | playing | not_found | too_long | unplayable |
                      spotify | removed | empty | error | duplicate,
          "message":  ready-to-post chat line (no @mention; the bot adds it),
          "title", "artist", "source", "duration", "position"  (when queued)
        }
        """
        if on_done is None:
            return
        result = {"ok": ok, "status": status, "message": message}
        if track is not None:
            result.update({
                "title": track.title, "artist": track.artist,
                "source": track.source.value,
                "duration": int(track.duration_seconds or 0),
                "position": position,
            })
        try:
            on_done(result)
        except Exception as e:
            print(f"[Request] reply callback failed: {e}")

    def _queue_position(self, track):
        """0 = playing now, N = Nth in line, None = not in the queue any more."""
        if self.engine.current_track is track:
            return 0
        for i, t in enumerate(self.queue.get_list()):
            if t is track:
                return i + 1
        return None

    # Why a link didn't play -> (activity log, chat reply). See ytdlp_failure.
    _SC_LOCKED = ("drm", "preview", "geo")
    _LINK_FAILURES = {
        "drm": ("locked by SoundCloud (encrypted)",
                "\U0001F512 SoundCloud locks that track so only its own app can "
                "play it, and I couldn't find it on YouTube. Try the song name."),
        "preview": ("SoundCloud Go+ track: only a 30-second preview",
                    "\U0001F512 SoundCloud only gives apps a 30-second preview of "
                    "that track (Go+), and I couldn't find it on YouTube. Try the "
                    "song name."),
        "geo": ("not available in this country",
                "\u274c That SoundCloud track isn't available in this country, and "
                "I couldn't find it on YouTube. Try the song name."),
        "gone": ("private or removed",
                 "\u274c That {src} link is private or was removed."),
        "private": ("private",
                    "\u274c That {src} link is private."),
        "short_link": ("the short link wouldn't open",
                       "\u274c Couldn't open that SoundCloud short link. Paste the "
                       "full soundcloud.com link instead."),
        "bad_link": ("not a link to a song",
                     "\u274c That isn't a link to a {src} song. Copy the song's "
                     "own link and try again."),
    }

    def _locked_on_soundcloud(self, placeholder, max_s):
        """SoundCloud won't let this track play outside its own app (encrypted,
        a Go+ preview, or blocked here). Same song from YouTube, or None."""
        name = self.engine.soundcloud_song_name(placeholder.url)
        if not name:
            return None
        self.signals.status_msg.emit(
            f"SoundCloud locks that track \u2014 looking for "
            f"\u201c{self._short(name, 60)}\u201d on YouTube")
        found, _ = self.engine.search_youtube_ex(name, max_duration=max_s)
        return found

    def _resolve_request(self, placeholder, text, is_sc, is_yt, on_done=None):
        shown = self._short(text, 60)
        max_s = MAX_SONG_MINUTES * 60
        note = ""           # put in front of the reply (e.g. "came from YouTube")
        try:
            if is_sc or is_yt:
                if not self.engine.resolve_stream_url(placeholder):
                    src = "SoundCloud" if is_sc else "YouTube"
                    why = getattr(placeholder, "resolve_error", "")
                    if is_sc and "on.soundcloud.com/" in (placeholder.url or "").lower() \
                            and why in ("", "bad_link"):
                        why = "short_link"          # never got past the short link
                    found = None
                    if is_sc and why in self._SC_LOCKED:
                        found = self._locked_on_soundcloud(placeholder, max_s)
                    if found is None:
                        self._drop_placeholder(placeholder)
                        log, msg = self._LINK_FAILURES.get(why, ("", ""))
                        self.signals.status_msg.emit(
                            f"Couldn't play that {src} link" + (f" \u2014 {log}" if log else ""))
                        self._report(on_done, False, "unplayable", (msg or (
                            "\u274c Couldn't play that {src} link right now. Try "
                            "again, another link, or the song name.")).format(src=src))
                        return
                    for attr in ("title", "artist", "url", "source", "video_id",
                                 "artwork_url", "duration_seconds", "album",
                                 "stream_url", "is_music_video"):
                        if hasattr(found, attr):
                            setattr(placeholder, attr, getattr(found, attr))
                    note = ("\U0001F512 SoundCloud locks that track, so it's "
                            "playing from YouTube. ")
            else:
                # max_duration lets the search skip a 1-hour upload and take
                # the next hit instead of rejecting the request as too long.
                found, reason = self.engine.search_youtube_ex(text, max_duration=max_s)
                if not found:
                    self._drop_placeholder(placeholder)
                    self.signals.status_msg.emit(f"No results for \u201c{shown}\u201d")
                    msg = {
                        "too_long": f"\u274c Everything I found for \u201c{shown}\u201d "
                                    f"is over {MAX_SONG_MINUTES} min.",
                        "unplayable": f"\u274c Found \u201c{shown}\u201d but none of "
                                      f"the top results would play. Try a YouTube link.",
                        "spotify": "\u274c Couldn't read that Spotify link. "
                                   "Try the song name instead.",
                    }.get(reason, f"\u274c No results for \u201c{shown}\u201d. "
                                  f"Try artist + song name, or a YouTube link.")
                    self._report(on_done, False, reason or "not_found", msg)
                    return
                for attr in ("title", "artist", "url", "source", "video_id",
                             "artwork_url", "duration_seconds", "album",
                             "stream_url", "is_music_video"):
                    if hasattr(found, attr):
                        setattr(placeholder, attr, getattr(found, attr))

            if self._blocked(placeholder):
                self._drop_placeholder(placeholder)
                self.signals.status_msg.emit(
                    f"Blocked song requested by {placeholder.requested_by} \u2014 ignored")
                self._report(on_done, False, "blocked",
                             "\U0001F6AB That song is blocked on this stream.")
                return

            if placeholder.duration_seconds > max_s:
                self._drop_placeholder(placeholder)
                self.signals.status_msg.emit(f"Too long \u2014 max {MAX_SONG_MINUTES} min")
                self._report(on_done, False, "too_long",
                             f"\u274c {self._short(self._track_label(placeholder), 70)} "
                             f"is {self._fmt(placeholder.duration_seconds)} "
                             f"\u2014 max is {MAX_SONG_MINUTES} min.")
                return

            placeholder.pending = False
            self.queue._notify()

            src = "SoundCloud" if placeholder.source == TrackSource.SOUNDCLOUD else "YouTube"
            self.signals.status_msg.emit(
                f"Queued \u2014 {placeholder.artist} \u2013 {placeholder.title}  ({src})")
            print(f"[Queue] +{placeholder.artist} - {placeholder.title} "
                  f"by {placeholder.requested_by}")

            # Start it straight away if nothing is playing, THEN work out
            # where it landed so the reply says "playing now" vs "#3".
            self._advance_if_idle()
            pos = self._queue_position(placeholder)
            label = self._track_label(placeholder)
            dur = f" [{self._fmt(placeholder.duration_seconds)}]" \
                if placeholder.duration_seconds else ""
            if pos == 0:
                self._report(on_done, True, "playing",
                             f"{note}\u25b6\ufe0f Playing now: {label}{dur}",
                             placeholder, 0)
            elif pos is None:
                # Cleared/removed from the queue while it was being looked up.
                self._report(on_done, False, "removed",
                             f"\u26a0\ufe0f Found {label} but it was removed from "
                             f"the queue before it could be added.")
            else:
                self._report(on_done, True, "queued",
                             f"{note}\u2705 Added: {label}{dur} \u2014 #{pos} in queue",
                             placeholder, pos)
        except Exception as e:
            self._drop_placeholder(placeholder)
            self.signals.status_msg.emit(f"Error \u2014 {e}")
            self._report(on_done, False, "error",
                         f"\u26a0\ufe0f Something went wrong adding \u201c{shown}\u201d. "
                         f"Try again in a moment.")
            return

        self._advance_if_idle()

    # ──────────────────────────────────────────────────────────────────────
    #  Engine event handlers
    # ──────────────────────────────────────────────────────────────────────

    def _on_track_ended(self):
        failed = None
        with self._playback_lock:
            if self._pending_start is not None:
                failed = self._pending_start
                self._pending_start = None
            else:
                cur = self.engine.current_track
                if cur is not None:
                    self.queue.push_to_history(cur)

        if failed is not None:
            # Never played — keep it in history (Back / requeue still work)
            # but leave it off the stream playlist.
            try:
                failed._play_failed = True
            except Exception:
                pass
            self.session_log.mark_failed(failed)
            self.queue.push_to_history(failed)
            self.signals.status_msg.emit(
                f"Couldn't play {failed.title} — moved to history")

        self._advance_if_idle()

    def _on_state_changed(self, value):
        st = PlayerState(value)
        if st == PlayerState.PLAYING:
            with self._playback_lock:
                self._pending_start = None
            cur = self.engine.current_track
            if cur is not None:
                if not self.session_log.record(cur):
                    self.session_log.refresh_durations(cur)

        playing = st == PlayerState.PLAYING
        self.btn_play.setText("\u23F8" if playing else "\u25B6")

        if st == PlayerState.PLAYING:
            self._set_dot(THEME["ok"])
        elif st in (PlayerState.PAUSED, PlayerState.LOADING):
            self._set_dot(THEME["warn"])
        else:
            self._set_dot(THEME["faint"])

    def _set_status(self, msg):
        print(f"[Status] {msg}")
        if not hasattr(self, 'log_list'):
            return

        timestamp = time.strftime("%H:%M:%S")
        self.log_list.addItem(f"[{timestamp}]  {msg}")
        self.log_list.scrollToBottom()

        while self.log_list.count() > 50:
            self.log_list.takeItem(0)

    # ──────────────────────────────────────────────────────────────────────
    #  Artwork
    # ──────────────────────────────────────────────────────────────────────

    def _load_artwork(self, track):
        self._art_token += 1
        token = self._art_token
        url = getattr(track, "artwork_url", "") or ""
        if not url:
            self.signals.artwork_ready.emit(token, None)
            return

        def work():
            data = None
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw = resp.read()
                img = QImage()
                if img.loadFromData(raw):
                    data = img
            except Exception:
                data = None
            self.signals.artwork_ready.emit(token, data)

        threading.Thread(target=work, daemon=True).start()

    def _on_artwork(self, token, image):
        if token != self._art_token:
            return
        if image is None:
            self._art_pixmap = None
            self.art.setPixmap(placeholder_art(self.art_size))
            return
        self._art_pixmap = rounded_pixmap(QPixmap.fromImage(image), self.art_size)
        self.art.setPixmap(self._art_pixmap)

    # ──────────────────────────────────────────────────────────────────────
    #  Music-video window
    # ──────────────────────────────────────────────────────────────────────

    def _local_video_url(self, track):
        vid = getattr(track, "video_id", "") or ""
        if vid:
            return f"http://127.0.0.1:{STATUS_PORT}/stream/{vid}"
        return getattr(track, "video_stream_url", "") or ""

    def _ensure_video(self):
        if self._video_ready:
            return self.video is not None and self.video.available
        self._video_ready = True
        if vlc is None:
            print("[Video] python-vlc unavailable — video window disabled")
            return False
        try:
            self.video_win = VideoWindow(on_close=self._on_video_closed)
            self.video = VideoController(self.video_win.surface, self.engine)
        except Exception as e:
            print(f"[Video] setup failed: {e}")
            self.video = None
            self.video_win = None
            return False
        return self.video.available

    def _open_video_window(self):
        if not ENABLE_VIDEO_WINDOW:
            return
        t = self.engine.current_track
        if not (t and getattr(t, "is_music_video", False) and self.engine.mv_active
                and getattr(t, "video_stream_url", "")):
            return
        self._video_dismissed = False
        if not self._ensure_video():
            self.signals.status_msg.emit("Video needs python-vlc installed")
            return
        self.video_win.set_title(f"{t.artist} \u2013 {t.title}")
        self.video.play(self._local_video_url(t), key=id(t))
        self.video_win.show()
        self.video_win.raise_()
        self.video_win.activateWindow()

    def _on_video_closed(self):
        self._video_dismissed = True
        if self.video is not None:
            self.video.stop()

    def _sync_video_tick(self):
        if not ENABLE_VIDEO_WINDOW or self.video is None:
            return
        state = self.engine.state
        if state not in (PlayerState.PLAYING, PlayerState.PAUSED):
            return
        if not self.engine.mv_active:
            return
        try:
            self.video.sync_to(self.engine.elapsed_seconds,
                               state == PlayerState.PAUSED,
                               running=self.engine.clock_running)
        except Exception:
            pass

    def _update_video(self, track, state, showing):
        if not ENABLE_VIDEO_WINDOW:
            if hasattr(self, "btn_video"):
                self.btn_video.setEnabled(False)
            return
        is_mv = (showing and track is not None
                 and getattr(track, "is_music_video", False)
                 and self.engine.mv_active
                 and bool(getattr(track, "video_stream_url", "")))

        if hasattr(self, "btn_video"):
            self.btn_video.setEnabled(bool(is_mv) and vlc is not None)

        if is_mv and not self._video_dismissed:
            if not self._ensure_video():
                return
            if not self.video_win.isVisible():
                self.video_win.show()
                self.video_win.raise_()
            self.video_win.set_title(f"{track.artist} \u2013 {track.title}")
            self.video.play(self._local_video_url(track), key=id(track))
            self.video.set_paused(state == PlayerState.PAUSED)
        else:
            if self.video_win is not None and self.video_win.isVisible():
                self.video_win.hide()
            if self.video is not None:
                self.video.stop()

    # ──────────────────────────────────────────────────────────────────────
    #  Pre-resolution (warm the next track(s) so they start instantly)
    # ──────────────────────────────────────────────────────────────────────

    def _prefetch_next(self):
        if self.engine.state != PlayerState.PLAYING:
            return
        now = time.time()
        for i, track in enumerate(self.queue.get_list(limit=PREFETCH_AHEAD)):
            url = getattr(track, "url", "") or ""
            if not url:
                continue
            warm_spool = (i == 0)

            if getattr(track, "pending", False):
                continue
            needs_audio = not getattr(track, "stream_url", "")
            needs_video = bool(
                track.source == TrackSource.YOUTUBE
                and getattr(track, "video_id", "")
                and not getattr(track, "video_stream_url", "")
                and not getattr(track, "_video_unavailable", False)
                and (track.is_music_video or self.engine._detect_music_video(track))
            )
            if not needs_audio and not needs_video:
                continue

            key = id(track)
            if self._prefetch_retry_at.get(key, 0) > now:
                continue
            with self._prefetch_lock:
                if key in self._prefetching:
                    continue
                self._prefetching.add(key)
            threading.Thread(target=self._prefetch_worker,
                             args=(track, key, warm_spool), daemon=True).start()

    def _prefetch_worker(self, track, key, warm_spool=False):
        resolved_ok = True
        video_failed = False
        try:
            if not getattr(track, "stream_url", ""):
                stream = self.engine.resolve_stream_url(track)
                if stream:
                    track.stream_url = stream
                    print(f"[Prefetch] Audio ready: {getattr(track, 'title', '?')}")
                else:
                    resolved_ok = False
                    print(f"[Prefetch] Audio resolve failed: {getattr(track, 'title', '?')}")

            if (track.source == TrackSource.YOUTUBE
                    and getattr(track, "video_id", "")
                    and not getattr(track, "video_stream_url", "")):
                if track.is_music_video or self.engine._detect_music_video(track):
                    track.is_music_video = True
                    self.engine._resolve_video_stream(track)
                    if track.video_stream_url:
                        print(f"[Prefetch] Video ready: {getattr(track, 'title', '?')}")
                        if warm_spool:
                            self._warm_spool(track.video_id)
                    else:
                        video_failed = True
        except Exception as e:
            resolved_ok = False
            print(f"[Prefetch] Failed: {e}")
        finally:
            with self._prefetch_lock:
                self._prefetching.discard(key)
            # A video that won't resolve used to count as success, so the
            # 400ms UI tick started another yt-dlp run the moment the last
            # one finished — nonstop for the whole song, each run pinning a
            # CPU core next to OBS and the audio. Failures now back off, and
            # after two the track just plays audio-only.
            if resolved_ok and not video_failed:
                self._prefetch_fails.pop(key, None)
                self._prefetch_retry_at.pop(key, None)
            else:
                n = self._prefetch_fails.get(key, 0) + 1
                self._prefetch_fails[key] = n
                self._prefetch_retry_at[key] = time.time() + min(600, 30 * (2 ** (n - 1)))
                if video_failed and resolved_ok and n >= 2:
                    track._video_unavailable = True
                    print(f"[Prefetch] No music video for: "
                          f"{getattr(track, 'title', '?')} \u2014 plays audio-only")

    def _warm_spool(self, video_id):
        if not video_id:
            return
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{STATUS_PORT}/stream/{video_id}",
                headers={"Range": "bytes=0-262143"})
            local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with local.open(req, timeout=30) as r:
                r.read(1024)
            print(f"[Prefetch] Spool warmed: {video_id}")
        except Exception as e:
            print(f"[Prefetch] Spool warm failed for {video_id}: {e}")

    # ──────────────────────────────────────────────────────────────────────
    #  Periodic UI refresh
    # ──────────────────────────────────────────────────────────────────────

    def _update_ui(self):
        track = self.engine.current_track
        state = self.engine.state
        showing = bool(track) and state != PlayerState.STOPPED

        if showing:
            self.lbl_title.setText(track.title or "Unknown")
            artist = track.artist or "Unknown"
            self.lbl_artist.setText(artist)
        else:
            self.lbl_title.setText("Nothing playing")
            self.lbl_artist.setText("Add a song to get started")

        if state in (PlayerState.PLAYING, PlayerState.PAUSED, PlayerState.LOADING):
            el = self.engine.elapsed_seconds
            dur = self.engine.duration_seconds
            self.lbl_elapsed.setText(self._fmt(el))
            self.lbl_duration.setText(self._fmt(dur))
            if dur > 0 and not self._seeking:
                self.seek.setValue(int(el / dur * 1000))
        else:
            self.lbl_elapsed.setText("0:00")
            self.lbl_duration.setText("0:00")
            if not self._seeking:
                self.seek.setValue(0)

        key = (id(track) if track else None, showing)
        if key != self._last_track_key:
            self._last_track_key = key
            self._video_dismissed = False
            self._rebuild_chips(track if showing else None)
            self._refresh_queue()
            if showing:
                self._load_artwork(track)
            else:
                self._art_token += 1
                self._art_pixmap = None
                self.art.setPixmap(placeholder_art(self.art_size))

        self._update_video(track, state, showing)
        self._prefetch_next()

    def _rebuild_chips(self, track):
        while self.chips.count():
            item = self.chips.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not track:
            return

        src = "SoundCloud" if track.source == TrackSource.SOUNDCLOUD else "YouTube"
        src_col = THEME["sc"] if track.source == TrackSource.SOUNDCLOUD else THEME["yt"]
        self.chips.addWidget(self._chip(src, src_col))

        if track.is_music_video and self.engine.mv_active:
            self.chips.addWidget(self._chip("MUSIC VIDEO", THEME["accent"]))

        if track.requested_by:
            self.chips.addWidget(self._chip(f"req {track.requested_by}", THEME["muted"]))

        self.chips.addStretch(1)

    def _chip(self, text, colour):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{colour}; background:rgba(255,255,255,0.05);"
            f"border:1px solid {colour}; border-radius:9px;"
            f"padding:2px 9px; font-size:10px; font-weight:600;")
        return lbl

    def _refresh_queue(self):
        self.list.clear()
        self._row_map = []

        def add_row(text, fg, icon, section, idx, bold=False):
            it = QListWidgetItem(text)
            it.setForeground(QColor(fg))
            if icon is not None:
                it.setIcon(icon)
            if bold:
                f = it.font(); f.setBold(True); it.setFont(f)
            it.setSizeHint(QSize(0, 38))
            self.list.addItem(it)
            self._row_map.append((section, idx))

        for i, t in enumerate(self.queue.get_history()):
            add_row(f"  {t.artist} \u2013 {t.title}   {self._fmt(t.duration_seconds)}",
                    THEME["faint"], source_chip(t.source), "history", i)

        cur = self.engine.current_track
        has_cur = bool(cur) and self.engine.state != PlayerState.STOPPED
        if has_cur:
            glyph = "\u25B6" if self.engine.state == PlayerState.PLAYING else "\u23F8"
            tag = f"  [{cur.requested_by}]" if cur.requested_by else ""
            add_row(f"{glyph}  {cur.artist} \u2013 {cur.title}   "
                    f"{self._fmt(cur.duration_seconds)}{tag}",
                    THEME["accent"], source_chip(cur.source), "current", 0, bold=True)

        upcoming = self.queue.get_list()
        for i, t in enumerate(upcoming):
            tag = f"  [{t.requested_by}]" if t.requested_by else ""
            add_row(f"{i+1}.  {t.artist} \u2013 {t.title}   "
                    f"{self._fmt(t.duration_seconds)}{tag}",
                    THEME["text"], source_chip(t.source), "upcoming", i)

        n = len(upcoming)
        self.lbl_queue_head.setText(f"UP NEXT \u2022 {n}" if n else "UP NEXT")

    # ──────────────────────────────────────────────────────────────────────
    #  Queue list interactions
    # ──────────────────────────────────────────────────────────────────────

    def _selected(self):
        row = self.list.currentRow()
        if row < 0 or row >= len(self._row_map):
            return None, -1, None
        section, idx = self._row_map[row]
        if section == "history":
            hist = self.queue.get_history()
            if 0 <= idx < len(hist):
                return section, idx, hist[idx]
        elif section == "current":
            return section, 0, self.engine.current_track
        elif section == "upcoming":
            up = self.queue.get_list()
            if 0 <= idx < len(up):
                return section, idx, up[idx]
        return None, -1, None

    def _copy_track(self, t):
        return Track(
            title=t.title, artist=t.artist, url=t.url,
            stream_url="", video_stream_url="",
            source=t.source, video_id=t.video_id, artwork_url=t.artwork_url,
            duration_seconds=t.duration_seconds, requested_by=t.requested_by,
            is_music_video=t.is_music_video, album=t.album,
            platform=getattr(t, "platform", ""),
        )

    def _on_double_click(self, _item):
        sec, idx, t = self._selected()
        if not t:
            return
        with self._playback_lock:
            if sec == "upcoming":
                if self.engine.current_track:
                    self.queue.push_to_history(self.engine.current_track)
                for _ in range(idx):
                    skipped = self.queue.pop_next()
                    if skipped is None:
                        break
                    self.queue.push_to_history(skipped)
                play = self.queue.pop_next()
                if play is None:
                    return
            elif sec == "history":
                play = self._copy_track(t)
                if self.engine.current_track:
                    self.queue.push_to_history(self.engine.current_track)
            else:
                return
            self._pending_start = play
            self.engine.stop()
            self.engine.play_track(play)

    def _show_menu(self, pos):
        sec, idx, t = self._selected()
        m = QMenu(self)
        m.addAction("Play now", self._menu_play_now)
        m.addAction("Play next", self._menu_play_next)
        if sec == "current":
            m.addSeparator()
            m.addAction("Remove from stream playlist && skip",
                        lambda: self._remove_current(block=False))
            m.addAction("\U0001F6AB  Remove, skip && block",
                        lambda: self._remove_current(block=True))
        if sec == "history":
            m.addSeparator()
            m.addAction("Remove from stream playlist", lambda: self._menu_remove_history(False))
            m.addAction("\U0001F6AB  Remove && block", lambda: self._menu_remove_history(True))
        if sec == "upcoming":
            m.addAction("Remove", self._menu_remove)
            m.addAction("\U0001F6AB  Remove && block", self._menu_block_upcoming)
            m.addSeparator()
            m.addAction("Move up", lambda: self._menu_move(-1))
            m.addAction("Move down", lambda: self._menu_move(1))
        m.addSeparator()
        m.addAction("Requeue a copy", self._menu_requeue)

        if self._yt_on() and t and getattr(t, "video_id", None):
            m.addSeparator()
            m.addAction("\U0001F44D  Like on YouTube", lambda: self._yt_rate("like"))
            m.addAction("\U0001F44E  Dislike on YouTube", lambda: self._yt_rate("dislike"))
        m.exec(self.list.mapToGlobal(pos))

    def _menu_play_now(self):
        self._on_double_click(None)

    def _menu_play_next(self):
        sec, idx, t = self._selected()
        if not t:
            return
        if sec in ("history", "current"):
            self.queue.add_next(self._copy_track(t))
        elif sec == "upcoming" and idx > 0:
            self.queue.move(idx, 0)

    def _menu_remove(self):
        sec, idx, _ = self._selected()
        if sec == "upcoming" and idx >= 0:
            self.queue.remove_at(idx)

    # ── Removing / blocking ───────────────────────────────────────────────

    def _after_removal(self, what, block, track):
        again = " \u2014 press Export to update the site" if self.session_log.exported_url else ""
        verb = "Removed & blocked" if block else "Removed"
        self._set_status(f"{verb}: {self._short(self._track_label(track), 60)}{again}")
        self.save_state()

    def _remove_current(self, block=False):
        """Stop the playing song immediately and keep it off the playlist."""
        with self._playback_lock:
            cur = self.engine.current_track
            if cur is None or self.engine.state == PlayerState.STOPPED:
                self._set_status("Nothing is playing")
                return
            self._pending_start = None
            self.engine.stop()                 # silent immediately; no history entry
            self.session_log.remove(cur.url)
            if block:
                self.blocklist.add(cur, reason="killed on stream")
                self.queue.purge_url(cur.url)  # also any queued copies
        # Take the music video off screen right now, not on the next UI tick.
        try:
            self._update_video(None, PlayerState.STOPPED, False)
        except Exception:
            pass
        self._after_removal("current", block, cur)
        self._advance_if_idle()

    def _menu_remove_history(self, block):
        sec, idx, t = self._selected()
        if sec != "history" or t is None:
            return
        self.queue.remove_from_history(t)
        self.session_log.remove(t.url)
        if block:
            self.blocklist.add(t, reason="removed from history")
            self.queue.purge_url(t.url)
        self._after_removal("history", block, t)

    def _menu_block_upcoming(self):
        sec, idx, t = self._selected()
        if sec != "upcoming" or t is None:
            return
        self.blocklist.add(t, reason="removed from queue")
        self.queue.purge_url(t.url)
        self._after_removal("upcoming", True, t)

    def _blocked(self, track):
        return self.blocklist.matches(getattr(track, "url", ""), getattr(track, "video_id", ""))

    def _menu_move(self, delta):
        sec, idx, _ = self._selected()
        if sec != "upcoming" or idx < 0:
            return
        target = idx + delta
        if 0 <= target < self.queue.length:
            self.queue.move(idx, target)

    def _menu_requeue(self):
        _, _, t = self._selected()
        if t:
            self.queue.add(self._copy_track(t))

    # ──────────────────────────────────────────────────────────────────────
    #  YouTube API helpers
    # ──────────────────────────────────────────────────────────────────────

    def _yt_on(self):
        return bool(self.yt_api) and bool(getattr(self.yt_api, "enabled", False))

    def _yt_rate(self, rating):
        _, _, t = self._selected()
        if not t:
            t = self.engine.current_track
        if not t or not getattr(t, "video_id", None):
            self._set_status("No YouTube track selected")
            return
        self._set_status(f"Sending {rating}\u2026")

        def work():
            ok = self.yt_api.rate_video(t.video_id, rating)
            verb = "Liked" if rating == "like" else "Disliked"
            self.signals.status_msg.emit(f"{verb} {t.title}" if ok else f"{rating} failed")
        threading.Thread(target=work, daemon=True).start()

    def _yt_like_current(self):
        self._yt_simple(lambda v: self.yt_api.like_video(v), "Liked")

    def _yt_dislike_current(self):
        self._yt_simple(lambda v: self.yt_api.dislike_video(v), "Disliked")

    def _yt_simple(self, fn, verb):
        t = self.engine.current_track
        if not t or not getattr(t, "video_id", None):
            self._set_status("No YouTube track playing")
            return
        self._set_status(f"{verb[:-1]}ing\u2026")

        def work():
            ok = fn(t.video_id)
            self.signals.status_msg.emit(f"{verb} {t.title}" if ok else f"{verb} failed")
        threading.Thread(target=work, daemon=True).start()

    def _yt_export_playlist(self):
        if not self._yt_on():
            self._set_status("YouTube API not configured")
            return
        tracks = list(self.queue.get_history(limit=0))
        if self.engine.current_track:
            tracks.append(self.engine.current_track)
        yt_tracks = [t for t in tracks if getattr(t, "video_id", None)]
        if not yt_tracks:
            self._set_status("No YouTube tracks in history")
            return
        self._set_status(f"Exporting {len(yt_tracks)} tracks\u2026")

        def work():
            def prog(c, total):
                self.signals.status_msg.emit(f"Adding to playlist {c}/{total}")
            result = self.yt_api.export_history_as_playlist(
                yt_tracks, privacy="public", progress_callback=prog)
            if not result:
                self.signals.status_msg.emit("Playlist export failed")
                return
            url = result.get("url") if isinstance(result, dict) else result
            if not url:
                self.signals.status_msg.emit("Playlist export failed")
                return
            try:
                app = QApplication.instance()
                if app:
                    app.clipboard().setText(url)
            except Exception:
                pass
            self.signals.status_msg.emit("Playlist created — link copied")
            print(f"[YT-API] Playlist: {url}")
        threading.Thread(target=work, daemon=True).start()

    # ──────────────────────────────────────────────────────────────────────
    #  Playlist site (optional, self-hosted)
    # ──────────────────────────────────────────────────────────────────────

    def _site_entries(self):
        """(played_entries, earlier_tracks, queued_entries)

        played  = the session log (every song that started playing) with any
                  earlier history songs the log doesn't have put in front —
                  so songs from before the update / before a resume are kept.
        queued  = songs still waiting in Up Next (only sent if ticked)."""
        history = list(self.queue.get_history(limit=0))
        earlier = self.session_log.earlier_tracks(history)
        played = [track_entry(t) for t in earlier] + self.session_log.tracks()
        # The song playing right now belongs at the END. It's normally already
        # logged; if not (e.g. it's still starting), add it after the rest.
        cur = self.engine.current_track
        if cur is not None and getattr(cur, "url", "") \
                and not any(t is cur for t in history) \
                and self.session_log.earlier_tracks([cur]):
            played.append(track_entry(cur))
        queued = [track_entry(t) for t in self.queue.get_list()
                  if not getattr(t, "pending", False) and getattr(t, "url", "")]
        return played, earlier, queued

    def _site_export_dialog(self, default_title, n_played, n_queued, queue_default, again):
        """Title + 'include queue' prompt. Returns (title, include_queue) or None."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Export stream playlist")
        dlg.setStyleSheet(self._app_qss() + f"QDialog {{ background: {THEME['bg']}; }}"
                          f"QCheckBox {{ color: {THEME['text']}; font-size: 13px; }}")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        info = QLabel(f"{n_played} song{'s' if n_played != 1 else ''} played this stream"
                      + (" \u2014 updates the playlist you already published" if again else ""))
        info.setWordWrap(True)
        lay.addWidget(info)
        lay.addWidget(QLabel("Playlist title:"))
        edit = QLineEdit(default_title)
        edit.setObjectName("entry")
        edit.selectAll()
        lay.addWidget(edit)
        box = QCheckBox(f"Also include the {n_queued} song{'s' if n_queued != 1 else ''} "
                        f"still in Up Next")
        box.setChecked(queue_default and n_queued > 0)
        box.setEnabled(n_queued > 0)
        box.setVisible(n_queued > 0)
        lay.addWidget(box)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Publish")
        btns.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primarybtn")
        cancel = btns.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setObjectName("ghostbtn")
        cancel.setMinimumHeight(34)
        cancel.setStyleSheet("font-size: 13px; padding: 0 14px;")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        dlg.resize(420, dlg.sizeHint().height())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return edit.text(), box.isChecked()

    def _site_ask_token(self, settings, reason=""):
        text, ok = QInputDialog.getText(
            self, "Playlist site token",
            (reason + "\n\n" if reason else "") +
            "Paste the PLAYLIST_TOKEN from your playlist site's server.",
            QLineEdit.EchoMode.Password)
        token = (text or "").strip()
        if token.startswith("PLAYLIST_TOKEN="):
            token = token.split("=", 1)[1].strip()
        if not ok or not token:
            return None
        settings["token"] = token
        save_settings(settings)
        return token

    def _site_export(self):
        if self._site_exporting:
            self._set_status("Export already running\u2026")
            return
        played, earlier, queued = self._site_entries()
        if not played and not queued:
            self._set_status("Nothing has played this session yet")
            return

        settings = load_settings()
        if not (settings.get("token") or "").strip():
            if not self._site_ask_token(settings):
                self._set_status("Export cancelled \u2014 no token")
                return

        import datetime
        # Name it after the day the stream started (the site files it under that
        # date too), not the day Export is pressed — those differ after midnight.
        try:
            day = datetime.date.fromisoformat((self.session_log.started_at or "")[:10])
        except ValueError:
            day = datetime.date.today()
        default_title = self.session_log.title or \
            f"Stream \u2014 {day.strftime('%a %b')} {day.day}, {day.year}"
        choice = self._site_export_dialog(default_title, len(played), len(queued),
                                          bool(settings.get("include_queue")),
                                          bool(self.session_log.exported_url))
        if choice is None:
            return
        title, with_queue = choice
        title = (title or "").strip() or default_title
        self.session_log.set_title(title)
        if bool(settings.get("include_queue")) != with_queue and queued:
            settings["include_queue"] = with_queue
            save_settings(settings)
        entries = played + (queued if with_queue else [])
        if not entries:
            self._set_status("Nothing to export")
            return
        self._site_earlier = earlier
        host = (settings.get("host_name") or "").strip() or channel_name()

        self._site_exporting = True
        self.btn_site.setEnabled(False)
        self._set_status(f"Publishing {len(entries)} songs to the playlist site\u2026")
        sid, started = self.session_log.session_id, self.session_log.started_at

        def work():
            try:
                res = export_session(entries, settings, title, sid, started, host_name=host)
                self.signals.site_export_done.emit({"ok": True, "res": res})
            except ExportError as e:
                self.signals.site_export_done.emit({"ok": False, "error": str(e), "code": e.code})
            except Exception as e:
                self.signals.site_export_done.emit({"ok": False, "error": f"{type(e).__name__}: {e}"})
        threading.Thread(target=work, daemon=True).start()

    def _on_site_export_done(self, result):
        self._site_exporting = False
        self.btn_site.setEnabled(True)
        if not result.get("ok"):
            if result.get("code") == 401:
                s = load_settings()
                s["token"] = ""
                save_settings(s)
                self._set_status("Playlist site rejected the token \u2014 press Export and paste it again")
            else:
                self._set_status(f"Playlist export failed \u2014 {result.get('error')}")
            return
        res = result["res"] or {}
        url = res.get("url", "")
        self.session_log.adopt_earlier(getattr(self, "_site_earlier", []))
        self._site_earlier = []
        self.session_log.set_exported(url)
        try:
            QApplication.clipboard().setText(url)
        except Exception:
            pass
        skipped = len(res.get("skipped") or [])
        verb = "Updated" if res.get("replaced") else "Published"
        self._set_status(f"{verb} {res.get('tracks', 0)} songs \u2014 link copied: {url}"
                         + (f" ({skipped} skipped)" if skipped else ""))
        print(f"[Playlist] {verb}: {url}")

    # ──────────────────────────────────────────────────────────────────────
    #  Setup window, chat status, OBS link, updates
    # ──────────────────────────────────────────────────────────────────────

    def open_setup(self, tab=None):
        from setup_dialog import SetupDialog
        if self._setup_dlg is None:
            self._setup_dlg = SetupDialog(self, self.settings, self.chat)
        if isinstance(tab, str):
            self._setup_dlg.show_tab(tab)
        self._setup_dlg.show()
        self._setup_dlg.raise_()
        self._setup_dlg.activateWindow()

    def _on_chat_status(self, platform, level, text):
        b = getattr(self, "pills", {}).get(platform)
        if b is not None:
            b.setProperty("state", level)
            name = {"twitch": "Twitch", "kick": "Kick", "owncast": "Owncast"}.get(platform, platform)
            b.setToolTip(f"{name}: {text}")
            b.style().unpolish(b)
            b.style().polish(b)
        if self._setup_dlg is not None:
            self._setup_dlg.on_chat_status(platform, level, text)

    def obs_link(self):
        return f"http://localhost:{STATUS_PORT}/"

    def copy_obs_link(self):
        try:
            QApplication.clipboard().setText(self.obs_link())
        except Exception:
            pass
        self.btn_obs.setText("COPIED \u2713")
        QTimer.singleShot(1800, lambda: self.btn_obs.setText("COPY OBS LINK"))
        self._set_status(f"OBS link copied: {self.obs_link()} \u2014 in OBS add a "
                         f"Browser source, paste it, size 560 x 500")

    def _on_update_available(self, version, url):
        self.btn_update.setText(f"UPDATE {version} \u2192")
        self.btn_update.setToolTip(f"Version {version} is out \u2014 click to open the download page")
        try:
            self.btn_update.clicked.disconnect()
        except Exception:
            pass
        import desktop
        self.btn_update.clicked.connect(lambda: desktop.open_url(url))
        self.btn_update.show()
        self._set_status(f"A new version ({version}) is available \u2014 click UPDATE above "
                         f"the log to download it")

    def set_volume_from_chat(self, level):
        self.engine.set_volume(level)
        if hasattr(self, "vol"):
            self.vol.blockSignals(True)
            self.vol.setValue(self.engine.volume)
            self.vol.blockSignals(False)

    # ──────────────────────────────────────────────────────────────────────
    #  Status-server API (called from the HTTP server threads)
    # ──────────────────────────────────────────────────────────────────────

    def get_player_status(self):
        t = self.engine.current_track
        st = self.engine.state
        if not t or st == PlayerState.STOPPED:
            return {}
        sc = t.source == TrackSource.SOUNDCLOUD
        mv = (not sc) and t.is_music_video and self.engine.mv_active
        return {
            "title": t.title, "artist": t.artist,
            "videoId": t.video_id or None, "album": t.album or None,
            "imageSrc": t.artwork_url or None,
            "isPaused": st == PlayerState.PAUSED,
            # False while the audio is stuck (buffering, or a song that went
            # quiet waiting for the watchdog): the widget holds its picture.
            "clockRunning": self.engine.clock_running,
            "elapsed": self.engine.elapsed_seconds,
            "elapsedSeconds": self.engine.elapsed_seconds,
            "elapsedAt": time.time(),
            "serverNow": time.time(),
            "syncEngage": SYNC_ENGAGE, "syncRelease": SYNC_RELEASE,
            "syncTrim": SYNC_TRIM, "syncHard": SYNC_HARD,
            "duration": self.engine.duration_seconds,
            "songDuration": self.engine.duration_seconds,
            "durationSeconds": self.engine.duration_seconds,
            "isMV": t.is_music_video, "mvActive": mv,
            "scActive": sc, "scUrl": t.url if sc else "",
            "scTitle": t.title if sc else "", "scArtist": t.artist if sc else "",
            "scArtwork": t.artwork_url if sc else "",
            "scRequestedBy": t.requested_by if sc else "",
            "requestedBy": t.requested_by or "", "source": t.source.value,
        }

    def get_queue_status(self):
        tracks = []
        cur = self.engine.current_track
        base_idx = 0
        if cur and self.engine.state != PlayerState.STOPPED:
            c_dict = cur.to_dict()
            c_dict["title"] = f"▶ [NOW PLAYING] {c_dict.get('title', '')}"
            c_dict["index"] = 0
            tracks.append(c_dict)
            base_idx = 1

        for i, t in enumerate(self.queue.get_list(limit=15)):
            t_dict = t.to_dict()
            t_dict["index"] = base_idx + i
            tracks.append(t_dict)

        total = self.queue.length + (1 if cur else 0)
        return {"tracks": tracks, "length": total}

    # Same user + same request inside this window = one request delivered
    # twice (duplicate chat delivery, or two bots on one platform). Dropping
    # it here means a doubled chat message can never double-queue a song.
    ADDSONG_DEDUPE_SECONDS = 5.0

    def api_addsong(self, text, user, platform, on_done=None):
        """Entry point for every chat/web request (HTTP API and the in-process
        Kick bot). on_done(result) fires once with the real outcome."""
        key = ((user or "").strip().lower(), clean_request(text).lower())
        now = time.monotonic()
        with self._addsong_lock:
            recent = self._recent_addsong
            for k in [k for k, t in recent.items()
                      if now - t > self.ADDSONG_DEDUPE_SECONDS]:
                del recent[k]
            if key in recent:
                print(f"[Queue] Duplicate request ignored: {user}: {text}")
                # Bots stay silent on "duplicate" — the first copy replies.
                self._report(on_done, False, "duplicate", "")
                return True
            recent[key] = now
        threading.Thread(target=self._enqueue_request,
                         args=(text, user, on_done, platform), daemon=True).start()
        return True

    # ──────────────────────────────────────────────────────────────────────
    #  Persistence
    # ──────────────────────────────────────────────────────────────────────

    def save_state(self):
        save_queue_state(self.engine, self.queue)

    def start_new_session(self):
        try:
            p = _state_path()
            if os.path.isfile(p):
                os.remove(p)
        except Exception as e:
            print(f"[Session] Could not clear saved session: {e}")
        self.session_log.reset()
        self.signals.status_msg.emit("New session")

    def restore_queue_state(self):
        state = load_queue_state()
        if not state:
            return
        for d in state.get("history", []):
            t = dict_to_track(d)
            if t:
                self.queue.push_to_history(t)
        for d in state.get("queue", []):
            t = dict_to_track(d)
            if t:
                self.queue.add(t)

        cur = state.get("current")
        if cur:
            self.signals.status_msg.emit("Restoring last session\u2026")
            threading.Thread(target=self._restore_current,
                             args=(cur, state.get("current_elapsed", 0)),
                             daemon=True).start()
        else:
            self._advance_if_idle()
        self._refresh_queue()

    def _restore_current(self, track_dict, elapsed):
        try:
            track = dict_to_track(track_dict)
            if not track:
                return
            if not self.engine.resolve_stream_url(track):
                self.signals.status_msg.emit("Couldn't restore — playing next")
                self._advance_if_idle()
                return
            track.stream_url = track.stream_url or ""
            with self._playback_lock:
                self._pending_start = track
                self.engine.play_track(track)
            if elapsed > 5:
                import time
                time.sleep(1.5)
                self.engine.seek(elapsed)
            self.signals.status_msg.emit(f"Restored — {track.artist} \u2013 {track.title}")
        except Exception as e:
            self.signals.status_msg.emit(f"Restore error — {e}")
            self._advance_if_idle()

    # ──────────────────────────────────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────────────────────────────────

    def _set_dot(self, colour):
        pm = QPixmap(10, 10)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(colour))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(0, 0, 10, 10)
        p.end()
        self.dot.setPixmap(pm)

    @staticmethod
    def _fmt(sec):
        s = max(0, int(sec))
        if s >= 3600:
            return f"{s//3600}:{(s%3600)//60:02d}:{s%60:02d}"
        return f"{s//60}:{s%60:02d}"

    # ──────────────────────────────────────────────────────────────────────
    #  Stylesheet
    # ──────────────────────────────────────────────────────────────────────

    def _app_qss(self):
        t = THEME
        return f"""
        QWidget#root {{ background: {t['bg']}; }}
        QWidget {{ color: {t['text']}; font-family: 'Segoe UI', 'Helvetica Neue', Arial; }}

        QFrame#card {{
            background: {t['panel']};
            border: 1px solid {t['stroke']};
            border-radius: 16px;
        }}

        QLabel#wordmark {{ font-size: 15px; font-weight: 800; letter-spacing: 1px; }}
        QLabel#eyebrow {{ color: {t['faint']}; font-size: 10px; font-weight: 700;
                          letter-spacing: 2px; }}
        QLabel#title  {{ font-size: 21px; font-weight: 800; }}
        QLabel#artist {{ color: {t['muted']}; font-size: 13px; }}
        QLabel#time   {{ color: {t['faint']}; font-size: 11px; }}

        QLineEdit#entry {{
            background: {t['panel2']};
            border: 1px solid {t['stroke']};
            border-radius: 11px;
            padding: 9px 12px;
            font-size: 13px;
            selection-background-color: {t['accent']};
        }}
        QLineEdit#entry:focus {{ border: 1px solid {t['accent']}; }}

        QPushButton#primarybtn {{
            background: {t['accent']};
            color: white;
            border: none;
            border-radius: 11px;
            padding: 9px 16px;
            font-size: 13px;
            font-weight: 700;
        }}
        QPushButton#primarybtn:hover {{ background: {t['accent_2']}; }}
        QPushButton#primarybtn:pressed {{ background: {t['accent']}; }}

        QPushButton#playbtn {{
            background: {t['accent']};
            color: white;
            border: none;
            border-radius: 29px;
            font-size: 22px;
            padding-left: 2px;
        }}
        QPushButton#playbtn:hover {{ background: {t['accent_2']}; }}

        QPushButton#ghostbtn {{
            background: transparent;
            color: {t['muted']};
            border: 1px solid {t['stroke']};
            border-radius: 10px;
            font-size: 16px;
        }}
        QPushButton#ghostbtn:hover {{ color: {t['text']}; border-color: {t['accent']}; }}

        QPushButton#pill {{
            background: rgba(255,255,255,0.04); color: {t['faint']};
            border: 1px solid {t['stroke']}; border-radius: 10px;
            padding: 3px 7px; font-size: 10px; font-weight: 700;
        }}
        QPushButton#pill:hover {{ border-color: {t['accent']}; }}
        QPushButton#pill[state="ok"]         {{ color: {t['ok']}; }}
        QPushButton#pill[state="warn"]       {{ color: {t['warn']}; }}
        QPushButton#pill[state="connecting"] {{ color: {t['warn']}; }}
        QPushButton#pill[state="error"]      {{ color: {t['accent']}; }}

        QPushButton#linkbtn, QPushButton#linkbtn:flat {{
            background: transparent; color: {t['faint']};
            border: none; font-size: 11px; font-weight: 700; letter-spacing: 1px;
        }}
        QPushButton#linkbtn:hover {{ color: {t['accent']}; }}

        QListWidget#queue {{
            background: {t['panel2']};
            border: 1px solid {t['stroke']};
            border-radius: 12px;
            padding: 4px;
            outline: none;
            font-size: 13px;
        }}
        QListWidget#queue::item {{
            padding: 4px 8px;
            border-radius: 8px;
        }}
        QListWidget#queue::item:selected {{
            background: rgba(255,46,77,0.16);
            color: {t['text']};
        }}
        QListWidget#queue::item:hover {{ background: rgba(255,255,255,0.04); }}

        QListWidget#loglist {{
            background: transparent;
            border: none;
            outline: none;
            font-size: 12px;
            color: {t['muted']};
            font-family: Consolas, 'Courier New', monospace;
        }}
        QListWidget#loglist::item {{
            padding: 2px 0px;
        }}
        QListWidget#loglist::item:hover {{
            background: transparent;
        }}

        QScrollBar:vertical {{ background: transparent; width: 9px; margin: 4px 2px; }}
        QScrollBar::handle:vertical {{
            background: {t['stroke']}; border-radius: 4px; min-height: 28px; }}
        QScrollBar::handle:vertical:hover {{ background: {t['faint']}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

        QSlider::groove:horizontal {{
            background: rgba(255,255,255,0.12);
            height: 5px; border-radius: 2px;
        }}
        QSlider#seek::sub-page:horizontal {{
            background: {t['accent']}; height: 5px; border-radius: 2px;
        }}
        QSlider#seek::handle:horizontal {{
            background: white; width: 13px; height: 13px;
            margin: -4px 0; border-radius: 6px;
        }}
        QSlider#vol::sub-page:horizontal {{
            background: {t['muted']}; height: 5px; border-radius: 2px;
        }}
        QSlider#vol::handle:horizontal {{
            background: {t['muted']}; width: 11px; height: 11px;
            margin: -3px 0; border-radius: 5px;
        }}
        QSlider#vol::handle:horizontal:hover {{ background: {t['text']}; }}

        QMenu {{
            background: {t['panel2']};
            border: 1px solid {t['stroke']};
            border-radius: 10px;
            padding: 6px;
        }}
        QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
        QMenu::item:selected {{ background: {t['accent']}; color: white; }}
        QMenu::separator {{ height: 1px; background: {t['stroke']}; margin: 5px 8px; }}

        QToolTip {{
            background: {t['panel2']}; color: {t['text']};
            border: 1px solid {t['stroke']}; padding: 5px 8px; border-radius: 6px;
        }}
        """


# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════

def _widget_theme():
    """theme.json "widget" section → the OBS widget's /theme endpoint."""
    path = os.path.join(app_paths.data_dir(), THEME_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        w = d.get("widget") if isinstance(d, dict) else None
        return w if isinstance(w, dict) else {}
    except Exception:
        return {}


def _version_tuple(v):
    out = []
    for part in str(v).lstrip("vV").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def _check_for_update(signals):
    """Tell the window when a newer release is on GitHub (once per launch)."""
    from version import __version__
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{app_paths.REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"BLOODBOS-Radio/{__version__}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        tag = (d.get("tag_name") or "").strip()
        if tag and _version_tuple(tag) > _version_tuple(__version__):
            signals.update_available.emit(tag.lstrip("vV"), d.get("html_url") or
                                          f"https://github.com/{app_paths.REPO}/releases/latest")
    except Exception as e:
        print(f"[Update] check skipped: {type(e).__name__}")


def run(app, settings, tools):
    """Build everything and run the Qt event loop. Returns the exit code."""
    configure(settings)
    load_config()

    engine = AudioEngine()
    queue = QueueManager()
    yt_api = YouTubeAPI() if YouTubeAPI else None
    signals = SignalBridge()

    engine.on_track_ended = lambda: signals.track_ended.emit()
    engine.on_state_changed = lambda s: signals.state_changed.emit(s.value)
    engine.on_error = lambda m: signals.error.emit(m)
    engine.on_notice = lambda m: signals.status_msg.emit(m)
    queue.on_queue_changed = lambda: signals.queue_changed.emit()
    tools._notice = lambda m: signals.status_msg.emit(m)

    window = PlayerWindow(engine, queue, signals, yt_api=yt_api, settings=settings)
    settings.on_change(lambda section: configure(settings) if section in ("player", "app") else None)

    server = StatusServer(
        player_status_fn=window.get_player_status,
        queue_fn=window.get_queue_status,
        get_video_url_fn=engine.get_video_stream_url,
        get_theme_fn=_widget_theme,
        on_addsong=window.api_addsong,
        on_skip=lambda: signals.run_on_gui.emit(window._on_skip),
        on_back=lambda: signals.run_on_gui.emit(window._on_back),
        on_play=lambda: engine.resume(),
        on_pause=lambda: engine.pause(),
        on_volume=lambda v: signals.run_on_gui.emit(lambda: window.set_volume_from_chat(v)),
        port=STATUS_PORT,
    )
    server.start()

    # ── chat bots (Twitch, Kick, Owncast — all inside this process) ──
    from chat.manager import ChatManager
    chat = ChatManager(window, settings,
                       call_on_gui=signals.run_on_gui.emit,
                       on_status=signals.chat_status.emit,
                       notice=signals.status_msg.emit)
    window.chat = chat

    # Media keys (optional). Skipped on macOS, where listening to the keyboard
    # needs an Accessibility permission prompt nobody wants to explain.
    if sys.platform != "darwin":
        try:
            from pynput import keyboard

            def on_press(key):
                try:
                    if key == keyboard.Key.media_play_pause:
                        signals.run_on_gui.emit(window._on_play_pause)
                    elif key == keyboard.Key.media_next:
                        signals.run_on_gui.emit(window._on_skip)
                    elif key == keyboard.Key.media_previous:
                        signals.run_on_gui.emit(window._on_back)
                except Exception:
                    pass

            listener = keyboard.Listener(on_press=on_press)
            listener.daemon = True
            listener.start()
            print("[MediaKeys] Enabled")
        except Exception:
            print("[MediaKeys] not available")

    # First thing at launch: recover the previous session, or start fresh?
    # The prompt only appears when there's actually a saved session to resume
    # (a crash or an unclean exit) — a truly clean start opens straight away.
    info = saved_session_info()
    do_resume = False
    if info:
        choice = SessionDialog.ask(info)
        do_resume = (choice == "resume")
        if not do_resume:
            window.start_new_session()
    else:
        # No saved queue at all. Usually a genuinely fresh start — but if the
        # queue file was lost in a crash, a song log from the last few hours is
        # the stream still in progress, so it's kept.
        window.session_log.reset_if_stale(hours=6)

    window.show()
    if do_resume:
        window.restore_queue_state()

    chat.start()
    # Replay the current status of every platform into the header pills.
    for p in ("twitch", "kick", "owncast"):
        lvl, txt = chat.status(p)
        window._on_chat_status(p, lvl, txt)

    if not settings.get("first_run_done"):
        settings.update("first_run_done", True, notify=False)
        QTimer.singleShot(300, window.open_setup)

    if settings.get("player", "check_updates", True):
        threading.Thread(target=_check_for_update, args=(signals,), daemon=True).start()

    save_timer = QTimer()
    save_timer.timeout.connect(window.save_state)
    save_timer.start(30000)

    from version import __version__
    print("=" * 52)
    print(f"  {APP_NAME} {__version__}")
    print(f"  Data folder : {app_paths.data_dir()}")
    print(f"  YouTube API : {'enabled' if (yt_api and getattr(yt_api,'enabled',False)) else 'off'}")
    print(f"  OBS / API   : http://localhost:{STATUS_PORT}")
    print("=" * 52)

    def on_quit():
        window.save_state()
        try:
            chat.stop()
        except Exception:
            pass
        if getattr(window, "video", None) is not None:
            window.video.release()
        if getattr(window, "video_win", None) is not None:
            window.video_win.deleteLater()
        engine.cleanup()
        server.stop()

    app.aboutToQuit.connect(on_quit)
    return app.exec()


if __name__ == "__main__":
    import main as _main
    sys.exit(_main.main())
