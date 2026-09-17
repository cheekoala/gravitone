"""The folder index: fast answers, and no walking on a request thread."""

import json
import time
from pathlib import Path

import pytest

from gravitone import art, library, playlists, scanner
from gravitone.config import Config


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
    from gravitone.service import Session

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
    from gravitone.service import Session

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
    monkeypatch.setattr(covers, "_extract", lambda path, remember_failure=True, under=None: None)
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

    def fake_extract(path, remember_failure=True, under=None):
        extracted.parent.mkdir(parents=True, exist_ok=True)
        extracted.write_bytes(b"\xff\xd8\xff")
        covers.remember(under or path, extracted)
        return extracted

    monkeypatch.setattr(covers, "_extract", fake_extract)
    assert covers.find(folder / "one.mp3") == covers.find(folder / "two.mp3") == extracted


def test_a_record_is_searched_for_its_cover_not_just_one_track(tmp_path, monkeypatch):
    """The picture often sits on one track of an album and not the others."""
    folder = album(tmp_path / "album", "01 first.mp3", "02 second.mp3", "03 third.mp3")
    covers = art.Art(tmp_path / "covers")

    tried = []

    def fake_extract(path, remember_failure=True, under=None):
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
        covers, "_extract", lambda path, remember_failure=True, under=None: tried.append(path) or None
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
        again, "_extract", lambda path, remember_failure=True, under=None: calls.append(path) or None
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
        again, "_extract", lambda path, remember_failure=True, under=None: tried.append(path) or None
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
    from gravitone.service import Session

    folder = album(tmp_path / "music" / "Artist" / "Album", "a.mp3", "b.mp3")
    config = Config(root=str(tmp_path / "lib"))
    store = playlists.Store()
    library.init(config, store.current())
    library.add_source(config, tmp_path / "music", playlist=store.current())

    first = Session(config, tmp_path / "config.json", store=store)
    monkeypatch.setattr(
        first._art, "_extract", lambda path, remember_failure=True, under=None: None
    )
    # Two files, and nothing known about either yet: their records are an
    # album tag away, so each is a job until the tags are read.
    assert first.find_covers() == 2
    assert first.wait_for_covers(10)
    first._art.save()

    again = Session(config, tmp_path / "config.json", store=store)
    reads = []
    monkeypatch.setattr(
        again._art, "_extract", lambda path, remember_failure=True, under=None: reads.append(path)
    )
    assert again.find_covers() == 0          # nothing left to ask
    assert reads == []


# -- a folder can hold more than one record ------------------------------


class FakeTags:
    """Just enough of tags.Reader for the art index: album per file."""

    def __init__(self, albums: dict):
        self.albums = albums

    def known(self, path):
        from gravitone.tags import Tags

        name = Path(path).name
        if name in self.albums:
            return Tags(album=self.albums[name])
        return Tags(album=Path(path).parent.name, guessed=True)

    def read(self, path):
        return self.known(path)


def test_two_records_in_one_folder_get_their_own_answers(tmp_path, monkeypatch):
    """A folder of several albums used to answer as one: the first record
    read decided for every track in it, so art on the others never showed."""
    folder = album(
        tmp_path / "game music",
        "aa other 1.mp3", "aa other 2.mp3", "aa other 3.mp3",
        "aa other 4.mp3", "aa other 5.mp3", "aa other 6.mp3",
        "zz morrowind 1.mp3", "zz morrowind 2.mp3",
    )
    covers = art.Art(
        tmp_path / "covers",
        reader=FakeTags({
            name: ("Morrowind" if "morrowind" in name else "Other Game")
            for name in (p.name for p in folder.iterdir())
        }),
    )
    found = tmp_path / "covers" / "morrowind.jpg"
    found.parent.mkdir(parents=True)
    found.write_bytes(b"\xff\xd8\xff")

    def fake_extract(path, remember_failure=True, under=None):
        # Only the Morrowind tracks carry a picture.
        if "morrowind" not in path.name:
            return None
        covers.remember(under or path, found)
        return found

    monkeypatch.setattr(covers, "_extract", fake_extract)

    # Ask about the coverless record first, exactly as a listing would.
    assert covers.find(folder / "aa other 1.mp3") is None
    assert covers.find(folder / "zz morrowind 1.mp3") == found
    assert covers.find(folder / "zz morrowind 2.mp3") == found
    assert covers.find(folder / "aa other 6.mp3") is None


def test_a_records_neighbours_are_its_own_tracks(tmp_path):
    """Looking for a picture across the folder means reading files that
    belong to a different album."""
    folder = album(tmp_path / "mixed", "a.mp3", "b.mp3", "c.mp3", "d.mp3")
    covers = art.Art(
        tmp_path / "covers",
        reader=FakeTags({"a.mp3": "One", "b.mp3": "Two", "c.mp3": "One", "d.mp3": "Two"}),
    )
    assert [p.name for p in covers.siblings(folder / "a.mp3")] == ["a.mp3", "c.mp3"]


def test_a_folder_keyed_index_is_not_read_back(tmp_path):
    """The answers a folder-keyed run wrote include the wrong ones, so they
    are dropped rather than carried into a run that knows better."""
    directory = tmp_path / "covers"
    directory.mkdir()
    (directory / art.INDEX_NAME).write_text(
        json.dumps({"version": 1, "folders": {str(tmp_path / "music"): None}})
    )
    covers = art.Art(directory)
    assert covers.remembered() == {}


def test_looking_again_drops_the_covers_it_pulled_out(tmp_path):
    """Otherwise a cover found under the old grouping is served straight
    back, from the file, whatever the new answer would be."""
    covers = art.Art(tmp_path / "covers")
    stale = tmp_path / "covers" / "old.jpg"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"\xff\xd8\xff")
    covers.forget()
    assert not stale.exists()


def test_a_picture_without_the_usual_marker_still_counts(tmp_path, monkeypatch):
    """Not every container flags its cover as an attached picture, and a
    picture is a picture."""
    song = album(tmp_path / "album", "a.flac") / "a.flac"
    monkeypatch.setattr(art.shutil, "which", lambda name: f"/usr/bin/{name}")

    class Done:
        stdout = json.dumps(
            {"streams": [{"codec_name": "png", "disposition": {"attached_pic": 0}}]}
        )

    monkeypatch.setattr(art.subprocess, "run", lambda *a, **k: Done())
    assert art.Art.has_picture(song) is True


def test_a_failed_art_scan_does_not_wedge_the_next_one(tmp_path, monkeypatch):
    """A scan that dies used to leave the counter up, and every later
    'find album art' then did nothing at all, silently."""
    from gravitone.service import Session

    album(tmp_path / "music" / "Artist" / "Album", "a.mp3")
    config = Config(root=str(tmp_path / "lib"))
    store = playlists.Store()
    library.init(config, store.current())
    library.add_source(config, tmp_path / "music", playlist=store.current())

    session = Session(config, tmp_path / "config.json", store=store)

    def boom(path, remember_failure=True, under=None):
        raise OSError("the disk went away")

    monkeypatch.setattr(session._art, "_extract", boom)
    assert session.find_covers() == 1
    assert session.wait_for_covers(10)
    assert session._art_pending == 0
    monkeypatch.setattr(
        session._art, "_extract", lambda path, remember_failure=True, under=None: None
    )
    assert session.forget_covers() == 1      # it can still be asked again


# -- the tagger's idea of the format is not the format -------------------


def picture_flac(path: Path, mime: str) -> Path:
    """A FLAC carrying a JPEG cover that declares itself as `mime`."""
    import shutil as _shutil
    import struct
    import subprocess

    if not _shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", "color=c=orange:s=400x400:d=1",
            "-frames:v", "1", "-map", "0:a", "-map", "1:v",
            "-c:a", "flac", "-c:v", "mjpeg", "-disposition:v", "attached_pic",
            str(path),
        ],
        check=True,
    )
    # Rewrite the PICTURE block's declared type, leaving the JPEG alone -
    # which is exactly the state plenty of tagged files are in.
    data = path.read_bytes()
    out, pos = b"fLaC", 4
    while True:
        header, length = data[pos], int.from_bytes(data[pos + 1:pos + 4], "big")
        body = data[pos + 4:pos + 4 + length]
        pos += 4 + length
        if header & 0x7F == 6:
            old = int.from_bytes(body[4:8], "big")
            body = (
                body[:4]
                + struct.pack(">I", len(mime)) + mime.encode()
                + body[8 + old:]
            )
        out += bytes([header & 0x80 | header & 0x7F]) + len(body).to_bytes(3, "big") + body
        if header & 0x80:
            break
    path.write_bytes(out + data[pos:])
    return path


def test_a_cover_is_read_from_its_bytes_not_its_label(tmp_path):
    """A JPEG filed as image/png is common, and decoding it by the label
    fails outright: ffmpeg picks the PNG decoder and refuses the picture."""
    song = picture_flac(tmp_path / "album" / "01 song.flac", "image/png")
    covers = art.Art(tmp_path / "covers")
    found = covers.find(song)
    assert found is not None
    assert found.read_bytes()[:3] == b"\xff\xd8\xff"     # the JPEG it really is


def test_a_correctly_labelled_cover_still_comes_out(tmp_path):
    song = picture_flac(tmp_path / "album" / "01 song.flac", "image/jpeg")
    covers = art.Art(tmp_path / "covers")
    assert covers.find(song) is not None


def test_what_a_file_is_comes_from_its_first_bytes(tmp_path):
    cases = {
        "a.bin": (b"\xff\xd8\xff\xe0JFIF", ".jpg"),
        "b.bin": (b"\x89PNG\r\n\x1a\n" + b"\0" * 8, ".png"),
        "c.bin": (b"RIFF\0\0\0\0WEBPVP8 ", ".webp"),
        "d.bin": (b"GIF89a" + b"\0" * 8, ".gif"),
        "e.bin": (b"not a picture at all", None),
        "f.bin": (b"", None),
    }
    for name, (head, expected) in cases.items():
        target = tmp_path / name
        target.write_bytes(head)
        assert art.Art._sniff(target) == expected, name


def test_the_working_file_is_not_left_behind(tmp_path):
    song = picture_flac(tmp_path / "album" / "01 song.flac", "image/png")
    covers = art.Art(tmp_path / "covers")
    covers.find(song)
    assert list((tmp_path / "covers").glob("*.raw")) == []
