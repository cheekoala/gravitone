"""Album art: the picture inside the file, or the one next to it.

Extraction is an ffmpeg call, so it happens once per file and the result is
kept as a small file next to the other caches. Everything here answers
"not yet" rather than blocking a request.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
from pathlib import Path

from bgsoundtrack.config import AUDIO_EXTENSIONS, config_path

# What a cover is usually called when it sits beside the music.
BESIDE = (
    "cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.jpeg",
    "folder.png", "front.jpg", "front.png", "album.jpg", "albumart.jpg",
)
MAX_BYTES = 6 * 1024 * 1024
# How many tracks of a record to try before deciding it has no cover. Plenty
# of rips carry the picture on only one or two files.
SIBLINGS = 5


def cache_dir(config_file: Path | None = None) -> Path:
    base = config_file or config_path()
    return base.parent / "covers"


class Art:
    """Covers for files, found once and remembered."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or cache_dir()
        self._lock = threading.Lock()
        self._known: dict = {}      # path -> cover file, or None for "none"
        self._working: set = set()

    @staticmethod
    def real(path: Path) -> Path:
        """The file itself, not the link pointing at it."""
        try:
            return path.resolve()
        except OSError:
            return path

    def _key(self, path: Path) -> str:
        # Albums share art, so key on the folder rather than the track: one
        # extraction covers a whole record. Resolve first, or every link in
        # the library folder would claim the same cover.
        return hashlib.sha1(str(self.real(path).parent).encode("utf-8")).hexdigest()[:16]

    def answered(self, path: Path) -> tuple:
        """(have we looked, what we found). "No cover" is an answer.

        Without this, a record with no art anywhere gets ffmpeg run over it
        again every time one of its tracks plays.
        """
        path = self.real(path)
        with self._lock:
            if str(path.parent) in self._known:
                return (True, self._known[str(path.parent)])
        return (False, None)

    def cached(self, path: Path) -> Path | None:
        """The cover we already have for this track, if any."""
        path = self.real(path)
        with self._lock:
            if str(path.parent) in self._known:
                return self._known[str(path.parent)]
        for suffix in (".jpg", ".png"):
            candidate = self.directory / f"{self._key(path)}{suffix}"
            if candidate.exists():
                with self._lock:
                    self._known[str(path.parent)] = candidate
                return candidate
        return None

    def beside(self, path: Path) -> Path | None:
        path = self.real(path)
        for name in BESIDE:
            candidate = path.parent / name
            try:
                if candidate.is_file() and candidate.stat().st_size <= MAX_BYTES:
                    return candidate
            except OSError:
                continue
        return None

    def siblings(self, path: Path) -> list:
        """This track, then a few of its neighbours in the same folder.

        The picture often lives on one track of a record and not the rest, so
        looking only at what happens to be playing finds nothing far too
        often.
        """
        found = [path]
        try:
            for other in sorted(path.parent.iterdir()):
                if len(found) >= SIBLINGS:
                    break
                if other == path or other.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                if other.is_file():
                    found.append(other)
        except OSError:
            pass
        return found

    def find(self, path: Path) -> Path | None:
        """Look properly: cache, then a file beside it, then inside the
        record's own tracks."""
        path = self.real(path)
        asked, found = self.answered(path)
        if asked:
            return found
        beside = self.beside(path)
        if beside is not None:
            with self._lock:
                self._known[str(path.parent)] = beside
            return beside

        for candidate in self.siblings(path):
            extracted = self._extract(candidate, remember_failure=False)
            if extracted is not None:
                return extracted
        with self._lock:
            self._known[str(path.parent)] = None    # asked the record, properly
        return None

    def _extract(self, path: Path, remember_failure: bool = True) -> Path | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{self._key(path)}.jpg"
        try:
            done = subprocess.run(
                [
                    ffmpeg, "-v", "error", "-y",
                    "-i", str(path),
                    "-an", "-map", "0:v?", "-frames:v", "1",
                    "-vf", "scale=min(600\\,iw):-1",
                    str(target),
                ],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            if remember_failure:
                with self._lock:
                    self._known[str(path.parent)] = None   # asked and answered
            return None
        with self._lock:
            self._known[str(path.parent)] = target
        return target

    def want(self, path: Path) -> None:
        """Find it in the background - for the track that just started."""
        path = self.real(path)
        key = str(path.parent)
        with self._lock:
            if key in self._known or key in self._working:
                return
            self._working.add(key)

        def work() -> None:
            try:
                self.find(path)
            finally:
                with self._lock:
                    self._working.discard(key)

        threading.Thread(target=work, daemon=True, name="bgst-art").start()

    def has_any(self) -> bool:
        return bool(shutil.which("ffmpeg"))
