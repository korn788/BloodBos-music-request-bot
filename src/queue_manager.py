# queue_manager.py — Unified queue for YouTube + SoundCloud tracks

import threading
import json
from typing import Optional, Callable
from audio_engine import Track, TrackSource


class QueueManager:
    """Thread-safe queue that manages tracks from any source"""

    def __init__(self):
        self._queue: list[Track] = []
        self._history: list[Track] = []  # previously played tracks (for !back)
        self._lock = threading.RLock()
        self._max_history = 100

        # Callbacks
        self.on_queue_changed: Optional[Callable] = None

    @property
    def length(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return len(self._queue) == 0

    def _notify(self):
        if self.on_queue_changed:
            try:
                self.on_queue_changed()
            except Exception:
                pass

    def add(self, track: Track):
        """Add a track to the end of the queue"""
        with self._lock:
            self._queue.append(track)
        self._notify()

    def add_next(self, track: Track):
        """Add a track to play next (front of queue)"""
        with self._lock:
            self._queue.insert(0, track)
        self._notify()

    def pop_next(self) -> Optional[Track]:
        """Remove and return the next track"""
        with self._lock:
            if not self._queue:
                return None
            return self._queue.pop(0)

    def peek_next(self) -> Optional[Track]:
        """Look at the next track without removing it"""
        with self._lock:
            return self._queue[0] if self._queue else None

    def remove_at(self, index: int) -> Optional[Track]:
        """Remove a track at a specific index"""
        track = None
        with self._lock:
            if 0 <= index < len(self._queue):
                track = self._queue.pop(index)
        if track is not None:
            self._notify()
        return track

    def move(self, from_index: int, to_index: int):
        """Move a track from one position to another"""
        with self._lock:
            if 0 <= from_index < len(self._queue) and 0 <= to_index < len(self._queue):
                track = self._queue.pop(from_index)
                self._queue.insert(to_index, track)
        self._notify()

    def clear(self):
        """Clear the entire queue"""
        with self._lock:
            self._queue.clear()
        self._notify()

    def get_list(self, limit: int = 0) -> list[Track]:
        """Get a copy of the queue (optionally limited)"""
        with self._lock:
            if limit > 0:
                return list(self._queue[:limit])
            return list(self._queue)

    def push_to_history(self, track: Track):
        """Add a track to the history (for back button)"""
        with self._lock:
            self._history.append(track)
            if len(self._history) > self._max_history:
                self._history.pop(0)
        self._notify()

    def pop_from_history(self) -> Optional[Track]:
        """Get the last played track from history"""
        with self._lock:
            return self._history.pop() if self._history else None

    def get_history(self, limit: int = 10) -> list[Track]:
        """Get a copy of the play history (most recent last)"""
        with self._lock:
            if limit > 0:
                return list(self._history[-limit:])
            return list(self._history)

    def remove_from_history(self, track: Track) -> bool:
        """Remove one specific history entry (by identity)."""
        removed = False
        with self._lock:
            for i, t in enumerate(self._history):
                if t is track:
                    self._history.pop(i)
                    removed = True
                    break
        if removed:
            self._notify()
        return removed

    def purge_url(self, url: str) -> int:
        """Remove every queued and history entry for this URL (used by Block)."""
        if not url:
            return 0
        with self._lock:
            before = len(self._queue) + len(self._history)
            self._queue = [t for t in self._queue if t.url != url]
            self._history = [t for t in self._history if t.url != url]
            n = before - len(self._queue) - len(self._history)
        if n:
            self._notify()
        return n

    def get_display_list(self, limit: int = 10) -> list[dict]:
        """Get queue as a list of dicts for display"""
        with self._lock:
            result = []
            for i, track in enumerate(self._queue[:limit]):
                result.append({
                    "index": i,
                    "title": track.title,
                    "artist": track.artist,
                    "source": track.source.value,
                    "duration": track.duration_seconds,
                    "requested_by": track.requested_by,
                })
            return result
