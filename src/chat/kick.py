# kick.py — Kick chat: reading chat, sign-in, replying
#
# Reading chat needs NO sign-in. The bot reads Kick's live chat feed the same
# way kick.com's own chat box does: it asks Kick for the current feed key
# (Kick changed it in October 2026 and may again), then listens to your
# channel's chat room. Just type your channel name in Setup.
#
# Replying needs a sign-in ("Sign in to Kick"). Kick's sign-in always needs a
# Kick developer app with a client secret. Release builds can ship one; if this
# build doesn't, the Setup window walks you through making your own free app
# (two minutes, once).
#
# Advanced: instead of the live feed, chat can come from Kick's official
# webhooks through a small relay on your own server (extras/install_kick_relay.sh).
# Set relay_url in settings.json and the relay key in Setup to use it.

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
import uuid
import webbrowser
from collections import deque

import aiohttp

import app_keys
from chat import http_util, oauth_local

API_BASE = "https://api.kick.com/public/v1"
OAUTH_BASE = "https://id.kick.com"
PUBLIC_KEY_URL = f"{API_BASE}/public-key"
REALTIME_URL = "https://web.kick.com/api/v1/realtime/connection"
# Kick's live-chat feed key as of November 2026, used only if Kick can't be
# asked for the current one.
FALLBACK_PUSHER = ("34bf7a0ff419a2a775b9", "us2")
# Only what the bot needs: who signed in (user:read) and posting (chat:write).
# Asking for a scope the Kick app doesn't have ticked makes Kick refuse the
# sign-in with "invalid authorization request".
SCOPES = ["user:read", "chat:write"]
MSG_LIMIT = 480
CHAT_EVENT = "chat.message.sent"
RELAY_SILENCE = 60
MAX_EVENT_AGE = 600
RESUBSCRIBE_EVERY = 1800
MOD_BADGES = {"broadcaster", "moderator"}

KICK_HEADERS = {"User-Agent": http_util.BROWSER_UA, "Accept": "application/json",
                "Referer": "https://kick.com/", "Origin": "https://kick.com"}


class AuthProblem(Exception):
    pass


def _clip(s):
    s = (s or "").strip()
    return s if len(s) <= MSG_LIMIT else s[:MSG_LIMIT - 1] + "…"


def normalize_channel(text):
    """'https://kick.com/Some_Name' → 'some-name' (Kick slugs use dashes)."""
    t = (text or "").strip()
    t = re.sub(r"^https?://", "", t, flags=re.I)
    t = re.sub(r"^(www\.)?kick\.com/", "", t, flags=re.I)
    t = t.strip("/ @").split("/")[0].split("?")[0]
    return t.lower().replace("_", "-")


# ═══════════════════════════════════════════════════════════════════════════
#  Which Kick app signs people in
# ═══════════════════════════════════════════════════════════════════════════

def kick_app(settings):
    """(client_id, client_secret, redirect_uri) or None.

    The streamer's own app (entered in Setup) wins over one built into this
    release, so a self-made app keeps working across updates."""
    own = settings.secret("kick_app")
    if own.get("client_id") and own.get("client_secret"):
        return own["client_id"].strip(), own["client_secret"].strip(), app_keys.KICK_REDIRECT_URI
    built = app_keys.builtin_kick_app()
    if built:
        return built[0], built[1], app_keys.KICK_REDIRECT_URI
    return None


def _app_for_token(settings, tok):
    """The app that issued this token (refreshing needs the same one)."""
    app = kick_app(settings)
    cid = (tok or {}).get("client_id")
    if not cid or (app and app[0] == cid):
        return app
    own = settings.secret("kick_app")
    if own.get("client_id") == cid and own.get("client_secret"):
        return cid, own["client_secret"], app_keys.KICK_REDIRECT_URI
    built = app_keys.builtin_kick_app()
    if built and built[0] == cid:
        return built[0], built[1], app_keys.KICK_REDIRECT_URI
    return app


async def token_request(app, form):
    cid, secret, _ = app
    form = dict(form, client_id=cid, client_secret=secret)
    status, text = await http_util.request(
        "POST", f"{OAUTH_BASE}/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        data=form, impersonate=True)
    if status != 200:
        print(f"[Kick] token request failed ({status}): {text[:200]}")
        return None
    return http_util.loads(text)


async def _whoami(access):
    hdrs = {"Authorization": f"Bearer {access}", "Accept": "application/json"}
    status, text = await http_util.request("GET", f"{API_BASE}/users", headers=hdrs,
                                           impersonate=True)
    user = ((http_util.loads(text) or {}).get("data") or [{}])[0] if status == 200 else {}
    status, text = await http_util.request("GET", f"{API_BASE}/channels", headers=hdrs,
                                           impersonate=True)
    chan = ((http_util.loads(text) or {}).get("data") or [{}])[0] if status == 200 else {}
    return user or {}, chan or {}


def login_blocking(settings, cancel=None, open_browser=None):
    """Sign in to Kick (call from a worker thread). Returns (ok, message)."""
    app = kick_app(settings)
    if not app:
        return False, "Add your Kick app's Client ID and Client Secret first."
    cid, _, redirect = app
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    params = []
    if "127.0.0.1" in redirect:
        params.append(("x", "127.0.0.1"))   # stops Kick rewriting it to localhost
    params += [("response_type", "code"), ("client_id", cid), ("redirect_uri", redirect),
               ("scope", " ".join(SCOPES)), ("code_challenge", challenge),
               ("code_challenge_method", "S256"), ("state", state)]
    url = f"{OAUTH_BASE}/oauth/authorize?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    try:
        (open_browser or webbrowser.open)(url)
    except Exception:
        pass
    code, err = oauth_local.wait_for_code(redirect, state, "Kick", timeout=300, cancel=cancel)
    if not code:
        return False, {"cancelled": "Cancelled.", "timed out": "Timed out waiting for Kick."}.get(
            err, err or "Kick sign-in didn't finish.")
    tok = asyncio.run(token_request(app, {"grant_type": "authorization_code",
                                          "redirect_uri": redirect,
                                          "code_verifier": verifier, "code": code}))
    if not tok or not tok.get("access_token"):
        return False, ("Kick didn't accept the sign-in. If you made your own Kick app, "
                       "check its Client Secret.")
    user, chan = asyncio.run(_whoami(tok["access_token"]))
    name = user.get("name") or chan.get("slug") or ""
    if not chan.get("slug") and name:            # /channels may need a scope we don't ask for
        chan = {"slug": normalize_channel(name), "broadcaster_user_id": user.get("user_id") or 0}
    settings.set_secret("kick", {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(tok.get("expires_in") or 3600),
        "scope": tok.get("scope", " ".join(SCOPES)),
        "client_id": cid,
        "user_id": user.get("user_id") or "",
        "username": name,
    }, replace=True, notify=False)
    if not (settings.get("kick", "channel") or "").strip() and chan.get("slug"):
        settings.update("kick", {"channel": chan["slug"],
                                 "broadcaster_user_id": int(chan.get("broadcaster_user_id") or 0),
                                 "chatroom_id": 0}, notify=False)
    settings.set("kick", "enabled", True, notify=False)
    settings.notify("kick")
    return True, f"Signed in as {name or 'your Kick account'}"


# ═══════════════════════════════════════════════════════════════════════════
#  Channel lookup and the live-chat feed key
# ═══════════════════════════════════════════════════════════════════════════

async def lookup_channel(slug):
    """{'chatroom_id', 'broadcaster_user_id', 'channel_id'} or None / 'missing'."""
    for url in (f"https://kick.com/api/v2/channels/{urllib.parse.quote(slug)}",
                f"https://kick.com/api/v1/channels/{urllib.parse.quote(slug)}"):
        status, text = await http_util.request("GET", url, headers=KICK_HEADERS,
                                               impersonate=True, timeout=20)
        if status == 404:
            return "missing"
        d = http_util.loads(text) if status == 200 else None
        if isinstance(d, dict):
            room = d.get("chatroom") or {}
            rid = room.get("id") if isinstance(room, dict) else None
            if rid:
                return {"chatroom_id": int(rid),
                        "broadcaster_user_id": int(d.get("user_id") or 0),
                        "channel_id": int(d.get("id") or 0)}
    return None


def pusher_credentials_from(data):
    inner = (data or {}).get("data") if isinstance((data or {}).get("data"), dict) else data
    conns = (inner or {}).get("connections") if isinstance(inner, dict) else None
    for c in conns or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("provider") or "pusher").lower() != "pusher":
            continue
        cr = c.get("credentials") or {}
        key = str(cr.get("app_key") or cr.get("key") or "").strip()
        cluster = str(cr.get("cluster") or "us2").strip()
        if key.isalnum() and cluster.replace("-", "").isalnum():
            return key, cluster
    return None


async def fetch_pusher_credentials(channel_id=0):
    """Ask Kick (like kick.com does) for the current feed key, Pusher only."""
    body = {"client": {"id": str(uuid.uuid4()), "type": "web"},
            "capabilities": {"accepted_providers": [{"provider": "pusher"}]}}
    hdrs = dict(KICK_HEADERS, **{"Content-Type": "application/json"})
    urls = [REALTIME_URL]
    if channel_id:
        urls.insert(0, f"https://web.kick.com/api/v1/realtime/channels/{int(channel_id)}/chat/connection")
    for url in urls:
        status, text = await http_util.request("POST", url, headers=hdrs, json_body=body,
                                               impersonate=True, timeout=15)
        if status == 200:
            found = pusher_credentials_from(http_util.loads(text))
            if found:
                return found
    return None


def pusher_url(key, cluster):
    return (f"wss://ws-{cluster}.pusher.com/app/{key}"
            "?protocol=7&client=js&version=8.4.0&flash=false")


# ═══════════════════════════════════════════════════════════════════════════
#  Webhook signature check for relay mode (RSA PKCS#1 v1.5 / SHA-256)
# ═══════════════════════════════════════════════════════════════════════════

_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_RSA_OID = bytes.fromhex("2a864886f70d010101")


def _der(buf, i):
    tag = buf[i]
    ln = buf[i + 1]
    i += 2
    if ln & 0x80:
        n = ln & 0x7F
        if not 0 < n <= 4:
            raise ValueError("bad DER length")
        ln = int.from_bytes(buf[i:i + n], "big")
        i += n
    if i + ln > len(buf):
        raise ValueError("truncated DER")
    return tag, buf[i:i + ln], i + ln


def parse_public_key(text):
    text = (text or "").strip()
    if text.startswith("{"):
        d = json.loads(text)
        text = ((d.get("data") or {}).get("public_key") or d.get("public_key") or "").strip()
    b64 = "".join(ln.strip() for ln in text.splitlines()
                  if ln.strip() and not ln.startswith("-----"))
    der = base64.b64decode(b64)
    tag, seq, _ = _der(der, 0)
    if tag != 0x30:
        raise ValueError("not a key")
    t1, c1, j = _der(seq, 0)
    if t1 == 0x02:
        _, c2, _ = _der(seq, j)
        return int.from_bytes(c1, "big"), int.from_bytes(c2, "big")
    _, oid, _ = _der(c1, 0)
    if oid != _RSA_OID:
        raise ValueError("not an RSA key")
    tb, bits, _ = _der(seq, j)
    if tb != 0x03 or not bits or bits[0] != 0:
        raise ValueError("bad key bits")
    _, rsa, _ = _der(bits[1:], 0)
    _, cn, k = _der(rsa, 0)
    _, ce, _ = _der(rsa, k)
    return int.from_bytes(cn, "big"), int.from_bytes(ce, "big")


def _rsa_sha256_ok(key, message, sig):
    n, e = key
    k = (n.bit_length() + 7) // 8
    if len(sig) > k:
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        return False
    return hmac.compare_digest(em, b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t)


def signature_ok(key, msg_id, ts, body, sig_b64):
    try:
        sig = base64.b64decode(sig_b64)
    except Exception:
        return False
    for msg in (f"{msg_id}.{ts}.".encode() + body, f"{msg_id}{ts}".encode() + body):
        if _rsa_sha256_ok(key, msg, sig):
            return True
    return False


_TS = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$")


def _epoch(ts):
    m = _TS.match((ts or "").strip())
    if not m:
        return None
    import calendar
    y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
    t = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) + float(m.group(7) or 0)
    z = m.group(8)
    if z and z != "Z":
        sign = 1 if z[0] == "+" else -1
        z = z[1:].replace(":", "")
        t -= sign * (int(z[:2]) * 3600 + int(z[2:]) * 60)
    return t


# ═══════════════════════════════════════════════════════════════════════════
#  The bot
# ═══════════════════════════════════════════════════════════════════════════

class KickBot:
    platform = "kick"

    def __init__(self, ctx):
        self.ctx = ctx
        self.settings = ctx.settings
        self.channel = ""
        self.chatroom_id = 0
        self.broadcaster_id = 0
        self.channel_id = 0
        self.token = None
        self._token_lock = None
        self._send_lock = None
        self._seen = set()
        self._seen_order = deque()
        self._pusher = None              # (key, cluster) last known good
        self._signed_in_note = ""
        # relay mode
        self._app_tok = None
        self._app_tok_exp = 0.0
        self._pubkey = None
        self._pubkey_tried = 0.0
        self._rejected = 0
        self._subscribed = False

    # ── dedupe ──

    def _remember(self, *ids):
        dup = any(i and i in self._seen for i in ids)
        for i in ids:
            if i and i not in self._seen:
                self._seen.add(i)
                self._seen_order.append(i)
        while len(self._seen_order) > 2000:
            self._seen.discard(self._seen_order.popleft())
        return dup

    # ── tokens ──

    async def _access_token(self, force_refresh=False):
        async with self._token_lock:
            tok = self.settings.secret("kick")
            if not tok.get("access_token"):
                return None
            if force_refresh or int(tok.get("expires_at") or 0) - time.time() < 300:
                app = _app_for_token(self.settings, tok)
                new = await token_request(app, {"grant_type": "refresh_token",
                                                "refresh_token": tok.get("refresh_token", "")}) \
                    if app and tok.get("refresh_token") else None
                if new and new.get("access_token"):
                    tok.update({"access_token": new["access_token"],
                                "refresh_token": new.get("refresh_token") or tok.get("refresh_token", ""),
                                "expires_at": int(time.time()) + int(new.get("expires_in") or 3600),
                                "scope": new.get("scope") or tok.get("scope", "")})
                    self.settings.set_secret("kick", tok, replace=True, notify=False)
                elif int(tok.get("expires_at") or 0) < time.time():
                    self.ctx.status("warn", f"Reading #{self.channel} — Kick sign-in expired, "
                                            f"click Sign in to Kick so the bot can reply")
                    return None
            return tok["access_token"]

    async def _app_token(self):
        if self._app_tok and self._app_tok_exp - time.time() > 300:
            return self._app_tok
        app = kick_app(self.settings)
        if not app:
            return None
        tok = await token_request(app, {"grant_type": "client_credentials"})
        if tok and tok.get("access_token"):
            self._app_tok = tok["access_token"]
            self._app_tok_exp = time.time() + int(tok.get("expires_in") or 3600)
            return self._app_tok
        return None

    # ── ids ──

    async def _resolve_ids(self):
        k = self.settings.section("kick")
        if (k.get("channel") or "") == self.channel and k.get("chatroom_id"):
            self.chatroom_id = int(k["chatroom_id"])
            self.broadcaster_id = int(k.get("broadcaster_user_id") or 0)
            self.channel_id = int(k.get("channel_id") or 0)
            if self.broadcaster_id:
                return True
        delay = 10
        while True:
            found = await lookup_channel(self.channel)
            if found == "missing":
                self.ctx.status("error", f"Kick says there's no channel “{self.channel}” — "
                                         f"check the name in Setup")
                await asyncio.sleep(120)
                continue
            if found:
                self.chatroom_id = found["chatroom_id"]
                self.broadcaster_id = found["broadcaster_user_id"]
                self.channel_id = found["channel_id"]
                self.settings.update("kick", dict(found), notify=False)
                return True
            # kick.com didn't answer (Cloudflare, outage). The official API can
            # at least give the broadcaster id when we're signed in.
            token = await self._access_token() if self.settings.secret("kick").get("access_token") else None
            if token and not self.broadcaster_id:
                status, text = await http_util.request(
                    "GET", f"{API_BASE}/channels?slug={urllib.parse.quote(self.channel)}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    impersonate=True)
                row = ((http_util.loads(text) or {}).get("data") or [{}])[0] if status == 200 else {}
                if row.get("broadcaster_user_id"):
                    self.broadcaster_id = int(row["broadcaster_user_id"])
            if self.chatroom_id or (self._relay_mode() and self.broadcaster_id):
                return True
            self.ctx.status("connecting", f"Looking up #{self.channel} on Kick… (retrying)")
            await asyncio.sleep(delay)
            delay = min(300, delay * 2)

    def _relay_mode(self):
        return bool((self.settings.get("kick", "relay_url") or "").strip()
                    and self.settings.secret("kick_relay").get("relay_key"))

    def _reply_note(self):
        if self.settings.secret("kick").get("access_token"):
            return ""
        return " — sign in so the bot can reply"

    # ── main ──

    async def run(self):
        self.channel = normalize_channel(self.settings.get("kick", "channel"))
        if not self.channel:
            self.ctx.status("off", "Add your Kick channel name in Setup")
            return
        self._token_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self.ctx.status("connecting", f"Finding #{self.channel}…")
        await self._resolve_ids()
        if self._relay_mode():
            sub = asyncio.create_task(self._keep_subscribed()) if kick_app(self.settings) else None
            try:
                await self._relay_forever()
            finally:
                if sub:
                    sub.cancel()
        else:
            await self._pusher_forever()

    # ── live chat feed (Pusher) ──

    async def _pusher_forever(self):
        backoff = 1
        refresh_key = True
        while True:
            if refresh_key or not self._pusher:
                found = await fetch_pusher_credentials(self.channel_id)
                if found and found != self._pusher:
                    print(f"[Kick] chat feed key {found[0]} ({found[1]})")
                self._pusher = found or self._pusher or FALLBACK_PUSHER
                refresh_key = False
            started = time.monotonic()
            try:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    await self._pusher_session(session)
            except _BadKey:
                refresh_key = True
                self._pusher = None
                print("[Kick] feed key refused — asking Kick for the current one")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[Kick] chat feed dropped: {type(e).__name__}: {e}")
                self.ctx.status("connecting", f"Reconnecting to #{self.channel} chat…")
            if time.monotonic() - started > 60:
                backoff = 1
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)
            if backoff >= 8:
                refresh_key = True

    async def _pusher_session(self, session):
        key, cluster = self._pusher
        ws = await session.ws_connect(pusher_url(key, cluster), heartbeat=None,
                                      headers={"Origin": "https://kick.com",
                                               "User-Agent": http_util.BROWSER_UA})
        try:
            activity = 120
            first = await ws.receive(timeout=20)
            env = self._pusher_env(first)
            if env.get("event") == "pusher:error":
                self._pusher_error(env)
            if env.get("event") != "pusher:connection_established":
                raise ConnectionError(f"unexpected first message: {str(env)[:120]}")
            try:
                activity = int((http_util.loads(env.get("data")) or {}).get("activity_timeout") or 120)
            except (TypeError, ValueError):
                pass
            await ws.send_str(json.dumps({"event": "pusher:subscribe",
                                          "data": {"auth": "",
                                                   "channel": f"chatrooms.{self.chatroom_id}.v2"}}))
            waiting_pong = False
            while True:
                try:
                    msg = await ws.receive(timeout=(30 if waiting_pong else activity))
                except asyncio.TimeoutError:
                    if waiting_pong:
                        raise ConnectionError("no pong from the chat feed")
                    await ws.send_str(json.dumps({"event": "pusher:ping", "data": {}}))
                    waiting_pong = True
                    continue
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    if ws.close_code == 4001:
                        raise _BadKey()
                    raise ConnectionError(f"feed closed (code {ws.close_code})")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                waiting_pong = False
                env = self._pusher_env(msg)
                ev = env.get("event")
                if ev == "pusher:ping":
                    await ws.send_str(json.dumps({"event": "pusher:pong", "data": {}}))
                elif ev == "pusher:error":
                    self._pusher_error(env)
                elif ev == "pusher_internal:subscription_succeeded":
                    self.ctx.status("ok" if not self._reply_note() else "warn",
                                    f"Reading #{self.channel} chat{self._reply_note()}")
                elif ev == "App\\Events\\ChatMessageEvent":
                    data = env.get("data")
                    if isinstance(data, str):
                        data = http_util.loads(data)
                    if isinstance(data, dict):
                        self._on_chat(data)
        finally:
            await ws.close()

    @staticmethod
    def _pusher_env(msg):
        d = http_util.loads(getattr(msg, "data", "") or "")
        return d if isinstance(d, dict) else {}

    @staticmethod
    def _pusher_error(env):
        data = env.get("data")
        if isinstance(data, str):
            data = http_util.loads(data) or {}
        code = (data or {}).get("code")
        if code == 4001:
            raise _BadKey()
        raise ConnectionError(f"chat feed error {code}: {(data or {}).get('message')}")

    def _on_chat(self, d):
        mid = str(d.get("id") or "")
        if self._remember(mid):
            return
        if d.get("chatroom_id") and self.chatroom_id and int(d["chatroom_id"]) != self.chatroom_id:
            return
        content = (d.get("content") or "").strip()
        if not content.startswith("!"):
            return
        sender = d.get("sender") or {}
        user = (sender.get("username") or sender.get("slug") or "").strip()
        if not user:
            return
        badges = {b.get("type") for b in ((sender.get("identity") or {}).get("badges") or [])
                  if isinstance(b, dict)}
        is_mod = bool(badges & MOD_BADGES) or (
            self.broadcaster_id and int(sender.get("id") or 0) == self.broadcaster_id)
        self._dispatch(user, content, mid or None, is_mod)

    def _dispatch(self, user, content, mid, is_mod):
        print(f"[Kick] {user}: {content}")

        async def reply(m):
            await self.send(f"@{user} {m}", mid)

        asyncio.create_task(self.ctx.router.handle("kick", user, content, is_mod, reply))

    # ── replies ──

    async def send(self, text, reply_to=None):
        text = _clip(text)
        if not text:
            return
        token = await self._access_token()
        if not token or not self.broadcaster_id:
            print(f"[Kick] (not signed in — can't reply) {text}")
            return
        async with self._send_lock:
            body = {"type": "user", "broadcaster_user_id": int(self.broadcaster_id),
                    "content": text}
            if reply_to:
                body["reply_to_message_id"] = reply_to
            status, resp = 0, ""
            for attempt in range(3):
                status, resp = await http_util.request(
                    "POST", f"{API_BASE}/chat", json_body=body, impersonate=True,
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json", "Accept": "application/json"})
                if 200 <= status < 300:
                    break
                if status == 401 and attempt == 0:
                    token = await self._access_token(force_refresh=True)
                    if not token:
                        break
                    continue
                if status in (400, 404, 422) and "reply_to_message_id" in body:
                    body.pop("reply_to_message_id")
                    continue
                if status in (0, 429, 500, 502, 503):
                    await asyncio.sleep(1.5)
                    continue
                break
            if not (200 <= status < 300):
                print(f"[Kick] send failed ({status}): {resp[:200]}")
            await asyncio.sleep(0.35)

    # ── relay mode (advanced) ──

    async def _ensure_subscription(self):
        token = await self._app_token()
        if not token:
            self.ctx.status("error", "Relay mode needs a Kick app (Client ID/Secret) to "
                                     "subscribe to chat webhooks")
            return False
        bid = int(self.broadcaster_id)
        hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        status, text = await http_util.request(
            "GET", f"{API_BASE}/events/subscriptions?broadcaster_user_id={bid}", headers=hdrs,
            impersonate=True)
        for s in ((http_util.loads(text) or {}).get("data") or []) if status == 200 else []:
            if s.get("event") == CHAT_EVENT and str(s.get("broadcaster_user_id")) == str(bid):
                self._subscribed = True
                return True
        status, text = await http_util.request(
            "POST", f"{API_BASE}/events/subscriptions",
            headers=dict(hdrs, **{"Content-Type": "application/json"}), impersonate=True,
            json_body={"broadcaster_user_id": bid, "method": "webhook",
                       "events": [{"name": CHAT_EVENT, "version": 1}]})
        if 200 <= status < 300:
            item = ((http_util.loads(text) or {}).get("data") or [{}])[0]
            problem = str(item.get("error") or "")
            if not problem or "exist" in problem.lower():
                self._subscribed = True
                return True
        print(f"[Kick] webhook subscription refused ({status}): {text[:300]}")
        return False

    async def _keep_subscribed(self):
        retry = 30
        while True:
            try:
                ok = await self._ensure_subscription()
            except Exception as e:
                print(f"[Kick] subscription check failed: {e}")
                ok = False
            await asyncio.sleep(RESUBSCRIBE_EVERY if ok else retry)
            retry = 30 if ok else min(600, retry * 2)

    async def _public_key(self, force=False):
        if self._pubkey is not None and not force:
            return self._pubkey
        if time.time() - self._pubkey_tried < 60:
            return self._pubkey
        self._pubkey_tried = time.time()
        status, text = await http_util.request("GET", PUBLIC_KEY_URL,
                                               headers={"Accept": "application/json"},
                                               impersonate=True)
        if status == 200:
            try:
                self._pubkey = parse_public_key(text)
            except Exception as e:
                print(f"[Kick] couldn't read Kick's public key: {e}")
        return self._pubkey

    async def _authentic(self, env, body):
        if self.settings.get("kick", "relay_verify") is False:
            return True
        if env.get("verified") is True:
            return True
        key = await self._public_key()
        if key is None:
            return False
        args = (env.get("message_id") or "", env.get("timestamp") or "", body,
                env.get("signature") or "")
        if signature_ok(key, *args):
            return True
        fresh = await self._public_key(force=True)
        return bool(fresh and fresh != key and signature_ok(fresh, *args))

    async def _on_relay_event(self, env):
        if env.get("type") != CHAT_EVENT:
            return
        try:
            body = base64.b64decode(env.get("body_b64") or "")
            data = json.loads(body)
        except Exception:
            return
        mid = str(data.get("message_id") or "")
        eid = str(env.get("message_id") or "")
        if (mid and mid in self._seen) or (eid and eid in self._seen):
            return
        if not await self._authentic(env, body):
            self._rejected += 1
            if self._rejected in (1, 5) or self._rejected % 50 == 0:
                print(f"[Kick] ignored a relayed message that failed Kick's signature check "
                      f"({self._rejected} so far)")
            return
        sent, got = _epoch(env.get("timestamp")), env.get("received_at")
        if sent and isinstance(got, (int, float)) and got - sent > MAX_EVENT_AGE:
            self._remember(mid, eid)
            return
        b = (data.get("broadcaster") or {}).get("user_id")
        if self.broadcaster_id and b and str(b) != str(self.broadcaster_id):
            return
        if self._remember(mid, eid):
            return
        sender = data.get("sender") or {}
        user = (sender.get("username") or "").strip()
        content = (data.get("content") or "").strip()
        if not user or not content.startswith("!"):
            return
        badges = {(x.get("text") or x.get("type") or "").lower()
                  for x in ((sender.get("identity") or {}).get("badges") or [])
                  if isinstance(x, dict)}
        is_mod = bool(badges & MOD_BADGES) or str(sender.get("user_id")) == str(self.broadcaster_id)
        self._dispatch(user, content, mid or None, is_mod)

    async def _relay_forever(self):
        relay_url = (self.settings.get("kick", "relay_url") or "").strip().rstrip("/")
        relay_key = self.settings.secret("kick_relay").get("relay_key", "")
        url = f"{relay_url}/events"
        backoff = 1
        last_id = None
        while True:
            hdrs = {"Accept": "text/event-stream", "Cache-Control": "no-cache",
                    "Authorization": f"Bearer {relay_key}"}
            if last_id:
                hdrs["Last-Event-ID"] = last_id
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=RELAY_SILENCE)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as s:
                    async with s.get(url, headers=hdrs) as r:
                        if r.status == 401:
                            self.ctx.status("error", "The Kick relay rejected the relay key")
                            await asyncio.sleep(60)
                            continue
                        if r.status != 200:
                            raise ConnectionError(f"relay answered HTTP {r.status}")
                        event, data, eid = "message", [], None
                        async for raw in r.content:
                            line = raw.decode("utf-8", "replace").rstrip("\r\n")
                            if line == "":
                                if data and event == "hello":
                                    backoff = 1
                                    self.ctx.status("ok" if not self._reply_note() else "warn",
                                                    f"Reading #{self.channel} (relay)"
                                                    f"{self._reply_note()}")
                                elif data and event == "kick":
                                    env = http_util.loads("\n".join(data))
                                    if isinstance(env, dict):
                                        asyncio.create_task(self._on_relay_event(env))
                                if eid:
                                    last_id = eid
                                event, data, eid = "message", [], None
                                continue
                            if line.startswith(":"):
                                continue
                            field, _, value = line.partition(":")
                            if value.startswith(" "):
                                value = value[1:]
                            if field == "data":
                                data.append(value)
                            elif field == "event":
                                event = value
                            elif field == "id":
                                eid = value
                        raise ConnectionError("relay closed the stream")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[Kick] relay connection lost ({type(e).__name__}: {e})")
                self.ctx.status("connecting", "Reconnecting to the Kick relay…")
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)


class _BadKey(Exception):
    """The live-chat feed refused our key (Kick changed it)."""
