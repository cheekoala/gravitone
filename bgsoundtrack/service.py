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

from bgsoundtrack import config as config_module, engine, library, player
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

    def __init__(self, config: Config, config_path: Path | None = None):
        self.config = config
        self.config_path = config_path
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._controls: engine.Controls | None = None
        self._now: NowPlaying | None = None
        self._error: str | None = None
        self._history: list[str] = []
        self._backend: player.Backend | None = None

    # -- state -----------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def snapshot(self) -> dict:
        now = self._now
        return {
            "running": self.running,
            "error": self._error,
            "backend": self._backend.name if self._backend else None,
            "players": player.available(),
            "history": list(self._history[-12:]),
            "now": None
            if now is None
            else {
                "kind": now.kind,
                "name": now.name,
                "duration": now.duration,
                "elapsed": round(now.elapsed(), 2),
            },
            "config": self.config.to_dict(),
            "library": {
                section: [
                    {
                        "name": entry.name,
                        "target": str(entry.resolve()),
                    }
                    for entry in library.tracks(self.config, section)
                ]
                for section in library.SECTIONS
            },
            "broken": {
                section: [entry.name for entry in library.broken(self.config, section)]
                for section in library.SECTIONS
            },
        }

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
            )
            self._controls = controls
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

    def update_config(self, values: dict) -> None:
        """Apply settings and persist them. Changes land on the next gap."""
        for key, value in values.items():
            config_module.set_value(self.config, key, value)
        self.config.validate()
        config_module.save(self.config, self.config_path)

    def link(self, path: str, section: str = "music", relative: bool = False) -> dict:
        library.init(self.config)
        result = library.link(
            self.config, [Path(path).expanduser()], section=section, relative=relative
        )
        return {
            "linked": [p.name for p in result.linked],
            "skipped": [[p.name, reason] for p, reason in result.skipped],
        }

    def unlink(self, name: str, section: str = "music") -> list[str]:
        return [p.name for p in library.unlink(self.config, [name], section=section)]

    def prune(self) -> list[str]:
        return [p.name for p in library.prune(self.config)]
