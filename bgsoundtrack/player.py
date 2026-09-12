"""Audio playback via whatever command-line player is installed.

No Python audio dependencies: we shell out to ffplay / mpv / afplay / cvlc,
one process per track, which also gives us a free 'stop' (kill the process).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bgsoundtrack import mixer


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


def available() -> list[str]:
    return [name for name in CANDIDATES if shutil.which(name)]


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
) -> Playback:
    command = backend.command(path, volume, duration, start)
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
