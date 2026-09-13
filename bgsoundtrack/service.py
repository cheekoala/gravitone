"""A player session the UI can drive: start, skip, stop, and ask what's on.

The engine is a blocking loop, so it runs on its own thread and reports
through this object. Everything here is safe to call from a request handler.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from bgsoundtrack import (
    __version__,
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

    # -- state -----------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

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
            "history": list(self._history[-12:]),
            "now": None if now is None else self._describe(now),
            "hidden": self.config.hide_gaps,
            "config": self.config.to_dict(),
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

    def _describe(self, now: NowPlaying) -> dict:
        """What the UI may know about what is playing.

        In hidden mode the gap's length and progress never leave this process
        - a countdown you can read is not a silence you can sink into.
        """
        hide = self.config.hide_gaps and now.kind != "track"
        return {
            "kind": now.kind,
            "name": now.name,
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
            exists = directory.is_dir()
            listed.append(
                {
                    "path": str(directory),
                    "exists": exists,
                    "count": len(library.scan(directory)) if exists else 0,
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
            self._history.append(name)
            self._probe_async(event.path, self._now)

    def _probe_async(self, path: Path, entry: NowPlaying) -> None:
        """Fill in the track length in the background - ffprobe must not
        delay playback, and it may not be installed at all."""

        def work() -> None:
            duration = player.probe_duration(path)
            if duration is not None and self._now is entry:
                entry.duration = duration

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
