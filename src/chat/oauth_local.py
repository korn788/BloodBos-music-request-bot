# oauth_local.py — the little web page your browser lands on after you click
# "Authorize" on Twitch or Kick.
#
# Sign-in pages send the browser back to an address on this computer
# (http://localhost:<port>/...). For a minute or two while you sign in, the app
# listens there, picks up the login, and shows a "you can close this tab" page.
# It only listens on this computer's own loopback address.

import html
import http.server
import json
import socket
import threading
import time
import urllib.parse

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0e0e12;color:#f3f3f6;font-family:'Segoe UI',system-ui,-apple-system,sans-serif}}
.card{{background:#16161d;border:1px solid #2a2a35;border-radius:18px;padding:40px 48px;
text-align:center;max-width:440px}}.big{{font-size:46px;line-height:1}}h1{{font-size:22px;margin:14px 0 8px}}
p{{color:#9b9bab;font-size:15px;line-height:1.5;margin:0}}</style></head>
<body><div class="card"><div class="big" id="icon">{icon}</div><h1 id="head">{head}</h1>
<p id="body">{body}</p></div>{script}</body></html>"""

DONE_TEXT = "You can close this tab and go back to BLOODBOS RADIO."


def page(ok, platform, detail=""):
    if ok is None:
        return PAGE.format(title=platform, icon="&#8987;", head=f"Connecting {platform}…",
                           body="One moment…", script="")
    head = f"{platform} connected!" if ok else f"{platform} sign-in didn't finish"
    body = DONE_TEXT if ok else (html.escape(detail) or "Go back to the app and try again.")
    return PAGE.format(title=platform, icon="&#10004;" if ok else "&#10006;",
                       head=head, body=body, script="").replace(
        "&#10004;", '<span style="color:#46d39a">&#10004;</span>').replace(
        "&#10006;", '<span style="color:#ff2e4d">&#10006;</span>')


# Twitch's implicit flow puts the login in the part of the address after "#",
# which browsers never send to a server — so the landing page reads it with a
# few lines of JavaScript and hands it to the app, then wipes it from the
# address bar.
IMPLICIT_SCRIPT = """<script>
(function(){
  var h=new URLSearchParams(location.hash.slice(1)), q=new URLSearchParams(location.search);
  var d={access_token:h.get('access_token')||'', state:h.get('state')||q.get('state')||'',
         scope:h.get('scope')||'', error:q.get('error')||h.get('error')||'',
         error_description:q.get('error_description')||h.get('error_description')||''};
  try{history.replaceState(null,'',location.pathname);}catch(e){}
  function show(ok,msg){
    document.getElementById('icon').innerHTML=ok?'<span style="color:#46d39a">&#10004;</span>'
                                               :'<span style="color:#ff2e4d">&#10006;</span>';
    document.getElementById('head').textContent=ok?'%(platform)s connected!':'%(platform)s sign-in didn\\'t finish';
    document.getElementById('body').textContent=ok?'%(done)s':(msg||'Go back to the app and try again.');
  }
  fetch('/__oauth_result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})
    .then(function(r){return r.json();}).then(function(r){show(r.ok,r.message);})
    .catch(function(){show(false,'Could not reach the app — is it still open?');});
})();
</script>"""


class _Server:
    """Listens on 127.0.0.1 and ::1 at one port (browsers pick either for
    "localhost")."""

    def __init__(self, port, handler_cls):
        self.servers = []
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            try:
                cls = type("S", (http.server.ThreadingHTTPServer,),
                           {"address_family": family, "daemon_threads": True,
                            "allow_reuse_address": True})
                srv = cls((host, port), handler_cls)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                self.servers.append(srv)
            except OSError:
                pass

    def close(self):
        for srv in self.servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass


def _wait(event, timeout, cancel):
    end = time.time() + timeout
    while time.time() < end:
        if event.wait(0.25):
            return True
        if cancel is not None and cancel.is_set():
            return False
    return False


def wait_for_code(redirect_uri, expected_state, platform, timeout=300, cancel=None):
    """Authorization-code flow (Kick). Returns (code, error)."""
    u = urllib.parse.urlparse(redirect_uri)
    port = u.port or 80
    box = {}
    got = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (q.get("code") or [""])[0]
            state = (q.get("state") or [""])[0]
            err = (q.get("error_description") or q.get("error") or [""])[0]
            if not code and not err:          # favicon.ico and friends
                self.send_response(404)
                self.end_headers()
                return
            ok = bool(code) and state == expected_state
            if ok:
                box["code"] = code
            else:
                box["error"] = err or "the sign-in answer didn't match (try again)"
            body = page(ok, platform, box.get("error", "")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            got.set()

    srv = _Server(port, H)
    if not srv.servers:
        return None, (f"Port {port} on this computer is being used by another program, "
                      f"so the sign-in can't come back to the app. Close that program "
                      f"and try again.")
    try:
        if not _wait(got, timeout, cancel):
            return None, "cancelled" if (cancel and cancel.is_set()) else "timed out"
    finally:
        time.sleep(0.3)           # let the browser receive the page
        srv.close()
    return box.get("code"), box.get("error")


def wait_for_implicit_token(redirect_uri, expected_state, platform, timeout=300, cancel=None):
    """Implicit flow (Twitch). Returns (token_dict, error)."""
    u = urllib.parse.urlparse(redirect_uri)
    port = u.port or 80
    path = u.path or "/"
    box = {}
    got = threading.Event()
    script = IMPLICIT_SCRIPT % {"platform": platform, "done": DONE_TEXT}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = urllib.parse.urlparse(self.path).path
            if p.rstrip("/") != path.rstrip("/"):
                return self._send(404, b"", "text/plain")
            body = PAGE.format(title=platform, icon="&#8987;", head=f"Connecting {platform}…",
                               body="One moment…", script=script).encode()
            self._send(200, body, "text/html; charset=utf-8")

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/__oauth_result":
                return self._send(404, b"", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            try:
                d = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            except ValueError:
                d = {}
            ok = bool(d.get("access_token")) and d.get("state") == expected_state
            if ok:
                box["token"] = d
                msg = ""
            else:
                msg = d.get("error_description") or d.get("error") or \
                    "The sign-in answer didn't match. Try again from the app."
                box["error"] = msg
            self._send(200, json.dumps({"ok": ok, "message": msg}).encode(), "application/json")
            got.set()

    srv = _Server(port, H)
    if not srv.servers:
        return None, (f"Port {port} on this computer is being used by another program, "
                      f"so the sign-in can't come back to the app. Close that program "
                      f"and try again.")
    try:
        if not _wait(got, timeout, cancel):
            return None, "cancelled" if (cancel and cancel.is_set()) else "timed out"
    finally:
        time.sleep(0.3)
        srv.close()
    return box.get("token"), box.get("error")
