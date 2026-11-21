## Download

| Your computer | File |
|---|---|
| **Windows 10 / 11** | **BLOODBOS-Radio-Setup-Windows.exe** — double-click it. If Windows says *"Windows protected your PC"*, click **More info → Run anyway**. |
| **Mac with an Apple chip** (M1–M4) | **BLOODBOS-Radio-macOS-AppleSilicon.dmg** — open it and drag the app to Applications. |
| **Mac with an Intel chip** | **BLOODBOS-Radio-macOS-Intel.dmg** |
| **Linux** (any) | paste into a terminal: `curl -fsSL https://raw.githubusercontent.com/korn788/BloodBos-music-request-bot/main/install.sh \| bash` — installs the ready-made app (**BLOODBOS-Radio-Linux-x86_64.tar.gz**) |
| **Ubuntu / Debian / Mint / Pop!_OS** | **bloodbos-radio_amd64.deb** — double-click it. |

First time on a Mac: if macOS says it can't open the app, go to **System Settings → Privacy & Security** and click **Open Anyway**.

Then follow the **Setup** window that opens by itself: click **Connect Twitch**, type your **Kick** channel, and/or enter your **Owncast** address and admin password. Full instructions: [README](https://github.com/korn788/BloodBos-music-request-bot#readme).

## New in 1.0.1

- **Fixed: the music video replaying the same few seconds right before a song ends.** When a song's audio got stuck for a moment (a network hiccup, or a song that goes quiet at its very end), the video kept playing, got pulled back, and played the same stretch again and again. Now the video waits for the audio — in the OBS widget and in the app's video window.
- **SoundCloud links that SoundCloud locks now play from YouTube.** SoundCloud encrypts many label releases so only its own app can play them, and gives other apps just a 30-second preview of Go+ tracks — those requests all failed with *"won't play (private, removed or region-locked)"*. The bot now finds the same song on YouTube and says so in chat. When a link really can't play, the reply says why (private or removed, not available in your country, a short link that won't open). Your browser's cookies (used for age-restricted YouTube videos) are no longer sent to SoundCloud, where a logged-in account could get even playable tracks back locked.
- **Songs shared from a phone work as requests.** Sharing from the SoundCloud app (and others) adds a line like *"Listen to … on #SoundCloud"* in front of the link; when viewers pasted all of it after `!sr` or `!sc`, the request failed. The bot now picks out the YouTube, SoundCloud or Spotify link and ignores the rest.
- **The OBS widget reloads itself when you update the app**, so OBS never keeps running an old copy. Updating from 1.0.0: refresh the browser source once after installing (select it in OBS and click **Refresh**).
- **Playlist export** suggests a title from the day the stream started, not the day you press Export.
- Other Twitch or Kick tools set up on your computer can no longer change which app the sign-in buttons use.

## In 1.0.0

- **One app for Twitch, Kick and Owncast** — the separate Twitch bot and the .NET Owncast bot are now built in. Sign in with buttons: Twitch and Kick open their normal login pages, and if the last page won't load you can finish by pasting its address into Setup.
- **Fixed: the music video looping right before a song ends.** When the song's audio ran a little longer than its video, both the OBS widget and the app's video window restarted the video from the beginning. The video now holds its last frame.
- **Kick chat works again, with no server and no login**: the app reads Kick's chat on your own computer, using the chat system kick.com itself switched to in October 2026, and falls back to the older one by itself if needed.
- **Installers** for Windows, macOS (Apple chip and Intel) and Linux, with VLC included on Windows and Mac.
- **YouTube keeps working**: the app downloads yt-dlp (and the JavaScript runtime YouTube now needs) and updates it daily.
- **Safer by default**: `!skip`, `!pause`, `!volume` and `!clear` are for you and your mods unless you open them up in Setup; other websites can't control the player through its local API.
