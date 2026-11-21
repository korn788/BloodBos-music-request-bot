# youtube_api.py — YouTube Data API v3 integration for like/dislike + playlist export
# Uses OAuth2 refresh token for authentication (no browser flow needed)

import json
import os
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional
from datetime import datetime


class YouTubeAPI:
    """YouTube Data API v3 client using OAuth2 refresh token."""

    TOKEN_URL = "https://oauth2.googleapis.com/token"
    API_BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, config_path: str = None):
        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._token_expires: float = 0
        self._config: dict = {}
        self._enabled = False

        # Try to load config
        if config_path is None:
            # Optional, advanced: youtube.json in the app's data folder with your
            # own Google OAuth client (client_id, client_secret, refresh_token).
            import app_paths
            config_path = os.path.join(app_paths.data_dir(), "youtube.json")

        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    self._config = json.load(f)
                # Validate required fields
                required = ["client_id", "client_secret", "refresh_token"]
                if all(self._config.get(k) for k in required):
                    self._enabled = True
                    print(f"[YT-API] Loaded config from {config_path}")
                else:
                    print(f"[YT-API] Config missing required fields: {required}")
            except Exception as e:
                print(f"[YT-API] Error loading config: {e}")
        else:
            pass   # optional feature; quietly off when not set up

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── OAuth2 Token Management ──

    def _refresh_access_token(self) -> Optional[str]:
        """Get a fresh access token using the refresh token."""
        with self._lock:
            # Check if current token is still valid
            if self._access_token and time.time() < self._token_expires - 60:
                return self._access_token

            try:
                data = urllib.parse.urlencode({
                    "client_id": self._config["client_id"],
                    "client_secret": self._config["client_secret"],
                    "refresh_token": self._config["refresh_token"],
                    "grant_type": "refresh_token",
                }).encode()

                req = urllib.request.Request(self.TOKEN_URL, data=data, method="POST")
                req.add_header("Content-Type", "application/x-www-form-urlencoded")

                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = json.loads(resp.read())
                    self._access_token = result["access_token"]
                    # Token typically valid for 3600 seconds
                    self._token_expires = time.time() + result.get("expires_in", 3600)
                    print("[YT-API] Access token refreshed")
                    return self._access_token

            except Exception as e:
                print(f"[YT-API] Token refresh failed: {e}")
                return None

    def _api_request(self, method: str, endpoint: str,
                     params: dict = None, body: dict = None) -> Optional[dict]:
        """Make an authenticated YouTube API request."""
        token = self._refresh_access_token()
        if not token:
            return None

        url = f"{self.API_BASE}/{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        data = json.dumps(body).encode() if body else None

        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 204:
                    return {"ok": True}
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode()[:500]
            except Exception:
                pass
            print(f"[YT-API] HTTP {e.code} on {method} {endpoint}: {body_text}")
            return None
        except Exception as e:
            print(f"[YT-API] Request error: {e}")
            return None

    # ── Like / Dislike ──

    def rate_video(self, video_id: str, rating: str = "like") -> bool:
        """
        Rate a video. rating can be 'like', 'dislike', or 'none' (remove rating).
        Returns True on success.
        """
        if not self._enabled or not video_id:
            return False

        result = self._api_request("POST", "videos/rate", params={
            "id": video_id,
            "rating": rating,
        })
        if result is not None:
            print(f"[YT-API] Rated {video_id} as '{rating}'")
            return True
        return False

    def like_video(self, video_id: str) -> bool:
        return self.rate_video(video_id, "like")

    def dislike_video(self, video_id: str) -> bool:
        return self.rate_video(video_id, "dislike")

    def remove_rating(self, video_id: str) -> bool:
        return self.rate_video(video_id, "none")

    # ── Playlist Creation ──

    def create_playlist(self, title: str, description: str = "",
                        privacy: str = "unlisted") -> Optional[str]:
        """
        Create a new playlist. Returns the playlist ID or None.
        privacy: 'public', 'unlisted', or 'private'
        """
        if not self._enabled:
            return None

        result = self._api_request("POST", "playlists", params={
            "part": "snippet,status",
        }, body={
            "snippet": {
                "title": title,
                "description": description,
            },
            "status": {
                "privacyStatus": privacy,
            }
        })
        if result and "id" in result:
            print(f"[YT-API] Created playlist: {result['id']} ({title})")
            return result["id"]
        return None

    def add_to_playlist(self, playlist_id: str, video_id: str,
                        position: int = None) -> bool:
        """Add a video to a playlist. Returns True on success."""
        if not self._enabled or not playlist_id or not video_id:
            return False

        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id,
                }
            }
        }
        if position is not None:
            body["snippet"]["position"] = position

        result = self._api_request("POST", "playlistItems", params={
            "part": "snippet",
        }, body=body)
        if result and "id" in result:
            return True
        return False

    def export_history_as_playlist(self, tracks: list, title: str = None,
                                   description: str = None,
                                   privacy: str = "unlisted",
                                   progress_callback=None) -> Optional[str]:
        """
        Export a list of tracks (with video_id) as a YouTube playlist.
        Returns the playlist URL or None.
        progress_callback(current, total) is called for each video added.
        """
        if not self._enabled:
            print("[YT-API] Not enabled — cannot export playlist")
            return None

        # Filter to YouTube tracks with video IDs, deduplicate
        seen = set()
        yt_tracks = []
        for t in tracks:
            vid = getattr(t, "video_id", None) or ""
            if vid and vid not in seen:
                seen.add(vid)
                yt_tracks.append(t)

        if not yt_tracks:
            print("[YT-API] No YouTube tracks to export")
            return None

        # Default title with date
        if not title:
            title = f"Stream Playlist — {datetime.now().strftime('%B %d, %Y')}"
        if not description:
            description = (
                f"Auto-generated playlist with {len(yt_tracks)} tracks "
                f"from stream on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )

        print(f"[YT-API] Exporting {len(yt_tracks)} tracks as '{title}'...")

        # Create the playlist
        playlist_id = self.create_playlist(title, description, privacy)
        if not playlist_id:
            print("[YT-API] Failed to create playlist")
            return None

        # Give YouTube's database a moment to propagate the new playlist ID globally
        print("[YT-API] Waiting 2 seconds for playlist creation to propagate...")
        time.sleep(2.0)

        # Add each track
        added = 0
        for i, track in enumerate(yt_tracks):
            if progress_callback:
                progress_callback(i + 1, len(yt_tracks))

            vid = track.video_id

            # We omit the 'position' argument here. By not forcing an index,
            # YouTube simply appends the video to the end of the playlist.
            # This prevents HTTP 400 errors if a previous track failed to add.
            ok = self.add_to_playlist(playlist_id, vid)
            if ok:
                added += 1
            else:
                print(f"[YT-API] Failed to add {vid} ({track.title})")

            # Delay to avoid write-operation rate limits
            if i < len(yt_tracks) - 1:
                time.sleep(0.5)

        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        print(f"[YT-API] Playlist exported: {added}/{len(yt_tracks)} tracks")
        print(f"[YT-API] URL: {playlist_url}")
        return playlist_url
