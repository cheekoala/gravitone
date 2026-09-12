"""The 'custom soundtrack' folder: symlinks in, no duplicated bytes.

Tracks stay wherever they already live (a Steam folder, a NAS mount, an
external drive). The library only holds symlinks pointing at them, so adding
a 40 GB collection costs a few kilobytes of directory entries.
"""

from __future__ import annotations

import os
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


def existing_targets(directory: Path) -> set[Path]:
    """Resolved targets of every link already in `directory`."""
    targets = set()
    for entry in directory.iterdir() if directory.exists() else []:
        try:
            targets.add(entry.resolve())
        except OSError:
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
        if target in already:
            skipped.append((source, "already in library"))
            continue

        name = unicodedata.normalize("NFC", target.name)
        destination = _unique_name(directory, name)
        link_to = (
            Path(os.path.relpath(target, destination.parent)) if relative else target
        )
        try:
            destination.symlink_to(link_to)
        except OSError as exc:
            skipped.append((source, f"could not link: {exc}"))
            continue
        already.add(target)
        linked.append(destination)

    return LinkResult(linked=linked, skipped=skipped)


def unlink(config: Config, names: list[str], section: str = "music") -> list[Path]:
    """Remove entries from the library. Only symlinks are ever removed."""
    directory = section_dir(config, section)
    removed = []
    for name in names:
        entry = directory / Path(name).name
        if not entry.is_symlink():
            if entry.exists():
                raise LibraryError(
                    f"{entry} is a real file, not a symlink - refusing to delete it"
                )
            raise LibraryError(f"not in the {section} library: {name}")
        entry.unlink()
        removed.append(entry)
    return removed


def tracks(config: Config, section: str = "music") -> list[Path]:
    """Playable entries of a section, sorted, broken links dropped."""
    directory = section_dir(config, section)
    if not directory.is_dir():
        return []
    found = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir():
            continue
        if not entry.exists():  # dangling symlink
            continue
        if is_audio(entry):
            found.append(entry)
    return found


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
