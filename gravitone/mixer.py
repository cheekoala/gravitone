"""Live volume through the system mixer (PulseAudio / PipeWire).

ffplay's `-volume` is read once at startup, so moving a slider in the UI does
nothing to the song already playing - it only affects the next one. On a
PulseAudio or PipeWire desktop (Fedora, Plasma, GNOME, ...) the fix is to set
the volume on the *stream* instead, which is the same knob the desktop's own
mixer moves, and it takes effect immediately.

Everything here degrades to a no-op: without `pactl` we simply keep passing
`-volume` to the player, which is what happened before.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

# Shows up in the desktop mixer as "gravitone" instead of "ffplay", so per-app
# volume set there sticks to us rather than to whichever player we spawned.
STREAM_ENV = {
    "PULSE_PROP_application.name": "gravitone",
    "PULSE_PROP_application.icon_name": "multimedia-audio-player",
    "PULSE_PROP_media.role": "music",
}


def environment() -> dict:
    """Environment for a player process, tagged for the system mixer."""
    env = dict(os.environ)
    env.update(STREAM_ENV)
    return env


def available() -> bool:
    return shutil.which("pactl") is not None


def _pactl(*args: str, capture: bool = False):
    executable = shutil.which("pactl")
    if not executable:
        return None
    try:
        done = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout if capture else True


def sink_inputs_for(pid: int) -> list[str]:
    """Stream ids belonging to a process (and its children)."""
    raw = _pactl("-f", "json", "list", "sink-inputs", capture=True)
    if not raw:
        return []
    try:
        streams = json.loads(raw)
    except json.JSONDecodeError:
        return []
    found = []
    for stream in streams:
        properties = stream.get("properties", {}) or {}
        owner = properties.get("application.process.id")
        try:
            if owner is not None and int(owner) == pid:
                found.append(str(stream.get("index")))
        except (TypeError, ValueError):
            continue
    return found


def set_volume(pid: int, volume: int) -> bool:
    """Set this process's playback volume, 0-100. True if the mixer took it."""
    volume = max(0, min(100, int(volume)))
    changed = False
    for index in sink_inputs_for(pid):
        if _pactl("set-sink-input-volume", index, f"{volume}%"):
            changed = True
    return changed
