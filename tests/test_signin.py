# test_signin.py — the sign-in landing page on this computer, the
# "paste the address you ended up on" fallback, and opening the browser with
# a clean environment. No internet, no real accounts, no browser needed.

import os
import socket
import sys
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

from chat import oauth_local  # noqa: E402
import desktop  # noqa: E402

LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def test_parse_return_address():
    p = oauth_local.parse_return_address
    tw = p("http://localhost:17563/callback#access_token=abc&scope=user%3Aread%3Achat&state=s1&token_type=bearer")
    assert tw["access_token"] == "abc" and tw["state"] == "s1" and tw["scope"] == "user:read:chat"
    assert p("  'http://localhost:3000/?code=XYZ&state=s2'  ") == {"code": "XYZ", "state": "s2"}
    assert p("access_token=abc&state=s1")["access_token"] == "abc"          # just the part after #
    assert p("http://localhost:3000/?error=access_denied&state=s2")["error"] == "access_denied"
    assert p("https://www.twitch.tv/") == {} and p("") == {} and p("hello") == {}


def test_browser_opens_only_once_listening_and_paste_finishes():
    port = _free_port()
    seen = {}
    man = oauth_local.Manual()

    def ready(listening):
        seen["listening_flag"] = listening
        seen["really_listening"] = _listening(port)
        # the page "didn't load": the user pastes the address instead
        threading.Timer(0.3, lambda: man.offer(
            f"http://localhost:{port}/callback#access_token=tok&scope=x&state=st&token_type=bearer")).start()

    tok, err = oauth_local.wait_for_implicit_token(
        f"http://localhost:{port}/callback", "st", "Twitch", timeout=10, manual=man, on_ready=ready)
    assert seen == {"listening_flag": True, "really_listening": True}, seen
    assert err is None and tok["access_token"] == "tok"


def test_pasted_address_from_another_attempt_is_refused():
    port = _free_port()
    man = oauth_local.Manual()
    ready = lambda _l: man.offer(f"http://localhost:{port}/callback#access_token=tok&state=OLD")  # noqa: E731
    tok, err = oauth_local.wait_for_implicit_token(
        f"http://localhost:{port}/callback", "new", "Twitch", timeout=10, manual=man, on_ready=ready)
    assert tok is None and "isn't from this Twitch sign-in" in err


def test_browser_landing_still_works():
    port = _free_port()
    out = {}

    def ready(_listening):
        def browser():
            page = LOCAL.open(f"http://localhost:{port}/?code=XYZ&state=st9", timeout=5).read().decode()
            out["page"] = page
        threading.Thread(target=browser).start()

    code, err = oauth_local.wait_for_code(f"http://localhost:{port}", "st9", "Kick",
                                          timeout=10, on_ready=ready)
    assert (code, err) == ("XYZ", None)


def test_busy_port_falls_back_to_pasting():
    port = _free_port()
    blockers = []
    for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            b = socket.socket(fam)
            b.bind((host, port))
            b.listen(1)
            blockers.append(b)
        except OSError:
            pass
    try:
        code, err = oauth_local.wait_for_code(f"http://localhost:{port}", "s", "Kick", timeout=5)
        assert code is None and "being used by another program" in err     # no paste box offered
        man = oauth_local.Manual()
        flags = []

        def ready(listening):
            flags.append(listening)
            man.offer(f"http://localhost:{port}/?code=C1&state=s")

        code, err = oauth_local.wait_for_code(f"http://localhost:{port}", "s", "Kick",
                                              timeout=5, manual=man, on_ready=ready)
        assert flags == [False] and (code, err) == ("C1", None)
    finally:
        for b in blockers:
            b.close()


def test_clean_env_for_packaged_builds(monkeypatch):
    monkeypatch.setattr(desktop, "_BASE", None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/app/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    monkeypatch.setenv("QT_PLUGIN_PATH", "/opt/app/_internal/PyQt6/Qt6/plugins")
    desktop.remember_environment()
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb;wayland")             # set by the app later
    env = desktop.clean_env()
    assert env["LD_LIBRARY_PATH"] == "/usr/local/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in env and "QT_PLUGIN_PATH" not in env
    assert "QT_QPA_PLATFORM" not in env or os.environ.get("QT_QPA_PLATFORM") != env.get("QT_QPA_PLATFORM")
    monkeypatch.setattr(desktop, "_BASE", None)
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    desktop.remember_environment()
    assert "LD_LIBRARY_PATH" not in desktop.clean_env()               # the bundle's own value
