"""Playlist scheduling: songs, and the quiet (or windy, or rainy) bits between.

The engine owns the shape of a session:

    song -> gap -> song -> gap -> ...

Every gap is a random length between `gap_min` and `gap_max`. A gap is either
silence, or an ambient bed (wind, rain, tavern noise) picked at random and cut
to the gap length - `ambient_chance` decides which.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from bgsoundtrack import library, player, playlists
from bgsoundtrack.config import Config


@dataclass(frozen=True)
class Event:
    """Something the engine did, for the UI to print."""

    kind: str  # "track" | "ambient" | "silence" | "done"
    path: Path | None = None
    duration: float | None = None


class Controls:
    """Thread-safe flags the UI sets while the engine runs."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._skip = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self._skip.set()

    def skip(self) -> None:
        self._skip.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def take_skip(self) -> bool:
        if self._skip.is_set():
            self._skip.clear()
            return True
        return False


def shuffled_cycle(tracks: list[Path], rng: random.Random) -> list[Path]:
    """One shuffled pass. Kept separate so 'no repeats until every track has
    played' is easy to reason about (and to test)."""
    order = list(tracks)
    rng.shuffle(order)
    return order


class Engine:
    def __init__(
        self,
        config: Config,
        backend: player.Backend,
        rng: random.Random | None = None,
        controls: Controls | None = None,
        sleep=time.sleep,
        monotonic=time.monotonic,
        store: playlists.Store | None = None,
    ):
        self.config = config
        self.backend = backend
        # Which playlist is playing, and a version to notice edits by.
        self.store = store or playlists.Store(
            playlists=[library.resolve(config, None)]
        )
        self.rng = rng or random.Random()
        self.controls = controls or Controls()
        self._sleep = sleep
        self._monotonic = monotonic

    # -- gap planning ----------------------------------------------------

    def gap_length(self) -> float:
        return self.rng.uniform(self.config.gap_min, self.config.gap_max)

    def pick_ambient(self, ambient: list[Path], gap: float) -> Path | None:
        """Ambience for this gap, or None for plain silence."""
        if not ambient:
            return None
        if gap < self.config.ambient_min_tail:
            return None
        if self.rng.random() >= self.config.ambient_chance:
            return None
        return self.rng.choice(ambient)

    # -- waiting ---------------------------------------------------------

    def _wait(self, seconds: float, playback: player.Playback | None = None) -> None:
        """Sleep in slices so skip/stop stay responsive, stopping playback early."""
        deadline = self._monotonic() + seconds
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            if self.controls.stopping or self.controls.take_skip():
                break
            if playback is not None and not playback.running:
                break
            self._sleep(min(0.2, remaining))
        if playback is not None:
            playback.stop()

    # -- the session -----------------------------------------------------

    def run(self, on_event=None):
        """Play until the playlist is exhausted (or forever, if config.loop).

        The track list is rebuilt whenever the playlist changes underneath us -
        switching playlist, linking, removing a track - so an edit lands on the
        next track instead of the next full pass.
        """
        emit = on_event or (lambda event: None)
        music: list[Path] = []
        ambient: list[Path] = []
        queue: list[Path] = []
        seen_version = None
        started = False

        while not self.controls.stopping:
            if seen_version != self.store.version:
                playlist = self.store.current()
                music = library.tracks(self.config, "music", playlist)
                ambient = library.tracks(self.config, "ambient", playlist)
                seen_version = self.store.version
                queue = []
                if not music:
                    raise RuntimeError(
                        f"nothing to play in {playlist.name} - add music with "
                        f"'bgst link <path>' or 'bgst source add <folder>'"
                    )

            if not queue:
                if started and not self.config.loop:
                    break
                queue = (
                    shuffled_cycle(music, self.rng) if self.config.shuffle else list(music)
                )
                started = True

            track = queue.pop(0)
            emit(Event("track", path=track))
            playback = player.play(self.backend, track, volume=self.config.volume)
            self._wait_for_track(playback)
            if self.controls.stopping:
                break

            gap = self.gap_length()
            if gap <= 0:
                continue
            bed = self.pick_ambient(ambient, gap)
            if bed is None:
                emit(Event("silence", duration=gap))
                self._wait(gap)
            else:
                emit(Event("ambient", path=bed, duration=gap))
                bed_playback = player.play(
                    self.backend, bed, volume=self.config.ambient_volume, duration=gap
                )
                self._wait(gap, bed_playback)

        emit(Event("done"))

    def _wait_for_track(self, playback: player.Playback) -> None:
        while playback.running:
            if self.controls.stopping or self.controls.take_skip():
                playback.stop()
                return
            self._sleep(0.2)
        playback.wait()
