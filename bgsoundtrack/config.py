"""Configuration handling.

The config is a small JSON file. Everything in it has a sane default, so a
fresh install works with no configuration at all.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

# The longest gap the UI offers: an hour of quiet between two songs.
MAX_GAP = 3600.0

# How a library can be ordered - these are the table's sortable columns.
SORTS = ("name", "title", "artist", "album", "length")

MUSIC_DIRNAME = "music"
AMBIENT_DIRNAME = "ambient"

AUDIO_EXTENSIONS = frozenset(
    {
        ".mp3",
        ".ogg",
        ".oga",
        ".opus",
        ".flac",
        ".wav",
        ".m4a",
        ".aac",
        ".wma",
        ".aiff",
        ".aif",
        ".mp4",
        ".webm",
    }
)


def default_root() -> Path:
    """Where the 'custom soundtrack' folder lives by default."""
    env = os.environ.get("BGSOUNDTRACK_ROOT")
    if env:
        return Path(env).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "custom soundtrack"


def config_path() -> Path:
    env = os.environ.get("BGSOUNDTRACK_CONFIG")
    if env:
        return Path(env).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "bgsoundtrack" / "config.json"


@dataclass
class Config:
    """Player settings.

    Gap defaults are deliberately conservative: long enough that the music
    does not feel wall-to-wall, short enough that a quiet stretch never feels
    like the player died.
    """

    root: str = ""
    gap_min: float = 12.0
    gap_max: float = 45.0
    ambient_chance: float = 0.65
    ambient_min_tail: float = 3.0
    shuffle: bool = True
    volume: int = 70
    ambient_volume: int = 45
    loop: bool = True
    # Start an ambient bed somewhere in the middle of the file, so the same
    # rain loop does not open with the same three seconds every time.
    ambient_random_start: bool = True
    # Hidden mode: never say how long a gap is or how much of it is left.
    # The wait is the point; a countdown ruins it.
    hide_gaps: bool = False
    # How the library is listed, and the order it plays in with shuffle off.
    sort_by: str = "name"
    sort_desc: bool = False
    # Show the file on disk rather than the title tag. Some libraries are
    # better named than they are tagged.
    show_filenames: bool = True

    # How long sound takes to arrive and to leave, in seconds. 0 cuts
    # straight in and out, which is what a skip used to sound like.
    fade: float = 1.5

    # Nudge for a party, in seconds: added to this machine's clock when it
    # works out where the party has got to. Both ends normally run NTP and
    # need nothing, but a clock that is known to be a second out can say so.
    party_offset: float = 0.0

    # Folders played in place. An alternative to linking: nothing is added to
    # the library folder, the tree is simply scanned every time it is needed,
    # so whatever you drop in there later is picked up on the next pass.
    music_sources: list = field(default_factory=list)
    ambient_sources: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.root:
            self.root = str(default_root())

    @property
    def root_path(self) -> Path:
        return Path(self.root).expanduser()

    @property
    def music_dir(self) -> Path:
        return self.root_path / MUSIC_DIRNAME

    @property
    def ambient_dir(self) -> Path:
        return self.root_path / AMBIENT_DIRNAME

    def sources(self, section: str) -> list[Path]:
        names = self.music_sources if section == "music" else self.ambient_sources
        return [Path(name).expanduser() for name in names]

    def set_sources(self, section: str, paths: list) -> None:
        values = [str(Path(p).expanduser()) for p in paths]
        if section == "music":
            self.music_sources = values
        else:
            self.ambient_sources = values

    def validate(self) -> None:
        if self.gap_min < 0:
            raise ValueError("gap_min must be >= 0")
        if self.gap_max < self.gap_min:
            raise ValueError("gap_max must be >= gap_min")
        if self.gap_max > MAX_GAP:
            raise ValueError(f"gap_max must be <= {MAX_GAP} seconds (one hour)")
        if not 0.0 <= self.ambient_chance <= 1.0:
            raise ValueError("ambient_chance must be between 0 and 1")
        for name in ("volume", "ambient_volume"):
            value = getattr(self, name)
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
        if not 0.0 <= self.fade <= 10.0:
            raise ValueError("fade must be between 0 and 10 seconds")
        if abs(self.party_offset) > 3600:
            raise ValueError("party_offset must be within an hour")
        for name in ("music_sources", "ambient_sources"):
            if not isinstance(getattr(self, name), list):
                raise ValueError(f"{name} must be a list of folders")
        if self.sort_by not in SORTS:
            raise ValueError(f"sort_by must be one of {', '.join(SORTS)}")

    def to_dict(self) -> dict:
        return asdict(self)


_FIELD_TYPES = {f.name: f.type for f in fields(Config)}


def _coerce(name: str, value):
    """Turn a string (from the CLI or a hand-edited config) into the field type."""
    kind = _FIELD_TYPES[name]
    if kind == "list":
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        text = str(value).strip()
        if not text:
            return []
        return [part for part in text.split(os.pathsep) if part]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{name}: expected a boolean, got {value!r}")
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    return str(value)


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read config {path}: {exc}") from exc
    known = {name: raw[name] for name in _FIELD_TYPES if name in raw}
    return Config(**{name: _coerce(name, value) for name, value in known.items()})


def save(config: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def set_value(config: Config, name: str, value) -> Config:
    if name not in _FIELD_TYPES:
        raise KeyError(name)
    setattr(config, name, _coerce(name, value))
    config.validate()
    return config


def known_keys() -> list[str]:
    return list(_FIELD_TYPES)
