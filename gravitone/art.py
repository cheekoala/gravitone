"""Album art, taken from the files themselves.

Only pictures embedded in the audio are used - no guessing from a cover.jpg
lying in the folder, which is someone else's idea of what this record looks
like. A file either carries its art or it does not.

Extraction is an ffmpeg call, so it happens once per record and the result is
written next to the other caches, along with the answer "this one has none" -
otherwise every restart would run ffmpeg over every coverless album again.

A record is a folder *and* an album tag, not a folder alone. Plenty of people
keep a whole game's music in one directory, and keying on the folder gave all
of it a single answer: whichever record happened to be read first decided for
the rest, and one coverless file at the top of the listing hid the art of
everything below it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path

from gravitone.config import AUDIO_EXTENSIONS, config_path

MAX_BYTES = 6 * 1024 * 1024
# How many tracks of a record to try before deciding it has no cover. Plenty
# of rips carry the picture on only one or two files.
SIBLINGS = 5
INDEX_NAME = "index.json"
# 2: answers are keyed by record (folder + album) rather than by folder, so
# the answers a folder-keyed run wrote are wrong here and are dropped.
INDEX_VERSION = 2
# Any of these as a video stream means a picture rides along with the audio.
# The attached_pic disposition is the usual marker, but not every container
# and tagger sets it, and a picture is a picture.
IMAGE_CODECS = {"mjpeg", "png", "bmp", "gif", "webp", "tiff", "jpeg2000"}
# What the bytes themselves say they are. Taggers get the declared type wrong
# often enough - a JPEG filed as image/png is common - and a decoder picked
# from the label then refuses a picture that is perfectly fine.
SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)
SUFFIXES = (".jpg", ".png", ".gif", ".bmp", ".webp")


def cache_dir(config_file: Path | None = None) -> Path:
    base = config_file or config_path()
    return base.parent / "covers"


class Art:
    """Covers for files, found once and remembered."""

    def __init__(self, directory: Path | None = None, reader=None):
        self.directory = directory or cache_dir()
        self._lock = threading.Lock()
        self._known: dict = {}      # record id -> cover file, or None for "none"
        self._working: set = set()
        self._dirty = False
        self.reader = reader        # tags, for telling records in a folder apart
        self.load()

    def set_reader(self, reader) -> None:
        self.reader = reader

    @staticmethod
    def real(path: Path) -> Path:
        """The file itself, not the link pointing at it."""
        try:
            return path.resolve()
        except OSError:
            return path

    def album(self, path: Path) -> str:
        """The album this track says it belongs to, or "" if it does not say.

        Only a tag counts. A guess made from the path is the folder by
        another name, and grouping by it would say nothing the folder has
        not already said.
        """
        if self.reader is None:
            return ""
        try:
            tags = self.reader.known(path)
        except Exception:
            return ""
        if not tags or tags.guessed or not tags.album:
            return ""
        return tags.album.strip().casefold()

    def album_key(self, path: Path) -> str:
        """An id for the record this track belongs to."""
        return self._key(path)

    def _key(self, path: Path) -> str:
        # A record is a folder plus an album tag. Albums share their art, so
        # one extraction still covers a whole record - but a folder holding
        # several records no longer gets one answer for all of them. Resolve
        # first, or every link in the library folder would claim the same
        # cover.
        path = self.real(path)
        return self._id(path.parent, self.album(path))

    @staticmethod
    def _id(folder: Path, album: str = "") -> str:
        seed = f"{folder}\0{album}" if album else str(folder)
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

    def answered(self, path: Path) -> tuple:
        """(have we looked, what we found). "No cover" is an answer.

        Without this, a record with no art anywhere gets ffmpeg run over it
        again every time one of its tracks plays.
        """
        key = self._key(path)
        with self._lock:
            if key in self._known:
                return (True, self._known[key])
        return (False, None)

    def cached(self, path: Path) -> Path | None:
        """The cover we already have for this track, if any."""
        key = self._key(path)
        with self._lock:
            if key in self._known:
                return self._known[key]
        for suffix in SUFFIXES:
            candidate = self.directory / f"{key}{suffix}"
            if candidate.exists():
                with self._lock:
                    self._known[key] = candidate
                return candidate
        return None

    def siblings(self, path: Path) -> list:
        """This track, then a few of its neighbours in the same folder.

        The picture often lives on one track of a record and not the rest, so
        looking only at what happens to be playing finds nothing far too
        often.
        """
        found = [path]
        album = self.album(path)
        try:
            for other in sorted(path.parent.iterdir()):
                if len(found) >= SIBLINGS:
                    break
                if other == path or other.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                # Neighbours of a *different* record are no help, and asking
                # them is how a folder of several albums ends up with one
                # answer for all of it.
                if album and self.album(other) != album:
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
            extracted = self._extract(candidate, remember_failure=False, under=path)
            if extracted is not None:
                return extracted
        self.remember(path, None)   # asked the record, properly
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
                    "-show_entries", "stream=codec_name:stream_disposition=attached_pic",
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
        return any(
            (s.get("disposition") or {}).get("attached_pic")
            or s.get("codec_name") in IMAGE_CODECS
            for s in streams
        )

    def _extract(
        self, path: Path, remember_failure: bool = True, under: Path | None = None
    ) -> Path | None:
        """Pull the picture out of one file and file it under a record.

        `under` is the track being asked about: the answer belongs to its
        record, even when the picture came off a neighbour.
        """
        owner = under if under is not None else path
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        if not self.has_picture(path):
            if remember_failure:
                self.remember(owner, None)
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        key = self._key(owner)
        cover = self._copy_picture(ffmpeg, path, key) or self._decode_picture(
            ffmpeg, path, key
        )
        if cover is None:
            if remember_failure:
                self.remember(owner, None)   # asked and answered
            return None
        self.remember(owner, cover)
        return cover

    def _copy_picture(self, ffmpeg: str, path: Path, key: str) -> Path | None:
        """Lift the picture out byte for byte, and believe the bytes.

        Decoding it means trusting the type the tagger wrote down, and a
        JPEG labelled image/png - which happens a lot - then fails to decode
        at all. Copying the stream asks no questions; the format is read off
        the bytes afterwards, and the picture is shrunk from the file we now
        hold rather than from the label.
        """
        raw = self.directory / f"{key}.raw"
        try:
            done = subprocess.run(
                [
                    ffmpeg, "-v", "error", "-y",
                    "-i", str(path),
                    "-an", "-map", "0:v:0", "-c", "copy", "-f", "image2",
                    str(raw),
                ],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            raw.unlink(missing_ok=True)
            return None
        suffix = self._sniff(raw) if done.returncode == 0 else None
        if suffix is None:
            raw.unlink(missing_ok=True)
            return None
        shrunk = self._shrink(ffmpeg, raw, key)
        if shrunk is not None:
            raw.unlink(missing_ok=True)
            return shrunk
        target = self.directory / f"{key}{suffix}"
        try:
            raw.replace(target)
        except OSError:
            raw.unlink(missing_ok=True)
            return None
        return target

    def _shrink(self, ffmpeg: str, source: Path, key: str) -> Path | None:
        """A screen-sized JPEG of a picture we already hold as a file.

        ffmpeg reads the real format off this one, so art whose declared type
        was wrong gets resized like any other.
        """
        target = self.directory / f"{key}.jpg"
        try:
            done = subprocess.run(
                [
                    ffmpeg, "-v", "error", "-y",
                    "-i", str(source),
                    "-frames:v", "1",
                    "-vf", "scale=min(600\\,iw):-1",
                    str(target),
                ],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            target.unlink(missing_ok=True)
            return None
        if done.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            return None
        return target

    def _decode_picture(self, ffmpeg: str, path: Path, key: str) -> Path | None:
        """The straight read, for a picture that will not copy out whole."""
        target = self.directory / f"{key}.jpg"
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
            target.unlink(missing_ok=True)
            return None
        if done.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            return None
        return target

    @staticmethod
    def _sniff(path: Path) -> str | None:
        """What a file really is, from its first bytes."""
        try:
            with path.open("rb") as handle:
                head = handle.read(16)
        except OSError:
            return None
        if not head:
            return None
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ".webp"
        for magic, suffix in SIGNATURES:
            if head.startswith(magic):
                return suffix
        return None

    def want(self, path: Path) -> None:
        """Find it in the background - for the track that just started."""
        path = self.real(path)
        key = self._key(path)
        with self._lock:
            if key in self._known or key in self._working:
                return
            self._working.add(key)

        def work() -> None:
            try:
                # Know which record this is before filing an answer for it,
                # or the answer lands under the folder and is orphaned the
                # moment the album tag turns up.
                if self.reader is not None:
                    try:
                        self.reader.read(path)
                    except Exception:
                        pass
                self.find(path)
                self.save()      # remember it for the next run, too
            finally:
                with self._lock:
                    self._working.discard(key)

        threading.Thread(target=work, daemon=True, name="gravitone-art").start()

    def has_any(self) -> bool:
        return bool(shutil.which("ffmpeg"))

    # -- remembering across restarts -------------------------------------

    def remember(self, path: Path, cover: Path | None) -> None:
        """File an answer under a track's record (a folder is taken as one)."""
        key = self._id(path) if path.is_dir() else self._key(path)
        with self._lock:
            self._known[key] = cover
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
        for folder, cover in (raw.get("records") or {}).items():
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
                "records": {
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
        # The extracted files go too. They are named after the record they
        # belong to, and looking again is exactly when that grouping may be
        # about to change - a stale one would be served straight back.
        try:
            for stale in self.directory.iterdir():
                if stale.suffix in SUFFIXES or stale.suffix == ".raw":
                    stale.unlink(missing_ok=True)
        except OSError:
            pass
        self.save()
