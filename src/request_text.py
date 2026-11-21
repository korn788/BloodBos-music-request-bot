# request_text.py — find the song link in whatever was pasted.
#
# Sharing a song from a phone rarely gives just the link. SoundCloud's app
# shares something like
#
#     Listen to Freak On a Leash by Korn on #SoundCloud
#     https://on.soundcloud.com/AbC123
#
# and people paste all of it after !sr. The words in front change with every
# song (they carry its title and artist), so instead of cutting off a fixed
# header, a request is reduced to the first YouTube, SoundCloud or Spotify link
# anywhere in it. No link: the text is a song search, exactly as typed.

import re

# Invisible characters some chat apps attach to a message (Chatterino and 7TV
# add one to get around Twitch's "duplicate message" filter). Glued to the end
# of a link, they break it.
_INVISIBLE = re.compile("[­͏᠎​-‏⁠-⁤﻿\U000e0000-\U000e007f]")

_LINK = re.compile(
    r"(?<![\w.@/-])"                     # not the tail of a longer name (xsoundcloud.com)
    r"(?:https?://)?"                    # shared links have it; typed ones often don't
    r"(?:[a-z0-9-]+\.)*"                 # on. m. www. music. open.
    r"(?:soundcloud\.com|youtube\.com|youtu\.be|spotify\.com|spotify\.link)"
    r"(?=[/?#\s]|$)"
    r"[^\s<>\"'`‘’“”]*",
    re.I)

# Punctuation that ends a sentence rather than the link: "…here: <link>."
_TRAIL = ".,;:!?)]}>*…。！，"


def music_link(text):
    """The first YouTube / SoundCloud / Spotify link in `text`, or None."""
    text = _INVISIBLE.sub("", text or "")
    for m in _LINK.finditer(text):
        link = m.group(0).rstrip(_TRAIL)
        rest = link.split("://", 1)[-1]
        if "/" not in rest.rstrip("/"):
            continue                     # just the site's name, no song
        if not link.lower().startswith(("http://", "https://")):
            link = "https://" + link
        return link
    return None


def clean_request(text):
    """What to look a request up as: its link if it has one, otherwise the
    text as typed (a song search)."""
    text = _INVISIBLE.sub("", text or "").strip()
    return music_link(text) or text
