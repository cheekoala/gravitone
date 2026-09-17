"""Playlist scheduling: songs, and the quiet (or windy, or rainy) bits between.

The engine owns the shape of a session:

    song -> gap -> song -> gap -> ...

Every gap is a random length between `gap_min` and `gap_max`. A gap is either
silence, or an ambient bed (wind, rain, tavern noise) picked at random and cut
to the gap length - `ambient_chance` decides which.

In a **party** the engine stops rolling dice and reads a timetable instead
(see `party.py`): the same shape, but every item pinned to an absolute
instant that another machine has worked out identically. Nothing is played
because the last thing finished - it is played because it is time.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from bgsoundtrack import library, party as party_module, player, playlists
from bgsoundtrack.config import Config


@dataclass(frozen=True)
class Event:
    """Something the engine did, for the UI to print."""

    kind: str  # "track" | "ambient" | "silence" | "done"
    path: Path | None = None
    duration: float | None = None
    start: float | None = None  # where an ambient bed was started from
    # What is playing when this machine has no file for it: in a party the
    # slot is kept, and named, rather than skipped.
    label: str | None = None


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
        party: party_module.Party | None = None,
        finder=None,
        now=time.time,
    ):
        self.config = config
        self.backend = backend
        # A party replaces every choice this engine would make with one
        # already written down; `finder` says which of its tracks are here.
        self.party = party
        self.finder = finder or (lambda: {})
        self._now = now
        # Which playlist is playing, and a version to notice edits by.
        self.store = store or playlists.Store(
            playlists=[library.resolve(config, None)]
        )
        self.rng = rng or random.Random()
        self.controls = controls or Controls()
        self._sleep = sleep
        self._monotonic = monotonic
        # What is sounding right now, so the volume can be changed live.
        self._playback: player.Playback | None = None
        self._playing: str | None = None

    # -- gap planning ----------------------------------------------------

    def gap_length(self) -> float:
        return self.rng.uniform(self.config.gap_min, self.config.gap_max)

    def ambient_start(self, path: Path, gap: float) -> float:
        """Where to drop into an ambient track.

        Anywhere that still leaves the whole gap covered - a two minute rain
        loop over a thirty second gap can start anywhere in its first ninety
        seconds. Without a known length (no ffprobe) we start at the top.
        """
        if not self.config.ambient_random_start:
            return 0.0
        duration = player.probe_duration(path)
        if not duration:
            return 0.0
        room = duration - gap
        if room <= 1.0:
            return 0.0
        return self.rng.uniform(0.0, room)

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

    # An unplanned ending is faded through the mixer, which is slower and
    # less exact than a filter, so it is kept short: a skip still has to feel
    # like a skip.
    INTERRUPT_FADE = 0.8

    def _cut(self, playback: player.Playback | None, level: int) -> None:
        """End a sound that was interrupted, rather than dropping it.

        Nothing scheduled this, so the player's own fade cannot help: the
        sound is walked down through the mixer and the process ended quiet.
        Where there is no mixer it simply stops, as it always did.
        """
        if playback is None:
            return
        playback.fade_out(
            min(self.config.fade, self.INTERRUPT_FADE),
            volume=level,
            sleep=self._sleep,
        )

    def _wait(self, seconds: float, playback: player.Playback | None = None) -> None:
        """Sleep in slices so skip/stop stay responsive, stopping playback early."""
        deadline = self._monotonic() + seconds
        cut = False
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            if self.controls.stopping or self.controls.take_skip():
                cut = True
                break
            if playback is not None and not playback.running:
                break
            self._sleep(min(0.2, remaining))
        if cut:
            self._cut(playback, self.config.ambient_volume)
        elif playback is not None:
            playback.stop()      # it ran its course, and faded itself out

    # -- the session -----------------------------------------------------

    def run(self, on_event=None):
        """Play until the playlist is exhausted (or forever, if config.loop).

        The track list is rebuilt whenever the playlist changes underneath us -
        switching playlist, linking, removing a track - so an edit lands on the
        next track instead of the next full pass.
        """
        emit = on_event or (lambda event: None)
        if self.party is not None:
            self._run_party(emit)
            emit(Event("done"))
            return
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
                        f"'bgst folder add <folder>' or 'bgst link <path>'"
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
            playback = player.play(
                self.backend,
                track,
                volume=self.config.volume,
                fade=self.config.fade,
                length=player.probe_duration(track),
            )
            self._hold(playback, "track")
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
                start = self.ambient_start(bed, gap)
                emit(Event("ambient", path=bed, duration=gap, start=start))
                bed_playback = player.play(
                    self.backend,
                    bed,
                    volume=self.config.ambient_volume,
                    duration=gap,
                    start=start,
                    fade=self.config.fade,
                )
                self._hold(bed_playback, "ambient")
                self._wait(gap, bed_playback)

        self._hold(None, None)
        emit(Event("done"))

    # -- playing to a timetable ------------------------------------------

    def _run_party(self, emit) -> None:
        """Play what the party says, when the party says.

        Every item is started against the wall clock and held until its
        scheduled end, however the audio itself behaves. A file that runs
        short leaves a little more quiet; one that is missing leaves quiet
        for its whole slot. Neither moves anything that comes after, which is
        what keeps two machines together without a word between them.
        """
        table = party_module.Timetable(self.party)
        here: dict = {}
        seen_version = None

        while not self.controls.stopping:
            if seen_version != self.store.version:
                here = self.finder() or {}
                seen_version = self.store.version
            elapsed = self._now() + self.config.party_offset - self.party.epoch
            found = table.at(elapsed)
            if found is None:
                break                       # the party is over
            item, into = found
            remaining = item.duration - into
            if remaining <= 0.05:
                # Landed on the seam between two items. Step over it by the
                # width of the seam, not by a fixed slice: rounding an item
                # boundary up to 50ms would start the next one that late.
                self._sleep(max(remaining, 0.001))
                continue
            path = here.get(item.member.id) if item.member else None
            playback = None
            if item.kind == "track" and path is not None:
                emit(Event("track", path=path, duration=remaining))
                playback = player.play(
                    self.backend,
                    path,
                    volume=self.config.volume,
                    duration=remaining,
                    start=into,
                    fade=self.config.fade,
                )
                self._hold(playback, "track")
            elif item.kind == "ambient" and path is not None:
                start = item.seek + into
                emit(Event("ambient", path=path, duration=remaining, start=start))
                playback = player.play(
                    self.backend,
                    path,
                    volume=self.config.ambient_volume,
                    duration=remaining,
                    start=start,
                    fade=self.config.fade,
                )
                self._hold(playback, "ambient")
            elif item.member is not None:
                # In the party, not on this machine. It still takes its turn.
                emit(Event("absent", duration=remaining, label=item.member.label))
            else:
                emit(Event("silence", duration=remaining))
            self._wait_out(remaining, playback)
        self._hold(None, None)

    def _wait_out(self, seconds: float, playback: player.Playback | None) -> None:
        """Hold a slot for its whole length, whatever the audio does.

        Unlike the free-running loop, the end of the sound is not the end of
        the item: stopping early is exactly how a party drifts apart. Skip
        means sit this one out - the sound stops, the slot does not.
        """
        deadline = self._monotonic() + seconds
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0 or self.controls.stopping:
                break
            if self.controls.take_skip() and playback is not None:
                # Sitting this one out: the sound leaves, the slot stays.
                self._cut(playback, self.config.volume)
                self._hold(None, None)
                playback = None
            self._sleep(min(0.2, remaining))
        if playback is not None:
            if self.controls.stopping:
                self._cut(playback, self.config.volume)
            else:
                playback.stop()   # its slot ended; it faded itself out

    def _hold(self, playback: player.Playback | None, kind: str | None) -> None:
        self._playback, self._playing = playback, kind

    def live_volume(self) -> bool:
        """Push the configured level at whatever is sounding right now.

        Players read their volume flag once at startup, so without this a
        slider only takes effect on the next track.
        """
        playback = self._playback
        if playback is None or self._playing is None:
            return False
        level = (
            self.config.ambient_volume
            if self._playing == "ambient"
            else self.config.volume
        )
        return playback.set_volume(level)

    def _wait_for_track(self, playback: player.Playback) -> None:
        while playback.running:
            if self.controls.stopping or self.controls.take_skip():
                self._cut(playback, self.config.volume)
                return
            self._sleep(0.2)
        playback.wait()
