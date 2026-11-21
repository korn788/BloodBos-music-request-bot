# audio_engine.py — Audio/Video playback engine using VLC + yt-dlp
# Handles YouTube + SoundCloud with music video detection

import os
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, Callable
from urllib.parse import urlparse, parse_qs

import vlc_loader

vlc = vlc_loader.load()
if vlc is None:
    raise ImportError("VLC is not available: " + vlc_loader.error())

# Hide console windows on Windows when running yt-dlp
_STARTUPINFO = None
if platform.system() == "Windows":
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _STARTUPINFO.wShowWindow = 0  # SW_HIDE

# Set by main.py: the app settings and the helper-program manager (tools.py).
_settings = None
_tools = None


def configure(settings=None, tools=None):
    global _settings, _tools
    _settings = settings
    _tools = tools


# Browser to pull cookies from for age-restricted / login-gated content.
# Options: "chrome", "firefox", "edge", "brave", "opera", "chromium", "vivaldi",
# "safari" — or "none" (the default). Chosen in Settings; the environment
# variable YTDLP_COOKIE_BROWSER overrides it.
_COOKIE_BROWSERS = {"chrome", "firefox", "edge", "brave", "opera", "chromium",
                    "vivaldi", "safari", "whale"}


def _cookie_browser():
    b = os.environ.get("YTDLP_COOKIE_BROWSER", "")
    if not b and _settings is not None:
        b = _settings.get("player", "cookies_browser") or ""
    b = (b or "").strip().lower()
    return b if b in _COOKIE_BROWSERS else None


# yt-dlp is background work — looking up the next songs, music videos and chat
# requests. Each run also starts a JavaScript runtime to solve YouTube's
# challenges, which pins a CPU core for a second or two; several at once (a
# chat request + prefetch of the next two songs + the video) competed with the
# song that's playing and with OBS. Run them at low CPU priority so playback
# always wins. The counter lets stall notices say what was running.
_LOW_PRIORITY = {}
if platform.system() == "Windows":
    _LOW_PRIORITY["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)
_ytdlp_active = 0
_ytdlp_active_lock = threading.Lock()

# yt-dlp is a Python program: make it print UTF-8 so song titles in any
# language survive the trip (Windows pipes default to the ANSI code page).
_CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


def ytdlp_active() -> int:
    """How many yt-dlp lookups are running right now."""
    return _ytdlp_active


def _ytdlp(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """subprocess.run for yt-dlp: low priority, no console window, counted."""
    global _ytdlp_active
    if _tools is not None:
        _tools.wait_ready(timeout=120)      # first launch: helpers still downloading
    if platform.system() != "Windows" and shutil.which("nice"):
        args = ["nice", "-n", "10"] + args
    with _ytdlp_active_lock:
        _ytdlp_active += 1
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", env=_CHILD_ENV,
                              startupinfo=_STARTUPINFO, **_LOW_PRIORITY)
    finally:
        with _ytdlp_active_lock:
            _ytdlp_active -= 1


def _ytdlp_base_args(cookies: bool = True) -> list[str]:
    """Common yt-dlp flags: the managed yt-dlp, its JS runtime, optional cookies.

    cookies=False for SoundCloud. The browser cookies are there for
    age-restricted YouTube videos; sent to SoundCloud they log yt-dlp in as
    you, and a logged-in account can get a track back copy-protected that
    plays fine for a guest.
    """
    cmd = _tools.ytdlp_cmd() if _tools is not None else ["yt-dlp"]
    args = cmd + ["--no-warnings", "--no-playlist", "--socket-timeout", "10",
                  "--remote-components", "ejs:github"]
    if _tools is not None:
        args += _tools.js_runtime_args()
    browser = _cookie_browser() if cookies else None
    if browser:
        args += ["--cookies-from-browser", browser]
    return args


# For the few pages the app opens itself (SoundCloud short links): look like a
# browser, not a script.
_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# Why yt-dlp turned a link down, read from its ERROR line. These are final
# answers (trying again gives the same one); anything else, like a network
# blip, gets one more try.
_FAILURES = (
    ("drm", ("drm protected",)),          # SoundCloud: label releases are encrypted
    ("geo", ("geo restrict", "not available from your location",
             "not available in your country")),
    ("gone", ("http error 404", "video unavailable", "has been removed",
              "this video has been terminated")),
    ("private", ("http error 401", "private video")),
    ("bad_link", ("unsupported url", "is not a valid url")),
)


def ytdlp_failure(stderr: str) -> str:
    """drm | geo | gone | private | bad_link, or "" when it's not clear-cut."""
    errors = " ".join(line for line in (stderr or "").lower().splitlines()
                      if "error" in line)
    for reason, needles in _FAILURES:
        if any(n in errors for n in needles):
            return reason
    return ""


def _error_line(stderr: str) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() if "ERROR" in ln]
    return (lines[-1] if lines else (stderr or "").strip())[:300]


# Values that mean "we don't have real metadata yet."
#
# player_qt._enqueue_request queues a placeholder row the instant a request
# lands, titled "Loading…" with artist "…", then relies on resolve_stream_url
# to fill in the real values. The old guard here was `== "Unknown"`, which
# those placeholders never match — so yt-dlp fetched correct metadata and it
# was silently discarded, leaving every URL request stuck on "Loading…".
_PLACEHOLDER_TITLES = {"", "unknown", "loading\u2026", "loading...", "na"}
_PLACEHOLDER_ARTISTS = {"", "unknown", "\u2026", "...", "na"}


def _is_placeholder(value: str, pool: set) -> bool:
    return (value or "").strip().lower() in pool


class TrackSource(str, Enum):
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"


@dataclass
class Track:
    title: str = "Unknown"
    artist: str = "Unknown"
    url: str = ""
    stream_url: str = ""          # audio stream
    video_stream_url: str = ""    # video stream (for MVs)
    source: TrackSource = TrackSource.YOUTUBE
    video_id: str = ""
    artwork_url: str = ""
    duration_seconds: int = 0
    requested_by: str = ""
    is_music_video: bool = False  # True if this track has a music video
    album: str = ""               # empty album often means MV
    platform: str = ""            # twitch / kick / owncast / "" (added locally)

    def to_dict(self):
        d = asdict(self)
        d["source"] = self.source.value
        return d


class PlayerState(str, Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"
    LOADING = "loading"


class AudioEngine:
    """VLC-based audio player with yt-dlp stream resolution and MV support"""

    def __init__(self):
        # BLOODBOS_VLC_ARGS adds VLC options, e.g. "--aout=pulse" to pick an
        # audio system, or "--aout=dummy" for silent testing.
        import shlex
        extra = shlex.split(os.environ.get("BLOODBOS_VLC_ARGS", ""))
        self._instance = vlc.Instance("--no-video", "--quiet", "--no-xlib", *extra)
        self._player: vlc.MediaPlayer = self._instance.media_player_new()
        self._state = PlayerState.STOPPED
        self._current_track: Optional[Track] = None
        self._volume = 50
        self._player.audio_set_volume(self._volume)
        self._mv_active = False  # True when MV is being shown in widget
        self._manually_stopped = False  # True when stop() called by user/skip
        self._play_generation = 0  # Incremented on every new play_track call; lets watchdogs self-cancel
        # The end of a track can be noticed by VLC's EndReached event AND by the
        # watchdog at almost the same moment; reporting it twice made the queue
        # treat the NEXT song (just starting) as a failed start. Each play
        # generation now reports its end exactly once.
        self._ended_gen = -1
        self._end_lock = threading.Lock()
        self._last_elapsed = 0.0   # last real position reading (see elapsed_seconds)
        self._elapsed_moved = 0.0  # monotonic time that reading last changed (see clock_running)

        # Callbacks
        self.on_track_ended: Optional[Callable] = None
        self.on_state_changed: Optional[Callable[[PlayerState], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_notice: Optional[Callable[[str], None]] = None   # activity-log line
        self._seek_at = 0.0       # last seek; the clock jumps then, it isn't a stall

        events = self._player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_end_reached)
        events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_vlc_error)

        # Stream URL cache
        self._url_cache: dict[str, dict] = {}
        self._video_cache: dict[str, dict] = {}
        # Videos YouTube just refused: don't run yt-dlp for them again until
        # this time. Without it, the prefetcher and the OBS widget re-ran a
        # failing lookup back to back for as long as the song played.
        self._video_fail_until: dict[str, float] = {}
        self._video_forced_at: dict[str, float] = {}
        # 300s meant a song requested twice in one stream — which happens
        # constantly — paid a full yt-dlp resolve the second time. googlevideo
        # and SoundCloud URLs outlive this comfortably, and a stale entry is
        # already handled: play_track's retry busts the cache and re-resolves.
        self._cache_ttl = 3600
        self._resolve_lock = threading.Lock()

    @property
    def state(self) -> PlayerState:
        return self._state

    @property
    def current_track(self) -> Optional[Track]:
        return self._current_track

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def mv_active(self) -> bool:
        return self._mv_active

    @mv_active.setter
    def mv_active(self, val: bool):
        self._mv_active = val

    @property
    def elapsed_seconds(self) -> float:
        """Song position in seconds.

        VLC only reports a time while it's strictly playing. In between — the
        instant a song ends, a buffering hiccup — this used to drop to 0, and
        the music-video players read that 0 as "the song started over" and
        rewound the video. Now the last real reading is held; the position only
        goes back to 0 when a new song starts or playback stops.
        """
        st = self._state
        if st in (PlayerState.PLAYING, PlayerState.PAUSED):
            if self._player.is_playing() or st == PlayerState.PAUSED:
                ms = self._player.get_time()
                if ms is not None and ms >= 0:
                    value = ms / 1000.0
                    if value != self._last_elapsed:
                        self._last_elapsed = value
                        self._elapsed_moved = time.monotonic()
            return self._last_elapsed
        return 0.0

    # VLC moves the song position about four times a second. Not moving for
    # this long while "playing" means the audio really is stuck.
    CLOCK_STALL = 1.0

    @property
    def clock_running(self) -> bool:
        """True while the song's position is really moving forward.

        False while paused or loading, while VLC is buffering, and while a song
        that has stopped making sound waits for the watchdog to end it. The
        music-video players hold their picture while this is False. They used
        to play on against a frozen position and get pulled back every few
        seconds: the loop right before a song ended.
        """
        if self._state != PlayerState.PLAYING:
            return False
        self.elapsed_seconds                     # take a fresh reading
        return time.monotonic() - self._elapsed_moved < self.CLOCK_STALL

    @property
    def duration_seconds(self) -> float:
        if self._current_track and self._current_track.duration_seconds > 0:
            return self._current_track.duration_seconds
        ms = self._player.get_length()
        return max(0, ms / 1000.0) if ms >= 0 else 0

    def _set_state(self, state: PlayerState):
        self._state = state
        if self.on_state_changed:
            try:
                self.on_state_changed(state)
            except Exception:
                pass

    def _claim_end(self, gen=None) -> bool:
        """True exactly once per play generation (see _ended_gen)."""
        with self._end_lock:
            g = self._play_generation if gen is None else gen
            if g != self._play_generation or g == self._ended_gen:
                return False
            self._ended_gen = g
            return True

    def _on_end_reached(self, event):
        if self._manually_stopped or not self._claim_end():
            return
        self._set_state(PlayerState.STOPPED)
        self._mv_active = False
        if self.on_track_ended:
            threading.Thread(target=self.on_track_ended, daemon=True).start()

    def _on_vlc_error(self, event):
        if self._manually_stopped or not self._claim_end():
            return
        self._set_state(PlayerState.STOPPED)
        self._mv_active = False
        if self.on_error:
            self.on_error("VLC playback error")
        if self.on_track_ended:
            threading.Thread(target=self.on_track_ended, daemon=True).start()

    def _start_watchdog(self, is_hls: bool = False):
        """Background watchdog: detects when VLC stops playing (for HLS streams
        where EndReached may not fire). Checks every 2 seconds."""
        my_generation = self._play_generation  # captured at start — if it changes, this watchdog is stale

        def _watch():
            time.sleep(12 if is_hls else 5)  # HLS needs longer grace period to buffer
            stall_count = 0
            last_time = -1
            # Audible-stall tracking: the song's clock should advance 2s per
            # 2s sample. When it falls behind, VLC ran out of data and the
            # listener (and the stream) heard a gap.
            clock = None            # (wall time, media seconds) at the last sample
            gap = 0.0               # seconds lost in the current stall
            last_notice = 0.0
            while self._state in (PlayerState.PLAYING, PlayerState.LOADING):
                time.sleep(2)
                # Exit if a new track has started (generation changed) or user stopped
                if self._play_generation != my_generation or self._manually_stopped:
                    return
                if self._state == PlayerState.PAUSED:
                    stall_count = 0
                    clock = None
                    continue
                if self._state == PlayerState.STOPPED:
                    break

                vlc_state = self._player.get_state()
                current_time = self._player.get_time()

                now = time.time()
                media = current_time / 1000.0
                dur = self.duration_seconds
                measurable = (self._state == PlayerState.PLAYING and current_time >= 0
                              and vlc_state in (vlc.State.Playing, vlc.State.Buffering)
                              and now - self._seek_at > 4
                              and not (dur > 0 and dur - media < 4))
                if measurable and clock is not None:
                    lost = (now - clock[0]) - (media - clock[1])
                    if lost > 0.75:            # get_time() jitters ~0.25s; this is real
                        gap += lost
                    elif gap:
                        if gap >= 1.0 and now - last_notice > 20:
                            last_notice = now
                            src = ("SoundCloud" if self._current_track
                                   and self._current_track.source == TrackSource.SOUNDCLOUD
                                   else "YouTube")
                            busy = ytdlp_active()
                            self._notice(
                                f"Audio stalled {gap:.1f}s waiting for data from {src} "
                                f"(network)" + (f" — {busy} song lookup"
                                                f"{'s' if busy != 1 else ''} running"
                                                if busy else ""))
                        gap = 0.0
                clock = (now, media) if measurable else None

                # VLC reports Ended or Stopped
                if vlc_state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
                    if self._play_generation != my_generation or self._manually_stopped:
                        return
                    if not self._claim_end(my_generation):
                        return
                    print(f"[Watchdog] VLC state: {vlc_state} — triggering track end")
                    self._set_state(PlayerState.STOPPED)
                    self._mv_active = False
                    if self.on_track_ended:
                        self.on_track_ended()
                    return

                # Check if playback stalled (time not advancing while supposedly playing)
                if self._state == PlayerState.PLAYING:
                    if current_time == last_time and current_time >= 0:
                        stall_count += 1
                        if stall_count >= 3:  # 6 seconds of no progress
                            dur = self.duration_seconds
                            elapsed = self.elapsed_seconds
                            if dur > 0 and elapsed > 0 and (dur - elapsed) < 5:
                                if self._play_generation != my_generation or self._manually_stopped:
                                    return
                                if not self._claim_end(my_generation):
                                    return
                                print(f"[Watchdog] Track stalled near end — triggering track end")
                                self._set_state(PlayerState.STOPPED)
                                self._mv_active = False
                                if self.on_track_ended:
                                    self.on_track_ended()
                                return
                            stall_count = 0
                    else:
                        stall_count = 0
                    last_time = current_time

        threading.Thread(target=_watch, daemon=True).start()

    def play_track(self, track: Track):
        """Resolve stream URL if needed and start playing"""
        # Stop any currently playing track first
        self._player.stop()
        time.sleep(0.1)

        self._play_generation += 1  # invalidates any watchdog from the previous track
        gen = self._play_generation
        self._last_elapsed = 0.0
        self._elapsed_moved = 0.0
        self._set_state(PlayerState.LOADING)
        self._current_track = track
        self._mv_active = False
        self._manually_stopped = False

        def _load_and_play():
            try:
                for play_attempt in range(2):  # retry once with a fresh stream URL if VLC fails to start
                    # Resolve audio stream — on retry, bust cache and re-resolve
                    if play_attempt == 0:
                        stream = track.stream_url
                        if not stream:
                            stream = self.resolve_stream_url(track)
                    else:
                        print(f"[VLC] Retrying with fresh stream URL for: {track.title}")
                        track.stream_url = ""
                        # Remove stale cache entry so resolve_stream_url fetches a new one
                        with self._resolve_lock:
                            self._url_cache.pop(track.url, None)
                        stream = self.resolve_stream_url(track)

                    if gen != self._play_generation:
                        return      # a newer song took over while this one was looked up
                    if not stream:
                        if not self._claim_end(gen):
                            return
                        self._set_state(PlayerState.STOPPED)
                        if self.on_error:
                            self.on_error(f"Could not resolve: {track.title}")
                        if self.on_track_ended:
                            self.on_track_ended()
                        return
                    track.stream_url = stream

                # Check for music video (YouTube only)
                    if track.source == TrackSource.YOUTUBE and track.video_id:
                        if track.is_music_video or self._detect_music_video(track):
                            track.is_music_video = True
                            threading.Thread(
                                target=self._resolve_video_stream,
                                args=(track,), daemon=True
                            ).start()

                    # Make sure player is fully stopped before loading new media
                    self._player.stop()
                    time.sleep(0.1)

                    media = self._instance.media_new(stream)
                    # HLS streams (SoundCloud) need more buffering time
                    is_hls = "m3u8" in stream.lower() or "soundcloud.com" in (track.url or "").lower()
                    media.add_option(f":network-caching={'8000' if is_hls else '3000'}")
                    if is_hls:
                        media.add_option(":live-caching=8000")
                    media.add_option(":no-video")
                    self._player.set_media(media)
                    self._player.audio_set_volume(self._volume)
                    self._player.play()
                    time.sleep(0.05)

                    # Verify playback actually started. Poll at 100ms instead
                    # of sleeping 0.5s up front then checking every 0.5s —
                    # same 15s ceiling, but a stream that opens fast is
                    # detected right away rather than paying a fixed toll on
                    # every single track.
                    started = False
                    for _ in range(150):  # wait up to 15 seconds
                        vlc_state = self._player.get_state()
                        if vlc_state == vlc.State.Playing:
                            started = True
                            break
                        elif vlc_state in (vlc.State.Error, vlc.State.Ended):
                            print(f"[VLC] Failed to play: {track.title} (state: {vlc_state})")
                            break
                        if self._manually_stopped or gen != self._play_generation:
                            return
                        time.sleep(0.1)

                    if not started:
                        print(f"[VLC] Could not start playback for: {track.title} (attempt {play_attempt + 1})")
                        if play_attempt == 0:
                            continue  # retry with a fresh stream URL
                        # Both attempts failed — give up and advance queue
                        if not self._claim_end(gen):
                            return
                        self._set_state(PlayerState.STOPPED)
                        if self.on_error:
                            self.on_error(f"Could not play: {track.title}")
                        if self.on_track_ended:
                            self.on_track_ended()
                        return

                    # Get duration from VLC
                    for _ in range(20):
                        length = self._player.get_length()
                        if length > 0:
                            track.duration_seconds = int(length / 1000)
                            break
                        time.sleep(0.2)

                    self._set_state(PlayerState.PLAYING)

                    # Start watchdog for HLS streams (SoundCloud etc)
                    self._start_watchdog(is_hls=is_hls)

                    # If MV, mark as active once video stream is ready
                    if track.source == TrackSource.YOUTUBE and track.is_music_video and track.video_id:
                        for _ in range(30):  # wait up to 15s for video resolve
                            # Bail if a different track started playing
                            if self._current_track is not track:
                                break
                            if self._manually_stopped:
                                break
                            if track.video_stream_url:
                                # Double-check we're still on this track
                                if self._current_track is track:
                                    self._mv_active = True
                                    print(f"[MV] Active: {track.title}")
                                break
                            time.sleep(0.5)

                    break  # playback started successfully — exit retry loop

            except Exception as e:
                if not self._claim_end(gen):
                    return
                self._set_state(PlayerState.STOPPED)
                if self.on_error:
                    self.on_error(str(e))
                if self.on_track_ended:
                    self.on_track_ended()

        threading.Thread(target=_load_and_play, daemon=True).start()

    def pause(self):
        if self._state == PlayerState.PLAYING:
            self._player.pause()
            self._set_state(PlayerState.PAUSED)

    def resume(self):
        if self._state == PlayerState.PAUSED:
            self._player.play()
            self._set_state(PlayerState.PLAYING)
        elif self._state == PlayerState.STOPPED and self._current_track:
            self.play_track(self._current_track)

    def toggle_pause(self):
        if self._state == PlayerState.PLAYING:
            self.pause()
        elif self._state == PlayerState.PAUSED:
            self.resume()

    def stop(self):
        self._manually_stopped = True
        self._last_elapsed = 0.0
        self._player.stop()
        self._set_state(PlayerState.STOPPED)
        self._current_track = None
        self._mv_active = False

    def set_volume(self, level: int):
        self._volume = max(0, min(100, level))
        self._player.audio_set_volume(self._volume)

    def seek(self, seconds: float):
        if self._player.is_playing() or self._state == PlayerState.PAUSED:
            self._seek_at = time.time()
            self._player.set_time(int(seconds * 1000))

    def _notice(self, msg: str):
        print(f"[Audio] {msg}")
        if self.on_notice:
            try:
                self.on_notice(msg)
            except Exception:
                pass

    # ── Music Video detection ──

    def _detect_music_video(self, track: Track) -> bool:
        """Heuristic: a YouTube track is likely an MV if it has no album info
        or if the title contains common MV indicators"""
        t = (track.title or "").lower()
        mv_indicators = ["official music video", "official video", "music video",
                         "(official)", "[official]", "m/v", "mv"]
        for ind in mv_indicators:
            if ind in t:
                return True
        # No album usually means it's a video, not a song
        if not track.album and track.source == TrackSource.YOUTUBE:
            return True
        return False

    def _resolve_video_stream(self, track: Track):
        """Resolve a video (not audio-only) stream URL for MV playback"""
        if not track.video_id:
            return
        vid = track.video_id

        # Check cache
        with self._resolve_lock:
            cached = self._video_cache.get(vid)
            if cached and cached["expires"] > time.time():
                track.video_stream_url = cached["url"]
                return
            if self._video_fail_until.get(vid, 0) > time.time():
                return          # failed moments ago — same answer, skip the yt-dlp run

        try:
            url = f"https://www.youtube.com/watch?v={vid}"
            # Try format 22 (720p mp4) then 18 (360p mp4) then best single-stream
            result = _ytdlp(
                _ytdlp_base_args() +
                [
                 "-f", "22/18/best[ext=mp4]/best[ext=webm]/bestvideo*/best",
                 "--get-url", url],
                timeout=45
            )

            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                video_url = lines[0].strip()
                if video_url.startswith("http"):
                    with self._resolve_lock:
                        self._video_cache[vid] = {
                            "url": video_url,
                            "expires": time.time() + self._cache_ttl
                        }
                        self._video_fail_until.pop(vid, None)
                    track.video_stream_url = video_url
                    print(f"[MV] Video stream resolved for: {track.title}")
                    return

            print(f"[MV] Could not resolve video for {vid}")
        except Exception as e:
            print(f"[MV] Video resolve error: {e}")
        with self._resolve_lock:
            self._video_fail_until[vid] = time.time() + 30

    def get_video_stream_url(self, video_id: str, force: bool = False) -> Optional[str]:
        """Get cached or fresh video stream URL for proxy endpoint.

        force=True drops the cached URL first. The spool asks for that after
        googlevideo answers 403/410 — before, this signature had no force
        argument, the spool's call fell back to the plain one, and it got the
        same dead cached URL back on every retry, so the video went black and
        kept hammering YouTube with 403s for up to an hour (the cache TTL).
        """
        with self._resolve_lock:
            now = time.time()
            # At most one forced re-resolve per video every 20s: if a URL
            # fetched seconds ago is already being refused, another yt-dlp
            # run won't fix it, and the spool gives up after a few tries.
            if force and now - self._video_forced_at.get(video_id, 0) > 20:
                self._video_forced_at[video_id] = now
                self._video_cache.pop(video_id, None)
            cached = self._video_cache.get(video_id)
            if cached and cached["expires"] > now:
                return cached["url"]
            if self._video_fail_until.get(video_id, 0) > now:
                return None

        # Resolve fresh
        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            result = _ytdlp(
                _ytdlp_base_args() +
                [
                 "-f", "22/18/best[ext=mp4]/best[ext=webm]/bestvideo*/best",
                 "--get-url", url],
                timeout=45
            )
            if result.returncode == 0 and result.stdout.strip():
                video_url = result.stdout.strip().split("\n")[0].strip()
                if video_url.startswith("http"):
                    with self._resolve_lock:
                        self._video_cache[video_id] = {
                            "url": video_url,
                            "expires": time.time() + self._cache_ttl
                        }
                        self._video_fail_until.pop(video_id, None)
                    return video_url
        except Exception as e:
            print(f"[MV] Proxy resolve error: {e}")
        with self._resolve_lock:
            self._video_fail_until[video_id] = time.time() + 30
        return None

    # ── Audio stream resolution ──

    def resolve_stream_url(self, track: Track) -> Optional[str]:
        """Use yt-dlp to get a direct audio stream URL"""
        url = track.url
        if not url:
            return None

        # Clean SoundCloud URLs
        if "soundcloud.com" in url.lower():
            qmark = url.find("?")
            if qmark > 0:
                url = url[:qmark]
                track.url = url

        # Resolve SC short URLs (what the phone app shares): yt-dlp's
        # SoundCloud support only takes the full soundcloud.com address.
        if "on.soundcloud.com/" in url.lower():
            import urllib.request
            opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
            for ua in ("Mozilla/5.0", _BROWSER_UA):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": ua})
                    resolved = opener.open(req, timeout=8).url
                except Exception as e:
                    print(f"[SoundCloud] couldn't open short link {url}: {e}")
                    continue
                qmark = resolved.find("?")
                if qmark > 0:
                    resolved = resolved[:qmark]
                if "soundcloud.com/" in resolved and "on.soundcloud.com/" not in resolved:
                    url = resolved
                    track.url = url
                    break
                print(f"[SoundCloud] short link {url} led to {resolved}")

        # Check cache
        cache_key = url
        with self._resolve_lock:
            cached = self._url_cache.get(cache_key)
            if cached and cached["expires"] > time.time():
                return cached["url"]

        # Format selection — unchanged from before.
        # SoundCloud uses HLS (m3u8), so include HLS protocol formats first for SC URLs
        is_sc = "soundcloud.com" in url.lower()
        base = _ytdlp_base_args(cookies=not is_sc)
        # Why it didn't resolve, for the chat reply (see ytdlp_failure; plus
        # "preview" for a SoundCloud Go+ track that guests only get 30 s of).
        track.resolve_error = ""
        fmt = (
            "bestaudio[protocol=m3u8_native]/bestaudio[protocol=m3u8]/"
            "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio*/bestaudio/best"
            if is_sc else
            "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio*/bestaudio/best"
        )

        for attempt in range(2):
            try:
                # ONE extraction for metadata AND the stream URL. These were
                # two separate yt-dlp launches over the same page, so the
                # extractor ran twice and the interpreter started twice for
                # data a single run already has. %(urls)s prints the URL of
                # the format -f selected, so they collapse cleanly.
                meta_result = _ytdlp(
                    base +
                    ["-f", fmt, "--no-download",
                     "--print",
                     "%(title)s|||%(uploader)s|||%(duration)s|||"
                     "%(thumbnail)s|||%(album)s|||%(format_id)s|||%(urls)s",
                     url],
                    timeout=60
                )

                stream_url = ""
                if meta_result.returncode == 0 and meta_result.stdout.strip():
                    meta_line = meta_result.stdout.strip().split("\n")[0]
                    parts = meta_line.split("|||")
                    if len(parts) >= 7:
                        candidate = parts[6].strip().split("\n")[0].strip()
                        if candidate.startswith("http"):
                            stream_url = candidate
                        if is_sc and "preview" in parts[5].lower():
                            # SoundCloud Go+ track: apps only get a 30 s clip.
                            print(f"[yt-dlp] {url}: only a 30-second preview (SoundCloud Go+)")
                            track.resolve_error = "preview"
                            return None
                    if len(parts) >= 5:
                        if _is_placeholder(track.title, _PLACEHOLDER_TITLES):
                            new_title = parts[0].strip()
                            if new_title and new_title != "NA":
                                track.title = new_title
                        if _is_placeholder(track.artist, _PLACEHOLDER_ARTISTS):
                            new_artist = parts[1].strip()
                            if new_artist and new_artist != "NA":
                                track.artist = new_artist
                        try:
                            dur = int(float(parts[2]))
                            if dur > 0:
                                track.duration_seconds = dur
                        except (ValueError, IndexError):
                            pass
                        thumb = parts[3].strip()
                        if not track.artwork_url and thumb and thumb != "NA":
                            track.artwork_url = thumb
                        album = parts[4].strip()
                        if album and album != "NA":
                            track.album = album

                elif meta_result.returncode != 0:
                    why = ytdlp_failure(meta_result.stderr)
                    if why:                     # a final answer: don't ask again
                        print(f"[yt-dlp] {url}: {why} \u2014 {_error_line(meta_result.stderr)}")
                        track.resolve_error = why
                        return None

                # Fallback only: if %(urls)s came back unusable (older yt-dlp,
                # unusual extractor), resolve the old way rather than failing.
                if not stream_url:
                    url_result = _ytdlp(
                        base +
                        ["-f", fmt, "--get-url", url],
                        timeout=60
                    )

                    if url_result.returncode != 0:
                        print(f"[yt-dlp] Failed for {url} (attempt {attempt+1}): {url_result.stderr[:300]}")
                        track.resolve_error = ytdlp_failure(url_result.stderr)
                        if attempt == 0 and not track.resolve_error:
                            continue  # retry once before giving up
                        return None

                    stream_url = url_result.stdout.strip().split("\n")[0].strip()

                if stream_url.startswith("http"):
                    with self._resolve_lock:
                        self._url_cache[cache_key] = {
                            "url": stream_url,
                            "expires": time.time() + self._cache_ttl
                        }
                    print(f"[yt-dlp] Resolved: {track.title} by {track.artist}")
                    return stream_url

            except FileNotFoundError:
                print("[yt-dlp] ERROR: yt-dlp not found!")
                return None
            except subprocess.TimeoutExpired:
                if attempt == 0:
                    print(f"[yt-dlp] Timeout resolving (retrying): {url}")
                    continue
                print(f"[yt-dlp] Timeout resolving: {url}")
            except Exception as e:
                print(f"[yt-dlp] Error: {e}")
                return None

        return None

    # How many search hits to pull, and how many of them to actually try to
    # resolve before giving up. The flat search is cheap; each resolve is a
    # full yt-dlp run, so only the first few are attempted.
    SEARCH_RESULTS = 8
    SEARCH_TRY = 4

    def search_youtube(self, query: str,
                       max_duration: Optional[int] = None) -> Optional[Track]:
        """Search YouTube by name; returns a playable Track or None."""
        return self.search_youtube_ex(query, max_duration)[0]

    def search_youtube_ex(self, query: str, max_duration: Optional[int] = None):
        """Search YouTube by name. Returns (Track, "") or (None, reason).

        reason is one of: "empty", "spotify", "no_results", "too_long",
        "unplayable" — so chat can be told WHY a request didn't make it.

        Why name searches used to fail while links worked
        -------------------------------------------------
        The old version asked yt-dlp to search AND pull the #1 hit's audio
        formats in one run (-f + %(urls)s). If that one video couldn't produce
        formats — age-gated, live/premiere, region-locked, members-only, a
        format hiccup — yt-dlp exited non-zero, the whole search returned
        None, and chat got "No results" even though the song exists. There
        was no fallback to hit #2. And because the raw text went in as a
        bare positional argument, anything containing a colon
        ("code: pandorum - ...") was read as a URL with scheme "code:" and
        rejected as unsupported, and anything starting with "-" was read as
        an option.

        Now it's two steps:
          1. A flat search ("ytsearch8:<query>") that only lists hits — no
             format extraction, so it can't fail on a single bad video.
          2. Each viable hit goes through resolve_stream_url — the exact path
             a pasted link takes — until one resolves. Hits that are live,
             upcoming, Shorts, or longer than max_duration are skipped before
             spending a resolve on them.

        The resolved stream URL lands in the cache (not on track.stream_url),
        so a soon-to-play track is an instant cache hit while one queued deep
        simply expires and re-resolves — same reasoning as before.
        """
        query = (query or "").strip()
        if not query:
            return None, "empty"

        # Spotify links can't be played directly — turn them into a search.
        if "open.spotify.com/" in query.lower() or "spotify.link/" in query.lower():
            converted = self.spotify_to_query(query)
            if not converted:
                print(f"[Search] Couldn't read Spotify link: {query}")
                return None, "spotify"
            print(f"[Search] Spotify -> \"{converted}\"")
            query = converted

        entries = self._flat_search(query)
        if not entries:
            print(f"[Search] No hits for \"{query}\"")
            return None, "no_results"

        candidates = []
        too_long = 0
        for e in entries:
            vid = (e.get("id") or "").strip()
            if not vid or len(vid) != 11:
                continue                      # channels/playlists in results
            live = (e.get("live_status") or "").lower()
            if e.get("is_live") or live in ("is_live", "is_upcoming", "post_live"):
                continue
            url = e.get("url") or e.get("webpage_url") or ""
            if "/shorts/" in url:
                continue
            dur = e.get("duration")
            try:
                dur = int(float(dur)) if dur is not None else 0
            except (TypeError, ValueError):
                dur = 0
            if max_duration and dur > max_duration:
                too_long += 1
                continue
            candidates.append((e, vid, dur))

        if not candidates:
            print(f"[Search] {len(entries)} hits for \"{query}\" but none usable"
                  f"{f' ({too_long} too long)' if too_long else ''}")
            return None, ("too_long" if too_long else "no_results")

        skipped_long_after = 0
        tried = 0
        for e, vid, dur in candidates[:self.SEARCH_TRY]:
            tried += 1
            artist = (e.get("channel") or e.get("uploader") or "").strip()
            if artist.endswith(" - Topic"):
                artist = artist[:-len(" - Topic")]
            thumbs = e.get("thumbnails") or []
            artwork = ""
            if thumbs and isinstance(thumbs, list):
                artwork = (thumbs[-1] or {}).get("url", "") or ""
            if not artwork:
                artwork = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

            track = Track(
                title=(e.get("title") or "Unknown").strip(),
                artist=artist or "Unknown",
                url=f"https://www.youtube.com/watch?v={vid}",
                source=TrackSource.YOUTUBE,
                video_id=vid,
                artwork_url=artwork,
                duration_seconds=dur,
            )
            # Same resolve a pasted link gets. Fills album/duration and caches
            # the stream URL; returns None if this video won't play.
            if self.resolve_stream_url(track):
                if max_duration and track.duration_seconds > max_duration:
                    # Flat search had no duration for it; the resolve did.
                    print(f"[Search] Skipping (too long): {track.title}")
                    skipped_long_after += 1
                    continue
                print(f"[Search] \"{query}\" -> {track.artist} - {track.title}")
                return track, ""
            print(f"[Search] Hit didn't resolve, trying next: {track.title}")

        print(f"[Search] None of the top hits for \"{query}\" would play")
        return None, ("too_long" if skipped_long_after == tried else "unplayable")

    def _flat_search(self, query: str) -> list:
        """List search hits without extracting formats. Returns entry dicts."""
        import json as _json
        # --no-playlist is meaningless for a search and only invites edge
        # cases, so it's dropped here. The "ytsearchN:" prefix makes the
        # query unambiguous (colons are fine), and "--" stops a query that
        # starts with "-" from being parsed as an option.
        args = [a for a in _ytdlp_base_args() if a != "--no-playlist"]
        args += ["--flat-playlist", "-J", "--",
                 f"ytsearch{self.SEARCH_RESULTS}:{query}"]
        for attempt in range(2):
            try:
                result = _ytdlp(args, timeout=45)
                if result.returncode != 0 or not result.stdout.strip():
                    print(f"[Search] yt-dlp search failed (attempt {attempt+1}): "
                          f"{(result.stderr or '').strip()[:300]}")
                    continue
                data = _json.loads(result.stdout)
                return [e for e in (data.get("entries") or []) if isinstance(e, dict)]
            except FileNotFoundError:
                print("[yt-dlp] ERROR: yt-dlp not found!")
                return []
            except subprocess.TimeoutExpired:
                print(f"[Search] timeout (attempt {attempt+1}): {query}")
            except Exception as e:
                print(f"[Search] error: {type(e).__name__}: {e}")
                return []
        return []

    @staticmethod
    def spotify_to_query(link: str) -> Optional[str]:
        """Turn a Spotify track link into "Artist - Title" for a YouTube search.

        Reads the public track page's og: tags (no Spotify API key needed):
          og:title       -> "Song Name"
          og:description -> "Artist · Album · Song · 2020"
        Falls back to the oEmbed endpoint, which gives the title only.
        """
        import html as _html
        import json as _json
        import urllib.parse
        import urllib.request

        m = re.search(r"https?://\S+", link)
        url = (m.group(0) if m else link).strip()
        ua = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
              "Accept-Language": "en-US,en;q=0.9"}
        try:
            req = urllib.request.Request(url, headers=ua)
            with urllib.request.urlopen(req, timeout=8) as r:
                page = r.read(600_000).decode("utf-8", "replace")

            def og(prop):
                mm = (re.search(r'<meta[^>]+property="og:%s"[^>]+content="([^"]*)"' % prop, page)
                      or re.search(r'<meta[^>]+content="([^"]*)"[^>]+property="og:%s"' % prop, page))
                return _html.unescape(mm.group(1)).strip() if mm else ""

            title = og("title")
            desc = og("description")
            artist = desc.split("·")[0].strip() if "·" in desc else ""
            if title:
                return f"{artist} - {title}" if artist else title
        except Exception as e:
            print(f"[Spotify] page read failed: {e}")

        try:
            o = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe="")
            with urllib.request.urlopen(urllib.request.Request(o, headers=ua), timeout=8) as r:
                title = (_json.loads(r.read().decode("utf-8", "replace")).get("title") or "").strip()
            return title or None
        except Exception as e:
            print(f"[Spotify] oEmbed failed: {e}")
        return None

    def soundcloud_song_name(self, url: str) -> Optional[str]:
        """"Artist - Title" of a SoundCloud track, read even when the track
        itself won't play here (encrypted, a Go+ preview, blocked in this
        country) — so the request can be found on YouTube instead."""
        try:
            r = _ytdlp(_ytdlp_base_args(cookies=False) +
                       ["--ignore-no-formats-error", "--print",
                        "%(artists.0)s|||%(uploader)s|||%(title)s", url],
                       timeout=45)
        except Exception as e:
            print(f"[SoundCloud] couldn't read the track's name: {e}")
            return None
        line = (r.stdout or "").strip().split("\n")[0]
        if r.returncode != 0 or not line:
            print(f"[SoundCloud] couldn't read the track's name: {_error_line(r.stderr)}")
            return None
        artist, uploader, title = [("" if v.strip() == "NA" else v.strip())
                                   for v in (line.split("|||") + ["", "", ""])[:3]]
        if not title:
            return None
        # A label's upload: the account is the label, and the title usually
        # already says "Artist - Song".
        who = artist or ("" if " - " in title else uploader)
        return f"{who} - {title}" if who and who.lower() not in title.lower() else title

    @staticmethod
    def is_soundcloud_url(text: str) -> bool:
        t = text.lower().strip()
        return "soundcloud.com/" in t or "on.soundcloud.com/" in t

    @staticmethod
    def is_youtube_url(text: str) -> bool:
        t = text.lower().strip()
        return any(x in t for x in ["youtube.com/watch", "youtu.be/", "music.youtube.com/watch"])

    @staticmethod
    def extract_video_id(url: str) -> Optional[str]:
        if "youtu.be/" in url:
            path = url.split("youtu.be/")[1]
            return path.split("?")[0].split("&")[0].strip() or None
        if "youtube.com/watch" in url or "music.youtube.com/watch" in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            vids = params.get("v", [])
            return vids[0] if vids else None
        return None

    def cleanup(self):
        try:
            self._player.stop()
            self._player.release()
            self._instance.release()
        except Exception:
            pass
