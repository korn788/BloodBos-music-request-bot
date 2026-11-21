# test_chat_protocols.py — Twitch EventSub and Kick live-chat handling against
# local fake servers (no internet, no real accounts).
#
#   python -m pytest tests/ -q        (or: python tests/test_chat_protocols.py)

import asyncio
import json
import os
import sys
import tempfile

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ.setdefault("BLOODBOS_DATA_DIR", tempfile.mkdtemp(prefix="bbr-test-"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

import app_paths  # noqa: E402
from settings import Settings  # noqa: E402


class FakeRouter:
    def __init__(self):
        self.seen = []

    async def handle(self, platform, user, text, is_mod, reply):
        self.seen.append((platform, user, text, is_mod))
        await reply(f"ok:{text}")
        return True


class Ctx:
    def __init__(self, settings, router):
        self.settings = settings
        self.router = router
        self.statuses = []
        self.notices = []

    def status(self, level, text):
        self.statuses.append((level, text))

    def notice(self, msg):
        self.notices.append(msg)


def _settings():
    d = tempfile.mkdtemp(prefix="bbr-set-")
    return Settings(os.path.join(d, "settings.json"), os.path.join(d, "tokens.json"))


async def _serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


# ═══════════════════════════════ Twitch ═══════════════════════════════

async def _twitch_case():
    from chat import twitch
    state = {"subs": 0, "sent": [], "ws2": False}

    def note(ev_id, text, user="viewer", badges=()):
        return {"metadata": {"message_id": ev_id, "message_type": "notification",
                             "subscription_type": "channel.chat.message"},
                "payload": {"event": {"broadcaster_user_id": "100", "message_id": f"m-{ev_id}",
                                      "chatter_user_id": "7", "chatter_user_login": user,
                                      "chatter_user_name": user, "message": {"text": text},
                                      "badges": [{"set_id": b} for b in badges]}}}

    async def validate(req):
        if req.headers.get("Authorization") != "OAuth goodtoken":
            return web.json_response({"status": 401}, status=401)
        return web.json_response({"client_id": "cid", "login": "streamer", "user_id": "100",
                                  "scopes": twitch.SCOPES, "expires_in": 5000000})

    async def users(req):
        return web.json_response({"data": [{"id": "100", "login": req.query["login"]}]})

    async def subscribe(req):
        body = await req.json()
        assert body["transport"]["session_id"] == "sess1", body
        assert body["condition"] == {"broadcaster_user_id": "100", "user_id": "100"}
        state["subs"] += 1
        state["sub_event"].set()
        return web.json_response({"data": [{}]}, status=202)

    async def send(req):
        body = await req.json()
        state["sent"].append(body)
        return web.json_response({"data": [{"message_id": f"sent{len(state['sent'])}",
                                            "is_sent": True}]})

    async def ws1(req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        await ws.send_json({"metadata": {"message_type": "session_welcome"},
                            "payload": {"session": {"id": "sess1", "keepalive_timeout_seconds": 10}}})
        await asyncio.wait_for(state["sub_event"].wait(), 5)
        await ws.send_json(note("e1", "!sr hello"))
        await ws.send_json(note("e1", "!sr hello"))                    # duplicate delivery
        await ws.send_json(note("e2", "just chatting"))                # not a command
        await ws.send_json(note("e3", "!skip", "mod1", ["moderator"]))
        await ws.send_json({"metadata": {"message_type": "session_keepalive"}, "payload": {}})
        await ws.send_json({"metadata": {"message_type": "session_reconnect"},
                            "payload": {"session": {"id": "sess1", "reconnect_url":
                                                    f"ws://127.0.0.1:{state['port']}/ws2"}}})
        await asyncio.sleep(3)
        await ws.close()
        return ws

    async def ws2(req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        state["ws2"] = True
        await ws.send_json({"metadata": {"message_type": "session_welcome"},
                            "payload": {"session": {"id": "sess1", "keepalive_timeout_seconds": 10}}})
        await asyncio.sleep(0.3)
        await ws.send_json(note("e4", "!song"))
        await asyncio.sleep(5)
        return ws

    app = web.Application()
    app.router.add_get("/oauth2/validate", validate)
    app.router.add_get("/helix/users", users)
    app.router.add_post("/helix/eventsub/subscriptions", subscribe)
    app.router.add_post("/helix/chat/messages", send)
    app.router.add_get("/ws", ws1)
    app.router.add_get("/ws2", ws2)
    state["sub_event"] = asyncio.Event()
    runner, port = await _serve(app)
    state["port"] = port
    twitch.EVENTSUB_URL = f"ws://127.0.0.1:{port}/ws"
    twitch.HELIX = f"http://127.0.0.1:{port}/helix"
    twitch.VALIDATE_URL = f"http://127.0.0.1:{port}/oauth2/validate"

    s = _settings()
    s.set_secret("twitch", {"access_token": "goodtoken", "client_id": "cid"})
    s.set("twitch", "channel", "streamer")
    router = FakeRouter()
    ctx = Ctx(s, router)
    bot = twitch.TwitchBot(ctx)
    task = asyncio.create_task(bot.run())
    for _ in range(100):
        if len(router.seen) >= 3 and len(state["sent"]) >= 3:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # an expired login is reported, not retried forever
    s2 = _settings()
    s2.set_secret("twitch", {"access_token": "expired"})
    ctx2 = Ctx(s2, FakeRouter())
    await asyncio.wait_for(twitch.TwitchBot(ctx2).run(), 10)
    await runner.cleanup()
    return router, state, ctx, ctx2


def test_twitch_eventsub():
    router, state, ctx, ctx2 = asyncio.run(_twitch_case())
    texts = [r[2] for r in router.seen]
    assert texts == ["!sr hello", "!skip", "!song"], router.seen
    assert router.seen[1][3] is True and router.seen[0][3] is False   # mod badge
    assert state["subs"] == 1, "reconnect must not resubscribe"
    assert state["ws2"]
    assert [m["message"] for m in state["sent"]] == ["ok:!sr hello", "ok:!skip", "ok:!song"]
    assert state["sent"][0]["reply_parent_message_id"] == "m-e1"
    assert ("ok", "Reading #streamer as streamer") in ctx.statuses
    assert ctx2.statuses[-1][0] == "error" and "expired" in ctx2.statuses[-1][1]


# ═══════════════════════════════ Kick ═══════════════════════════════

async def _kick_case():
    from chat import kick
    state = {"realtime_calls": 0, "sent": [], "keys": [], "pong": False}

    def chat_msg(mid, content, user="kviewer", badges=()):
        data = {"id": mid, "chatroom_id": 555, "content": content,
                "sender": {"id": 9, "username": user,
                           "identity": {"badges": [{"type": b} for b in badges]}}}
        return {"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.555.v2",
                "data": json.dumps(data)}

    async def realtime(req):
        body = await req.json()
        assert body["capabilities"]["accepted_providers"] == [{"provider": "pusher"}]
        state["realtime_calls"] += 1
        key = "oldkey" if state["realtime_calls"] == 1 else "testkey"
        return web.json_response({"data": {"connections": [
            {"provider": "pusher", "credentials": {"app_key": key, "cluster": "us2"}}]},
            "message": "success"})

    async def pusher(req):
        key = req.match_info["key"]
        state["keys"].append(key)
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        if key != "testkey":
            await ws.send_json({"event": "pusher:error",
                                "data": {"code": 4001, "message": "App key not in this cluster"}})
            await ws.close(code=4001)
            return ws
        await ws.send_json({"event": "pusher:connection_established",
                            "data": json.dumps({"socket_id": "1.2", "activity_timeout": 120})})
        sub = await ws.receive_json(timeout=5)
        assert sub == {"event": "pusher:subscribe",
                       "data": {"auth": "", "channel": "chatrooms.555.v2"}}, sub
        await ws.send_json({"event": "pusher_internal:subscription_succeeded",
                            "channel": "chatrooms.555.v2", "data": "{}"})
        await ws.send_json(chat_msg("k1", "!sr kick song"))
        await ws.send_json(chat_msg("k1", "!sr kick song"))            # duplicate
        await ws.send_json(chat_msg("k2", "hello"))                    # not a command
        await ws.send_json(chat_msg("k3", "!skip", "kmod", ["moderator"]))
        await ws.send_json({"event": "pusher:ping", "data": {}})
        try:
            pong = await ws.receive_json(timeout=5)
            state["pong"] = pong.get("event") == "pusher:pong"
        except Exception:
            pass
        await asyncio.sleep(5)
        return ws

    async def send(req):
        assert req.headers.get("Authorization") == "Bearer kicktoken"
        state["sent"].append(await req.json())
        return web.json_response({"data": {"is_sent": True}})

    app = web.Application()
    app.router.add_post("/realtime", realtime)
    app.router.add_get("/app/{key}", pusher)
    app.router.add_post("/public/v1/chat", send)
    runner, port = await _serve(app)
    kick.REALTIME_URL = f"http://127.0.0.1:{port}/realtime"
    kick.API_BASE = f"http://127.0.0.1:{port}/public/v1"
    kick.pusher_url = lambda key, cluster: f"ws://127.0.0.1:{port}/app/{key}"

    s = _settings()
    s.update("kick", {"channel": "somechannel", "chatroom_id": 555,
                      "broadcaster_user_id": 777, "channel_id": 0, "feed": "pusher"})
    s.set_secret("kick", {"access_token": "kicktoken", "refresh_token": "r",
                          "expires_at": 9999999999})
    router = FakeRouter()
    ctx = Ctx(s, router)
    task = asyncio.create_task(kick.KickBot(ctx).run())
    for _ in range(150):
        if len(router.seen) >= 2 and len(state["sent"]) >= 2 and state["pong"]:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await runner.cleanup()
    return router, state, ctx


def test_kick_pusher():
    router, state, ctx = asyncio.run(_kick_case())
    assert state["keys"][:2] == ["oldkey", "testkey"], state["keys"]   # 4001 → new key
    assert [r[2] for r in router.seen] == ["!sr kick song", "!skip"], router.seen
    assert router.seen[1][3] is True and router.seen[0][3] is False
    assert state["pong"]
    assert state["sent"][0] == {"type": "user", "broadcaster_user_id": 777,
                                "content": "@kviewer ok:!sr kick song",
                                "reply_to_message_id": "k1"}
    assert any(lvl == "ok" and "Reading #somechannel" in txt for lvl, txt in ctx.statuses)


def test_kick_helpers():
    from chat import kick
    assert kick.normalize_channel("https://kick.com/Some_Name/") == "some-name"
    assert kick.normalize_channel("  @korn788 ") == "korn788"
    creds = kick.pusher_credentials_from(
        {"data": {"connections": [{"credentials": {"app_key": "34bf7a0ff419a2a775b9",
                                                   "cluster": "us2"}, "provider": "pusher"}],
                  "mode": "websocket"}, "message": "success"})
    assert creds == ("34bf7a0ff419a2a775b9", "us2")
    assert kick.pusher_credentials_from({"data": {"connections": [
        {"provider": "centrifugo", "credentials": {}}]}}) is None


# Kick's Centrifugo chat server, with frames exactly as Kick sends them
# (two publications to a frame, "{}" pings, token refresh).

def _kick_chat(mid, content, user="kviewer", badges=()):
    return {"id": mid, "chatroom_id": 555, "content": content, "type": "message",
            "sender": {"id": 9, "username": user, "slug": user,
                       "identity": {"color": "#fff", "badges": [{"type": b} for b in badges]}}}


def _cf_push(data):
    return json.dumps({"push": {"channel": "chatrooms.555.v2", "pub": {"data": {
        "event": "App\\Events\\ChatMessageEvent", "data": json.dumps(data)}}}})


async def _kick_two_servers_case(centrifugo_works=True, relay=False):
    from chat import kick
    state = {"offered": [], "tokens": 0, "token_clients": [], "cf_conns": 0, "cf_connect": [],
             "cf_sub": None, "cf_pong": False, "cf_refresh": [], "pusher_keys": [],
             "pusher_pong": False, "sent": [], "relay_hits": 0, "oauth_hits": 0}

    async def realtime(req):
        body = await req.json()
        offered = [x["provider"] for x in body["capabilities"]["accepted_providers"]]
        state["offered"].append(offered)
        if "centrifugo" in offered:          # what Kick answers when Centrifugo is accepted
            return web.json_response({"data": {"connections": [{"provider": "centrifugo",
                "credentials": {"url": f"ws://127.0.0.1:{state['port']}/connection/websocket"}}]}})
        return web.json_response({"data": {"connections": [{"provider": "pusher",
            "credentials": {"app_key": "testkey", "cluster": "us2"}}]}})

    async def token(req):
        state["token_clients"].append((await req.json()).get("client_id"))
        state["tokens"] += 1
        return web.json_response({"data": {"token": f"tok-{state['tokens']}"}})

    async def centrifugo(req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        state["cf_conns"] += 1
        connect = json.loads((await ws.receive(timeout=5)).data)
        state["cf_connect"].append(connect)
        if not centrifugo_works:
            await ws.send_str(json.dumps({"id": connect["id"],
                                          "error": {"code": 109, "message": "token expired"}}))
            await ws.close()
            return ws
        await ws.send_str(json.dumps({"id": connect["id"], "connect": {
            "client": "c", "version": "6.9.7 PRO", "expires": True, "ttl": 2, "ping": 25, "pong": True}}))
        sub = json.loads((await ws.receive(timeout=5)).data)
        state["cf_sub"] = sub
        await ws.send_str(json.dumps({"id": sub["id"], "subscribe": {}}))
        await ws.send_str(_cf_push(_kick_chat("c1", "!sr kick song")) + "\n"
                          + _cf_push(_kick_chat("c1", "!sr kick song")))           # dup, same frame
        await ws.send_str(_cf_push(_kick_chat("c2", "hello")))                      # not a command
        await ws.send_str(_cf_push(_kick_chat("c3", "!skip", "kmod", ["moderator"])))
        await ws.send_str("{}")                                                      # server ping
        end = asyncio.get_running_loop().time() + 10
        while not (state["cf_pong"] and state["cf_refresh"]):
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                break
            try:
                m = await ws.receive(timeout=left)
            except asyncio.TimeoutError:
                break
            if m.type != aiohttp.WSMsgType.TEXT:
                break
            if m.data == "{}":
                state["cf_pong"] = True
                continue
            cmd = json.loads(m.data)
            if "refresh" in cmd:
                state["cf_refresh"].append(cmd["refresh"]["token"])
                await ws.send_str(json.dumps({"id": cmd["id"], "refresh": {
                    "client": "c", "expires": True, "ttl": 2}}))
        await asyncio.sleep(2)
        return ws

    async def pusher(req):
        state["pusher_keys"].append(req.match_info["key"])
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        await ws.send_json({"event": "pusher:connection_established",
                            "data": json.dumps({"socket_id": "1.2", "activity_timeout": 120})})
        await ws.receive_json(timeout=5)
        await ws.send_json({"event": "pusher_internal:subscription_succeeded",
                            "channel": "chatrooms.555.v2", "data": "{}"})
        for mid, text, user, badges in (("p1", "!sr kick song", "kviewer", ()),
                                        ("p3", "!skip", "kmod", ("moderator",))):
            await ws.send_json({"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.555.v2",
                                "data": json.dumps(_kick_chat(mid, text, user, badges))})
        await asyncio.sleep(5)
        return ws

    async def send(req):
        state["sent"].append(await req.json())
        return web.json_response({"data": {"is_sent": True}})

    async def relay_events(req):
        state["relay_hits"] += 1
        return web.Response(status=500)

    async def oauth(req):
        state["oauth_hits"] += 1
        return web.json_response({"access_token": "apptoken", "expires_in": 3600})

    app = web.Application()
    app.router.add_post("/realtime", realtime)
    app.router.add_post("/token", token)
    app.router.add_get("/connection/websocket", centrifugo)
    app.router.add_get("/app/{key}", pusher)
    app.router.add_post("/public/v1/chat", send)
    app.router.add_get("/relay/events", relay_events)
    app.router.add_post("/oauth/token", oauth)
    runner, port = await _serve(app)
    state["port"] = port
    kick.REALTIME_URL = f"http://127.0.0.1:{port}/realtime"
    kick.REALTIME_TOKEN_URL = f"http://127.0.0.1:{port}/token"
    kick.API_BASE = f"http://127.0.0.1:{port}/public/v1"
    kick.OAUTH_BASE = f"http://127.0.0.1:{port}"
    kick.REFRESH_MIN = 0.2
    kick.pusher_url = lambda key, cluster: f"ws://127.0.0.1:{port}/app/{key}"

    s = _settings()
    s.update("kick", {"channel": "somechannel", "chatroom_id": 555,
                      "broadcaster_user_id": 777, "channel_id": 0})
    s.set_secret("kick", {"access_token": "kicktoken", "refresh_token": "r",
                          "expires_at": 9999999999})
    if relay:      # relay settings but no own Kick app: must not use the relay
        s.set("kick", "relay_url", f"http://127.0.0.1:{port}/relay")
        s.set_secret("kick_relay", {"relay_key": "k"})
    router = FakeRouter()
    ctx = Ctx(s, router)
    task = asyncio.create_task(kick.KickBot(ctx).run())
    for _ in range(200):
        done = len(router.seen) >= 2 and len(state["sent"]) >= 2
        if centrifugo_works:
            done = done and state["cf_pong"] and state["cf_refresh"]
        if done:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await runner.cleanup()
    return router, state, ctx


def test_kick_centrifugo():
    router, state, ctx = asyncio.run(_kick_two_servers_case(relay=True))
    assert state["offered"][0] == ["centrifugo", "pusher"], state["offered"]   # like kick.com
    assert state["cf_connect"][0]["connect"] == {"token": "tok-1", "name": "js"}
    assert state["cf_sub"]["subscribe"] == {"channel": "chatrooms.555.v2"}
    assert [r[2] for r in router.seen] == ["!sr kick song", "!skip"], router.seen
    assert router.seen[1][3] is True and router.seen[0][3] is False             # mod badge
    assert state["cf_pong"], "server ping not answered"
    assert state["cf_refresh"] == ["tok-2"], state["cf_refresh"]                 # renewed in time
    assert state["token_clients"][0] == state["token_clients"][1]                # same guest id
    assert state["pusher_keys"] == []
    assert state["sent"][0]["content"] == "@kviewer ok:!sr kick song"
    assert any(lvl == "ok" and "Reading #somechannel" in txt for lvl, txt in ctx.statuses)
    assert state["relay_hits"] == 0 and state["oauth_hits"] == 0                 # never the relay


def test_kick_falls_back_to_pusher():
    router, state, ctx = asyncio.run(_kick_two_servers_case(centrifugo_works=False))
    assert state["cf_conns"] == 2, state["cf_conns"]                 # two tries, then switch
    assert state["offered"][:3] == [["centrifugo", "pusher"], ["centrifugo"], ["pusher"]], state["offered"]
    assert state["pusher_keys"][:1] == ["testkey"], state["pusher_keys"]
    assert [r[2] for r in router.seen] == ["!sr kick song", "!skip"], router.seen
    assert any(lvl == "ok" and "Reading #somechannel" in txt for lvl, txt in ctx.statuses)


def test_kick_realtime_parsing():
    from chat import kick
    both = {"data": {"connections": [
        {"provider": "centrifugo", "credentials": {"url": "wss://realtime.us-east-1.platform.kick.com/connection/websocket"}},
        {"provider": "pusher", "credentials": {"app_key": "34bf7a0ff419a2a775b9", "cluster": "us2"}}]}}
    assert kick.realtime_from(both) == ("centrifugo", "wss://realtime.us-east-1.platform.kick.com/connection/websocket")
    assert kick.realtime_from(both, ("pusher",)) == ("pusher", ("34bf7a0ff419a2a775b9", "us2"))
    assert kick.realtime_from({"data": {"connections": [
        {"provider": "centrifugo", "credentials": {"url": "http://evil.example/x"}}]}}) is None
    assert kick.realtime_from({"data": {"connections": [
        {"provider": "pusher", "credentials": {"app_key": "bad key!", "cluster": "us2"}}]}}) is None


def test_owncast_helpers():
    from chat import owncast
    assert owncast.normalize_url("stream.example.com/admin/") == "https://stream.example.com"
    assert owncast.normalize_url("http://10.0.0.5:8080") == "http://10.0.0.5:8080"
    assert owncast.strip_html("<p>!sr a &amp; b</p>") == "!sr a & b"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
