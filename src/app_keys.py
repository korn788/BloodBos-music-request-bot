# app_keys.py — the OAuth apps behind the "Connect" buttons
#
# A client ID is not a secret: it's in the address bar of every sign-in page.
#
# Twitch: signing in uses Twitch's "implicit" flow, which needs no secret, so
#         the client ID below is all the app needs. Its redirect URL must be
#         registered on that Twitch app (dev.twitch.tv > Your Console > Apps).
#
# Kick:   Kick's sign-in always needs the app's client secret. The source code
#         never contains one. Release builds can include one from the GitHub
#         repository secrets KICK_CLIENT_ID / KICK_CLIENT_SECRET (the workflow
#         writes them into _build_keys.py). Without that, the Connect window
#         walks the streamer through making their own free Kick app instead.
#
# Every value can be overridden with an environment variable named BLOODBOS_
# plus the name (BLOODBOS_TWITCH_CLIENT_ID, ...). The prefix matters: plain
# TWITCH_CLIENT_ID-style variables are often set for other Twitch tools, and
# picking those up would send this app's sign-in to someone else's app.

import os

TWITCH_CLIENT_ID = "7ym4xs9mycv12dwoj1w6clbu2v8zgq"
TWITCH_REDIRECT_URI = "http://localhost:17563/callback"

KICK_CLIENT_ID = ""
KICK_CLIENT_SECRET = ""
KICK_REDIRECT_URI = "http://localhost:3000"

try:                                    # written by the release workflow only
    from _build_keys import *           # noqa: F401,F403
except ImportError:
    pass

for _name in ("TWITCH_CLIENT_ID", "TWITCH_REDIRECT_URI", "KICK_CLIENT_ID",
              "KICK_CLIENT_SECRET", "KICK_REDIRECT_URI"):
    _v = os.environ.get("BLOODBOS_" + _name, "").strip()
    if _v:
        globals()[_name] = _v


def builtin_kick_app():
    """(client_id, client_secret) shipped with this build, or None."""
    if KICK_CLIENT_ID and KICK_CLIENT_SECRET:
        return KICK_CLIENT_ID, KICK_CLIENT_SECRET
    return None
