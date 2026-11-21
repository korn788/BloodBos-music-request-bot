# main.py — start BLOODBOS RADIO
#
#   python main.py                 normal start
#   python main.py --data-dir DIR  keep settings/logins/queue in DIR
#   python main.py --selftest      check the install works, then exit
#   python main.py --version

import argparse
import os
import socket
import sys
import time

import app_paths
import desktop
from version import __version__


# ── logging: everything printed also goes to a log file ─────────────────────

class _Tee:
    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, s):
        try:
            self._fh.write(s)
            self._fh.flush()
        except Exception:
            pass
        if self._stream is not None:
            try:
                self._stream.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        for f in (self._fh, self._stream):
            try:
                if f is not None:
                    f.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def _setup_logging():
    d = app_paths.logs_dir()
    base = os.path.join(d, "bloodbos-radio.log")
    for i in (2, 1):                      # keep the last three runs
        src = base if i == 1 else f"{base}.{i - 1}"
        try:
            if os.path.exists(src):
                os.replace(src, f"{base}.{i}")
        except OSError:
            pass
    try:
        fh = open(base, "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    print(f"BLOODBOS RADIO {__version__} — {time.strftime('%Y-%m-%d %H:%M:%S')} — "
          f"{sys.platform} — Python {sys.version.split()[0]}")


def _setup_environment():
    # Keep the desktop's own environment for opening pages and folders later.
    desktop.remember_environment()
    # Packaged apps can't count on the system's certificate store being found
    # (macOS especially) — use the bundled certifi list for HTTPS.
    if app_paths.FROZEN and not os.environ.get("SSL_CERT_FILE"):
        try:
            import certifi
            os.environ["SSL_CERT_FILE"] = certifi.where()
        except Exception:
            pass
    if sys.platform.startswith("linux"):
        # The music-video window needs an X11 window id for VLC; on Wayland
        # desktops that's XWayland. Fall back to native Wayland if X isn't there.
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb;wayland")
    # The helpers the app downloads live in its own bin folder.
    os.environ["PATH"] = app_paths.bin_dir() + os.pathsep + os.environ.get("PATH", "")


def _port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _already_running(port):
    import json
    import urllib.request
    local = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a proxy
    try:
        with local.open(f"http://127.0.0.1:{port}/status", timeout=2) as r:
            json.loads(r.read() or b"{}")
            return True
    except Exception:
        return False


def _icon():
    from PyQt6.QtGui import QIcon
    for name in ("icon.png", "icon.ico"):
        p = app_paths.resource("assets", name)
        if os.path.isfile(p):
            return QIcon(p)
    return QIcon()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bloodbos-radio")
    ap.add_argument("--data-dir", help="folder for settings, logins and the queue")
    ap.add_argument("--selftest", action="store_true", help="check the install, then exit")
    ap.add_argument("--kick-relay", nargs=2, metavar=("URL", "KEY"),
                    help="advanced: read Kick chat through your own webhook relay "
                         "(use --kick-relay off off to go back)")
    ap.add_argument("--version", action="store_true")
    args, _qt_args = ap.parse_known_args(argv if argv is not None else sys.argv[1:])

    if args.version:
        print(__version__)
        return 0
    if args.selftest:
        import selftest
        return selftest.run()

    if args.data_dir:
        app_paths.set_data_dir(args.data_dir)

    if args.kick_relay:
        from settings import Settings
        url, key = args.kick_relay
        st = Settings()
        if url.lower() == "off":
            st.set("kick", "relay_url", "", notify=False)
            st.set_secret("kick_relay", None, notify=False)
            print("Kick relay turned off — chat comes from Kick's live feed again.")
        else:
            st.set("kick", "relay_url", url.strip().rstrip("/"), notify=False)
            st.set_secret("kick_relay", {"relay_key": key.strip()}, notify=False)
            print(f"Kick relay set to {url}. Start the app normally now.")
        return 0
    first_launch = not os.path.exists(os.path.join(app_paths.data_dir(), "settings.json"))
    _setup_logging()
    _setup_environment()

    from PyQt6.QtWidgets import QApplication, QMessageBox
    app = QApplication(sys.argv)
    app.setApplicationName(app_paths.APP_NAME)
    app.setApplicationDisplayName(app_paths.APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(_icon())
    app.setDesktopFileName(app_paths.APP_ID)      # Linux docks match the .desktop entry

    from settings import Settings, import_legacy
    settings = Settings()
    if first_launch and not app_paths.FROZEN:
        # Running from the folder of an older version? Bring its choices over.
        notes = import_legacy(settings, app_paths.program_dir())
        if notes:
            print(f"[Setup] imported from the old version: {', '.join(notes)}")

    import vlc_loader
    if vlc_loader.load() is None:
        QMessageBox.critical(None, f"{app_paths.APP_NAME} — VLC needed",
                             "The music player is built on VLC, and it couldn't be loaded.\n\n"
                             + vlc_loader.install_hint()
                             + f"\n\n(Details: {vlc_loader.error()})")
        return 1

    port = int(settings.get("player", "port") or 9111)
    if not _port_free(port):
        if _already_running(port):
            QMessageBox.information(None, app_paths.APP_NAME,
                                    f"{app_paths.APP_NAME} is already running.")
            return 0
        QMessageBox.critical(None, app_paths.APP_NAME,
                             f"Another program is using port {port}, which this app needs "
                             f"for OBS.\n\nClose that program, or change \"port\" in "
                             f"settings.json in:\n{app_paths.data_dir()}")
        return 1

    from tools import Tools
    tools = Tools(settings)
    tools.start()

    import audio_engine
    audio_engine.configure(settings=settings, tools=tools)

    import player_qt
    return player_qt.run(app, settings, tools)


if __name__ == "__main__":
    sys.exit(main())
