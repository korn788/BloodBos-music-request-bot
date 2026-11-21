"""SoundCloud links: locked tracks, short links, cookies and honest replies.

SoundCloud serves many label releases only encrypted (yt-dlp: "This video is
DRM protected") and Go+ tracks only as a 30-second preview. Those requests now
come from YouTube instead, and every other refusal gets its real reason. yt-dlp
is faked here, so these run anywhere.
"""
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BLOODBOS_VLC_ARGS", "--aout=dummy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

ae = pytest.importorskip("audio_engine")

SC_URL = "https://soundcloud.com/korn/blind"
M3U8 = "https://cf-hls-opus-media.sndcdn.com/playlist/abc.128.opus/playlist.m3u8"


# ── what yt-dlp says ──────────────────────────────────────────────────────

@pytest.mark.parametrize("stderr, reason", [
    ("ERROR: [soundcloud] 1570698946: This video is DRM protected", "drm"),
    ("ERROR: [soundcloud] 99: This video is not available from your location due to "
     "geo restriction", "geo"),
    ("ERROR: [soundcloud] korn/gone: Unable to download JSON metadata: HTTP Error 404: "
     "Not Found (caused by <HTTPError 404: Not Found>)", "gone"),
    ("ERROR: [youtube] dQw4w9WgXcQ: Video unavailable", "gone"),
    ("ERROR: [youtube] dQw4w9WgXcQ: Private video. Sign in if you've been granted access",
     "private"),
    ("ERROR: Unsupported URL: https://on.soundcloud.com/AbC", "bad_link"),
    ("ERROR: [soundcloud] x: Unable to download JSON metadata: HTTP Error 429: Too Many "
     "Requests", ""),
    ("ERROR: [soundcloud] x: Unable to download JSON metadata: <urlopen error timed out>", ""),
    ("", ""),
])
def test_reads_why_ytdlp_said_no(stderr, reason):
    assert ae.ytdlp_failure(stderr) == reason


# ── the lookup, with a fake yt-dlp ────────────────────────────────────────

def ok(stdout):
    return lambda args: subprocess.CompletedProcess(args, 0, stdout + "\n", "")


def fail(stderr):
    return lambda args: subprocess.CompletedProcess(args, 1, "", stderr + "\n")


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("YTDLP_COOKIE_BROWSER", "firefox")     # cookies set up for YouTube
    calls = []
    answers = []

    def fake_ytdlp(args, timeout):
        calls.append(args)
        return answers[min(len(calls), len(answers)) - 1](args)

    monkeypatch.setattr(ae, "_ytdlp", fake_ytdlp)
    eng = ae.AudioEngine.__new__(ae.AudioEngine)
    eng._resolve_lock = threading.Lock()
    eng._url_cache = {}
    eng._cache_ttl = 3600
    eng.calls, eng.answers = calls, answers
    return eng


def track(url):
    return ae.Track(title="Loading…", artist="…", url=url,
                    source=ae.TrackSource.SOUNDCLOUD)


def test_playable_soundcloud_track_without_browser_cookies(engine):
    engine.answers.append(ok(f"Blind|||Korn|||258.0|||https://i1.sndcdn.com/a.jpg|||NA|||"
                             f"hls_opus_64|||{M3U8}"))
    t = track(SC_URL + "?si=abc&utm_source=clipboard")
    assert engine.resolve_stream_url(t) == M3U8
    assert (t.title, t.artist, t.duration_seconds, t.url) == ("Blind", "Korn", 258, SC_URL)
    assert len(engine.calls) == 1
    assert "--cookies-from-browser" not in engine.calls[0]


def test_youtube_still_gets_the_browser_cookies(engine):
    engine.answers.append(ok("Blind|||Korn|||258|||NA|||NA|||140|||https://rr1.googlevideo.com/x"))
    t = ae.Track(title="Loading…", artist="…", url="https://youtu.be/abc")
    assert engine.resolve_stream_url(t)
    i = engine.calls[0].index("--cookies-from-browser")
    assert engine.calls[0][i + 1] == "firefox"


@pytest.mark.parametrize("stderr, reason", [
    ("ERROR: [soundcloud] 1570698946: This video is DRM protected", "drm"),
    ("ERROR: [soundcloud] korn/blind: Unable to download JSON metadata: HTTP Error 404: "
     "Not Found", "gone"),
])
def test_a_final_answer_is_not_asked_again(engine, stderr, reason):
    engine.answers.append(fail(stderr))
    t = track(SC_URL)
    assert engine.resolve_stream_url(t) is None
    assert t.resolve_error == reason
    assert len(engine.calls) == 1


def test_go_plus_preview_is_not_played(engine):
    engine.answers.append(ok(f"Blind|||Korn|||258.0|||NA|||NA|||hls_mp3_0_0_preview|||{M3U8}"))
    t = track(SC_URL)
    assert engine.resolve_stream_url(t) is None
    assert t.resolve_error == "preview"


def test_a_network_blip_is_tried_again(engine):
    engine.answers.append(fail("ERROR: [soundcloud] x: Unable to download JSON metadata: "
                               "<urlopen error timed out>"))
    t = track(SC_URL)
    assert engine.resolve_stream_url(t) is None
    assert t.resolve_error == ""
    assert len(engine.calls) == 4           # (lookup + fallback) x 2 tries


def test_short_link_is_opened_first(engine, monkeypatch):
    class Resp:
        url = SC_URL + "?si=abc&utm_source=clipboard&utm_medium=text"

    class Opener:
        def open(self, req, timeout=None):
            return Resp()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Opener())
    engine.answers.append(ok(f"Blind|||Korn|||258.0|||NA|||NA|||hls_opus_64|||{M3U8}"))
    t = track("https://on.soundcloud.com/AbCdEf123")
    assert engine.resolve_stream_url(t) == M3U8
    assert t.url == SC_URL and engine.calls[0][-1] == SC_URL


def test_short_link_that_wont_open(engine, monkeypatch):
    class Opener:
        def open(self, req, timeout=None):
            raise OSError("blocked")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Opener())
    engine.answers.append(fail("ERROR: Unsupported URL: https://on.soundcloud.com/AbC"))
    t = track("https://on.soundcloud.com/AbC")
    assert engine.resolve_stream_url(t) is None
    assert t.resolve_error == "bad_link" and "on.soundcloud.com/" in t.url


@pytest.mark.parametrize("printed, name", [
    ("Korn|||Korn|||Blind", "Korn - Blind"),
    ("NA|||Korn|||Blind", "Korn - Blind"),
    ("NA|||Roadrunner Records|||Korn - Blind", "Korn - Blind"),
    ("Kendrick Lamar|||Kendrick Lamar|||Not Like Us", "Kendrick Lamar - Not Like Us"),
    ("NA|||NA|||Blind", "Blind"),
])
def test_name_of_a_locked_track(engine, printed, name):
    engine.answers.append(ok(printed))
    assert engine.soundcloud_song_name(SC_URL) == name
    args = engine.calls[0]
    assert "--ignore-no-formats-error" in args and "--cookies-from-browser" not in args


def test_name_of_a_track_that_cannot_be_read(engine):
    engine.answers.append(fail("ERROR: [soundcloud] x: HTTP Error 404: Not Found"))
    assert engine.soundcloud_song_name(SC_URL) is None


# ── what chat is told (real player window, fake lookups) ─────────────────

@pytest.fixture(scope="module")
def window():
    pq = pytest.importorskip("player_qt")
    if pq.vlc is None:
        pytest.skip("VLC is not installed")
    import app_paths
    app_paths.set_data_dir(tempfile.mkdtemp())
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from settings import Settings
    from queue_manager import QueueManager
    settings = Settings()
    settings.set("player", "check_updates", False, notify=False)
    ae.configure(settings=settings)
    pq.configure(settings)
    eng = ae.AudioEngine()

    def fake_play(t):
        eng._current_track = t
        eng._state = ae.PlayerState.PLAYING
    eng.play_track = fake_play
    w = pq.PlayerWindow(eng, QueueManager(), pq.SignalBridge(), yt_api=None, settings=settings)
    w._app = app
    yield w
    eng.cleanup()


def ask(window, text, resolve_error="", name="Korn - Blind", youtube=True):
    eng = window.engine
    eng._current_track, eng._state = None, ae.PlayerState.STOPPED
    window.queue.clear() if hasattr(window.queue, "clear") else None

    def fake_resolve(t):
        t.resolve_error = resolve_error
        return None

    def fake_search(query, max_duration=None):
        if not youtube:
            return None, "no_results"
        return ae.Track(title="Blind", artist="Korn", url="https://youtu.be/abc",
                        source=ae.TrackSource.YOUTUBE, video_id="abc",
                        duration_seconds=258), ""

    eng.resolve_stream_url = fake_resolve
    eng.soundcloud_song_name = lambda url: name
    eng.search_youtube_ex = fake_search
    got, done = [], threading.Event()
    window.api_addsong(text, f"viewer{len(text)}{resolve_error}{youtube}", "twitch",
                       on_done=lambda r: (got.append(r), done.set()))
    assert done.wait(10)
    return got[0]


def test_locked_track_plays_from_youtube(window):
    r = ask(window, f"Listen to Blind by Korn on #SoundCloud {SC_URL}", "drm")
    assert r["ok"] and r["source"] == "youtube"
    assert r["message"].startswith("\U0001F512 SoundCloud locks that track, so it's playing "
                                   "from YouTube. ")
    assert "Korn - Blind" in r["message"]


def test_preview_only_track_plays_from_youtube(window):
    r = ask(window, SC_URL, "preview")
    assert r["ok"] and r["source"] == "youtube"


def test_locked_track_not_on_youtube_says_so(window):
    r = ask(window, SC_URL, "drm", youtube=False)
    assert not r["ok"] and r["status"] == "unplayable"
    assert "only its own app can play it" in r["message"]


@pytest.mark.parametrize("url, why, says", [
    (SC_URL, "gone", "❌ That SoundCloud link is private or was removed."),
    ("https://on.soundcloud.com/AbC", "bad_link",
     "❌ Couldn't open that SoundCloud short link. Paste the full soundcloud.com link "
     "instead."),
    (SC_URL, "", "❌ Couldn't play that SoundCloud link right now. Try again, another "
                 "link, or the song name."),
    ("https://youtu.be/abc", "gone", "❌ That YouTube link is private or was removed."),
])
def test_other_refusals_say_why(window, url, why, says):
    r = ask(window, url, why)
    assert not r["ok"] and r["message"] == says
