# setup_dialog.py — the Setup window: connect chat, pick a few options, get
# the OBS link. Opens by itself the first time the app starts.

import sys
import threading

from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

import app_keys
import app_paths
import desktop
from chat import kick as kick_mod
from chat import oauth_local
from chat import owncast as owncast_mod
from chat import twitch as twitch_mod

LEVEL_COLOUR = {"ok": "ok", "warn": "warn", "connecting": "warn", "error": "accent", "off": "faint"}
COOKIE_CHOICES = [("Don't use a login (recommended)", "none"), ("Firefox", "firefox"),
                  ("Chrome", "chrome"), ("Edge", "edge"), ("Brave", "brave"),
                  ("Opera", "opera"), ("Vivaldi", "vivaldi"), ("Chromium", "chromium")]
if sys.platform == "darwin":
    COOKIE_CHOICES.append(("Safari", "safari"))


def _label(text, name=None, wrap=True):
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lb


def _button(text, name="ghostbtn", slot=None):
    b = QPushButton(text)
    b.setObjectName(name)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if slot:
        b.clicked.connect(slot)
    return b


class SetupDialog(QDialog):
    _job_done = pyqtSignal(str, object)       # (job name, result) from worker threads
    _login_link = pyqtSignal(str, str)        # (platform, sign-in link) from worker threads

    def __init__(self, window, settings, chat):
        super().__init__(window)
        self.window = window
        self.settings = settings
        self.chat = chat
        self.theme = getattr(sys.modules.get("player_qt"), "THEME", {})
        self._cancel = {}
        self._manual = {}                     # platform → oauth_local.Manual while signing in
        self._links = {}                      # platform → sign-in link
        self._help = {}                       # platform → (box, address field, note)
        self._k_app_open = False
        self.setWindowTitle("Setup")
        self.setMinimumWidth(560)
        self.resize(600, 720)
        self.setStyleSheet(self._qss())
        self._job_done.connect(self._on_job_done)
        self._login_link.connect(self._on_login_link)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self._chat_tab()), "Chat")
        self.tabs.addTab(self._scroll(self._player_tab()), "Player && OBS")
        lay.addWidget(self.tabs, 1)

        foot = QHBoxLayout()
        foot.addWidget(_label("Settings save as you go.", "hint", wrap=False))
        foot.addStretch(1)
        foot.addWidget(_button("Done", "primarybtn", self.close))
        lay.addLayout(foot)

        for p in ("twitch", "kick", "owncast"):
            lvl, txt = chat.status(p) if chat else ("off", "")
            self.on_chat_status(p, lvl, txt)
        self._refresh()

    def show_tab(self, name):
        self.tabs.setCurrentIndex(1 if name == "player" else 0)

    @staticmethod
    def _scroll(page):
        page.setObjectName("page")
        area = QScrollArea()
        area.setObjectName("page")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(page)
        return area

    # ═════════════════════════════════ Chat tab ═════════════════════════════════

    def _card(self, title):
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel(title)
        t.setObjectName("cardtitle")
        head.addWidget(t)
        head.addStretch(1)
        status = QLabel("")
        status.setObjectName("status")
        status.setWordWrap(True)
        status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(status, 1)
        v.addLayout(head)
        return card, v, status

    def _chat_tab(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(4, 10, 4, 4)
        v.setSpacing(12)
        v.addWidget(_label("Viewers request songs by typing <b>!sr</b> and a song name or "
                           "link. Connect the places you stream — one, two or all three.",
                           "intro"))

        # ── Twitch ──
        card, c, self.st_twitch = self._card("Twitch")
        row = QHBoxLayout()
        self.tw_account = _label("", "body")
        row.addWidget(self.tw_account, 1)
        self.tw_btn = _button("Connect Twitch", "primarybtn", self._twitch_connect)
        self.tw_out = _button("Sign out", "ghostsmall", self._twitch_signout)
        row.addWidget(self.tw_out)
        row.addWidget(self.tw_btn)
        c.addLayout(row)
        c.addWidget(self._signin_help("twitch"))
        grid = QGridLayout()
        grid.addWidget(_label("Channel", "field", wrap=False), 0, 0)
        self.tw_channel = QLineEdit(self.settings.get("twitch", "channel") or "")
        self.tw_channel.setObjectName("entry")
        self.tw_channel.setPlaceholderText("your Twitch channel (blank = the account you connect)")
        self.tw_channel.editingFinished.connect(self._twitch_channel_saved)
        grid.addWidget(self.tw_channel, 0, 1)
        c.addLayout(grid)
        c.addWidget(_label("Replies come from the account you connect. Want a separate bot "
                           "account? Connect with that account and type your own channel "
                           "above.", "hint"))
        v.addWidget(card)

        # ── Kick ──
        card, c, self.st_kick = self._card("Kick")
        grid = QGridLayout()
        grid.addWidget(_label("Your channel", "field", wrap=False), 0, 0)
        self.k_channel = QLineEdit(self.settings.get("kick", "channel") or "")
        self.k_channel.setObjectName("entry")
        self.k_channel.setPlaceholderText("e.g. yourname  (from kick.com/yourname)")
        self.k_channel.editingFinished.connect(self._kick_channel_saved)
        grid.addWidget(self.k_channel, 0, 1)
        c.addLayout(grid)
        c.addWidget(_label("Song requests from Kick chat work as soon as your channel is "
                           "filled in. Sign in so the bot can also answer in chat.", "hint"))
        row = QHBoxLayout()
        self.k_account = _label("", "body")
        row.addWidget(self.k_account, 1)
        self.k_out = _button("Sign out", "ghostsmall", self._kick_signout)
        self.k_btn = _button("Sign in to Kick", "primarybtn", self._kick_connect)
        row.addWidget(self.k_out)
        row.addWidget(self.k_btn)
        c.addLayout(row)
        c.addWidget(self._signin_help("kick"))

        # Your own Kick app (needed when this build doesn't include one)
        self.k_app_box = QFrame()
        self.k_app_box.setObjectName("subcard")
        b = QVBoxLayout(self.k_app_box)
        b.setContentsMargins(14, 12, 14, 12)
        b.setSpacing(8)
        b.addWidget(_label("<b>Advanced — most people don't need this.</b> Use a Kick "
                           "“developer app” of your own instead of the one built into the app "
                           "(or if your copy doesn't have one). About two minutes:", "body"))
        r = QHBoxLayout()
        r.addWidget(_label("<b>1.</b> Open Kick's developer settings. (Kick asks you to turn "
                           "on two-factor login first.)", "body"), 1)
        r.addWidget(_button("Open", "ghostsmall",
                            lambda: desktop.open_url("https://kick.com/settings/developer")))
        b.addLayout(r)
        b.addWidget(_label("<b>2.</b> Click <b>Create App</b> — any name is fine — and set its "
                           "<b>Redirect URL</b> to:", "body"))
        r = QHBoxLayout()
        redir = QLineEdit(app_keys.KICK_REDIRECT_URI)
        redir.setReadOnly(True)
        redir.setObjectName("entry")
        r.addWidget(redir, 1)
        r.addWidget(_button("Copy", "ghostsmall",
                            lambda: QApplication.clipboard().setText(app_keys.KICK_REDIRECT_URI)))
        b.addLayout(r)
        b.addWidget(_label("Tick the permissions to <b>read user information</b> and "
                           "<b>write to chat</b>, then save the app.", "body"))
        b.addWidget(_label("<b>3.</b> Copy the app's keys into these two boxes:", "body"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.k_cid = QLineEdit(self.settings.secret("kick_app").get("client_id", ""))
        self.k_cid.setObjectName("entry")
        form.addRow(_label("Client ID", "field", False), self.k_cid)
        self.k_secret = QLineEdit(self.settings.secret("kick_app").get("client_secret", ""))
        self.k_secret.setObjectName("entry")
        self.k_secret.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(_label("Client Secret", "field", False), self.k_secret)
        b.addLayout(form)
        r = QHBoxLayout()
        r.addStretch(1)
        r.addWidget(_button("Save and sign in", "primarybtn", self._kick_save_app))
        b.addLayout(r)
        c.addWidget(self.k_app_box)
        self.k_app_toggle = _button("Use my own Kick app (advanced)…", "linkbtn",
                                    self._kick_toggle_app)
        c.addWidget(self.k_app_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.k_note = _label("", "hint")
        c.addWidget(self.k_note)
        v.addWidget(card)

        # ── Owncast ──
        card, c, self.st_owncast = self._card("Owncast")
        self.o_connected = QWidget()
        r = QHBoxLayout(self.o_connected)
        r.setContentsMargins(0, 0, 0, 0)
        self.o_account = _label("", "body")
        r.addWidget(self.o_account, 1)
        r.addWidget(_button("Disconnect", "ghostsmall", self._owncast_disconnect))
        c.addWidget(self.o_connected)
        self.o_form = QWidget()
        g = QGridLayout(self.o_form)
        g.setContentsMargins(0, 0, 0, 0)
        g.addWidget(_label("Server address", "field", False), 0, 0)
        self.o_url = QLineEdit(self.settings.get("owncast", "server_url") or "")
        self.o_url.setObjectName("entry")
        self.o_url.setPlaceholderText("e.g. https://stream.example.com")
        g.addWidget(self.o_url, 0, 1)
        g.addWidget(_label("Admin password", "field", False), 1, 0)
        self.o_pw = QLineEdit()
        self.o_pw.setObjectName("entry")
        self.o_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.o_pw.setPlaceholderText("the password for your Owncast admin page")
        g.addWidget(self.o_pw, 1, 1)
        self.o_token_lbl = _label("Access token", "field", False)
        self.o_token = QLineEdit()
        self.o_token.setObjectName("entry")
        self.o_token.setPlaceholderText("from Admin › Integrations › Access Tokens")
        g.addWidget(self.o_token_lbl, 2, 0)
        g.addWidget(self.o_token, 2, 1)
        self.o_token_lbl.hide()
        self.o_token.hide()
        r = QHBoxLayout()
        r.addWidget(_button("I have an access token instead", "linkbtn", self._owncast_token_mode))
        r.addStretch(1)
        self.o_btn = _button("Connect", "primarybtn", self._owncast_connect)
        r.addWidget(self.o_btn)
        g.addLayout(r, 3, 0, 1, 2)
        c.addWidget(self.o_form)
        c.addWidget(_label("Your password is only used once, to create a “Music Bot” chat "
                           "user on your server. It is never saved.", "hint"))
        v.addWidget(card)
        v.addStretch(1)
        return page

    # ── sign-in help (when the browser's last page won't load) ──

    def _signin_help(self, platform):
        box = QFrame()
        box.setObjectName("subcard")
        v = QVBoxLayout(box)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        v.addWidget(_label("<b>Page didn't load, or the wrong browser opened?</b> Open the "
                           "sign-in in another browser — or paste the address of the page you "
                           "ended up on (the login is in that address).", "body"))
        r = QHBoxLayout()
        r.addWidget(_button("Copy sign-in link", "ghostsmall", lambda: self._copy_link(platform)))
        r.addWidget(_button("Open it again", "ghostsmall",
                            lambda: desktop.open_url(self._links.get(platform, ""))
                            if self._links.get(platform) else None))
        r.addStretch(1)
        v.addLayout(r)
        r = QHBoxLayout()
        edit = QLineEdit()
        edit.setObjectName("entry")
        edit.setPlaceholderText("Paste the address from your browser's address bar")
        edit.returnPressed.connect(lambda: self._paste_return(platform))
        r.addWidget(edit, 1)
        r.addWidget(_button("Finish sign-in", "ghostsmall", lambda: self._paste_return(platform)))
        v.addLayout(r)
        note = _label("", "hint")
        note.hide()
        v.addWidget(note)
        box.hide()
        self._help[platform] = (box, edit, note)
        return box

    def _on_login_link(self, platform, url):
        self._links[platform] = url
        # Give the browser a moment first; most sign-ins never need this box.
        QTimer.singleShot(6000, lambda: self._help[platform][0].setVisible(platform in self._cancel))

    def _copy_link(self, platform):
        url = self._links.get(platform)
        if url:
            QApplication.clipboard().setText(url)
            self._help_note(platform, "Copied — paste it into another browser's address bar.")

    def _paste_return(self, platform):
        box, edit, note = self._help[platform]
        man = self._manual.get(platform)
        if man is None:
            return
        if man.offer(edit.text()):
            self._help_note(platform, "Finishing sign-in…")
        else:
            self._help_note(platform, "That's not it — copy the whole address from the address "
                                      "bar of the page you ended up on (it starts with "
                                      "http://localhost).")

    def _help_note(self, platform, text):
        note = self._help[platform][2]
        note.setText(text)
        note.setVisible(bool(text))

    def _end_help(self, platform):
        self._manual.pop(platform, None)
        box, edit, note = self._help[platform]
        box.hide()
        edit.clear()
        note.setText("")
        note.hide()

    # ── Twitch actions ──

    def _twitch_connect(self):
        if "twitch" in self._cancel:
            self._cancel.pop("twitch").set()
            self._end_help("twitch")
            self._refresh()
            return
        ev = threading.Event()
        self._cancel["twitch"] = ev
        man = self._manual["twitch"] = oauth_local.Manual()
        self.tw_btn.setText("Cancel")
        self.tw_account.setText("A Twitch page opened in your browser — log in and click "
                                "<b>Authorize</b>.")
        self.chat.run_thread(
            lambda: twitch_mod.login_blocking(
                self.settings, ev, manual=man,
                on_url=lambda url: self._login_link.emit("twitch", url)),
            lambda res: self._job_done.emit("twitch", res))

    def _twitch_signout(self):
        self.settings.set_secret("twitch", None)
        self._refresh()

    def _twitch_channel_saved(self):
        ch = self.tw_channel.text().strip().lstrip("#").lower()
        if "twitch.tv/" in ch:
            ch = ch.split("twitch.tv/", 1)[1].split("/")[0]
            self.tw_channel.setText(ch)
        self.settings.set("twitch", "channel", ch)

    # ── Kick actions ──

    def _kick_channel_saved(self):
        ch = kick_mod.normalize_channel(self.k_channel.text())
        if ch != self.k_channel.text():
            self.k_channel.setText(ch)
        if ch != (self.settings.get("kick", "channel") or ""):
            self.settings.update("kick", {"channel": ch, "chatroom_id": 0,
                                          "broadcaster_user_id": 0, "channel_id": 0})

    def _kick_save_app(self):
        cid, sec = self.k_cid.text().strip(), self.k_secret.text().strip()
        if not cid or not sec:
            self.k_note.setText("Paste both the Client ID and the Client Secret from your Kick app.")
            return
        self.settings.set_secret("kick_app", {"client_id": cid, "client_secret": sec},
                                 replace=True, notify=False)
        self._kick_connect()

    def _kick_toggle_app(self):
        self._k_app_open = not self._k_app_open
        self._refresh()

    def _kick_connect(self):
        if "kick" in self._cancel:
            self._cancel.pop("kick").set()
            self._end_help("kick")
            self._refresh()
            return
        if not kick_mod.kick_app(self.settings):
            self.k_note.setText(kick_mod.NO_KICK_APP)
            return
        self._kick_channel_saved()
        ev = threading.Event()
        self._cancel["kick"] = ev
        man = self._manual["kick"] = oauth_local.Manual()
        self.k_btn.setText("Cancel")
        self.k_account.setText("A Kick page opened in your browser — log in and click "
                               "<b>Allow</b>.")
        own = self.settings.secret("kick_app").get("client_id")
        self.k_note.setText(("If Kick shows “invalid authorization request”, your Kick app's "
                             f"Redirect URL must be exactly {app_keys.KICK_REDIRECT_URI} and it "
                             "needs the read-user-info and write-to-chat permissions.")
                            if own else "")
        self.chat.run_thread(
            lambda: kick_mod.login_blocking(
                self.settings, ev, manual=man,
                on_url=lambda url: self._login_link.emit("kick", url)),
            lambda res: self._job_done.emit("kick", res))

    def _kick_signout(self):
        self.settings.set_secret("kick", None)
        self._refresh()

    # ── Owncast actions ──

    def _owncast_token_mode(self):
        show = not self.o_token.isVisible()
        self.o_token.setVisible(show)
        self.o_token_lbl.setVisible(show)

    def _owncast_connect(self):
        url, pw, tok = self.o_url.text(), self.o_pw.text(), self.o_token.text()
        self.o_btn.setEnabled(False)
        self.o_btn.setText("Connecting…")
        self.chat.run_async(owncast_mod.connect(url, pw, tok),
                            lambda res: self._job_done.emit("owncast", res))

    def _owncast_disconnect(self):
        self.settings.set_secret("owncast", None)
        self._refresh()

    # ── results from worker threads ──

    def _on_job_done(self, job, res):
        if isinstance(res, BaseException):
            res = (False, f"{type(res).__name__}: {res}")
        if job in ("twitch", "kick"):
            self._end_help(job)
        if job == "twitch":
            self._cancel.pop("twitch", None)
            ok, msg = res
            self._refresh()
            if not ok:
                self.tw_account.setText(f"<span style='color:{self.theme.get('accent')}'>"
                                        f"{msg}</span>")
        elif job == "kick":
            self._cancel.pop("kick", None)
            ok, msg = res
            self._refresh()
            if ok:
                self.k_channel.setText(self.settings.get("kick", "channel") or "")
                self.k_note.setText("")
            else:
                self.k_account.setText(f"<span style='color:{self.theme.get('accent')}'>"
                                       f"{msg}</span>")
        elif job == "owncast":
            self.o_btn.setEnabled(True)
            self.o_btn.setText("Connect")
            ok, msg, server, token = (res + ("", ""))[:4] if len(res) < 4 else res
            if ok:
                self.o_pw.clear()
                self.o_token.clear()
                self.settings.set_secret("owncast", {"access_token": token}, replace=True,
                                         notify=False)
                self.settings.update("owncast", {"server_url": server, "enabled": True})
            self._refresh()
            if not ok:
                self.o_account.setText("")
                self.st_owncast.setText(f"<span style='color:{self.theme.get('accent')}'>"
                                        f"{msg}</span>")

    def _refresh(self):
        tw = self.settings.secret("twitch")
        if "twitch" not in self._cancel:
            if tw.get("access_token"):
                self.tw_account.setText(f"Connected as <b>{tw.get('login', '?')}</b>")
                self.tw_btn.setText("Reconnect")
                self.tw_out.show()
            else:
                self.tw_account.setText("Not connected yet.")
                self.tw_btn.setText("Connect Twitch")
                self.tw_out.hide()
        if not self.tw_channel.hasFocus():
            self.tw_channel.setText(self.settings.get("twitch", "channel") or "")

        k = self.settings.secret("kick")
        has_app = bool(kick_mod.kick_app(self.settings))
        if "kick" not in self._cancel:
            if k.get("access_token"):
                self.k_account.setText(f"Replies as <b>{k.get('username') or 'you'}</b>")
                self.k_btn.setText("Sign in again")
                self.k_out.show()
            else:
                self.k_account.setText("Not signed in — the bot reads chat but can't reply.")
                self.k_btn.setText("Sign in to Kick")
                self.k_out.hide()
        self.k_app_box.setVisible(self._k_app_open)
        if not has_app and "kick" not in self._cancel and not k.get("access_token") \
                and not self.k_note.text():
            self.k_note.setText(kick_mod.NO_KICK_APP)
        elif has_app and self.k_note.text() == kick_mod.NO_KICK_APP:
            self.k_note.setText("")

        o = self.settings.secret("owncast")
        url = self.settings.get("owncast", "server_url") or ""
        if o.get("access_token") and url:
            self.o_account.setText(f"Connected to <b>{owncast_mod.normalize_url(url)}</b>")
            self.o_connected.show()
            self.o_form.hide()
        else:
            self.o_connected.hide()
            self.o_form.show()

    def on_chat_status(self, platform, level, text):
        lab = {"twitch": getattr(self, "st_twitch", None), "kick": getattr(self, "st_kick", None),
               "owncast": getattr(self, "st_owncast", None)}.get(platform)
        if lab is None:
            return
        colour = self.theme.get(LEVEL_COLOUR.get(level, "faint"), "#9b9bab")
        lab.setText(f"<span style='color:{colour}'>●</span> {text}")

    # ═════════════════════════════ Player & OBS tab ═════════════════════════════

    def _player_tab(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(4, 10, 4, 4)
        v.setSpacing(12)

        card, c, _ = self._card("Song requests")
        row = QHBoxLayout()
        row.addWidget(_label("Longest song allowed", "field", False))
        self.p_max = QSpinBox()
        self.p_max.setRange(1, 120)
        self.p_max.setSuffix(" min")
        self.p_max.setValue(int(self.settings.get("player", "max_song_minutes") or 10))
        self.p_max.valueChanged.connect(
            lambda n: self.settings.set("player", "max_song_minutes", int(n)))
        row.addWidget(self.p_max)
        row.addStretch(1)
        c.addLayout(row)
        self.p_anyone = QCheckBox("Let every viewer control playback")
        self.p_anyone.setChecked(bool(self.settings.get("player", "anyone_can_control")))
        self.p_anyone.toggled.connect(
            lambda on: self.settings.set("player", "anyone_can_control", bool(on)))
        c.addWidget(self.p_anyone)
        c.addWidget(_label("!skip, !back, !pause, !play, !volume and !clear. When this is off, "
                           "only you and your mods can use them. Everyone can always request "
                           "songs with !sr.", "hint"))
        v.addWidget(card)

        card, c, _ = self._card("YouTube")
        row = QHBoxLayout()
        row.addWidget(_label("Age-restricted videos", "field", False))
        self.p_cookies = QComboBox()
        for label, val in COOKIE_CHOICES:
            self.p_cookies.addItem(label, val)
        cur = (self.settings.get("player", "cookies_browser") or "none").lower()
        idx = max(0, self.p_cookies.findData(cur))
        self.p_cookies.setCurrentIndex(idx)
        self.p_cookies.currentIndexChanged.connect(
            lambda i: self.settings.set("player", "cookies_browser", self.p_cookies.itemData(i)))
        row.addWidget(self.p_cookies, 1)
        c.addLayout(row)
        c.addWidget(_label("Picking a browser lets the player use your YouTube login from "
                           "it, for videos YouTube only shows to signed-in adults. Firefox "
                           "works best; Chrome and Edge must be closed for it to work.", "hint"))
        self.p_video = QCheckBox("Show music videos in a window on this computer too")
        self.p_video.setChecked(bool(self.settings.get("player", "video_window", True)))
        self.p_video.toggled.connect(lambda on: self.settings.set("player", "video_window", bool(on)))
        c.addWidget(self.p_video)
        v.addWidget(card)

        card, c, _ = self._card("Add it to OBS")
        row = QHBoxLayout()
        link = QLineEdit(self.window.obs_link() if self.window else "http://localhost:9111/")
        link.setReadOnly(True)
        link.setObjectName("entry")
        row.addWidget(link, 1)
        row.addWidget(_button("Copy", "ghostsmall", lambda: self.window.copy_obs_link()))
        c.addLayout(row)
        c.addWidget(_label("In OBS: click <b>+</b> under Sources → <b>Browser</b> → paste the "
                           "link into <b>URL</b>, set <b>Width 560</b> and <b>Height 500</b>, "
                           "click OK. The now-playing card and music videos show up there. "
                           "Song audio plays through this app — capture it in OBS like any "
                           "other app (Desktop Audio or an Application Audio source).", "hint"))
        v.addWidget(card)

        card, c, _ = self._card("Help")
        row = QHBoxLayout()
        row.addWidget(_button("Open data folder", "ghostsmall",
                              lambda: desktop.open_path(app_paths.data_dir())))
        row.addWidget(_button("Open log folder", "ghostsmall",
                              lambda: desktop.open_path(app_paths.logs_dir())))
        row.addWidget(_button("Project page", "ghostsmall",
                              lambda: desktop.open_url(f"https://github.com/{app_paths.REPO}")))
        row.addStretch(1)
        c.addLayout(row)
        self.p_updates = QCheckBox("Tell me when a new version is out")
        self.p_updates.setChecked(bool(self.settings.get("player", "check_updates", True)))
        self.p_updates.toggled.connect(
            lambda on: self.settings.set("player", "check_updates", bool(on)))
        c.addWidget(self.p_updates)
        from version import __version__
        c.addWidget(_label(f"Version {__version__} · data folder: {app_paths.data_dir()}", "hint"))
        v.addWidget(card)
        v.addStretch(1)
        return page

    # ═════════════════════════════════ style ═════════════════════════════════

    def _qss(self):
        t = self.theme or {"bg": "#0e0e12", "panel": "#16161d", "panel2": "#1e1e27",
                           "stroke": "#2a2a35", "text": "#f3f3f6", "muted": "#9b9bab",
                           "faint": "#6a6a78", "accent": "#ff2e4d", "accent_2": "#ff5a3c"}
        return f"""
        QDialog {{ background: {t['bg']}; }}
        QWidget {{ color: {t['text']}; font-family: 'Segoe UI','Helvetica Neue',Arial;
                   font-size: 13px; }}
        QScrollArea#page, QWidget#page {{ background: {t['bg']}; border: none; }}
        QScrollBar:vertical {{ background: transparent; width: 9px; margin: 4px 2px; }}
        QScrollBar::handle:vertical {{ background: {t['stroke']}; border-radius: 4px;
                                       min-height: 28px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QTabWidget::pane {{ border: none; }}
        QTabBar::tab {{ background: transparent; color: {t['muted']}; padding: 8px 16px;
                        border-bottom: 2px solid transparent; font-weight: 700; }}
        QTabBar::tab:selected {{ color: {t['text']}; border-bottom: 2px solid {t['accent']}; }}
        QFrame#card {{ background: {t['panel']}; border: 1px solid {t['stroke']};
                       border-radius: 14px; }}
        QFrame#subcard {{ background: {t['panel2']}; border: 1px solid {t['stroke']};
                          border-radius: 10px; }}
        QLabel#cardtitle {{ font-size: 16px; font-weight: 800; }}
        QLabel#status {{ color: {t['muted']}; font-size: 12px; }}
        QLabel#intro {{ color: {t['muted']}; font-size: 13px; }}
        QLabel#body {{ color: {t['text']}; }}
        QLabel#hint {{ color: {t['faint']}; font-size: 12px; }}
        QLabel#field {{ color: {t['muted']}; font-weight: 600; }}
        QLineEdit#entry, QSpinBox, QComboBox {{
            background: {t['panel2']}; border: 1px solid {t['stroke']}; border-radius: 9px;
            padding: 6px 10px; selection-background-color: {t['accent']}; }}
        QLineEdit#entry:focus {{ border: 1px solid {t['accent']}; }}
        QPushButton#primarybtn {{ background: {t['accent']}; color: white; border: none;
            border-radius: 10px; padding: 8px 16px; font-weight: 700; }}
        QPushButton#primarybtn:hover {{ background: {t['accent_2']}; }}
        QPushButton#primarybtn:disabled {{ background: {t['stroke']}; color: {t['muted']}; }}
        QPushButton#ghostsmall {{ background: transparent; color: {t['muted']};
            border: 1px solid {t['stroke']}; border-radius: 9px; padding: 6px 12px; }}
        QPushButton#ghostsmall:hover {{ color: {t['text']}; border-color: {t['accent']}; }}
        QPushButton#linkbtn {{ background: transparent; color: {t['muted']}; border: none;
            padding: 2px 0; text-decoration: underline; }}
        QPushButton#linkbtn:hover {{ color: {t['accent']}; }}
        QCheckBox {{ spacing: 9px; font-weight: 600; }}
        QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px;
                                border: 1px solid {t['muted']}; background: {t['panel2']}; }}
        QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}
        QComboBox QAbstractItemView {{ background: {t['panel2']}; color: {t['text']};
                                       selection-background-color: {t['accent']}; }}
        """
