# commands.py — the chat commands, shared by Twitch, Kick and Owncast
#
#   !sr <song name or link>    request a song (also !addsong, !request, !play <song>)
#   !sc <SoundCloud link>      request a SoundCloud track
#   !song                      what's playing
#   !queue                     the next few songs
#   !skip !back !pause !play !volume <0-100> !clear
#                              playback control — mods and the streamer only,
#                              unless "Anyone can control playback" is ticked
#   !help                      list the commands

import asyncio

from request_text import clean_request

REQUEST_WAIT = 125          # the player answers within 100s; leave headroom


class CommandRouter:
    def __init__(self, window, settings, call_on_gui):
        """window: PlayerWindow. call_on_gui(fn): run fn on the Qt thread."""
        self.w = window
        self.settings = settings
        self.gui = call_on_gui

    def _anyone_controls(self):
        return bool(self.settings.get("player", "anyone_can_control", False))

    async def _request(self, platform, user, arg):
        """Hand a request to the player; return its final chat message."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def on_done(result):
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(result))

        self.w.api_addsong(arg, user, platform, on_done=on_done)
        try:
            result = await asyncio.wait_for(fut, REQUEST_WAIT)
        except asyncio.TimeoutError:
            return "⏳ Still looking for that one — it'll be added if it's found."
        if result.get("status") == "duplicate":
            return None                       # the first copy already answered
        msg = (result.get("message") or "").strip()
        if not msg:
            msg = ("✅ Added to the queue." if result.get("ok")
                   else "❌ That request didn't go through.")
        return msg

    async def handle(self, platform, user, text, is_mod, reply):
        """reply(msg) is an async function that answers the sender in chat."""
        parts = (text or "").strip().split(maxsplit=1)
        if not parts or not parts[0].startswith("!"):
            return False
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        w = self.w

        if cmd in ("!sr", "!addsong", "!request", "!songrequest", "!sc") or \
                (cmd == "!play" and arg):
            # "Listen to … on #SoundCloud https://…" pasted from a phone: the
            # link is the request, the words in front are just the share text.
            arg = clean_request(arg)

        if cmd in ("!sr", "!addsong", "!request", "!songrequest") or (cmd == "!play" and arg):
            if not arg:
                await reply("Usage: !sr <song name or link>")
                return True
            await reply(f"🔎 Searching: {arg[:120]}...")
            msg = await self._request(platform, user, arg)
            if msg:
                await reply(msg)
            return True

        if cmd == "!sc":
            if "soundcloud.com/" not in arg.lower():
                await reply("Usage: !sc <SoundCloud link>")
                return True
            await reply("🔎 Adding SoundCloud track...")
            msg = await self._request(platform, user, arg)
            if msg:
                await reply(msg)
            return True

        if cmd == "!song":
            d = w.get_player_status() or {}
            if not d.get("title"):
                await reply("Nothing is playing right now.")
            else:
                src = "SoundCloud" if d.get("scActive") else "YouTube"
                by = f" (requested by {d['requestedBy']})" if d.get("requestedBy") else ""
                artist = d.get("artist") or ""
                label = f"{artist} - {d['title']}" if artist and artist.lower() not in \
                    ("unknown", "…") else d["title"]
                await reply(f"🎵 Now playing: {label} [{src}]{by}")
            return True

        if cmd == "!queue":
            q = (w.get_queue_status() or {}).get("tracks") or []
            if not q:
                await reply("The queue is empty — request something with !sr")
            else:
                items = []
                for i, t in enumerate(q[:5]):
                    title = (t.get("title") or "").replace("▶ [NOW PLAYING] ", "▶ ")
                    artist = t.get("artist") or ""
                    if artist and artist not in ("Unknown", "…"):
                        title = f"{artist} - {title}"
                    items.append(f"{i + 1}. {title}")
                await reply("Queue: " + " | ".join(items))
            return True

        if cmd in ("!help", "!commands", "!music"):
            await reply("Song commands: !sr <song or link>, !sc <SoundCloud link>, !song, "
                        "!queue" + (", !skip, !back, !pause, !play, !volume"
                                    if self._anyone_controls() else
                                    " — mods: !skip !back !pause !play !volume !clear"))
            return True

        if cmd == "!ping":
            await reply("pong")
            return True

        control = {"!skip", "!next", "!back", "!prev", "!pause", "!play", "!resume",
                   "!volume", "!vol", "!clear"}
        if cmd not in control:
            return False
        if not (is_mod or self._anyone_controls()):
            await reply(f"Only mods can use {cmd} here.")
            return True

        from audio_engine import PlayerState
        if cmd in ("!skip", "!next"):
            self.gui(w._on_skip)
            await reply("⏭️ Skipped.")
        elif cmd in ("!back", "!prev"):
            self.gui(w._on_back)
            await reply("⏮️ Back.")
        elif cmd == "!pause":
            self.gui(w.engine.pause)
            await reply("⏸️ Paused.")
        elif cmd in ("!play", "!resume"):
            def resume():
                if w.engine.state == PlayerState.PAUSED:
                    w.engine.resume()
                elif w.engine.state == PlayerState.STOPPED:
                    w._advance_if_idle()
            self.gui(resume)
            await reply("▶️ Playing.")
        elif cmd in ("!volume", "!vol"):
            try:
                vol = int(arg)
                if not 0 <= vol <= 100:
                    raise ValueError
            except ValueError:
                await reply(f"Volume is {w.engine.volume}%. Usage: !volume <0-100>")
                return True
            self.gui(lambda: w.set_volume_from_chat(vol))
            await reply(f"🔊 Volume set to {vol}%.")
        elif cmd == "!clear":
            self.gui(w.queue.clear)
            await reply("🗑️ Queue cleared.")
        return True
