# owncast.py — Owncast chat: connecting, reading chat, replying
#
# Owncast has no "sign in with" button, so Connect works like this: you give
# the app your Owncast address and admin password once, and it asks your
# server for an access token for a "Music Bot" chat user (the same thing as
# Admin > Integrations > Access Tokens). Only that token is saved — never the
# password. You can also paste a token you made yourself.
#
# Reading chat: polls /api/chat every two seconds (works for any server,
# wherever it's hosted). Replying: /api/integrations/chat/send.

import asyncio
import html
import re
import urllib.parse
from collections import deque

from chat import http_util

BOT_NAME = "Music Bot"
TOKEN_SCOPES = ["CAN_SEND_MESSAGES", "CAN_SEND_SYSTEM_MESSAGES"]
POLL_SECONDS = 2.0
MSG_LIMIT = 480

_TAG = re.compile(r"<[^>]+>")


def normalize_url(text):
    """'stream.example.com/admin' → 'https://stream.example.com'"""
    t = (text or "").strip()
    if not t:
        return ""
    if not re.match(r"^https?://", t, flags=re.I):
        t = "https://" + t
    u = urllib.parse.urlparse(t)
    return f"{u.scheme.lower()}://{u.netloc}"


def strip_html(s):
    return html.unescape(_TAG.sub("", s or "")).replace("\xa0", " ").strip()


async def check_token(server, token):
    """(ok, message)."""
    status, text = await http_util.request(
        "GET", f"{server}/api/chat?accessToken={urllib.parse.quote(token)}", timeout=15)
    if status == 200:
        return True, "ok"
    if status in (401, 403):
        return False, "Owncast didn't accept that access token."
    if status == 0:
        return False, f"Couldn't reach {server} — check the address."
    return False, f"{server} answered HTTP {status} — is that your Owncast server?"


async def connect(server_url, admin_password="", token=""):
    """Returns (ok, message, server, token).

    Without "http://" or "https://" in what was typed, HTTPS is tried first
    and plain HTTP second (self-hosted servers on a home network often have
    no certificate)."""
    typed = (server_url or "").strip()
    if not typed:
        return False, "Type your Owncast address first.", "", ""
    if re.match(r"^https?://", typed, flags=re.I):
        candidates = [normalize_url(typed)]
    else:
        candidates = [normalize_url("https://" + typed), normalize_url("http://" + typed)]
    result = None
    for server in candidates:
        result = await _connect_one(server, admin_password, token)
        if result[0] or "Couldn't reach" not in result[1]:
            return result
    return result


async def _connect_one(server, admin_password, token):
    token = (token or "").strip()
    if not token:
        if not admin_password:
            return False, "Type your Owncast admin password (or paste an access token).", server, ""
        status, text = await http_util.request(
            "POST", f"{server}/api/admin/accesstokens/create",
            json_body={"name": BOT_NAME, "scopes": TOKEN_SCOPES},
            basic_auth=("admin", admin_password), timeout=20)
        if status == 401:
            return False, "Wrong admin password.", server, ""
        if status == 0:
            return False, f"Couldn't reach {server} — check the address.", server, ""
        if status == 404:
            return False, (f"{server} doesn't look like an Owncast server (or it's very old). "
                           f"Make an access token in Owncast's admin and paste it instead."), server, ""
        d = http_util.loads(text) or {}
        token = (d.get("accessToken") or d.get("token") or "").strip()
        if status != 200 or not token:
            return False, f"Owncast couldn't make a token (HTTP {status}).", server, ""
    ok, msg = await check_token(server, token)
    if not ok:
        return False, msg, server, ""
    host = urllib.parse.urlparse(server).netloc
    return True, f"Connected to {host}", server, token


class OwncastBot:
    platform = "owncast"

    def __init__(self, ctx):
        self.ctx = ctx
        self.settings = ctx.settings
        self.server = ""
        self.token = ""
        self._seen = set()
        self._seen_order = deque()
        self._send_lock = None
        self._use_integrations = False

    def _remember(self, mid):
        if mid in self._seen:
            return True
        self._seen.add(mid)
        self._seen_order.append(mid)
        while len(self._seen_order) > 3000:
            self._seen.discard(self._seen_order.popleft())
        return False

    async def _fetch(self):
        if self._use_integrations:
            return await http_util.request(
                "GET", f"{self.server}/api/integrations/chat",
                headers={"Authorization": f"Bearer {self.token}"}, timeout=15)
        return await http_util.request(
            "GET", f"{self.server}/api/chat?accessToken={urllib.parse.quote(self.token)}",
            timeout=15)

    async def run(self):
        self.server = normalize_url(self.settings.get("owncast", "server_url"))
        self.token = self.settings.secret("owncast").get("access_token", "")
        if not self.server or not self.token:
            self.ctx.status("off", "Not connected — add your Owncast server in Setup")
            return
        self._send_lock = asyncio.Lock()
        host = urllib.parse.urlparse(self.server).netloc
        self.ctx.status("connecting", f"Connecting to {host}…")
        first = True
        failures = 0
        while True:
            status, text = await self._fetch()
            if status in (401, 403) and not self._use_integrations:
                self._use_integrations = True          # admin-scoped tokens read here
                status, text = await self._fetch()
                if status in (401, 403):
                    self._use_integrations = False
            if status in (401, 403):
                self.ctx.status("error", "Owncast rejected the access token — reconnect in Setup")
                await asyncio.sleep(60)
                continue
            msgs = http_util.loads(text) if status == 200 else None
            if not isinstance(msgs, list):
                failures += 1
                if failures in (3, 30) or failures % 300 == 0:
                    self.ctx.status("connecting", f"Can't reach {host} — retrying…")
                await asyncio.sleep(min(30, POLL_SECONDS * failures))
                continue
            if failures or first:
                self.ctx.status("ok", f"Reading chat on {host}")
            failures = 0
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("id") or "")
                if not mid or self._remember(mid):
                    continue
                if first:
                    continue                       # history from before we started
                self._on_message(m)
            first = False
            await asyncio.sleep(POLL_SECONDS)

    def _on_message(self, m):
        mtype = m.get("type")
        if mtype and mtype != "CHAT":
            return
        user = m.get("user") or {}
        if user.get("isBot"):
            return                                 # our own replies, other bots
        text = strip_html(m.get("rawBody") or m.get("body") or "")
        if not text.startswith("!"):
            return
        name = (user.get("displayName") or "someone").strip()
        is_mod = "MODERATOR" in (user.get("scopes") or [])
        print(f"[Owncast] {name}: {text}")

        async def reply(msg):
            await self.send(f"@{name} {msg}")

        asyncio.create_task(self.ctx.router.handle("owncast", name, text, is_mod, reply))

    async def send(self, text):
        text = (text or "").strip()
        if len(text) > MSG_LIMIT:
            text = text[:MSG_LIMIT - 1] + "…"
        if not text:
            return
        async with self._send_lock:
            for ep in ("/api/integrations/chat/send", "/api/integrations/chat/system"):
                status, resp = await http_util.request(
                    "POST", f"{self.server}{ep}", json_body={"body": text},
                    headers={"Authorization": f"Bearer {self.token}"}, timeout=15)
                if 200 <= status < 300:
                    break
                if status in (401, 403):
                    continue                        # token may only have the other scope
                print(f"[Owncast] send failed ({status}): {resp[:200]}")
                break
