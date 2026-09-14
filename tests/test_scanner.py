"""The folder index: fast answers, and no walking on a request thread."""

import json
import time
from pathlib import Path

import pytest

from bgsoundtrack import art, library, playlists, scanner
from bgsoundtrack.config import Config


def album(directory, *names):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"\0" * 64)
    return directory


@pytest.fixture
def index(tmp_path):
    return scanner.Scanner(lambda path: path.suffix == ".mp3", tmp_path / "index.json")


def test_an_unknown_folder_is_walked_once(index, tmp_path):
    folder = album(tmp_path / "a", "one.mp3", "cover.jpg")
    assert [p.name for p in index.files(folder)] == ["one.mp3"]
    assert index.known(folder)


def test_a_known_folder_answers_from_the_index(index, tmp_path):
    folder = album(tmp_path / "a", "one.mp3")
    index.files(folder)
    (folder / "two.mp3").write_bytes(b"\0")     # changes on disk
    assert [p.name for p in index.files(folder, refresh=False)] == ["one.mp3"]
    index.refresh_now(folder)
    assert len(index.files(folder, refresh=False)) == 2


def test_a_stale_folder_is_refreshed_in_the_background(index, tmp_path, monkeypatch):
    folder = album(tmp_path / "a", "one.mp3")
    index.files(folder)
    (folder / "two.mp3").write_bytes(b"\0")
    monkeypatch.setattr(scanner, "REFRESH_AFTER", 0.0)   # "long enough ago"

    index.files(folder)          # returns the old answer, queues a walk
    assert index.wait(timeout=10)
    assert len(index.files(folder, refresh=False)) == 2


def test_never_walking_on_the_asking_thread_when_told_not_to(index, tmp_path):
    folder = album(tmp_path / "big", *[f"{n}.mp3" for n in range(50)])
    assert index.files(folder, block_if_unknown=False) == []
    assert index.wait(timeout=10)
    assert len(index.files(folder, refresh=False)) == 50


def test_the_index_survives_a_restart(tmp_path):
    folder = album(tmp_path / "a", "one.mp3", "two.mp3")
    first = scanner.Scanner(lambda path: path.suffix == ".mp3", tmp_path / "index.json")
    first.files(folder)
    first.save()

    again = scanner.Scanner(lambda path: path.suffix == ".mp3", tmp_path / "index.json")
    assert len(again.files(folder, refresh=False, block_if_unknown=False)) == 2
    stored = json.loads((tmp_path / "index.json").read_text())
    assert stored["version"] == scanner.CACHE_VERSION


def test_a_missing_folder_is_recorded_not_raised(index, tmp_path):
    assert index.files(tmp_path / "gone") == []
    assert index.known(tmp_path / "gone")


def test_the_version_moves_only_when_the_contents_change(index, tmp_path):
    folder = album(tmp_path / "a", "one.mp3")
    index.files(folder)
    after_first = index.version
    index.refresh_now(folder)
    assert index.version == after_first          # same files, same version
    (folder / "two.mp3").write_bytes(b"\0")
    index.refresh_now(folder)
    assert index.version > after_first


# -- what the UI polls --------------------------------------------------


def test_a_poll_does_not_rescan(tmp_path, monkeypatch):
    """The 1s poll used to re-walk every folder; now it must not walk at all."""
    from bgsoundtrack.service import Session

    folder = album(tmp_path / "music", *[f"{n:03d}.mp3" for n in range(200)])
    config = Config(root=str(tmp_path / "lib"))
    store = playlists.Store()
    library.init(config, store.current())
    library.add_source(config, folder, playlist=store.current())
    session = Session(config, tmp_path / "config.json", store=store)
    session.snapshot()      # first one may do the work

    walks = []
    original = library.SCANNER._walk
    monkeypatch.setattr(library.SCANNER, "_walk", lambda d: walks.append(d) or original(d))
    for _ in range(30):
        session.snapshot()
    assert walks == []


def test_counts_are_recomputed_when_something_changes(tmp_path):
    from bgsoundtrack.service import Session

    folder = album(tmp_path / "music", "a.mp3")
    config = Config(root=str(tmp_path / "lib"))
    store = playlists.Store()
    library.init(config, store.current())
    session = Session(config, tmp_path / "config.json", store=store)
    assert session.snapshot()["counts"]["music"] == 0

    library.add_source(config, folder, playlist=store.current())
    store.touch()
    assert session.snapshot()["counts"]["music"] == 1


# -- album art ----------------------------------------------------------


def test_a_picture_lying_in_the_folder_is_not_used(tmp_path, monkeypatch):
    """Only what the file carries counts - a stray cover.jpg is someone
    else's idea of what this record looks like."""
    folder = album(tmp_path / "album", "one.mp3")
    for name in ("cover.jpg", "folder.jpg", "front.png"):
        (folder / name).write_bytes(b"\xff\xd8\xff")
    covers = art.Art(tmp_path / "covers")
    monkeypatch.setattr(covers, "_extract", lambda path, remember_failure=True: None)
    assert covers.find(folder / "one.mp3") is None


def test_no_cover_is_remembered_as_no_cover(tmp_path, monkeypatch):
    folder = album(tmp_path / "album", "one.mp3")
    covers = art.Art(tmp_path / "covers")
    calls = []
    monkeypatch.setattr(art.shutil, "which", lambda name: None)   # no ffmpeg
    assert covers.find(folder / "one.mp3") is None


def test_art_is_shared_by_everything_in_one_folder(tmp_path, monkeypatch):
    folder = album(tmp_path / "album", "one.mp3", "two.mp3")
    covers = art.Art(tmp_path / "covers")
    extracted = tmp_path / "covers" / "art.jpg"

    def fake_extract(path, remember_failure=True):
        extracted.parent.mkdir(parents=True, exist_ok=True)
        extracted.write_bytes(b"\xff\xd8\xff")
        covers.remember(path.parent, extracted)
        return extracted

    monkeypatch.setattr(covers, "_extract", fake_extract)
    assert covers.find(folder / "one.mp3") == covers.find(folder / "two.mp3") == extracted


def test_a_record_is_searched_for_its_cover_not_just_one_track(tmp_path, monkeypatch):
    """The picture often sits on one track of an album and not the others."""
    folder = album(tmp_path / "album", "01 first.mp3", "02 second.mp3", "03 third.mp3")
    covers = art.Art(tmp_path / "covers")

    tried = []

    def fake_extract(path, remember_failure=True):
        tried.append(path.name)
        if path.name != "03 third.mp3":
            return None
        target = covers.directory / "cover.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xd8\xff")
        return target

    monkeypatch.setattr(covers, "_extract", fake_extract)
    assert covers.find(folder / "01 first.mp3") is not None
    assert tried == ["01 first.mp3", "02 second.mp3", "03 third.mp3"]


def test_a_record_with_no_art_anywhere_is_only_searched_once(tmp_path, monkeypatch):
    folder = album(tmp_path / "album", "01 a.mp3", "02 b.mp3")
    covers = art.Art(tmp_path / "covers")
    tried = []
    monkeypatch.setattr(
        covers, "_extract", lambda path, remember_failure=True: tried.append(path) or None
    )
    assert covers.find(folder / "01 a.mp3") is None
    assert covers.find(folder / "02 b.mp3") is None      # remembered, not retried
    assert len(tried) == 2


def test_art_for_a_link_belongs_to_the_file_it_points_at(tmp_path):
    real = album(tmp_path / "Artist" / "Album", "song.mp3")
    links = tmp_path / "library"
    links.mkdir()
    link = links / "song.mp3"
    link.symlink_to(real / "song.mp3")

    covers = art.Art(tmp_path / "covers")
    cover = tmp_path / "covers" / "album.jpg"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"\xff\xd8\xff")
    covers.remember(real, cover)          # found from the real file

    assert covers.find(link) == cover     # and the link gets it too


def test_answers_survive_a_restart(tmp_path, monkeypatch):
    """Both kinds: the cover we found, and the record that has none."""
    with_art = album(tmp_path / "with", "a.mp3")
    without = album(tmp_path / "without", "b.mp3")
    cover = tmp_path / "covers" / "found.jpg"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"\xff\xd8\xff")

    first = art.Art(tmp_path / "covers")
    first.remember(with_art, cover)
    first.remember(without, None)
    first.save()

    again = art.Art(tmp_path / "covers")
    calls = []
    monkeypatch.setattr(
        again, "_extract", lambda path, remember_failure=True: calls.append(path) or None
    )
    assert again.find(with_art / "a.mp3") == cover
    assert again.find(without / "b.mp3") is None
    assert calls == []          # nothing was read again


def test_a_cover_file_that_vanished_is_looked_for_again(tmp_path, monkeypatch):
    folder = album(tmp_path / "album", "a.mp3")
    cover = tmp_path / "covers" / "gone.jpg"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"\xff\xd8\xff")
    first = art.Art(tmp_path / "covers")
    first.remember(folder, cover)
    first.save()
    cover.unlink()

    again = art.Art(tmp_path / "covers")
    tried = []
    monkeypatch.setattr(
        again, "_extract", lambda path, remember_failure=True: tried.append(path) or None
    )
    assert again.find(folder / "a.mp3") is None
    assert tried, "a missing cover file should be looked for again"


def test_files_without_a_picture_are_not_handed_to_ffmpeg(tmp_path, monkeypatch):
    """The header says whether there is one; scanning the whole file to find
    out costs seconds per track."""
    folder = album(tmp_path / "album", "a.mp3")
    covers = art.Art(tmp_path / "covers")
    monkeypatch.setattr(art.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(art.Art, "has_picture", staticmethod(lambda path: False))
    ran = []
    monkeypatch.setattr(art.subprocess, "run", lambda *a, **k: ran.append(a))
    assert covers.find(folder / "a.mp3") is None
    assert ran == []


def test_a_restart_does_not_reask_the_whole_library(tmp_path, monkeypatch):
    """The point of writing answers down: a second run reads no files."""
    from bgsoundtrack.service import Session

    folder = album(tmp_path / "music" / "Artist" / "Album", "a.mp3", "b.mp3")
    config = Config(root=str(tmp_path / "lib"))
    store = playlists.Store()
    library.init(config, store.current())
    library.add_source(config, tmp_path / "music", playlist=store.current())

    first = Session(config, tmp_path / "config.json", store=store)
    monkeypatch.setattr(
        first._art, "_extract", lambda path, remember_failure=True: None
    )
    assert first.find_covers() == 1          # one record to look at
    for _ in range(100):
        if first._art_pending == 0:
            break
        time.sleep(0.02)
    first._art.save()

    again = Session(config, tmp_path / "config.json", store=store)
    reads = []
    monkeypatch.setattr(
        again._art, "_extract", lambda path, remember_failure=True: reads.append(path)
    )
    assert again.find_covers() == 0          # nothing left to ask
    assert reads == []
