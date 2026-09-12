"""Where the music comes from.

Two ways in, and they mix freely:

* **Linked** - `bgst link` puts a symlink in the 'custom soundtrack' folder,
  pointing at the file where it already lives. Adding a 40 GB collection
  costs a few kilobytes of directory entries, and you curate track by track.
* **Source folders** - a folder played in place. Nothing is added to the
  library at all; the tree is scanned when it is needed, so anything you drop
  in there later just shows up.
"""

from __future__ import annotations

import os
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from bgsoundtrack.config import AUDIO_EXTENSIONS, Config

SECTIONS = ("music", "ambient")


class LibraryError(Exception):
    pass


@dataclass(frozen=True)
class LinkResult:
    linked: list[Path]
    skipped: list[tuple[Path, str]]


@dataclass(frozen=True)
class Entry:
    """One playable track, and how it got into the library."""

    path: Path       # what the player opens
    target: Path     # the real file behind it
    origin: str      # "link" | "source"
    source: Path | None = None  # the source folder it was found in

    @property
    def name(self) -> str:
        return self.path.name


def section_dir(config: Config, section: str) -> Path:
    if section not in SECTIONS:
        raise LibraryError(f"unknown section {section!r} (expected one of {', '.join(SECTIONS)})")
    return config.music_dir if section == "music" else config.ambient_dir


def init(config: Config) -> list[Path]:
    """Create the library folders. Safe to run repeatedly."""
    created = []
    for section in SECTIONS:
        path = section_dir(config, section)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)
    return created


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def _unique_name(directory: Path, name: str) -> Path:
    """Pick a free filename in `directory`, appending -2, -3, ... on collision."""
    candidate = directory / name
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        counter += 1


def _sources(paths: list[Path], recursive: bool) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        path = path.expanduser()
        if path.is_dir():
            walker = path.rglob("*") if recursive else path.glob("*")
            found.extend(sorted(p for p in walker if p.is_file() and is_audio(p)))
        elif path.is_file():
            found.append(path)
        else:
            raise LibraryError(f"no such file or directory: {path}")
    return found


def _file_key(path: Path) -> tuple[int, int]:
    """Identity of the file behind a path: (device, inode).

    Identifies the same audio file through a symlink, a hard link, or a second
    path to the same mount, so re-linking a folder never duplicates entries.
    """
    info = path.stat()  # follows links
    return (info.st_dev, info.st_ino)


def existing_targets(directory: Path) -> set[tuple[int, int]]:
    """File identities of every entry already in `directory`."""
    targets = set()
    for entry in directory.iterdir() if directory.exists() else []:
        try:
            targets.add(_file_key(entry))
        except OSError:  # dangling link
            continue
    return targets


def link(
    config: Config,
    paths: list[Path],
    section: str = "music",
    recursive: bool = True,
    relative: bool = False,
) -> LinkResult:
    """Symlink `paths` into the library. Directories are scanned for audio."""
    directory = section_dir(config, section)
    directory.mkdir(parents=True, exist_ok=True)

    already = existing_targets(directory)
    linked: list[Path] = []
    skipped: list[tuple[Path, str]] = []

    for source in _sources(paths, recursive):
        if not is_audio(source):
            skipped.append((source, "not an audio file"))
            continue
        try:
            target = source.resolve(strict=True)
        except OSError as exc:
            skipped.append((source, str(exc)))
            continue
        try:
            key = _file_key(target)
        except OSError as exc:
            skipped.append((source, str(exc)))
            continue
        if key in already:
            skipped.append((source, "already in library"))
            continue

        name = unicodedata.normalize("NFC", target.name)
        destination = _unique_name(directory, name)
        link_to = (
            Path(os.path.relpath(target, destination.parent)) if relative else target
        )
        try:
            _make_link(destination, link_to, target)
        except OSError as exc:
            skipped.append((source, f"could not link: {exc}"))
            continue
        already.add(key)
        linked.append(destination)

    return LinkResult(linked=linked, skipped=skipped)


def _make_link(destination: Path, link_to: Path, target: Path) -> None:
    """Symlink, falling back to a hard link where symlinks are privileged.

    Windows only allows unprivileged symlinks with Developer Mode on. A hard
    link needs no privilege and still costs no extra disk - it just cannot
    cross volumes, which is why it is the fallback and not the default.
    """
    try:
        destination.symlink_to(link_to)
        return
    except OSError as exc:
        try:
            os.link(target, destination)
            return
        except OSError:
            raise exc


def unlink(config: Config, names: list[str], section: str = "music") -> list[Path]:
    """Remove entries from the library. Only symlinks are ever removed."""
    directory = section_dir(config, section)
    removed = []
    for name in names:
        entry = directory / Path(name).name
        if not entry.is_symlink() and not _is_hard_link(entry):
            if entry.exists():
                raise LibraryError(
                    f"{entry} is a real file, not a link - refusing to delete it"
                )
            raise LibraryError(f"not in the {section} library: {name}")
        entry.unlink()
        removed.append(entry)
    return removed


def _is_hard_link(path: Path) -> bool:
    """True if removing `path` leaves the audio behind under another name."""
    try:
        return path.is_file() and path.stat().st_nlink > 1
    except OSError:
        return False


# Source folders are re-scanned on demand; this keeps a 1-second poll from
# walking a big tree over and over. Mutating the sources clears it.
_SCAN_TTL = 5.0
_scan_cache: dict[Path, tuple[float, list[Path]]] = {}


def invalidate_cache() -> None:
    _scan_cache.clear()


def scan(directory: Path, ttl: float = _SCAN_TTL) -> list[Path]:
    """Audio files under `directory`, recursively. Cached briefly."""
    now = time.monotonic()
    cached = _scan_cache.get(directory)
    if cached and now - cached[0] < ttl:
        return cached[1]
    found: list[Path] = []
    if directory.is_dir():
        for entry in sorted(directory.rglob("*")):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_file() and is_audio(entry):
                    found.append(entry)
            except OSError:
                continue
    _scan_cache[directory] = (now, found)
    return found


def linked_entries(config: Config, section: str = "music") -> list[Entry]:
    """Symlinks (and hard links) sitting in the library folder."""
    directory = section_dir(config, section)
    if not directory.is_dir():
        return []
    found = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir() or not entry.exists():  # dangling link
            continue
        if is_audio(entry):
            try:
                target = entry.resolve()
            except OSError:
                continue
            found.append(Entry(path=entry, target=target, origin="link"))
    return found


def entries(config: Config, section: str = "music") -> list[Entry]:
    """Everything playable in a section: links first, then source folders.

    A file reachable both ways is listed once, as the link - so pointing a
    source folder at something you already linked does not double it up.
    """
    found = linked_entries(config, section)
    seen = set()
    for entry in found:
        try:
            seen.add(_file_key(entry.target))
        except OSError:
            continue

    for source in config.sources(section):
        for path in scan(source):
            try:
                key = _file_key(path)
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            found.append(Entry(path=path, target=path, origin="source", source=source))
    return found


def tracks(config: Config, section: str = "music") -> list[Path]:
    """Playable files of a section, links and source folders together."""
    return [entry.path for entry in entries(config, section)]


def add_source(config: Config, path: Path, section: str = "music") -> Path:
    """Play a folder in place, without linking anything out of it."""
    section_dir(config, section)  # validates the section name
    directory = Path(path).expanduser()
    if not directory.is_dir():
        raise LibraryError(f"not a folder: {directory}")
    directory = directory.resolve()
    current = config.sources(section)
    if directory in current:
        raise LibraryError(f"already a {section} source: {directory}")
    config.set_sources(section, current + [directory])
    invalidate_cache()
    return directory


def remove_source(config: Config, path: Path, section: str = "music") -> Path:
    section_dir(config, section)
    directory = Path(path).expanduser()
    current = config.sources(section)
    matches = [p for p in current if p == directory or str(p) == str(directory)]
    if not matches:
        resolved = directory.resolve() if directory.exists() else directory
        matches = [p for p in current if p == resolved]
        if not matches:
            raise LibraryError(f"not a {section} source: {directory}")
    config.set_sources(section, [p for p in current if p not in matches])
    invalidate_cache()
    return matches[0]


def broken(config: Config, section: str = "music") -> list[Path]:
    directory = section_dir(config, section)
    if not directory.is_dir():
        return []
    return sorted(
        entry
        for entry in directory.iterdir()
        if entry.is_symlink() and not entry.exists()
    )


def prune(config: Config, sections: tuple[str, ...] = SECTIONS) -> list[Path]:
    """Delete symlinks whose target has gone away."""
    removed = []
    for section in sections:
        for entry in broken(config, section):
            entry.unlink()
            removed.append(entry)
    return removed
