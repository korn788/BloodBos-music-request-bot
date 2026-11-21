# selftest.py — "does this install work?" (python main.py --selftest)
#
# Used by the release builds right after packaging, on every OS, so a broken
# installer never gets published. Needs no network, no audio device and no
# screen: it plays a generated tone through VLC into a dummy audio output,
# builds the whole UI offscreen, and asks the built-in web server for the
# OBS widget.

import json
import math
import os
import shutil
import socket
import struct
import sys
import tempfile
import time
import traceback
import urllib.request
import wave


def _tone(path, seconds=2.0, rate=22050):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                          for i in range(int(seconds * rate)))
        w.writeframes(frames)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run():
    # Windowed builds have no console; BLOODBOS_SELFTEST_LOG=path captures the
    # report for the build machine.
    log_path = os.environ.get("BLOODBOS_SELFTEST_LOG")
    if log_path:
        fh = open(log_path, "w", encoding="utf-8")

        class _Tee:
            def __init__(self, s):
                self.s = s

            def write(self, x):
                fh.write(x)
                fh.flush()
                if self.s is not None:
                    try:
                        self.s.write(x)
                    except Exception:
                        pass

            def flush(self):
                fh.flush()

        sys.stdout = _Tee(sys.stdout)
        sys.stderr = _Tee(sys.stderr)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    tmp = tempfile.mkdtemp(prefix="bloodbos-selftest-")
    import app_paths
    app_paths.set_data_dir(tmp)
    steps = []

    def ok(name):
        steps.append(name)
        print(f"  ok  {name}")

    try:
        print("BLOODBOS RADIO self-test")
        import vlc_loader
        vlc = vlc_loader.load()
        if vlc is None:
            raise RuntimeError("VLC failed to load: " + vlc_loader.error())
        ok(f"VLC {vlc.libvlc_get_version().decode(errors='replace')}")

        wav = os.path.join(tmp, "tone.wav")
        _tone(wav)
        inst = vlc.Instance("--quiet", "--no-video", "--aout=dummy")
        player = inst.media_player_new()
        player.set_media(inst.media_new(wav))
        player.play()
        deadline = time.time() + 10
        while time.time() < deadline and player.get_state() != vlc.State.Playing:
            time.sleep(0.05)
        state = player.get_state()
        player.stop()
        player.release()
        inst.release()
        if state != vlc.State.Playing:
            raise RuntimeError(f"VLC could not play a test tone (state {state})")
        ok("VLC plays audio")

        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)

        from settings import Settings
        settings = Settings()
        settings.set("player", "port", _free_port(), notify=False)
        settings.set("player", "check_updates", False, notify=False)

        from tools import Tools, ytdlp_asset, deno_asset
        tools = Tools(settings)
        ok(f"helper downloads: {ytdlp_asset()}, {deno_asset()}")

        import audio_engine
        audio_engine.configure(settings=settings, tools=tools)
        import player_qt
        from queue_manager import QueueManager
        from status_server import StatusServer
        player_qt.configure(settings)
        engine = audio_engine.AudioEngine()
        queue = QueueManager()
        signals = player_qt.SignalBridge()
        window = player_qt.PlayerWindow(engine, queue, signals, yt_api=None, settings=settings)
        ok("main window builds")

        from chat.manager import ChatManager
        chat = ChatManager(window, settings, call_on_gui=lambda fn: fn())
        window.chat = chat
        import setup_dialog
        dlg = setup_dialog.SetupDialog(window, settings, chat)
        ok("setup window builds")
        import chat.twitch, chat.kick, chat.owncast  # noqa: F401
        ok("chat modules load")

        port = settings.get("player", "port")
        server = StatusServer(player_status_fn=window.get_player_status,
                              queue_fn=window.get_queue_status,
                              get_video_url_fn=engine.get_video_stream_url,
                              port=port)
        server.start()
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with local.open(f"http://127.0.0.1:{port}/status", timeout=5) as r:
            json.loads(r.read())
        with local.open(f"http://127.0.0.1:{port}/", timeout=5) as r:
            page = r.read().decode("utf-8", "replace")
        if "mvVideo" not in page:
            raise RuntimeError("the OBS widget page wasn't served")
        server.stop()
        ok("OBS widget served")

        dlg.close()
        window.close()
        engine.cleanup()
        print("SELFTEST OK")
        return 0
    except BaseException:
        traceback.print_exc()
        print("SELFTEST FAILED")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
