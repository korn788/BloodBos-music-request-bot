# write_build_keys.py — put OAuth app keys from the build's environment into
# src/_build_keys.py (git-ignored), so a release can ship a built-in Kick app.
#
# In GitHub: Settings > Secrets and variables > Actions >
#   secrets:   KICK_CLIENT_SECRET
#   variables: KICK_CLIENT_ID, KICK_REDIRECT_URI (optional),
#              TWITCH_CLIENT_ID, TWITCH_REDIRECT_URI (optional)
# Anything not set keeps the default in src/app_keys.py. With no Kick secret,
# streamers make their own free Kick app in the Setup window instead.

import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "src", "_build_keys.py")
NAMES = ("TWITCH_CLIENT_ID", "TWITCH_REDIRECT_URI", "KICK_CLIENT_ID",
         "KICK_CLIENT_SECRET", "KICK_REDIRECT_URI")

values = {n: os.environ.get(n, "").strip() for n in NAMES}
values = {n: v for n, v in values.items() if v}
if bool(values.get("KICK_CLIENT_ID")) != bool(values.get("KICK_CLIENT_SECRET")):
    print("note: Kick needs both KICK_CLIENT_ID and KICK_CLIENT_SECRET — skipping Kick")
    values.pop("KICK_CLIENT_ID", None)
    values.pop("KICK_CLIENT_SECRET", None)

with open(OUT, "w", encoding="utf-8") as f:
    f.write("# generated at build time by packaging/write_build_keys.py — do not commit\n")
    for n, v in values.items():
        f.write(f"{n} = {v!r}\n")
print("build keys:", ", ".join(sorted(values)) or "(none — defaults)")
