# manager.py — runs the Twitch, Kick and Owncast bots
#
# One background thread runs an asyncio loop; each platform is a task on it.
# When settings or logins change (the Setup window saves), only the affected
# platform is restarted. A bot that crashes is restarted with a back-off, and
# its status ("Reading #channel", "Login expired", …) is reported to the UI.

import asyncio
import hashlib
import json
import threading
import traceback

from chat.commands import CommandRouter

PLATFORMS = ("twitch", "kick", "owncast")
NAMES = {"twitch": "Twitch", "kick": "Kick", "owncast": "Owncast"}

# Which settings / logins each platform depends on.
_DEPENDS = {
    "twitch": (["twitch"], ["twitch"]),
    "kick": (["kick"], ["kick", "kick_app", "kick_relay"]),
    "owncast": (["owncast"], ["owncast"]),
}


class _Ctx:
    """What a bot gets: settings, the command router, and status/notice hooks."""

    def __init__(self, manager, platform):
        self._m = manager
        self.platform = platform
        self.settings = manager.settings
        self.router = manager.router

    def status(self, level, text):
        self._m._set_status(self.platform, level, text)

    def notice(self, msg):
        self._m._notice(f"{NAMES[self.platform]} — {msg}")


class ChatManager:
    def __init__(self, window, settings, call_on_gui, on_status=None, notice=None):
        self.settings = settings
        self.router = CommandRouter(window, settings, call_on_gui)
        self._on_status = on_status or (lambda p, lvl, txt: None)
        self._notice_fn = notice or print
        self.loop = None
        self._thread = None
        self._tasks = {}
        self._fp = {}
        self._status = {p: ("off", "Not set up") for p in PLATFORMS}
        self._started = threading.Event()
        settings.on_change(lambda section: self.apply())

    # ── lifecycle ──

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop_main, daemon=True, name="chat")
        self._thread.start()
        self._started.wait(5)
        self.apply()

    def _loop_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._started.set()
        self.loop.run_forever()

    def stop(self):
        if not self.loop:
            return

        async def _stop_all():
            for t in list(self._tasks.values()):
                t.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_stop_all(), self.loop).result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    def apply(self):
        """Start/restart/stop bots to match the current settings."""
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._apply(), self.loop)

    # ── status ──

    def status(self, platform):
        return self._status.get(platform, ("off", ""))

    def _set_status(self, platform, level, text):
        if self._status.get(platform) == (level, text):
            return
        self._status[platform] = (level, text)
        print(f"[{NAMES[platform]}] {level}: {text}")
        try:
            self._on_status(platform, level, text)
        except Exception:
            pass

    def _notice(self, msg):
        try:
            self._notice_fn(msg)
        except Exception:
            print(msg)

    # ── bots ──

    def _fingerprint(self, platform):
        secs, secrets_ = _DEPENDS[platform]
        blob = {"s": {s: self.settings.section(s) for s in secs},
                "k": {s: self.settings.secret(s) for s in secrets_}}
        # Kick saves the ids it looks up into its own settings section; those
        # are not a reason to restart the bot.
        for k in ("chatroom_id", "broadcaster_user_id", "channel_id"):
            blob["s"].get("kick", {}).pop(k, None)
        # Kick refreshes its own token as it goes; same story.
        for k in ("access_token", "refresh_token", "expires_at"):
            blob["k"].get("kick", {}).pop(k, None)
        if platform == "kick":
            blob["k"]["signed_in"] = bool(self.settings.secret("kick").get("refresh_token")
                                          or self.settings.secret("kick").get("access_token"))
        return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()

    def _make_bot(self, platform):
        ctx = _Ctx(self, platform)
        if platform == "twitch":
            from chat.twitch import TwitchBot
            return TwitchBot(ctx)
        if platform == "kick":
            from chat.kick import KickBot
            return KickBot(ctx)
        from chat.owncast import OwncastBot
        return OwncastBot(ctx)

    async def _apply(self):
        for p in PLATFORMS:
            fp = self._fingerprint(p)
            task = self._tasks.get(p)
            if task and not task.done() and self._fp.get(p) == fp:
                continue
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            self._fp[p] = fp
            if not self.settings.get(p, "enabled", True):
                self._set_status(p, "off", "Turned off")
                self._tasks.pop(p, None)
                continue
            self._tasks[p] = asyncio.create_task(self._supervise(p))

    async def _supervise(self, platform):
        delay = 5
        while True:
            bot = self._make_bot(platform)
            try:
                await bot.run()
                return                      # not configured, or needs a new login
            except asyncio.CancelledError:
                raise
            except Exception as e:
                traceback.print_exc()
                self._set_status(platform, "error", f"Problem: {type(e).__name__} — restarting")
            await asyncio.sleep(delay)
            delay = min(120, delay * 2)

    # ── helpers for the Setup window (run work off the GUI thread) ──

    def run_async(self, coro, done):
        """Run a coroutine on the chat loop; done(result) on completion."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: done(f.exception() or f.result()))
        return fut

    @staticmethod
    def run_thread(fn, done):
        def work():
            try:
                res = fn()
            except Exception as e:
                traceback.print_exc()
                res = (False, f"{type(e).__name__}: {e}")
            done(res)
        threading.Thread(target=work, daemon=True).start()
