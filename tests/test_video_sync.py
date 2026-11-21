"""Music-video sync: the end of a song, and audio that gets stuck.

The loop people saw right before a song ended: the audio got stuck (buffering,
or a song that went quiet at its end and waited for the watchdog), the music
video played on against the frozen song position, and the sync pulled it back
every few seconds — the same stretch of video, over and over. These tests run
the app window's VideoController against a simulated VLC player and a
simulated clock, so they are exact and need no real video.
"""
import json
import os
import sys
import urllib.request
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pq = pytest.importorskip("player_qt")
vlc = pq.vlc
if vlc is None:
    pytest.skip("VLC is not installed", allow_module_level=True)
S = vlc.State


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeVideo:
    """Just enough of a vlc.MediaPlayer: a video `length` seconds long."""

    def __init__(self, length):
        self.length = length
        self.pos = 0.0
        self.state = S.NothingSpecial
        self.rate = 1.0

    def advance(self, dt):
        if self.state == S.Playing:
            self.pos += dt * self.rate
            if self.pos >= self.length:
                self.pos = self.length
                self.state = S.Ended

    # the calls VideoController makes
    def set_media(self, media):
        self.state, self.pos = S.NothingSpecial, 0.0

    def play(self):
        if self.state == S.Ended:
            self.pos = 0.0                  # VLC starts an ended media over
        self.state = S.Playing
        return 0

    def stop(self):
        self.state, self.pos = S.Stopped, 0.0

    def set_pause(self, on):
        if on and self.state in (S.Playing, S.Buffering):
            self.state = S.Paused
        elif not on and self.state == S.Paused:
            self.state = S.Playing

    def get_state(self):
        return self.state

    def is_playing(self):
        return self.state == S.Playing

    def get_time(self):
        return int(self.pos * 1000) if self.state not in (S.NothingSpecial, S.Stopped) else -1

    def get_length(self):
        return int(self.length * 1000)

    def set_time(self, ms):
        if self.state != S.Ended:
            self.pos = min(ms / 1000.0, self.length)

    def set_rate(self, r):
        self.rate = r
        return 0


def run(audio, length=20.0, until=30.0, poke=None):
    """Drive a VideoController like the app does: sync every 150 ms, play()
    every 400 ms. `audio(t)` -> (song position, running) or None when the song
    is over. Returns [(t, video position, state)]."""
    clock = FakeClock()
    with mock.patch("time.monotonic", clock):
        ctl = pq.VideoController(surface=None)
        video = FakeVideo(length)
        ctl._player, ctl._bound = video, True
        ctl.play("http://127.0.0.1:9111/stream/x", key="track")
        out, t, step, last_ui = [], 0.0, 0.05, -1.0
        while t < until:
            got = audio(t)
            if got is None:
                break
            pos, running = got
            if t - last_ui >= 0.4:
                ctl.play("http://127.0.0.1:9111/stream/x", key="track")
                last_ui = t
            if poke:
                poke(t, ctl, video)
            if round(t / step) % 3 == 0:
                ctl.sync_to(pos, False, running=running)
            out.append((round(t, 2), video.pos, video.state))
            video.advance(step)
            clock.t += step
            t += step
        return out


def jumps_back(samples, after=0.0):
    found, prev = [], None
    for t, pos, _ in samples:
        if prev is not None and t > after and pos < prev - 0.5:
            found.append((t, round(prev, 2), round(pos, 2)))
        prev = pos
    return found


def test_stuck_audio_near_the_end_does_not_loop_the_video():
    # audio stuck at 16.5 s; the engine reports it after 1 s; the watchdog ends
    # the song 7 s later. The video must not go back and replay.
    def audio(t):
        if t < 16.5:
            return t, True
        if t < 23.5:
            return 16.5, t < 17.5
        return None
    samples = run(audio)
    assert jumps_back(samples, after=5) == []
    stuck = [s for s in samples if 18.0 < s[0] < 23.4]
    assert stuck and all(s[2] == S.Paused for s in stuck), "video should wait while the audio is stuck"
    assert max(s[1] for s in stuck) < 18.0


def test_stuck_audio_mid_song_waits_then_catches_up():
    def audio(t):
        if t < 8.0:
            return t, True
        if t < 12.0:
            return 8.0, t < 9.0
        return t - 4.0, True
    samples = run(audio, until=18.0)
    assert len(jumps_back(samples, after=5)) <= 1        # one snap back when it resumes
    late = [s for s in samples if 15.0 < s[0] < 18.0]
    assert all(abs(pos - (t - 4.0)) < 0.35 for t, pos, _ in late), late[:3]


def test_audio_longer_than_video_holds_the_last_frame():
    samples = run(lambda t: (t, True) if t < 24.0 else None)
    assert jumps_back(samples, after=5) == []
    tail = [s for s in samples if s[0] > 20.5]
    assert tail and all(pos > 19.0 for _, pos, _ in tail)


def test_video_ahead_at_the_end_is_not_replayed():
    # The video runs 2.5 s ahead near the end, right after a correction (so
    # the corrector may not seek again yet). It used to freeze, decide the audio
    # was "back inside" the video, and replay its last seconds.
    def poke(t, ctl, video):
        if abs(t - 15.0) < 0.026:
            ctl._last_seek = pq.time.monotonic()        # a correction just happened
            video.pos = 17.5
    samples = run(lambda t: (t, True) if t < 24.0 else None, poke=poke)
    assert jumps_back(samples, after=15.5) == []


def test_rewinding_the_song_after_the_video_ended_plays_it_again():
    def audio(t):
        if t < 22.0:
            return t, True
        return t - 16.0, True                         # rewound to 6 s
    samples = run(audio, until=28.0)
    later = [s for s in samples if 24.0 < s[0] < 28.0]
    assert later and all(abs(pos - (t - 16.0)) < 0.5 for t, pos, _ in later), later[:3]
    assert all(st == S.Playing for _, _, st in later)


def test_engine_reports_a_stuck_clock():
    ae = pytest.importorskip("audio_engine")
    clock = FakeClock()

    class P:
        ms, playing = 0, True

        def is_playing(self):
            return self.playing

        def get_time(self):
            return self.ms

    with mock.patch("time.monotonic", clock):
        eng = ae.AudioEngine.__new__(ae.AudioEngine)
        eng._player = P()
        eng._state = ae.PlayerState.PLAYING
        eng._last_elapsed, eng._elapsed_moved = 0.0, 0.0
        for _ in range(8):                            # VLC moves ~4x a second
            clock.t += 0.25
            eng._player.ms += 250
            assert eng.clock_running
        clock.t += 0.6                                # no movement for 0.6 s
        assert eng.clock_running
        clock.t += 0.6                                # ... for 1.2 s
        assert not eng.clock_running
        assert eng.elapsed_seconds == 2.0             # the last real position is kept
        eng._player.playing = False                   # buffering
        eng._player.ms += 250
        clock.t += 0.25
        assert not eng.clock_running
        eng._player.playing = True
        assert eng.clock_running
        eng._state = ae.PlayerState.PAUSED
        assert not eng.clock_running


def test_status_server_reports_version_and_keeps_the_video_on_screen(monkeypatch):
    ss = pytest.importorskip("status_server")

    class FakeSpool:
        def __init__(self, video_id, *a):
            self.video_id, self.failed, self.stopped, self.closed = video_id, False, False, False
            self.last_access = FakeSpool.clock = FakeSpool.clock + 1

        def close(self):
            self.closed = True

    FakeSpool.clock = 0
    monkeypatch.setattr(ss, "_Spool", FakeSpool)
    status = {"title": "x", "videoId": "A", "mvActive": True}
    srv = ss.StatusServer(player_status_fn=lambda: status, queue_fn=lambda: {},
                          get_video_url_fn=lambda *a, **k: None, port=0)
    for vid in "ABCDE":                       # A is on screen and never read again
        srv._get_spool(vid)
    assert "A" in srv._spools and len(srv._spools) <= 4
    assert set(srv._spools) == {"A", "D", "E"}

    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ss.StatusServer(player_status_fn=lambda: {}, queue_fn=lambda: {},
                          get_video_url_fn=lambda *a, **k: None, port=port)
    srv.start()
    try:
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with local.open(f"http://127.0.0.1:{port}/status", timeout=5) as r:
            data = json.loads(r.read())
        assert set(data) == {"appVersion"} and data["appVersion"]
        assert not data.get("title")              # still "nothing playing" to the widget
    finally:
        srv.stop()
