"""Artist, album and title for a file, for sorting the library by them.

Tags come from ffprobe (part of ffmpeg, which most people already have for
playback). Files it cannot read - or a machine without ffprobe at all - fall
back to what the path says: `Artist/Album/01 Title.flac` is a convention for
a reason.

Reads are cached to disk, because probing a few thousand files is something
you want to do once, not on every listing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from gravitone.config import config_path

CACHE_VERSION = 2
PROBE_WORKERS = 8
# "01 Title", "01 - Title", "01. Title", "12_Title" - but not "1984" on its own.
_LEADING_NUMBER = re.compile(r"^\s*(\d{1,3})\s*(?:[-._)]\s*|\s+)")


@dataclass(frozen=True)
class Tags:
    title: str = ""
    artist: str = ""
    album: str = ""
    track: int = 0
    duration: float = 0.0
    guessed: bool = False   # nothing read; this came from the path

    def key(self, field: str) -> tuple:
        """Sort key for one field, with sensible tie-breakers.

        Unknowns sort last rather than clumping at the top, where they would
        look like the answer.
        """
        if field == "length":
            return (0 if self.duration else 1, f"{self.duration:012.3f}", self.title.lower())
        if field == "artist":
            first, rest = self.artist, (self.album, self.track, self.title)
        elif field == "album":
            first, rest = self.album, (self.track, self.title, self.artist)
        else:
            first, rest = self.title, (self.artist, self.album)
        return (0, first.lower(), *[str(part).lower() for part in rest]) if first else (
            1, "", *[str(part).lower() for part in rest]
        )


def cache_path(config_file: Path | None = None) -> Path:
    base = config_file or config_path()
    return base.parent / "tags.json"


def guess(path: Path) -> Tags:
    """Read what the path itself says: Artist/Album/01 Title.ext."""
    stem = path.stem
    number = 0
    match = _LEADING_NUMBER.match(stem)
    if match:
        number = int(match.group(1))
        stem = stem[match.end():]
    parent = path.parent.name
    grandparent = path.parent.parent.name if path.parent.parent != path.parent else ""
    return Tags(
        title=stem.strip(),
        artist=grandparent,
        album=parent,
        track=number,
        guessed=True,
    )


def _probe(path: Path) -> Tags | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        done = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries",
                "format=duration"
                ":format_tags=title,artist,album,album_artist,track"
                ":stream_tags=title,artist,album,album_artist,track",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        payload = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        return None

    found: dict = {}
    for block in [payload.get("format", {})] + list(payload.get("streams", [])):
        for name, value in (block.get("tags") or {}).items():
            found.setdefault(name.lower(), str(value))
    try:
        duration = float(payload.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if not found and not duration:
        return None

    fallback = guess(path)
    number = found.get("track", "")
    try:
        track = int(str(number).split("/")[0])
    except (TypeError, ValueError):
        track = fallback.track
    return Tags(
        title=found.get("title") or fallback.title,
        artist=found.get("artist") or found.get("album_artist") or "",
        album=found.get("album") or "",
        track=track,
        duration=duration,
    )


class Reader:
    """Tag lookups with a cache that survives restarts."""

    def __init__(self, path: Path | None = None):
        self.path = path or cache_path()
        self._lock = threading.Lock()
        self._entries: dict = {}
        self._dirty = False
        # A listing asks for the same file twice (sorting, then the row), and
        # each ask stat()s it. Hold the answer for a moment.
        self._memo: dict = {}
        self._memo_at = 0.0
        self._load()

    # -- cache ----------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if raw.get("version") != CACHE_VERSION:
            return
        self._entries = raw.get("files", {}) or {}

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            payload = {"version": CACHE_VERSION, "files": self._entries}
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _stamp(path: Path) -> str | None:
        try:
            info = path.stat()
        except OSError:
            return None
        return f"{info.st_size}:{int(info.st_mtime)}"

    # -- reading --------------------------------------------------------

    def cached(self, path: Path) -> Tags | None:
        stamp = self._stamp(path)
        if stamp is None:
            return None
        with self._lock:
            entry = self._entries.get(str(path))
        if not entry or entry.get("stamp") != stamp:
            return None
        return Tags(
            title=entry.get("title", ""),
            artist=entry.get("artist", ""),
            album=entry.get("album", ""),
            track=int(entry.get("track", 0) or 0),
            duration=float(entry.get("duration", 0) or 0),
            guessed=bool(entry.get("guessed")),
        )

    def store(self, path: Path, tags: Tags) -> None:
        self._memo.pop(str(path), None)
        stamp = self._stamp(path)
        if stamp is None:
            return
        with self._lock:
            self._entries[str(path)] = {
                "stamp": stamp,
                "title": tags.title,
                "artist": tags.artist,
                "album": tags.album,
                "track": tags.track,
                "duration": tags.duration,
                "guessed": tags.guessed,
            }
            self._dirty = True

    def read(self, path: Path) -> Tags:
        """Tags for one file: cache, then ffprobe, then the path."""
        hit = self.cached(path)
        if hit is not None:
            return hit
        tags = _probe(path) or guess(path)
        self.store(path, tags)
        return tags

    def known(self, path: Path, ttl: float = 2.0) -> Tags:
        """What we can answer right now, without probing anything."""
        now = time.monotonic()
        if now - self._memo_at > ttl:
            self._memo = {}
            self._memo_at = now
        key = str(path)
        hit = self._memo.get(key)
        if hit is None:
            hit = self.cached(path) or guess(path)
            self._memo[key] = hit
        return hit

    def forget_memo(self) -> None:
        self._memo = {}

    def pending(self, paths: list) -> list:
        return [path for path in paths if self.cached(path) is None]

    def read_all(self, paths: list, on_progress=None) -> int:
        """Fill the cache for a whole list, in parallel. Returns how many."""
        todo = self.pending(paths)
        if not todo:
            return 0
        done = 0
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            for _ in pool.map(self.read, todo):
                done += 1
                if on_progress and done % 25 == 0:
                    on_progress(done, len(todo))
        self.save()
        if on_progress:
            on_progress(done, len(todo))
        return done
