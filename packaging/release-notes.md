## Download

| Your computer | File |
|---|---|
| **Windows 10 / 11** | **BLOODBOS-Radio-Setup-Windows.exe** — double-click it. If Windows says *"Windows protected your PC"*, click **More info → Run anyway**. |
| **Mac with an Apple chip** (M1–M4) | **BLOODBOS-Radio-macOS-AppleSilicon.dmg** — open it and drag the app to Applications. |
| **Mac with an Intel chip** | **BLOODBOS-Radio-macOS-Intel.dmg** |
| **Linux** | paste into a terminal: `curl -fsSL https://raw.githubusercontent.com/korn788/BloodBos-music-request-bot/main/install.sh \| bash` |
| **Ubuntu / Debian / Mint / Pop!_OS** | **bloodbos-radio_amd64.deb** — double-click it. |

First time on a Mac: if macOS says it can't open the app, go to **System Settings → Privacy & Security** and click **Open Anyway**.

Then follow the **Setup** window that opens by itself: click **Connect Twitch**, type your **Kick** channel, and/or enter your **Owncast** address and admin password. Full instructions: [README](https://github.com/korn788/BloodBos-music-request-bot#readme).

## In this version

- **One app for Twitch, Kick and Owncast** — the separate Twitch bot and the .NET Owncast bot are now built in. Sign in with buttons instead of editing files.
- **Fixed: the music video looping right before a song ends.** When the song's audio ran a little longer than its video, both the OBS widget and the app's video window restarted the video from the beginning. The video now holds its last frame.
- **Kick chat works again without a server**: the app follows Kick's live-chat key change from October 2026 by itself.
- **Installers** for Windows, macOS (Apple chip and Intel) and Linux, with VLC included on Windows and Mac.
- **YouTube keeps working**: the app downloads yt-dlp (and the JavaScript runtime YouTube now needs) and updates it daily.
- **Safer by default**: `!skip`, `!pause`, `!volume` and `!clear` are for you and your mods unless you open them up in Setup; other websites can't control the player through its local API.
