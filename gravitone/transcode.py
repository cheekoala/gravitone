"""Making a copy of the music smaller, for handing to someone else.

A bundle of 400 FLACs is fifteen gigabytes, which is not a thing you send
anybody. The same 400 tracks as MP3 are about one and a half, and for
background music behind a game nobody is going to hear the difference.

Nothing here touches the originals. A transcode reads a file and writes a new
one into a temporary folder on its way into the zip; your library is never
opened for writing.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ffmpeg's own single-file timeout. A long ambient bed at a slow setting is
# still minutes of audio, not hours.
TIMEOUT = 600


@dataclass(frozen=True)
class Preset:
    """One way of making the copy."""

    name: str
    label: str
    suffix: str | None          # None: the file is taken as it is
    args: tuple = ()
    kbps: int = 0               # roughly, for guessing the size beforehand
    art: bool = True            # whether the cover survives the conversion

    @property
    def copies(self) -> bool:
        return self.suffix is None


PRESETS = {
    "original": Preset(
        "original", "Keep original", None,
        # Whatever they are: FLAC stays FLAC, MP3 stays MP3.
    ),
    "mp3": Preset(
        "mp3", "MP3 - high (about 245 kbps)", ".mp3",
        ("-c:a", "libmp3lame", "-q:a", "0"), 245,
    ),
    "mp3-small": Preset(
        "mp3-small", "MP3 - smaller (about 115 kbps)", ".mp3",
        # Measured against real music rather than the nominal rate: LAME's
        # VBR drops well under it on anything quiet.
        ("-c:a", "libmp3lame", "-q:a", "5"), 115,
    ),
    "opus": Preset(
        # Ogg Opus carries no cover art that ffmpeg can write, so the picture
        # is dropped rather than silently corrupting the file.
        "opus", "Opus - smallest (about 96 kbps)", ".opus",
        ("-c:a", "libopus", "-b:a", "96k", "-vbr", "on"), 105, art=False,
    ),
}
DEFAULT = "original"


class TranscodeError(Exception):
    pass


def get(name: str | None) -> Preset:
    preset = PRESETS.get(name or DEFAULT)
    if preset is None:
        raise TranscodeError(
            f"unknown audio setting {name!r} - try one of: {', '.join(PRESETS)}"
        )
    return preset


def available(preset: Preset) -> bool:
    """Whether this machine can actually make that copy."""
    if preset.copies:
        return True
    if not shutil.which("ffmpeg"):
        return False
    return encoder_of(preset) in encoders()


def encoder_of(preset: Preset) -> str:
    args = list(preset.args)
    return args[args.index("-c:a") + 1] if "-c:a" in args else ""


_encoders: tuple = (0.0, frozenset())


def encoders() -> frozenset:
    """Which audio encoders this ffmpeg was built with."""
    global _encoders
    if _encoders[1]:
        return _encoders[1]
    found = set()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            done = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=20,
            )
            for line in done.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("A"):
                    found.add(parts[1])
        except (OSError, subprocess.SubprocessError):
            pass
    _encoders = (1.0, frozenset(found))
    return _encoders[1]


def choices() -> list:
    """Every setting, and whether it can be used here."""
    return [
        {
            "name": preset.name,
            "label": preset.label,
            "kbps": preset.kbps,
            "available": available(preset),
        }
        for preset in PRESETS.values()
    ]


def estimate(sizes: list, durations: list, preset: Preset) -> int:
    """Roughly how big the copy will be, before anybody commits to it.

    Bitrate times length, which is what a constant-ish encoder does. A track
    of unknown length is guessed at three minutes rather than left out, so
    the number never flatters the answer.
    """
    if preset.copies:
        return sum(sizes)
    seconds = sum(length if length > 0 else 180.0 for length in durations)
    return int(seconds * preset.kbps * 1000 / 8)


def convert(source: Path, target: Path, preset: Preset) -> Path:
    """Write `source` to `target` in the preset's format."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TranscodeError("ffmpeg is needed to convert audio, and is not installed")
    command = [ffmpeg, "-v", "error", "-y", "-i", str(source), "-map", "0:a:0"]
    if preset.art:
        # The cover rides along, copied rather than re-encoded.
        command += ["-map", "0:v:0?", "-c:v", "copy", "-disposition:v", "attached_pic"]
    else:
        command += ["-vn"]
    command += ["-map_metadata", "0"]
    if target.suffix.lower() == ".mp3":
        command += ["-id3v2_version", "3", "-write_id3v1", "1"]
    command += list(preset.args) + [str(target)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError(f"{source.name} took too long to convert") from exc
    except OSError as exc:
        raise TranscodeError(f"could not run ffmpeg: {exc}") from exc
    if done.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        detail = (done.stderr or "").strip().splitlines()
        raise TranscodeError(
            f"{source.name}: {detail[-1] if detail else 'ffmpeg refused it'}"
        )
    return target


def renamed(name: str, preset: Preset) -> str:
    """What a track is called once converted."""
    if preset.copies:
        return name
    return str(Path(name).with_suffix(preset.suffix))
