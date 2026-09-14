"""Album art, taken from the files themselves.

Only pictures embedded in the audio are used - no guessing from a cover.jpg
lying in the folder, which is someone else's idea of what this record looks
like. A file either carries its art or it does not.

Extraction is an ffmpeg call, so it happens once per record and the result is
written next to the other caches, along with the answer "this one has none" -
otherwise every restart would run ffmpeg over every coverless album again.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path

from bgsoundtrack.config import AUDIO_EXTENSIONS, config_path

MAX_BYTES = 6 * 1024 * 1024
# How many tracks of a record to try before deciding it has no cover. Plenty
# of rips carry the picture on only one or two files.
SIBLINGS = 5
INDEX_NAME = "index.json"
INDEX_VERSION = 1


def cache_dir(config_file: Path | None = None) -> Path:
    base = config_file or config_path()
    return base.parent / "covers"


class Art:
    """Covers for files, found once and remembered."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or cache_dir()
        self._lock = threading.Lock()
        self._known: dict = {}      # folder -> cover file, or None for "none"
        self._working: set = set()
        self._dirty = False
        self.load()

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
        """The record's cover: what we already know, else read the files."""
        path = self.real(path)
        asked, found = self.answered(path)
        if asked:
            return found

        for candidate in self.siblings(path):
            extracted = self._extract(candidate, remember_failure=False)
            if extracted is not None:
                return extracted
        self.remember(path.parent, None)   # asked the record, properly
        return None

    @staticmethod
    def has_picture(path: Path) -> bool:
        """Does this file carry an embedded picture at all?

        Asking ffprobe reads the header (~45 ms); having ffmpeg try and fail
        means it scans the whole file (~3 s), which is the difference between
        a library that indexes and one that grinds.
        """
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return True     # cannot ask: let ffmpeg decide
        try:
            done = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-select_streams", "v",
                    "-show_entries", "stream_disposition=attached_pic",
                    "-of", "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        try:
            streams = json.loads(done.stdout or "{}").get("streams", [])
        except json.JSONDecodeError:
            return False
        return any((s.get("disposition") or {}).get("attached_pic") for s in streams)

    def _extract(self, path: Path, remember_failure: bool = True) -> Path | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        if not self.has_picture(path):
            if remember_failure:
                self.remember(path.parent, None)
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
                self.remember(path.parent, None)   # asked and answered
            return None
        self.remember(path.parent, target)
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
                self.save()      # remember it for the next run, too
            finally:
                with self._lock:
                    self._working.discard(key)

        threading.Thread(target=work, daemon=True, name="bgst-art").start()

    def has_any(self) -> bool:
        return bool(shutil.which("ffmpeg"))

    # -- remembering across restarts -------------------------------------

    def remember(self, folder: Path, cover: Path | None) -> None:
        with self._lock:
            self._known[str(folder)] = cover
            self._dirty = True

    def load(self) -> None:
        """Read what we found last time, misses included."""
        path = self.directory / INDEX_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if raw.get("version") != INDEX_VERSION:
            return
        known = {}
        for folder, cover in (raw.get("folders") or {}).items():
            if cover is None:
                known[folder] = None
                continue
            candidate = Path(cover)
            # A cover file that has since been deleted is worth looking for
            # again; a remembered miss is not.
            if candidate.exists():
                known[folder] = candidate
        with self._lock:
            self._known.update(known)

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            payload = {
                "version": INDEX_VERSION,
                "folders": {
                    folder: (str(cover) if cover else None)
                    for folder, cover in self._known.items()
                },
            }
            self._dirty = False
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / INDEX_NAME).write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def remembered(self) -> dict:
        """Every answer we hold: folder -> cover file, or None for none."""
        with self._lock:
            return dict(self._known)

    def forget(self) -> None:
        """Start over - for when files gained art they did not have before."""
        with self._lock:
            self._known.clear()
            self._dirty = True
        self.save()
