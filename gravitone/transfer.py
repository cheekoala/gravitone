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
import os
import tempfile
import time
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from gravitone import library, playlists, tags, transcode
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


def bundle_plan(
    config: Config, store: playlists.Store, names: list | None = None,
    audio: str | None = None, reader=None,
) -> dict:
    """What a bundle would contain, and roughly how big it would be.

    Worth knowing before you start: the same music is fifteen gigabytes as
    FLAC and one and a half as MP3, and nobody should have to find that out
    by waiting for it.
    """
    preset = transcode.get(audio)
    sizes, durations = [], []
    reader = reader or tags.Reader(tags.cache_path())
    for playlist in _chosen(store, names):
        for section in library.SECTIONS:
            for entry in library.entries(config, section, playlist):
                try:
                    sizes.append(entry.target.stat().st_size)
                except OSError:
                    sizes.append(0)
                durations.append(reader.known(entry.target).duration)
    return {
        "tracks": len(sizes),
        "bytes": transcode.estimate(sizes, durations, preset),
        "original": sum(sizes),
        "audio": preset.name,
        "label": preset.label,
    }


def export_bundle(
    config: Config,
    store: playlists.Store,
    path: Path,
    names: list | None = None,
    on_progress=None,
    audio: str | None = None,
    workers: int | None = None,
    should_stop=None,
) -> Report:
    """Write a zip with the audio inside, ready to hand to someone else.

    `audio` names a setting from `transcode`: the default copies the files
    untouched, and anything else converts on the way in, which is the
    difference between a bundle somebody can download and one they cannot.
    """
    preset = transcode.get(audio)
    if not transcode.available(preset):
        raise TransferError(
            f"{preset.label} needs ffmpeg with its encoder, and this machine "
            "does not have it"
        )
    path = Path(path).expanduser()
    if path.suffix.lower() != ".zip":
        path = path.with_suffix(".zip")
    path.parent.mkdir(parents=True, exist_ok=True)

    chosen = _chosen(store, names)
    data = manifest(config, store, names)
    skipped: list = []
    packed = 0
    stop = should_stop or (lambda: False)

    # One job per track, in order, so the zip is written in order too.
    jobs: list = []
    for playlist, item in zip(chosen, data["playlists"]):
        item["bundled"] = {}
        for section in library.SECTIONS:
            names_used: set = set()
            item["bundled"][section] = []
            for entry in library.entries(config, section, playlist):
                inner = transcode.renamed(entry.name, preset)
                stem, suffix = Path(inner).stem, Path(inner).suffix
                counter = 2
                while inner in names_used:
                    inner = f"{stem}-{counter}{suffix}"
                    counter += 1
                names_used.add(inner)
                jobs.append((entry, item["bundled"][section], inner,
                             f"{AUDIO_DIR}/{playlist.id}/{section}/{inner}"))

    total = len(jobs)
    # Audio is already compressed; storing is faster and no bigger.
    with tempfile.TemporaryDirectory(prefix="gravitone-bundle-") as workspace:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED, allowZip64=True) as bundle:
            for entry, listed, inner, arcname, source in _prepared(
                jobs, preset, Path(workspace), workers, stop, skipped
            ):
                try:
                    bundle.write(source, arcname)
                except OSError as exc:
                    skipped.append([entry.name, str(exc)])
                else:
                    listed.append(inner)
                    packed += 1
                    if on_progress:
                        on_progress(packed, total, entry.name)
                finally:
                    if source != entry.target:
                        Path(source).unlink(missing_ok=True)
            bundle.writestr(
                MANIFEST_NAME, json.dumps(data, indent=2), zipfile.ZIP_DEFLATED
            )
    if stop():
        path.unlink(missing_ok=True)
        raise TransferError("stopped before it finished; nothing was written")

    return Report(
        playlists=[p.name for p in chosen],
        tracks=packed,
        total=total,
        skipped=skipped,
        path=path,
    )


def _prepared(jobs, preset, workspace: Path, workers, stop, skipped):
    """Hand back each track ready to go into the zip, in order.

    Converting is the slow part and one file has nothing to do with the next,
    so several run at once - but only a few ahead of the writer, or a 400
    track bundle would put 400 temporary files on the disk before the first
    one was packed.
    """
    if preset.copies:
        for entry, listed, inner, arcname in jobs:
            if stop():
                return
            yield entry, listed, inner, arcname, entry.target
        return

    lanes = workers or min(8, (os.cpu_count() or 2))
    ahead = lanes * 2
    with ThreadPoolExecutor(max_workers=lanes) as pool:
        waiting: deque = deque()
        upcoming = iter(jobs)
        index = 0

        def send_one() -> bool:
            nonlocal index
            job = next(upcoming, None)
            if job is None:
                return False
            entry = job[0]
            target = workspace / f"{index:05d}{Path(job[2]).suffix}"
            index += 1
            waiting.append((job, pool.submit(transcode.convert, entry.target, target, preset)))
            return True

        while len(waiting) < ahead and send_one():
            pass
        while waiting:
            (entry, listed, inner, arcname), future = waiting.popleft()
            if stop():
                future.cancel()
                continue
            try:
                made = future.result()
            except (transcode.TranscodeError, OSError) as exc:
                skipped.append([entry.name, str(exc)])
            else:
                yield entry, listed, inner, arcname, made
            send_one()


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
