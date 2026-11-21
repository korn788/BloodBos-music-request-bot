"""Requests pasted straight from a phone's share sheet: only the link counts."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from request_text import clean_request, music_link  # noqa: E402

SC = "https://on.soundcloud.com/AbCdEf123"


@pytest.mark.parametrize("pasted, link", [
    # SoundCloud's app: a line about the song, then the link
    (f"Listen to Freak On a Leash by Korn on #SoundCloud {SC}", SC),
    (f"Listen to Freak On a Leash by Korn on #SoundCloud\n{SC}", SC),
    (f"Check out Blind by Korn on #SoundCloud #np {SC}", SC),
    (f"Listen to Freak On a Leash (Live) by Korn on #SoundCloud {SC}.", SC),
    ("https://soundcloud.com/korn/freak-on-a-leash?utm_source=clipboard&utm_medium=text"
     "&utm_campaign=social_sharing",
     "https://soundcloud.com/korn/freak-on-a-leash?utm_source=clipboard&utm_medium=text"
     "&utm_campaign=social_sharing"),
    ("m.soundcloud.com/korn/blind", "https://m.soundcloud.com/korn/blind"),
    ("soundcloud.com/korn/blind please!!", "https://soundcloud.com/korn/blind"),
    # YouTube and Spotify shares work the same way
    ("Check out this video: https://youtu.be/dQw4w9WgXcQ?si=Xy-_z1.",
     "https://youtu.be/dQw4w9WgXcQ?si=Xy-_z1"),
    ("play this https://www.youtube.com/watch?v=dQw4w9WgXc_ thx",
     "https://www.youtube.com/watch?v=dQw4w9WgXc_"),
    ("(https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc)",
     "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc"),
    ("[song](https://soundcloud.com/a/b)", "https://soundcloud.com/a/b"),
    ("link:https://soundcloud.com/a/b", "https://soundcloud.com/a/b"),
    # an invisible character a chat app glued onto the end
    ("https://youtu.be/dQw4w9WgXcQ\U000e0000", "https://youtu.be/dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ ͏", "https://youtu.be/dQw4w9WgXcQ"),
    # the first song link wins; other sites are ignored
    ("https://example.com/x then https://youtu.be/abc and https://youtu.be/def",
     "https://youtu.be/abc"),
])
def test_finds_the_link(pasted, link):
    assert music_link(pasted) == link
    assert clean_request(pasted) == link


@pytest.mark.parametrize("typed", [
    "korn freak on a leash",
    "Listen to Blind by Korn on #SoundCloud",       # share text without its link
    "is this on soundcloud.com?",                   # the site, not a song
    "xsoundcloud.com/a/b",
    "me@soundcloud.com",
    "youtube.com.example.net/watch?v=abc",
    "code: pandorum - 13",
])
def test_no_link_means_a_search(typed):
    assert music_link(typed) is None
    assert clean_request("  " + typed + " ") == typed


class FakeWindow:
    def __init__(self):
        self.requests = []

    def api_addsong(self, text, user, platform, on_done=None):
        self.requests.append(text)
        on_done({"ok": True, "status": "queued", "message": "✅ Added: Korn - Blind — #1 in queue"})


def run_command(text):
    from chat.commands import CommandRouter

    class Settings:
        def get(self, *a, **k):
            return a[2] if len(a) > 2 else k.get("default")

    w = FakeWindow()
    router = CommandRouter(w, Settings(), call_on_gui=lambda fn: fn())
    said = []

    async def reply(msg):
        said.append(msg)

    asyncio.run(router.handle("twitch", "viewer", text, False, reply))
    return w.requests, said


def test_sr_with_a_soundcloud_share_requests_just_the_link():
    requests, said = run_command(f"!sr Listen to Blind by Korn on #SoundCloud {SC}")
    assert requests == [SC]
    assert said[0] == f"🔎 Searching: {SC}..."


def test_sc_with_a_soundcloud_share_works():
    requests, said = run_command(f"!sc Listen to Blind by Korn on #SoundCloud\n{SC}")
    assert requests == [SC]
    assert said[0] == "🔎 Adding SoundCloud track..."


def test_sr_with_a_song_name_is_unchanged():
    requests, _ = run_command("!sr korn blind")
    assert requests == ["korn blind"]


def test_sc_without_a_link_still_explains_itself():
    requests, said = run_command("!sc Listen to Blind by Korn on #SoundCloud")
    assert requests == [] and said == ["Usage: !sc <SoundCloud link>"]
