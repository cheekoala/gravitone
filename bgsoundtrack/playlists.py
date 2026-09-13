"""Playlists: switchable sets of music, each remembering its own edits.

A playlist is three things:

* **sources** - folders played in place,
* **links** - its own folder of symlinks, under `custom soundtrack/playlists/`,
* **removals** - tracks you took out of *this* playlist, remembered so they
  stay out when you come back to it.

Pick a playlist and the player uses exactly that set. The one called Library
is the folder you already had (`custom soundtrack/music` and `/ambient`), so
nothing has to move for playlists to start working.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from bgsoundtrack.config import Config

DEFAULT_ID = "default"
DEFAULT_NAME = "Library"


class PlaylistError(Exception):
    pass


def slugify(name: str) -> str:
    """A filesystem-safe id. Folders are named after it, so keep it plain."""
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_name).strip("-").lower()
    return slug or "playlist"


@dataclass
class Playlist:
    id: str
    name: str
    music_sources: list = field(default_factory=list)
    ambient_sources: list = field(default_factory=list)
    excluded: list = field(default_factory=list)

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT_ID

    def sources(self, section: str) -> list[Path]:
        names = self.music_sources if section == "music" else self.ambient_sources
        return [Path(name).expanduser() for name in names]

    def set_sources(self, section: str, paths: list) -> None:
        values = [str(Path(p).expanduser()) for p in paths]
        if section == "music":
            self.music_sources = values
        else:
            self.ambient_sources = values

    def is_excluded(self, target: Path) -> bool:
        return str(target) in self.excluded

    def exclude(self, target: Path) -> None:
        if str(target) not in self.excluded:
            self.excluded.append(str(target))

    def include(self, target: Path) -> bool:
        """Undo a removal. True if there was one."""
        before = len(self.excluded)
        self.excluded = [p for p in self.excluded if p != str(target)]
        return len(self.excluded) != before

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "music_sources": list(self.music_sources),
            "ambient_sources": list(self.ambient_sources),
            "excluded": list(self.excluded),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Playlist":
        identifier = str(raw.get("id") or slugify(str(raw.get("name", ""))))
        return cls(
            id=identifier,
            name=str(raw.get("name") or identifier),
            music_sources=[str(p) for p in raw.get("music_sources", [])],
            ambient_sources=[str(p) for p in raw.get("ambient_sources", [])],
            excluded=[str(p) for p in raw.get("excluded", [])],
        )


def store_path(config_path: Path | None = None) -> Path:
    from bgsoundtrack.config import config_path as default_config_path

    base = config_path or default_config_path()
    return base.parent / "playlists.json"


class Store:
    """Every playlist, which one is selected, and a version to notice edits."""

    def __init__(self, playlists: list = None, active: str = DEFAULT_ID, path: Path = None):
        self.path = path
        self.playlists = list(playlists or [])
        if not any(p.id == DEFAULT_ID for p in self.playlists):
            self.playlists.insert(0, Playlist(id=DEFAULT_ID, name=DEFAULT_NAME))
        self.active = active if any(p.id == active for p in self.playlists) else DEFAULT_ID
        # Bumped on every change so a running engine can notice and rebuild
        # its queue at the next track, rather than at the next pass.
        self.version = 0

    # -- lookup ----------------------------------------------------------

    def get(self, identifier: str) -> Playlist:
        for playlist in self.playlists:
            if playlist.id == identifier:
                return playlist
        for playlist in self.playlists:  # accept the display name too
            if playlist.name.lower() == str(identifier).lower():
                return playlist
        raise PlaylistError(f"no such playlist: {identifier}")

    def current(self) -> Playlist:
        try:
            return self.get(self.active)
        except PlaylistError:
            self.active = DEFAULT_ID
            return self.get(DEFAULT_ID)

    # -- editing ---------------------------------------------------------

    def touch(self) -> None:
        self.version += 1

    def select(self, identifier: str) -> Playlist:
        playlist = self.get(identifier)
        self.active = playlist.id
        self.touch()
        return playlist

    def add(self, name: str) -> Playlist:
        name = name.strip()
        if not name:
            raise PlaylistError("a playlist needs a name")
        if any(p.name.lower() == name.lower() for p in self.playlists):
            raise PlaylistError(f"there is already a playlist called {name}")
        base = slugify(name)
        identifier, counter = base, 2
        while any(p.id == identifier for p in self.playlists):
            identifier = f"{base}-{counter}"
            counter += 1
        playlist = Playlist(id=identifier, name=name)
        self.playlists.append(playlist)
        self.touch()
        return playlist

    def rename(self, identifier: str, name: str) -> Playlist:
        name = name.strip()
        if not name:
            raise PlaylistError("a playlist needs a name")
        playlist = self.get(identifier)
        playlist.name = name
        self.touch()
        return playlist

    def remove(self, identifier: str) -> Playlist:
        playlist = self.get(identifier)
        if playlist.is_default:
            raise PlaylistError("the Library playlist cannot be removed")
        self.playlists = [p for p in self.playlists if p.id != playlist.id]
        if self.active == playlist.id:
            self.active = DEFAULT_ID
        self.touch()
        return playlist

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "playlists": [p.to_dict() for p in self.playlists],
        }

    def save(self, path: Path = None) -> Path | None:
        """Write the playlists out. A store with no file (one built in memory,
        or the engine's stand-in) has nothing to write, and says so quietly."""
        target = path or self.path
        if target is None:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        self.path = target
        return target


def load(config: Config, config_path: Path | None = None) -> Store:
    """Read playlists.json, or build it from the pre-playlist config."""
    path = store_path(config_path)
    if not path.exists():
        return _migrate(config, path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlaylistError(f"could not read {path}: {exc}") from exc
    store = Store(
        playlists=[Playlist.from_dict(item) for item in raw.get("playlists", [])],
        active=str(raw.get("active", DEFAULT_ID)),
        path=path,
    )
    return store


def _migrate(config: Config, path: Path) -> Store:
    """First run with playlists: the old source folders become the Library."""
    default = Playlist(
        id=DEFAULT_ID,
        name=DEFAULT_NAME,
        music_sources=list(config.music_sources),
        ambient_sources=list(config.ambient_sources),
    )
    store = Store(playlists=[default], path=path)
    store.save()
    return store
