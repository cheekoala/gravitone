"""Audio playback via whatever command-line player is installed.

No Python audio dependencies: we shell out to ffplay / mpv / afplay / cvlc,
one process per track, which also gives us a free 'stop' (kill the process).

Sound does not appear and vanish; it arrives and leaves. Where the player
takes a filter (ffplay) the fade is part of the decode, which is exact and
costs nothing. Where the sound has to stop at a moment nobody planned - you
pressed skip - there is no filter to schedule, so the fade is walked down
through the system mixer instead and the process is ended quiet.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from gravitone import mixer


class PlaybackError(Exception):
    pass


@dataclass(frozen=True)
class Backend:
    name: str
    executable: str

    def command(
        self,
        path: Path,
        volume: int,
        duration: float | None,
        start: float | None = None,
        fade: float = 0.0,
        length: float | None = None,
    ) -> list[str]:
        if self.name == "ffplay":
            cmd = [
                self.executable,
                "-nodisp",
                "-autoexit",
                "-hide_banner",
                "-loglevel",
                "error",
                "-volume",
                str(volume),
            ]
            if start:
                cmd += ["-ss", f"{start:.3f}"]
            if duration is not None:
                cmd += ["-t", f"{duration:.3f}"]
            filters = self.fades(fade, duration, start, length)
            if filters:
                cmd += ["-af", filters]
            return cmd + [str(path)]
        if self.name == "mpv":
            cmd = [
                self.executable,
                "--no-video",
                "--really-quiet",
                "--no-terminal",
                f"--volume={volume}",
            ]
            if start:
                cmd.append(f"--start={start:.3f}")
            if duration is not None:
                cmd.append(f"--length={duration:.3f}")
            return cmd + [str(path)]
        if self.name == "afplay":
            # afplay has no seek flag, so a start offset is simply ignored.
            cmd = [self.executable, "-v", f"{volume / 100:.3f}"]
            if duration is not None:
                cmd += ["-t", f"{duration:.3f}"]
            return cmd + [str(path)]
        if self.name == "cvlc":
            cmd = [
                self.executable,
                "--intf",
                "dummy",
                "--quiet",
                "--no-video",
                "--play-and-exit",
                f"--gain={volume / 100:.3f}",
            ]
            if start:
                cmd += ["--start-time", f"{start:.0f}"]
            if duration is not None:
                cmd += ["--run-time", f"{duration:.0f}"]
            return cmd + [str(path)]
        raise PlaybackError(f"unsupported backend {self.name!r}")

    @staticmethod
    def fades(
        fade: float,
        duration: float | None,
        start: float | None,
        length: float | None,
    ) -> str:
        """An ffmpeg filter that eases this stretch of audio in and out.

        The fade out needs to know when the end is. That is the `-t` we
        asked for, or what is left of a track we have a length for; without
        either, the sound still arrives gently and simply stops when the file
        does.
        """
        if fade <= 0:
            return ""
        played = duration
        if played is None and length:
            played = length - (start or 0.0)
        span = fade
        if played is not None:
            if played <= 0.5:
                return ""
            # Never let the two fades meet in the middle: a short bed would
            # otherwise be all fade and no sound.
            span = min(fade, played / 3)
        parts = [f"afade=t=in:st=0:d={span:.3f}"]
        if played is not None:
            parts.append(f"afade=t=out:st={played - span:.3f}:d={span:.3f}")
        return ",".join(parts)

    @property
    def fades_itself(self) -> bool:
        """Whether the player can fade from a filter, rather than the mixer."""
        return self.name == "ffplay"

    @property
    def supports_duration(self) -> bool:
        return True


# Ordered by preference: ffplay and mpv give us volume + hard duration limits.
CANDIDATES = ("ffplay", "mpv", "afplay", "cvlc", "vlc")


def detect(preferred: str | None = None) -> Backend:
    """Find an installed player, or raise with an install hint."""
    names = (preferred,) if preferred else CANDIDATES
    for name in names:
        executable = shutil.which(name)
        if executable:
            # `vlc` is driven with the same flags as `cvlc`.
            return Backend(name="cvlc" if name == "vlc" else name, executable=executable)
    if preferred:
        raise PlaybackError(f"player {preferred!r} not found on PATH")
    raise PlaybackError(
        "no audio player found. Install one of: ffmpeg (ffplay), mpv, or vlc."
    )


_available: tuple = (0.0, [])


def available(ttl: float = 15.0) -> list[str]:
    """Which players are installed. Cached - this is on a one-second poll."""
    global _available
    now = time.monotonic()
    if now - _available[0] < ttl:
        return list(_available[1])
    found = [name for name in CANDIDATES if shutil.which(name)]
    _available = (now, found)
    return list(found)


class Playback:
    """A single track playing in a child process."""

    def __init__(self, process: subprocess.Popen, path: Path):
        self.process = process
        self.path = path

    def set_volume(self, volume: int) -> bool:
        """Change the volume of this playing track, if the desktop allows it.

        The player's own `-volume` flag is read once at startup, so this goes
        through the system mixer instead. False means nothing could be
        changed and the new level will apply from the next track.
        """
        if self.process.poll() is not None:
            return False
        return mixer.set_volume(self.process.pid, volume)

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def fade_out(
        self, seconds: float, volume: int = 100, steps: int = 8, sleep=time.sleep
    ) -> bool:
        """Walk the sound down, then stop it.

        For an ending nobody scheduled - skip, or stop - there is no filter to
        arrange it in advance, so the system mixer is stepped down instead.
        False means there was no mixer to do it with and the sound stopped
        where it was.
        """
        if seconds <= 0 or self.process.poll() is not None:
            self.stop()
            return False
        faded = False
        for index in range(steps - 1, -1, -1):
            if not self.set_volume(round(volume * index / steps)):
                break           # no mixer here: nothing to fade with
            faded = True
            sleep(seconds / steps)
        self.stop()
        return faded

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    @property
    def running(self) -> bool:
        return self.process.poll() is None


def play(
    backend: Backend,
    path: Path,
    volume: int = 70,
    duration: float | None = None,
    start: float | None = None,
    fade: float = 0.0,
    length: float | None = None,
) -> Playback:
    command = backend.command(path, volume, duration, start, fade, length)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=mixer.environment(),
        )
    except OSError as exc:
        raise PlaybackError(f"could not start {backend.name}: {exc}") from exc
    return Playback(process, path)


_durations: dict = {}


def probe_duration(path: Path) -> float | None:
    """Track length in seconds via ffprobe, or None if unavailable.

    Cached per file (by size and mtime), since picking a random start point
    asks for the same handful of ambient tracks over and over.
    """
    try:
        info = path.stat()
        key = (str(path), info.st_size, int(info.st_mtime))
    except OSError:
        return None
    if key in _durations:
        return _durations[key]

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        value = float(out.stdout.strip())
    except ValueError:
        return None
    value = value if value > 0 else None
    _durations[key] = value
    return value
