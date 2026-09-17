"""Taking a library elsewhere.

Two shapes, for two different jobs:

* a **manifest** - JSON (or CSV for a plain track list) describing playlists,
  their folders, their links and their removals. Small, readable, diffable,
  and it points at files rather than carrying them: perfect for your own
  backup or for moving to a new machine that has the same music on it.
* a **bundle** - a zip with the audio inside, so someone else can unpack it
  and hear exactly what you hear.

Import never overwrites: it adds playlists (renaming on a clash) and skips
anything already there.
"""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from gravitone import library, playlists
from gravitone.config import Config

FORMAT = 1
MANIFEST_NAME = "gravitone.json"
AUDIO_DIR = "audio"


class TransferError(Exception):
    pass


@dataclass
class Report:
    playlists: list
    tracks: int = 0        # what the file carries: links, or packed audio
    folders: int = 0
    total: int = 0         # what those playlists actually play, all told
    skipped: list = None
    path: Path | None = None

    def __post_init__(self) -> None:
        self.skipped = self.skipped or []


def _chosen(store: playlists.Store, names: list | None) -> list:
    if not names:
        return list(store.playlists)
    return [store.get(name) for name in names]


# -- export -------------------------------------------------------------


def manifest(config: Config, store: playlists.Store, names: list | None = None) -> dict:
    """Everything needed to rebuild these playlists, minus the audio."""
    chosen = _chosen(store, names)
    out = {
        "gravitone": FORMAT,
        "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "settings": {
            key: value
            for key, value in config.to_dict().items()
            if key not in ("root", "music_sources", "ambient_sources")
        },
        "playlists": [],
    }
    for playlist in chosen:
        item = {
            "id": playlist.id,
            "name": playlist.name,
            "folders": {
                section: [str(path) for path in playlist.sources(section)]
                for section in library.SECTIONS
            },
            "links": {},
            "removed": list(playlist.excluded),
        }
        for section in library.SECTIONS:
            item["links"][section] = [
                {"name": entry.name, "target": str(entry.target)}
                for entry in library.linked_entries(config, section, playlist)
            ]
        out["playlists"].append(item)
    return out


def rows(config: Config, store: playlists.Store, names: list | None = None) -> list:
    """The same thing flattened, one row per track, for CSV."""
    listed = []
    for playlist in _chosen(store, names):
        for section in library.SECTIONS:
            for entry in library.entries(config, section, playlist):
                listed.append(
                    {
                        "playlist": playlist.name,
                        "section": section,
                        "name": entry.name,
                        "origin": entry.origin,
                        "path": str(entry.target),
                    }
                )
    return listed


def write_csv(data: list, path: Path) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["playlist", "section", "name", "origin", "path"]
        )
        writer.writeheader()
        writer.writerows(data)
    return path


def export(
    config: Config,
    store: playlists.Store,
    path: Path,
    names: list | None = None,
    fmt: str = "json",
) -> Report:
    """Write a manifest. `fmt` is json or csv."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    chosen = _chosen(store, names)
    if fmt == "csv":
        listed = rows(config, store, names)
        write_csv(listed, path)
        return Report(
            playlists=[p.name for p in chosen],
            tracks=len(listed),
            total=len(listed),
            path=path,
        )
    if fmt != "json":
        raise TransferError(f"unknown format {fmt!r} (expected json or csv)")

    data = manifest(config, store, names)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tracks = sum(
        len(item["links"][section])
        for item in data["playlists"]
        for section in library.SECTIONS
    )
    folders = sum(
        len(item["folders"][section])
        for item in data["playlists"]
        for section in library.SECTIONS
    )
    total = sum(
        len(library.entries(config, section, playlist))
        for playlist in chosen
        for section in library.SECTIONS
    )
    return Report(
        playlists=[p.name for p in chosen],
        tracks=tracks,
        folders=folders,
        total=total,
        path=path,
    )


def export_bundle(
    config: Config,
    store: playlists.Store,
    path: Path,
    names: list | None = None,
    on_progress=None,
) -> Report:
    """Write a zip with the audio inside, ready to hand to someone else."""
    path = Path(path).expanduser()
    if path.suffix.lower() != ".zip":
        path = path.with_suffix(".zip")
    path.parent.mkdir(parents=True, exist_ok=True)

    chosen = _chosen(store, names)
    data = manifest(config, store, names)
    skipped: list = []
    packed = 0

    # Audio is already compressed; storing is faster and no bigger.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED, allowZip64=True) as bundle:
        for playlist, item in zip(chosen, data["playlists"]):
            item["bundled"] = {}
            for section in library.SECTIONS:
                names_used: set = set()
                item["bundled"][section] = []
                for entry in library.entries(config, section, playlist):
                    inner = entry.name
                    stem, suffix = Path(inner).stem, Path(inner).suffix
                    counter = 2
                    while inner in names_used:
                        inner = f"{stem}-{counter}{suffix}"
                        counter += 1
                    names_used.add(inner)
                    arcname = f"{AUDIO_DIR}/{playlist.id}/{section}/{inner}"
                    try:
                        bundle.write(entry.target, arcname)
                    except OSError as exc:
                        skipped.append([entry.name, str(exc)])
                        continue
                    item["bundled"][section].append(inner)
                    packed += 1
                    if on_progress:
                        on_progress(packed, entry.name)
        bundle.writestr(
            MANIFEST_NAME, json.dumps(data, indent=2), zipfile.ZIP_DEFLATED
        )

    return Report(
        playlists=[p.name for p in chosen],
        tracks=packed,
        total=packed,
        skipped=skipped,
        path=path,
    )


# -- import -------------------------------------------------------------


def _add_playlist(store: playlists.Store, name: str) -> playlists.Playlist:
    """Never clobber a playlist that is already there."""
    wanted, counter = name, 2
    while any(p.name.lower() == wanted.lower() for p in store.playlists):
        wanted = f"{name} ({counter})"
        counter += 1
    return store.add(wanted)


def read_manifest(path: Path) -> dict:
    path = Path(path).expanduser()
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as bundle:
                raw = bundle.read(MANIFEST_NAME).decode("utf-8")
        else:
            raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise TransferError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict) or "playlists" not in data:
        raise TransferError(f"{path} is not a gravitone export")
    if int(data.get("gravitone", 0)) > FORMAT:
        raise TransferError(
            f"{path} was written by a newer gravitone (format {data['gravitone']})"
        )
    return data


def import_manifest(
    config: Config,
    store: playlists.Store,
    path: Path,
    rename: str | None = None,
) -> Report:
    """Recreate playlists from a manifest, using the files already on disk."""
    data = read_manifest(path)
    added, skipped = [], []
    tracks = folders = 0

    for item in data["playlists"]:
        name = rename if rename and len(data["playlists"]) == 1 else item.get("name", "Imported")
        playlist = _add_playlist(store, name)
        library.init(config, playlist)
        added.append(playlist.name)

        for section in library.SECTIONS:
            for folder in item.get("folders", {}).get(section, []):
                if not Path(folder).is_dir():
                    skipped.append([folder, "folder not on this machine"])
                    continue
                try:
                    library.add_source(config, Path(folder), section, playlist)
                    folders += 1
                except library.LibraryError as exc:
                    skipped.append([folder, str(exc)])

            for link in item.get("links", {}).get(section, []):
                target = Path(link.get("target", ""))
                if not target.exists():
                    skipped.append([link.get("name", str(target)), "file not found here"])
                    continue
                result = library.link(config, [target], section, playlist=playlist)
                tracks += len(result.linked)

        for removed in item.get("removed", []):
            playlist.exclude(Path(removed))

    store.save()
    library.invalidate_cache()
    return Report(playlists=added, tracks=tracks, folders=folders, skipped=skipped)


def import_bundle(
    config: Config,
    store: playlists.Store,
    path: Path,
    rename: str | None = None,
    into: Path | None = None,
) -> Report:
    """Unpack a shared bundle and make playlists that play it."""
    path = Path(path).expanduser()
    if not zipfile.is_zipfile(path):
        return import_manifest(config, store, path, rename)

    data = read_manifest(path)
    destination = Path(into) if into else config.root_path / "imported" / path.stem
    destination = destination.expanduser()
    added, skipped = [], []
    tracks = 0

    with zipfile.ZipFile(path) as bundle:
        members = [
            name
            for name in bundle.namelist()
            if name.startswith(f"{AUDIO_DIR}/") and not name.endswith("/")
        ]
        for name in members:
            # Never let an archive write outside where we said.
            target = (destination / name).resolve()
            if destination.resolve() not in target.parents:
                skipped.append([name, "unsafe path in the bundle"])
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(name) as source, target.open("wb") as out:
                out.write(source.read())
            tracks += 1

    for item in data["playlists"]:
        name = rename if rename and len(data["playlists"]) == 1 else item.get("name", "Imported")
        playlist = _add_playlist(store, name)
        library.init(config, playlist)
        added.append(playlist.name)
        for section in library.SECTIONS:
            folder = destination / AUDIO_DIR / item.get("id", "") / section
            if not folder.is_dir():
                continue
            library.add_source(config, folder, section, playlist)

    store.save()
    library.invalidate_cache()
    return Report(
        playlists=added, tracks=tracks, skipped=skipped, path=destination
    )


def looks_like_bundle(path: Path) -> bool:
    return zipfile.is_zipfile(Path(path).expanduser())
