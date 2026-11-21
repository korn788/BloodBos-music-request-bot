#!/usr/bin/env bash
# install_kick_relay.sh — Kick webhook relay for BLOODBOS RADIO (run on the VPS)
#
#   sudo bash install_kick_relay.sh your.domain
#
# OPTIONAL — most people never need this. BLOODBOS RADIO reads Kick chat
# straight from Kick's live chat feed. This relay is for anyone who would
# rather receive chat through Kick's official webhooks, which need a public
# HTTPS address — so it runs on a server you own, behind nginx:
#
#   https://<domain>/kick-relay/webhook   ← Kick posts chat here
#   https://<domain>/kick-relay/events    → the player listens here (needs the key)
#   https://<domain>/kick-relay/health    → status page, safe to open in a browser
#
# Safe to run again: it keeps the existing key, replaces the relay code,
# and only touches nginx if the route isn't there yet (with a backup and an
# `nginx -t` check that rolls back on any error).
set -euo pipefail

DOMAIN="${1:-}"
APP_DIR=/opt/kick-relay
PORT=9131
SVC=kick-relay
SNIPPET=/etc/nginx/snippets/kick-relay.conf

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*"; exit 1; }

[ -n "$DOMAIN" ] || die "usage:  sudo bash $0 your.domain   (the domain nginx serves with HTTPS)"
[ "$(id -u)" -eq 0 ] || die "run it with sudo:  sudo bash $0 $DOMAIN"
PY=$(command -v python3) || die "python3 is required"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    || die "python3 3.8 or newer is required"
command -v nginx >/dev/null || die "nginx not found — the relay needs nginx's HTTPS in front of it"

if command -v ss >/dev/null && ss -ltn "( sport = :$PORT )" 2>/dev/null | grep -q LISTEN \
        && ! systemctl is-active --quiet "$SVC"; then
    die "port $PORT is already used by something else (ss -ltnp | grep $PORT)"
fi

# ── relay code ────────────────────────────────────────────────────────────
say "installing relay to $APP_DIR"
mkdir -p "$APP_DIR"
cat > "$APP_DIR/kick_relay.py" <<'KICK_RELAY_PY_EOF'
#!/usr/bin/env python3
# kick_relay.py — Kick webhook relay for BLOODBOS RADIO
#
# Kick stopped running chat through its hosted Pusher app in October 2026, so
# bots can no longer just open a websocket and listen. The supported way to get
# chat now is Kick's official webhooks: Kick POSTs every chat message to a
# public HTTPS URL. A laptop on someone else's Wi-Fi can't receive those, so
# this relay runs on the VPS:
#
#     Kick ──POST /webhook──▶ this relay ──SSE /events──▶ player (kick_bot.py)
#
# It answers Kick instantly (so Kick never disables the webhook), checks each
# event's signature against Kick's public key when it can, keeps the last few
# minutes of events so a bot that briefly drops can catch up, and streams them
# to any connected bot over Server-Sent Events.
#
# Python 3.8+ standard library only. Config comes from the environment
# (the installer writes /opt/kick-relay/relay.env):
#   RELAY_KEY   shared secret bots must present on /events (required)
#   RELAY_PORT  default 9131          RELAY_BIND  default 127.0.0.1
#
# Endpoints (nginx maps https://<your domain>/kick-relay/... onto these):
#   POST /webhook   Kick's webhook target
#   GET  /events    event stream for bots  (?key=  or  Authorization: Bearer)
#   GET  /health    status JSON, no secrets — open it in a browser

import base64
import hashlib
import hmac
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

VERSION = "1.0"
PORT = int(os.environ.get("RELAY_PORT", "9131"))
BIND = os.environ.get("RELAY_BIND", "127.0.0.1")
KEY = os.environ.get("RELAY_KEY", "").strip()
PUBLIC_KEY_URL = os.environ.get("KICK_PUBLIC_KEY_URL",
                                "https://api.kick.com/public/v1/public-key")

BUFFER_EVENTS = 500      # events kept for catch-up
REPLAY_SECONDS = 120     # a reconnecting bot gets at most this much backlog
MAX_BODY = 256 * 1024
MAX_CLIENTS = 16
PING_EVERY = 15          # keep-alive comment so proxies and NAT keep the stream open
BOOT = secrets.token_hex(4)
STARTED = time.time()

KICK_HEADERS = ("Kick-Event-Type", "Kick-Event-Version", "Kick-Event-Message-Id",
                "Kick-Event-Message-Timestamp", "Kick-Event-Subscription-Id",
                "Kick-Event-Signature")


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════
#  Signature check (RSA PKCS#1 v1.5 / SHA-256, pure Python)
#
#  Kick signs  "<Message-Id>.<Message-Timestamp>.<raw body>"  with its private
#  key; the public key is served at api.kick.com/public/v1/public-key and can
#  rotate, so it's fetched (never hardcoded) and re-fetched when a check fails.
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
    """RSA (n, e) from a PEM, or from Kick's JSON {"data": {"public_key": PEM}}."""
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
    if t1 == 0x02:                                  # PKCS#1 RSAPublicKey
        _, c2, _ = _der(seq, j)
        return int.from_bytes(c1, "big"), int.from_bytes(c2, "big")
    _, oid, _ = _der(c1, 0)                         # SubjectPublicKeyInfo
    if oid != _RSA_OID:
        raise ValueError("not an RSA key")
    tb, bits, _ = _der(seq, j)
    if tb != 0x03 or not bits or bits[0] != 0:
        raise ValueError("bad key bits")
    _, rsa, _ = _der(bits[1:], 0)
    _, cn, k = _der(rsa, 0)
    _, ce, _ = _der(rsa, k)
    return int.from_bytes(cn, "big"), int.from_bytes(ce, "big")


def rsa_sha256_ok(key, message, sig):
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
    # The documented form first; the undotted form is what some community
    # libraries assume — accepting either costs nothing (both need Kick's key).
    for msg in (f"{msg_id}.{ts}.".encode() + body, f"{msg_id}{ts}".encode() + body):
        if rsa_sha256_ok(key, msg, sig):
            return True
    return False


class Verifier:
    def __init__(self):
        self.key = None
        self.lock = threading.Lock()
        self.last_fetch = 0.0
        self.state = "not loaded"

    def fetch(self, force=False):
        with self.lock:
            if not force and self.key:
                return self.key
            if time.time() - self.last_fetch < 60:
                return self.key
            self.last_fetch = time.time()
        try:
            req = urllib.request.Request(PUBLIC_KEY_URL, headers={
                "Accept": "application/json",
                "User-Agent": f"bloodbos-kick-relay/{VERSION}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                key = parse_public_key(r.read().decode("utf-8", "replace"))
            with self.lock:
                self.key = key
                self.state = "loaded"
            log(f"Kick public key loaded ({key[0].bit_length()}-bit RSA)")
        except Exception as e:
            with self.lock:
                self.state = f"unavailable ({type(e).__name__})"
            log(f"couldn't load Kick public key: {type(e).__name__}: {e} "
                f"— events go out marked unverified; the player checks them itself")
        return self.key

    def check(self, hdr, body):
        """True = signed by Kick, False = bad signature, None = can't tell."""
        key = self.fetch()
        if key is None:
            return None
        args = (hdr["Kick-Event-Message-Id"], hdr["Kick-Event-Message-Timestamp"],
                body, hdr["Kick-Event-Signature"])
        if signature_ok(key, *args):
            return True
        fresh = self.fetch(force=True)         # Kick may have rotated its key
        if fresh is not None and fresh != key and signature_ok(fresh, *args):
            return True
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  Event hub
# ═══════════════════════════════════════════════════════════════════════════

class Hub:
    def __init__(self):
        self.cond = threading.Condition()
        self.events = deque(maxlen=BUFFER_EVENTS)    # (seq, received_at, json)
        self.seq = 0
        self.clients = 0
        self.received = 0
        self.verified = 0
        self.bad_sig = 0
        self.last_type = ""
        self.last_at = None
        self.window = deque(maxlen=200)              # POST times, for rate limit

    def publish(self, envelope, verified):
        with self.cond:
            self.seq += 1
            self.received += 1
            if verified:
                self.verified += 1
            elif verified is False:
                self.bad_sig += 1
            self.last_type = envelope.get("type", "")
            self.last_at = time.time()
            envelope["relay_seq"] = f"{BOOT}-{self.seq}"
            self.events.append((self.seq, self.last_at,
                                json.dumps(envelope, separators=(",", ":"))))
            self.cond.notify_all()

    def resume_point(self, after_seq, oldest_ok):
        """Skip past anything buffered before oldest_ok (too stale to replay)."""
        with self.cond:
            s = after_seq
            for q, t, _ in self.events:
                if q > s and t < oldest_ok:
                    s = q
            return s

    def after(self, seq, timeout):
        """Events with sequence > seq, waiting up to timeout for one to arrive."""
        with self.cond:
            if not self.events or self.events[-1][0] <= seq:
                self.cond.wait(timeout)
            return [e for e in self.events if e[0] > seq]

    def flooded(self):
        now = time.time()
        with self.cond:
            self.window.append(now)
            return len(self.window) == self.window.maxlen and now - self.window[0] < 2.0


hub = Hub()
verifier = Verifier()


# ═══════════════════════════════════════════════════════════════════════════
#  HTTP
# ═══════════════════════════════════════════════════════════════════════════

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"kick-relay/{VERSION}"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _route(self):
        path, _, query = self.path.partition("?")
        path = "/" + path.strip("/")
        params = {}
        for part in query.split("&"):
            k, _, v = part.partition("=")
            if k:
                params[k] = urllib.parse.unquote(v)
        return path, params

    # ── Kick → relay ──
    def do_POST(self):
        path, _ = self._route()
        if path != "/webhook":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._send(413 if n > MAX_BODY else 400, {"error": "bad body size"})
            return
        body = self.rfile.read(n)
        hdr = {h: (self.headers.get(h) or "").strip() for h in KICK_HEADERS}
        if not all(hdr[h] for h in ("Kick-Event-Type", "Kick-Event-Message-Id",
                                    "Kick-Event-Message-Timestamp", "Kick-Event-Signature")):
            self._send(400, {"error": "not a Kick webhook"})
            return
        if hub.flooded():
            self._send(429, {"error": "slow down"})
            return
        # Answer Kick first: a slow or failing webhook gets unsubscribed.
        self._send(200, {"ok": True})
        verified = verifier.check(hdr, body)
        hub.publish({
            "type": hdr["Kick-Event-Type"],
            "version": hdr["Kick-Event-Version"],
            "message_id": hdr["Kick-Event-Message-Id"],
            "timestamp": hdr["Kick-Event-Message-Timestamp"],
            "subscription_id": hdr["Kick-Event-Subscription-Id"],
            "signature": hdr["Kick-Event-Signature"],
            "verified": verified,
            "received_at": time.time(),
            "body_b64": base64.b64encode(body).decode(),
        }, verified)
        tag = {True: "verified", False: "BAD SIGNATURE", None: "unverified"}[verified]
        log(f"webhook {hdr['Kick-Event-Type']} {hdr['Kick-Event-Message-Id']} ({tag}) "
            f"→ {hub.clients} bot(s)")

    # ── bots ← relay ──
    def do_GET(self):
        path, params = self._route()
        if path == "/health":
            self._send(200, {
                "ok": True, "version": VERSION, "boot": BOOT,
                "uptime_s": int(time.time() - STARTED),
                "webhooks_received": hub.received,
                "verified": hub.verified, "bad_signature": hub.bad_sig,
                "last_event_type": hub.last_type,
                "last_event_age_s": (int(time.time() - hub.last_at)
                                     if hub.last_at else None),
                "bots_connected": hub.clients,
                "kick_public_key": verifier.state,
                "webhook_path": "/webhook",
            })
            return
        if path != "/events":
            self._send(404, {"error": "not found"})
            return

        given = params.get("key", "")
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        if not KEY or not hmac.compare_digest(given.encode(), KEY.encode()):
            self._send(401, {"error": "bad or missing relay key"})
            return
        if hub.clients >= MAX_CLIENTS:
            self._send(503, {"error": "too many bots connected"})
            return

        # Where to resume. No Last-Event-ID = a fresh start: never replay old
        # commands into a player that just launched.
        last = (self.headers.get("Last-Event-ID") or params.get("since") or "").strip()
        with hub.cond:
            seq = hub.seq
        if last:
            boot, _, n = last.partition("-")
            if boot == BOOT and n.isdigit():
                seq = int(n)
            else:
                seq = 0          # relay restarted since: everything buffered is new
            seq = hub.resume_point(seq, time.time() - REPLAY_SECONDS)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        with hub.cond:
            hub.clients += 1
        peer = self.headers.get("X-Forwarded-For") or self.client_address[0]
        log(f"bot connected from {peer} ({hub.clients} connected)")
        try:
            hello = {"version": VERSION, "boot": BOOT, "kick_public_key": verifier.state,
                     "webhooks_received": hub.received}
            self.wfile.write(f"event: hello\ndata: {json.dumps(hello)}\n\n".encode())
            self.wfile.flush()
            while True:
                batch = hub.after(seq, PING_EVERY)
                if not batch:
                    self.wfile.write(b": ping\n\n")
                for s, _, data in batch:
                    self.wfile.write(f"id: {BOOT}-{s}\nevent: kick\ndata: {data}\n\n".encode())
                    seq = s
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with hub.cond:
                hub.clients -= 1
            log(f"bot disconnected ({hub.clients} connected)")


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if not KEY:
        log("RELAY_KEY is not set — refusing to start (bots need it to connect)")
        sys.exit(2)
    threading.Thread(target=verifier.fetch, daemon=True).start()
    srv = Server((BIND, PORT), Handler)
    log(f"kick-relay {VERSION} listening on {BIND}:{PORT} (boot {BOOT})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
KICK_RELAY_PY_EOF
"$PY" -m py_compile "$APP_DIR/kick_relay.py" || die "relay code didn't compile"
rm -rf "$APP_DIR/__pycache__"

id kickrelay >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin kickrelay

KEY=""
if [ -f "$APP_DIR/relay.env" ]; then
    KEY=$(sed -n 's/^RELAY_KEY=//p' "$APP_DIR/relay.env" | head -n1)
fi
[ -n "$KEY" ] || KEY=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')
umask 027
cat > "$APP_DIR/relay.env" <<EOF
RELAY_KEY=$KEY
RELAY_PORT=$PORT
RELAY_BIND=127.0.0.1
EOF
chown -R root:kickrelay "$APP_DIR"
chmod 750 "$APP_DIR"; chmod 640 "$APP_DIR/relay.env"; chmod 644 "$APP_DIR/kick_relay.py"

# ── service ───────────────────────────────────────────────────────────────
say "installing systemd service $SVC"
cat > "/etc/systemd/system/$SVC.service" <<EOF
[Unit]
Description=Kick webhook relay (BLOODBOS RADIO)
After=network-online.target
Wants=network-online.target

[Service]
User=kickrelay
Group=kickrelay
EnvironmentFile=$APP_DIR/relay.env
ExecStart=$PY -u $APP_DIR/kick_relay.py
Restart=always
RestartSec=2
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1
systemctl restart "$SVC"

ok=""
for _ in $(seq 1 20); do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 0.5
done
[ -n "$ok" ] || { journalctl -u "$SVC" -n 30 --no-pager; die "relay didn't start (log above)"; }
say "relay running on 127.0.0.1:$PORT"

# ── nginx route ───────────────────────────────────────────────────────────
mkdir -p "$(dirname "$SNIPPET")"
cat > "$SNIPPET" <<EOF
# Kick webhook relay — written by install_kick_relay.sh
location ^~ /kick-relay/ {
    proxy_pass http://127.0.0.1:$PORT/;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_cache off;
    gzip off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    client_max_body_size 256k;
}
EOF

STAMP=$(date +%Y%m%d-%H%M%S)
CHANGED=$("$PY" - "$DOMAIN" "$SNIPPET" "$STAMP" <<'NGINX_PY_EOF'
import glob, os, re, shutil, sys

domain, snippet, stamp = sys.argv[1], sys.argv[2], sys.argv[3]
include = f"include {snippet};"


def mask(text):
    """Same length as text, with comments blanked and braces/semicolons inside
    quotes neutralised, so brace matching can't be fooled."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if c in "'\"":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 1
                j += 1
            for k in range(i + 1, min(j, n)):
                if out[k] in "{};":
                    out[k] = " "
            i = j + 1
            continue
        i += 1
    return "".join(out)


def server_blocks(m):
    stack, found = [], []
    for i, c in enumerate(m):
        if c == "{":
            s = i - 1
            while s >= 0 and m[s] not in ";{}":
                s -= 1
            words = m[s + 1:i].split()
            stack.append((words[0] if words else "", i))
        elif c == "}" and stack:
            name, o = stack.pop()
            if name == "server":
                found.append((o, i))
    return found


files = []
for pat in ("/etc/nginx/sites-enabled/*", "/etc/nginx/conf.d/*.conf", "/etc/nginx/nginx.conf"):
    for p in sorted(glob.glob(pat)):
        real = os.path.realpath(p)
        if os.path.isfile(real) and real not in files:
            files.append(real)

dom = re.compile(r"\bserver_name\b[^;]*(?<![\w.-])" + re.escape(domain) + r"(?![\w.-])")
tls = re.compile(r"\blisten\b[^;]*\b443\b")
changed, present = [], False
for path in files:
    text = open(path, encoding="utf-8", errors="surrogateescape").read()
    m = mask(text)
    spots = [o for o, e in server_blocks(m) if dom.search(m[o:e]) and tls.search(m[o:e])]
    if not spots:
        continue
    if "kick-relay.conf" in m:
        present = True
        continue
    shutil.copy2(path, f"{path}.bak-kickrelay-{stamp}")
    for o in sorted(spots, reverse=True):
        text = text[:o + 1] + f"\n    {include}   # Kick webhook relay\n" + text[o + 1:]
    open(path, "w", encoding="utf-8", errors="surrogateescape").write(text)
    changed.append(path)

if changed:
    print("\n".join(changed))
elif present:
    print("PRESENT")
else:
    print("NOTFOUND")
NGINX_PY_EOF
)

case "$CHANGED" in
    NOTFOUND)
        warn "couldn't find an HTTPS (listen 443) server block for $DOMAIN in /etc/nginx."
        warn "Add this line inside that server { } block yourself, then: nginx -t && systemctl reload nginx"
        warn "    include $SNIPPET;"
        exit 1 ;;
    PRESENT)
        say "nginx route already in place" ;;
    *)
        if nginx -t >/dev/null 2>&1; then
            say "added /kick-relay/ route to: $(echo "$CHANGED" | tr '\n' ' ')"
        else
            nginx -t || true
            while read -r f; do
                [ -n "$f" ] && cp -f "$f.bak-kickrelay-$STAMP" "$f"
            done <<< "$CHANGED"
            die "nginx rejected the change — restored the original config, nothing else touched"
        fi ;;
esac
nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx config test failed"; }
systemctl reload nginx
sleep 1

# ── end-to-end check through the public URL ──────────────────────────────
PUB="https://$DOMAIN/kick-relay"
before=$(curl -fsS "http://127.0.0.1:$PORT/health" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["webhooks_received"])')
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$PUB/webhook" \
    -H 'Content-Type: application/json' \
    -H 'Kick-Event-Type: relay.selftest' -H 'Kick-Event-Version: 1' \
    -H "Kick-Event-Message-Id: selftest-$STAMP" \
    -H "Kick-Event-Message-Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    -H 'Kick-Event-Signature: c2VsZnRlc3Q=' \
    --data '{"selftest":true}' || true)
after=$(curl -fsS "http://127.0.0.1:$PORT/health" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["webhooks_received"])')
if [ "$code" = "200" ] && [ "$after" -gt "$before" ]; then
    say "public webhook URL works ($PUB/webhook)"
else
    warn "a test POST to $PUB/webhook didn't arrive (HTTP $code)."
    warn "Check: curl -v $PUB/health   and   journalctl -u $SVC -n 50"
fi
health=$(curl -fsS "http://127.0.0.1:$PORT/health" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["kick_public_key"])')
[ "$health" = "loaded" ] || warn "relay couldn't fetch Kick's public key ($health) — fine, the player checks signatures itself"

cat <<EOF

────────────────────────────────────────────────────────────────────
 Kick relay is installed.

 1) Kick:  kick.com > Settings > Developer > your app > Edit
           Enable Webhooks:  ON
           Webhook URL:      $PUB/webhook
           Save.

 2) Player: on the streaming computer, close BLOODBOS RADIO and start it
           once with these options (then start it normally after):

    bloodbos-radio --kick-relay "$PUB" "$KEY"

    (Windows: "%LOCALAPPDATA%\Programs\BLOODBOS Radio\BLOODBOS Radio.exe" --kick-relay ...
     macOS:   "/Applications/BLOODBOS Radio.app/Contents/MacOS/BLOODBOS Radio" --kick-relay ...)

    The app also needs a Kick app (Setup > Kick > "Use my own Kick app")
    so it can ask Kick to send your chat to the webhook.

 Status page:  $PUB/health
 Live log:     journalctl -u $SVC -f
────────────────────────────────────────────────────────────────────
EOF
