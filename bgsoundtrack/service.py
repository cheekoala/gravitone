"""A player session the UI can drive: start, skip, stop, and ask what's on.

The engine is a blocking loop, so it runs on its own thread and reports
through this object. Everything here is safe to call from a request handler.
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from bgsoundtrack import (
    __version__,
    art as art_module,
    config as config_module,
    engine,
    library,
    instance,
    picker,
    player,
    playlists,
    tags,
    transfer,
)
from bgsoundtrack.config import Config


@dataclass
class NowPlaying:
    kind: str  # "track" | "ambient" | "silence"
    name: str
    path: str | None = None
    duration: float | None = None  # None until ffprobe answers, for tracks
    started: float = field(default_factory=time.monotonic)

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started)


class Session:
    """Owns at most one running engine thread."""

    def __init__(
        self,
        config: Config,
        config_path: Path | None = None,
        store: playlists.Store | None = None,
    ):
        self.config = config
        self.config_path = config_path
        self.store = store or playlists.load(config, config_path)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._controls: engine.Controls | None = None
        self._now: NowPlaying | None = None
        self._error: str | None = None
        self._history: list[str] = []
        self._backend: player.Backend | None = None
        self._picker_available: bool | None = None
        self._runner: engine.Engine | None = None
        self._tags = tags.Reader(tags.cache_path(config_path))
        self._tags_pending = 0
        # Bumped when a background read finishes, so the UI knows the table
        # it is showing has been overtaken by real tags.
        self._tags_version = 0
        self._counts: tuple | None = None   # memo, keyed by what can change it
        self._album_map: tuple | None = None
        self._art = art_module.Art(
            art_module.cache_dir(self.config_path), reader=self._tags
        )
        self._art_pending = 0
        self._started = time.time()
        self._started = time.time()
        library.configure_index(self._index_path())

    # -- state -----------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _index_path(self) -> Path:
        base = self.config_path or config_module.config_path()
        return base.parent / "index.json"

    def _library_state(self) -> dict:
        """Counts, broken links and folder sizes - worked out once per change.

        This is polled every second, so it must not walk anything; it is
        recomputed only when the playlists, the folder index or the library
        folders have actually moved.
        """
        key = (
            self.store.version,
            library.SCANNER.version,
            self.playlist.id,
            len(self.playlist.excluded),
            tuple(self.playlist.music_sources),
            tuple(self.playlist.ambient_sources),
        )
        if self._counts and self._counts[0] == key:
            return self._counts[1]

        value = {
            "counts": {
                section: len(library.entries(self.config, section, self.playlist))
                for section in library.SECTIONS
            },
            "broken": {
                section: [
                    entry.name
                    for entry in library.broken(self.config, section, self.playlist)
                ]
                for section in library.SECTIONS
            },
            "sources": {
                section: self.sources(section) for section in library.SECTIONS
            },
        }
        self._counts = (key, value)
        return value

    def snapshot(self) -> dict:
        """Small enough to poll once a second; track lists live in library()."""
        now = self._now
        return {
            "running": self.running,
            "error": self._error,
            "backend": self._backend.name if self._backend else None,
            "players": player.available(),
            "picker": self.picker_available,
            "version": __version__,
            # Updated underneath ourselves? Then this process is serving a
            # newer page than the code it is running.
            "stale": instance.process_is_stale(),
            "sort": self.config.sort_by,
            "sorts": list(library.SORTS),
            "tags_pending": self._tags_pending,
            "tags_version": self._tags_version,
            "art_pending": self._art_pending,
            "started": self._started,
            "pid": os.getpid(),
            "history": [self._label(Path(item)) for item in self._history[-12:]],
            "now": None if now is None else self._describe(now),
            "hidden": self.config.hide_gaps,
            "config": self.config.to_dict(),
            **self._library_state(),
            "indexing": library.SCANNER.pending,
            "playlists": [
                {
                    "id": item.id,
                    "name": item.name,
                    "active": item.id == self.store.active,
                    "default": item.is_default,
                    "sources": len(item.music_sources) + len(item.ambient_sources),
                    "removed": len(item.excluded),
                }
                for item in self.store.playlists
            ],
            "playlist": self.playlist.id,
            "removed": [
                {"target": target, "name": Path(target).name}
                for target in self.playlist.excluded
            ],
        }

    @staticmethod
    def _real(path: Path) -> Path:
        """A playing track may be a link; its tags live against the file."""
        try:
            return Path(path).resolve()
        except OSError:
            return Path(path)

    def _label(self, path: Path) -> str:
        """What to call a track: its tags if we have them, else its file name."""
        known = self._tags.known(self._real(path))
        if known.guessed or not known.title:
            return Path(path).name
        return f"{known.title} — {known.artist}" if known.artist else known.title

    def _describe(self, now: NowPlaying) -> dict:
        """What the UI may know about what is playing.

        In hidden mode the gap's length and progress never leave this process
        - a countdown you can read is not a silence you can sink into.
        """
        hide = self.config.hide_gaps and now.kind != "track"
        real = self._real(now.path) if now.path else None
        known = self._tags.known(real) if real else None
        cover = self._art.cached(real) if real else None
        return {
            "kind": now.kind,
            "name": now.name,
            "path": now.path,
            # The player says what the tags say, and falls back to the file.
            "label": self._label(Path(now.path)) if now.path else now.name,
            "title": known.title if known and not known.guessed else "",
            "artist": known.artist if known else "",
            "album": known.album if known else "",
            "art": bool(cover),
            "album_id": self._art.album_key(real) if real else "",
            "duration": None if hide else now.duration,
            "elapsed": None if hide else round(now.elapsed(), 2),
        }

    @property
    def playlist(self) -> playlists.Playlist:
        return self.store.current()

    def library(self, section: str) -> dict:
        """The full track list of one section - fetched on demand."""
        found = library.entries(
            self.config, section, self.playlist, reader=self._tags
        )
        # The table shows title, artist, album and length whatever the sort,
        # so the real tags are always worth fetching in the background.
        self._warm_tags([entry.target for entry in found])
        return {
            "section": section,
            "tracks": [
                {
                    "name": entry.name,
                    "target": str(entry.target),
                    "origin": entry.origin,
                    "source": str(entry.source) if entry.source else None,
                    **self._tag_fields(entry.target),
                    "art": bool(self._art.cached(entry.target)),
                    "album_id": self._art.album_key(entry.target),
                }
                for entry in found
            ],
            "broken": [
                entry.name for entry in library.broken(self.config, section, self.playlist)
            ],
        }

    def sources(self, section: str) -> list[dict]:
        listed = []
        for directory in self.playlist.sources(section):
            listed.append(
                {
                    "path": str(directory),
                    "exists": directory.is_dir(),
                    # From the index; a folder nobody has walked yet reads 0
                    # until the worker gets to it, rather than blocking here.
                    "count": len(library.scan(directory, block_if_unknown=False)),
                }
            )
        return listed

    def _tag_fields(self, target: Path) -> dict:
        """Whatever we can say about a track without stopping to probe it."""
        known = self._tags.known(target)
        return {
            "title": known.title,
            "artist": known.artist,
            "album": known.album,
            "track": known.track,
            "duration": known.duration,
            "guessed": known.guessed,
        }

    def _warm_tags(self, targets: list) -> None:
        """Read the real tags in the background; listings never wait on it."""
        waiting = self._tags.pending(targets)
        self._tags_pending = len(waiting)
        if not waiting or getattr(self, "_tag_thread", None) and self._tag_thread.is_alive():
            return

        def work() -> None:
            def progress(done, total):
                self._tags_pending = max(0, total - done)

            read = self._tags.read_all(waiting, progress)
            self._tags_pending = 0
            if read:
                self._tags_version += 1
        # Bumped when a background read finishes, so the UI knows the table
        # it is showing has been overtaken by real tags.
        self._tags_version = 0
        self._counts: tuple | None = None   # memo, keyed by what can change it
        self._album_map: tuple | None = None
        self._art = art_module.Art(
            art_module.cache_dir(self.config_path), reader=self._tags
        )
        self._art_pending = 0
        self._started = time.time()
        self._started = time.time()
        library.configure_index(self._index_path())

        self._tag_thread = threading.Thread(target=work, daemon=True, name="bgst-tags")
        self._tag_thread.start()

    @property
    def picker_available(self) -> bool:
        """Whether this machine can show a native file chooser (probed once)."""
        if self._picker_available is None:
            self._picker_available = picker.available()
        return self._picker_available

    # -- transport -------------------------------------------------------

    def start(self, seed: int | None = None) -> None:
        with self._lock:
            if self.running:
                return
            self._error = None
            try:
                self._backend = player.detect()
            except player.PlaybackError as exc:
                self._error = str(exc)
                raise
            controls = engine.Controls()
            runner = engine.Engine(
                self.config,
                self._backend,
                rng=random.Random(seed) if seed is not None else random.Random(),
                controls=controls,
                store=self.store,
            )
            self._controls = controls
            self._runner = runner
            self._thread = threading.Thread(
                target=self._run, args=(runner,), daemon=True, name="bgst-engine"
            )
            self._thread.start()

    def _run(self, runner: engine.Engine) -> None:
        try:
            runner.run(self._on_event)
        except Exception as exc:  # surfaced in the UI rather than lost in a thread
            self._error = str(exc)
        finally:
            self._now = None

    def _on_event(self, event: engine.Event) -> None:
        if event.kind == "done":
            self._now = None
            return
        name = event.path.name if event.path else "silence"
        self._now = NowPlaying(
            kind=event.kind,
            name=name,
            path=str(event.path) if event.path else None,
            duration=event.duration,
        )
        if event.kind == "track":
            # Keep the path: the label is worked out when it is shown, so a
            # tag read that lands a second later fixes the history too.
            self._history.append(str(event.path))
            self._probe_async(self._real(event.path), self._now)
        if event.path:
            self._art.want(event.path)   # resolves the link itself

    def _probe_async(self, path: Path, entry: NowPlaying) -> None:
        """Read this track's length and tags in the background.

        Without this, a track only had a title and a cover if you had opened
        the library table at some point - the player would sit there showing
        a file name for music it could perfectly well have read.
        """

        def work() -> None:
            duration = player.probe_duration(path)
            if duration is not None and self._now is entry:
                entry.duration = duration
            before = self._tags.cached(path)
            known = self._tags.read(path)
            if before is None or before != known:
                self._tags.save()
                self._tags_version += 1

        threading.Thread(target=work, daemon=True).start()

    def skip(self) -> None:
        if self._controls:
            self._controls.skip()

    def stop(self) -> None:
        controls = self._controls
        if controls:
            controls.stop()
        thread = self._thread
        if thread:
            thread.join(timeout=5)
        self._now = None

    def toggle(self) -> None:
        if self.running:
            self.stop()
        else:
            self.start()

    # -- settings & library ---------------------------------------------

    def update_config(self, values: dict) -> bool:
        """Apply settings and persist them. Changes land on the next gap.

        Volume is the exception: it is pushed at the playing track straight
        away. Returns whether that worked - on a desktop without a usable
        mixer it only applies from the next track.
        """
        for key, value in values.items():
            config_module.set_value(self.config, key, value)
        self.config.validate()
        config_module.save(self.config, self.config_path)
        if self._runner and {"volume", "ambient_volume"} & set(values):
            return self._runner.live_volume()
        return True

    def link(self, path: str, section: str = "music", relative: bool = False) -> dict:
        library.init(self.config, self.playlist)
        result = library.link(
            self.config,
            [Path(path).expanduser()],
            section=section,
            relative=relative,
            playlist=self.playlist,
        )
        self.store.touch()
        return {
            "linked": [p.name for p in result.linked],
            "skipped": [[p.name, reason] for p, reason in result.skipped],
        }

    def unlink(self, name: str, section: str = "music") -> list[str]:
        removed = library.unlink(
            self.config, [name], section=section, playlist=self.playlist
        )
        self.store.touch()
        return [p.name for p in removed]

    def remove_track(self, name: str, section: str = "music") -> dict:
        """Take a track out of this playlist - unlink it, or remember it as
        removed when it comes from a source folder."""
        how, target = library.remove_track(
            self.config, name, section=section, playlist=self.playlist
        )
        self.store.save()
        self.store.touch()
        return {"how": how, "target": str(target), "name": name}

    def restore_track(self, target: str) -> str:
        path = library.restore_track(self.config, target, playlist=self.playlist)
        self.store.save()
        self.store.touch()
        return str(path)

    def ban(self, expected: str | None = None) -> dict:
        """Skip this track and take it out of the playlist, in one go.

        `expected` is the track the caller meant: a confirmation can arrive
        after the music has moved on, and banning whatever happens to be
        playing by then is not what anyone asked for.
        """
        now = self._now
        if now is None or not now.path:
            raise library.LibraryError("nothing is playing")
        if expected and expected != now.name:
            raise library.LibraryError(
                f"{expected} is no longer playing - nothing was banned"
            )
        section = "ambient" if now.kind == "ambient" else "music"
        result = self.remove_track(Path(now.path).name, section)
        self.skip()
        return result

    def prune(self) -> list[str]:
        return [p.name for p in library.prune(self.config, playlist=self.playlist)]

    def add_source(self, path: str, section: str = "music") -> str:
        added = library.add_source(
            self.config, Path(path), section=section, playlist=self.playlist
        )
        self.store.save()
        self.store.touch()
        return str(added)

    def remove_source(self, path: str, section: str = "music") -> str:
        removed = library.remove_source(
            self.config, Path(path), section=section, playlist=self.playlist
        )
        self.store.save()
        self.store.touch()
        return str(removed)

    # -- playlists -------------------------------------------------------

    def select_playlist(self, identifier: str) -> str:
        playlist = self.store.select(identifier)
        self.store.save()
        library.init(self.config, playlist)
        return playlist.id

    def add_playlist(self, name: str, source: str | None = None) -> str:
        playlist = self.store.add(name)
        library.init(self.config, playlist)
        if source:
            try:
                library.add_source(self.config, Path(source), playlist=playlist)
            except library.LibraryError:
                self.store.remove(playlist.id)
                raise
        self.store.save()
        self.store.touch()
        return playlist.id

    def rename_playlist(self, identifier: str, name: str) -> str:
        playlist = self.store.rename(identifier, name)
        self.store.save()
        return playlist.id

    def remove_playlist(self, identifier: str) -> str:
        playlist = self.store.remove(identifier)
        self.store.save()
        self.store.touch()
        return playlist.id

    # -- covers ----------------------------------------------------------

    def _albums(self) -> dict:
        """album id -> a track of that record, for the playlist on show.

        Built once per change. Looking this up by walking the whole library
        on every image request is what made a page of thumbnails crawl.
        """
        key = (
            self.store.version,
            library.SCANNER.version,
            self.playlist.id,
            len(self.playlist.excluded),
            # Which record a track belongs to is an album tag, so a tag that
            # has just been read can move it to a different one.
            self._tags_version,
        )
        if getattr(self, "_album_map", None) and self._album_map[0] == key:
            return self._album_map[1]
        mapping = {}
        for section in library.SECTIONS:
            for entry in library.entries(self.config, section, self.playlist):
                mapping.setdefault(self._art.album_key(entry.target), entry.target)
        self._album_map = (key, mapping)
        return mapping

    def cover(self, album: str) -> Path | None:
        """The cover for one record of this playlist, and only such a record."""
        track = self._albums().get(album)
        if track is None:
            return None
        return self._art.cached(track) or self._art.find(track)

    def cover_for_track(self, track: str) -> Path | None:
        path = self._real(Path(track).expanduser())
        return self.cover(self._art.album_key(path))

    def forget_covers(self) -> int:
        """Look again for everything - after tagging files that had none."""
        self._art.forget()
        self._tags_version += 1
        return self.find_covers()

    def find_covers(self) -> int:
        """Go looking for art for everything in this playlist, in the
        background. Returns how many tracks it has to work through; that
        falls to one job per record as soon as their tags are known.
        """
        if self._art_pending:
            return 0
        tracks = [
            entry.target
            for section in library.SECTIONS
            for entry in library.entries(self.config, section, self.playlist)
        ]
        # A track whose tags are unread has no known record yet, so it counts
        # as a job of its own until they are read; everything else is one job
        # per record, and records already answered are no job at all.
        unread = set(self._tags.pending(tracks))
        wanted, seen = [], set()
        for path in tracks:
            unknown = path in unread
            key = str(path) if unknown else self._art.album_key(path)
            if key in seen or (not unknown and self._art.answered(path)[0]):
                continue
            seen.add(key)
            wanted.append(path)
        if not wanted:
            return 0

        def work() -> None:
            try:
                # Which record a track belongs to is an album tag, so read the
                # tags before grouping - otherwise a folder holding several
                # albums looks like one record and answers as one.
                todo = self._tags.pending(tracks)
                if todo:
                    self._art_pending = len(todo)
                    self._tags.read_all(
                        todo,
                        on_progress=lambda done, total: setattr(
                            self, "_art_pending", max(1, total - done)
                        ),
                    )
                    self._tags.save()
                    self._tags.forget_memo()
                wanted, seen = [], set()
                for path in tracks:
                    record = self._art.album_key(path)
                    if record in seen or self._art.answered(path)[0]:
                        continue
                    seen.add(record)
                    wanted.append(path)
                self._art_pending = len(wanted)
                found = 0
                for path in wanted:
                    try:
                        hit = self._art.find(path)
                    except Exception:
                        hit = None      # one unreadable file is not the scan
                    if hit:
                        found += 1
                        if found % 5 == 0:
                            # Let the thumbnails appear as they are found.
                            self._art.save()
                            self._tags_version += 1
                    self._art_pending = max(0, self._art_pending - 1)
            finally:
                # Whatever happened, the scan is over: leaving this set would
                # turn every later "find art" into a silent no-op.
                self._art_pending = 0
                self._art.save()
                self._tags_version += 1      # the table has new thumbnails

        self._art_pending = len(wanted)
        threading.Thread(target=work, daemon=True, name="bgst-art-scan").start()
        return len(wanted)

    def pick(
        self, kind: str = "folder", title: str = "Choose a folder", suggested: str = ""
    ) -> picker.PickResult:
        return picker.pick(kind, title, suggested=suggested)

    # -- export & import -------------------------------------------------

    def export(self, path: str, kind: str = "json", only: list | None = None) -> dict:
        """Write the library out. `kind` is json, csv or bundle."""
        target = Path(path).expanduser()
        if kind == "bundle":
            report = transfer.export_bundle(self.config, self.store, target, only)
        else:
            report = transfer.export(self.config, self.store, target, only, fmt=kind)
        return {
            "path": str(report.path),
            "tracks": report.tracks,
            "total": report.total,
            "folders": report.folders,
            "playlists": report.playlists,
            "skipped": report.skipped,
            "bytes": report.path.stat().st_size if report.path.exists() else 0,
        }

    def import_file(self, path: str, name: str | None = None) -> dict:
        source = Path(path).expanduser()
        if not source.exists():
            raise library.LibraryError(f"no such file: {source}")
        if transfer.looks_like_bundle(source):
            report = transfer.import_bundle(self.config, self.store, source, name)
        else:
            report = transfer.import_manifest(self.config, self.store, source, name)
        self.store.touch()
        return {
            "playlists": report.playlists,
            "tracks": report.tracks,
            "folders": report.folders,
            "skipped": report.skipped,
            "path": str(report.path) if report.path else None,
        }
