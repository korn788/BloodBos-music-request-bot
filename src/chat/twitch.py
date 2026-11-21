# twitch.py — Twitch chat: sign-in, reading chat, replying
#
# Sign-in: Twitch's "implicit" flow. The Connect button opens Twitch in your
#   browser; you click Authorize; Twitch sends the browser back to this
#   computer and the app keeps the login. No client secret is involved.
#   Twitch logins made this way last about two months, then the app asks you
#   to click Connect again.
#
# Reading chat: EventSub over a WebSocket (Twitch's recommended way). Every
#   connection is fresh — if anything at all goes wrong the bot opens a new
#   socket and subscribes again, which is what makes it self-healing.
#
# Replying: Helix "Send Chat Message", as whoever signed in (your own account,
#   or a separate bot account if you signed in with one).

import asyncio
import secrets
import time
import urllib.parse
from collections import deque

import aiohttp

import app_keys
import desktop
from chat import http_util, oauth_local

SCOPES = ["user:read:chat", "user:write:chat"]
EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"
HELIX = "https://api.twitch.tv/helix"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
MSG_LIMIT = 490
MOD_BADGES = {"broadcaster", "moderator", "lead_moderator"}


class AuthProblem(Exception):
    """The login is gone or lacks permission — needs the Connect button."""


def _clip(s):
    s = (s or "").strip()
    return s if len(s) <= MSG_LIMIT else s[:MSG_LIMIT - 1] + "…"


def authorize_url(state):
    params = [("response_type", "token"), ("client_id", app_keys.TWITCH_CLIENT_ID),
              ("redirect_uri", app_keys.TWITCH_REDIRECT_URI), ("scope", " ".join(SCOPES)),
              ("state", state), ("force_verify", "true")]
    return ("https://id.twitch.tv/oauth2/authorize?"
            + urllib.parse.urlencode(params, quote_via=urllib.parse.quote))


async def validate(token):
    """Twitch's token check. dict = valid, {} = rejected, None = couldn't ask."""
    status, text = await http_util.request(
        "GET", VALIDATE_URL, headers={"Authorization": f"OAuth {token}"}, timeout=15)
    if status == 200:
        return http_util.loads(text) or None
    if status in (400, 401):
        return {}
    return None


def login_blocking(settings, cancel=None, open_browser=None, manual=None, on_url=None):
    """Run the whole sign-in (call from a worker thread). Returns (ok, message).
    on_url(url) hands the login link to the Setup window (to open it in another
    browser); manual is an oauth_local.Manual for pasting the address the
    browser ended up on when that page wouldn't load."""
    if not app_keys.TWITCH_CLIENT_ID:
        return False, "This build has no Twitch app configured."
    state = secrets.token_urlsafe(18)
    url = authorize_url(state)
    if on_url:
        on_url(url)

    def ready(_listening):
        try:
            (open_browser or desktop.open_url)(url)
        except Exception:
            pass

    tok, err = oauth_local.wait_for_implicit_token(
        app_keys.TWITCH_REDIRECT_URI, state, "Twitch", timeout=900, cancel=cancel,
        manual=manual, on_ready=ready)
    if not tok:
        return False, {"cancelled": "Cancelled.", "timed out": "Timed out waiting for Twitch."}.get(
            err, err or "Twitch sign-in didn't finish.")
    access = tok["access_token"]
    info = asyncio.run(validate(access))
    if not info:
        return False, "Twitch didn't accept that login. Please try again."
    granted = set(info.get("scopes") or [])
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        return False, f"Twitch didn't grant: {', '.join(missing)}. Please try again."
    settings.set_secret("twitch", {
        "access_token": access,
        "login": info.get("login", ""),
        "user_id": str(info.get("user_id", "")),
        "client_id": info.get("client_id", ""),
        "scopes": sorted(granted),
        "expires_at": int(time.time()) + int(info.get("expires_in") or 0),
    }, replace=True, notify=False)
    if not (settings.get("twitch", "channel") or "").strip():
        settings.set("twitch", "channel", info.get("login", ""), notify=False)
    settings.set("twitch", "enabled", True, notify=False)
    settings.notify("twitch")
    return True, f"Connected as {info.get('login', '?')}"


class TwitchBot:
    platform = "twitch"

    def __init__(self, ctx):
        self.ctx = ctx                      # manager context (settings, router, status…)
        self.settings = ctx.settings
        self.token = ""
        self.client_id = app_keys.TWITCH_CLIENT_ID
        self.uid = ""                       # who we are (signed-in account)
        self.login = ""
        self.bid = ""                       # whose chat we read (the channel)
        self.channel = ""
        self._send_lock = None
        self._seen = set()
        self._seen_order = deque()
        self._sent = set()

    # ── helpers ──

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Client-Id": self.client_id,
                "Content-Type": "application/json"}

    def _remember(self, mid):
        if not mid:
            return False
        if mid in self._seen:
            return True
        self._seen.add(mid)
        self._seen_order.append(mid)
        while len(self._seen_order) > 1000:
            self._seen.discard(self._seen_order.popleft())
        return False

    async def _check_login(self):
        """Validate; returns info dict. Raises AuthProblem when it's gone."""
        delay = 5
        while True:
            info = await validate(self.token)
            if info:
                return info
            if info == {}:
                raise AuthProblem("Twitch login expired — click Connect Twitch to sign in again")
            self.ctx.status("connecting", "Can't reach Twitch — retrying…")
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)

    async def _lookup_channel(self, login):
        status, text = await http_util.request(
            "GET", f"{HELIX}/users?login={urllib.parse.quote(login)}", headers=self._headers())
        if status == 401:
            raise AuthProblem("Twitch login expired — click Connect Twitch to sign in again")
        d = http_util.loads(text) or {}
        rows = d.get("data") or []
        return str(rows[0]["id"]) if rows else None

    # ── main ──

    async def run(self):
        tok = self.settings.secret("twitch")
        self.token = tok.get("access_token") or ""
        if tok.get("client_id"):
            self.client_id = tok["client_id"]
        if not self.token:
            self.ctx.status("off", "Not connected — click Connect Twitch")
            return
        self._send_lock = asyncio.Lock()
        try:
            self.ctx.status("connecting", "Checking login…")
            info = await self._check_login()
            self.uid = str(info.get("user_id"))
            self.login = info.get("login") or ""
            self.channel = ((self.settings.get("twitch", "channel") or self.login)
                            .strip().lstrip("#").lower())
            while True:
                bid = await self._lookup_channel(self.channel)
                if bid:
                    self.bid = bid
                    break
                self.ctx.status("error", f"Can't find the Twitch channel “{self.channel}” — "
                                         f"check the name in Setup")
                await asyncio.sleep(60)
            reader = asyncio.create_task(self._eventsub_forever())
            watcher = asyncio.create_task(self._watch_login())
            try:
                done, _ = await asyncio.wait({reader, watcher},
                                             return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    t.result()              # re-raises AuthProblem from either side
            finally:
                reader.cancel()
                watcher.cancel()
                await asyncio.gather(reader, watcher, return_exceptions=True)
        except AuthProblem as e:
            self.ctx.status("error", str(e))
            self.ctx.notice(str(e))

    async def _watch_login(self):
        """Twitch asks apps to re-check logins hourly; also warns before expiry."""
        warned_day = None
        while True:
            await asyncio.sleep(3600)
            info = await validate(self.token)
            if info == {}:
                self.ctx.status("error", "Twitch login expired — click Connect Twitch to sign in again")
                self.ctx.notice("Twitch login expired — open Setup and click Connect Twitch")
                raise AuthProblem("expired")
            if info:
                days = int(info.get("expires_in") or 0) // 86400
                if 0 < days <= 5 and warned_day != time.strftime("%Y-%m-%d"):
                    warned_day = time.strftime("%Y-%m-%d")
                    self.ctx.notice(f"Twitch login runs out in {days} day{'s' if days != 1 else ''} "
                                    f"— click Connect Twitch in Setup to renew it")

    async def _eventsub_forever(self):
        backoff = 1
        while True:
            started = time.monotonic()
            try:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    await self._socket(session)
            except AuthProblem:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ctx.status("connecting", f"Reconnecting to Twitch chat… ({type(e).__name__})")
                print(f"[Twitch] socket ended: {type(e).__name__}: {e}")
            if time.monotonic() - started > 60:
                backoff = 1
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)

    async def _socket(self, session):
        ws = await session.ws_connect(EVENTSUB_URL, heartbeat=None, max_msg_size=4 << 20)
        keepalive = 30
        subscribed = False
        try:
            while True:
                try:
                    msg = await ws.receive(timeout=keepalive + 15)
                except asyncio.TimeoutError:
                    raise ConnectionError("no keepalive from Twitch")
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    raise ConnectionError(f"Twitch closed the socket (code {ws.close_code})")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = http_util.loads(msg.data) or {}
                meta = data.get("metadata") or {}
                mtype = meta.get("message_type")
                payload = data.get("payload") or {}

                if mtype == "session_welcome":
                    sess = payload.get("session") or {}
                    keepalive = int(sess.get("keepalive_timeout_seconds") or 30)
                    if not subscribed:
                        await self._subscribe(sess.get("id"))
                        subscribed = True
                        self.ctx.status("ok", f"Reading #{self.channel} as {self.login}")
                elif mtype == "notification":
                    if self._remember(meta.get("message_id")):
                        continue
                    if meta.get("subscription_type") == "channel.chat.message":
                        self._on_chat(payload.get("event") or {})
                elif mtype == "session_reconnect":
                    # Twitch is moving us: open the new socket, wait for its
                    # welcome, then drop the old one. Subscriptions carry over.
                    url = (payload.get("session") or {}).get("reconnect_url")
                    if not url:
                        raise ConnectionError("reconnect without a URL")
                    new_ws = await session.ws_connect(url, heartbeat=None, max_msg_size=4 << 20)
                    first = await new_ws.receive(timeout=20)
                    d = http_util.loads(getattr(first, "data", "")) or {}
                    if (d.get("metadata") or {}).get("message_type") != "session_welcome":
                        await new_ws.close()
                        raise ConnectionError("reconnect socket didn't welcome us")
                    keepalive = int(((d.get("payload") or {}).get("session") or {})
                                    .get("keepalive_timeout_seconds") or keepalive)
                    await ws.close()
                    ws = new_ws
                elif mtype == "revocation":
                    reason = ((payload.get("subscription") or {}).get("status") or "revoked")
                    if reason in ("authorization_revoked", "user_removed"):
                        raise AuthProblem("Twitch login was revoked — click Connect Twitch again")
                    raise ConnectionError(f"subscription revoked: {reason}")
                # session_keepalive: nothing to do — receiving it is the point
        finally:
            await ws.close()

    async def _subscribe(self, session_id):
        body = {"type": "channel.chat.message", "version": "1",
                "condition": {"broadcaster_user_id": self.bid, "user_id": self.uid},
                "transport": {"method": "websocket", "session_id": session_id}}
        status, text = await http_util.request(
            "POST", f"{HELIX}/eventsub/subscriptions", headers=self._headers(), json_body=body)
        if status in (200, 202, 409):
            return
        if status == 401:
            raise AuthProblem("Twitch login expired — click Connect Twitch to sign in again")
        if status == 403:
            raise AuthProblem("Twitch won't let this login read that chat "
                              "(missing permission) — click Connect Twitch again")
        raise ConnectionError(f"subscribe failed ({status}): {text[:200]}")

    def _on_chat(self, ev):
        if str(ev.get("broadcaster_user_id")) != self.bid:
            return
        src = ev.get("source_broadcaster_user_id")
        if src and str(src) != self.bid:
            return                              # shared chat from another channel
        mid = ev.get("message_id") or ""
        if mid in self._sent:
            return
        text = ((ev.get("message") or {}).get("text") or "").strip()
        if not text.startswith("!"):
            return
        user = ev.get("chatter_user_name") or ev.get("chatter_user_login") or "someone"
        badges = {b.get("set_id") for b in (ev.get("badges") or []) if isinstance(b, dict)}
        is_mod = bool(badges & MOD_BADGES) or str(ev.get("chatter_user_id")) == self.bid
        print(f"[Twitch] {user}: {text}")

        async def reply(m):
            await self.send(m, reply_to=mid or None)

        asyncio.create_task(self.ctx.router.handle("twitch", user, text, is_mod, reply))

    async def send(self, text, reply_to=None):
        text = _clip(text)
        if not text or not self.bid:
            return
        async with self._send_lock:
            body = {"broadcaster_id": self.bid, "sender_id": self.uid, "message": text}
            if reply_to:
                body["reply_parent_message_id"] = reply_to
            for _ in range(3):
                status, resp = await http_util.request(
                    "POST", f"{HELIX}/chat/messages", headers=self._headers(), json_body=body)
                if 200 <= status < 300:
                    item = ((http_util.loads(resp) or {}).get("data") or [{}])[0]
                    if item.get("message_id"):
                        self._sent.add(item["message_id"])
                    if item.get("is_sent") is False:
                        reason = (item.get("drop_reason") or {}).get("message", "")
                        print(f"[Twitch] message dropped: {reason}")
                        if "reply_parent_message_id" in body:
                            body.pop("reply_parent_message_id")
                            continue
                    break
                if status == 401:
                    self.ctx.status("error", "Twitch login expired — click Connect Twitch")
                    break
                if status in (400, 422) and "reply_parent_message_id" in body:
                    body.pop("reply_parent_message_id")
                    continue
                if status in (0, 429, 500, 502, 503):
                    await asyncio.sleep(1.5)
                    continue
                print(f"[Twitch] send failed ({status}): {resp[:200]}")
                break
            await asyncio.sleep(0.35)        # stay well inside Twitch's chat limits
