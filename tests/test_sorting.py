"""Sorting the library by what the tags (or the paths) say."""

import json

import pytest

from bgsoundtrack import library, playlists, tags
from bgsoundtrack.config import Config


@pytest.fixture
def reader(tmp_path):
    return tags.Reader(tmp_path / "tags.json")


def test_the_path_is_read_when_there_are_no_tags(tmp_path):
    path = tmp_path / "Rakka" / "Deep Field" / "03 - Ossuary.flac"
    guessed = tags.guess(path)
    assert guessed.artist == "Rakka"
    assert guessed.album == "Deep Field"
    assert guessed.title == "Ossuary"
    assert guessed.track == 3
    assert guessed.guessed is True


@pytest.mark.parametrize(
    "name, title, track",
    [
        ("01 Ashes.flac", "Ashes", 1),
        ("01 - Ashes.flac", "Ashes", 1),
        ("01. Ashes.flac", "Ashes", 1),
        ("Ashes.flac", "Ashes", 0),
        ("12_Ashes.flac", "Ashes", 12),
    ],
)
def test_track_numbers_are_peeled_off_titles(tmp_path, name, title, track):
    guessed = tags.guess(tmp_path / name)
    assert (guessed.title, guessed.track) == (title, track)


def test_unknown_fields_sort_last(reader):
    known = tags.Tags(title="B", artist="Someone", album="An album")
    unknown = tags.Tags(title="A", artist="", album="")
    assert known.key("artist") < unknown.key("artist")


def test_the_cache_survives_a_new_reader(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"\0")
    first = tags.Reader(tmp_path / "tags.json")
    first.store(song, tags.Tags(title="Real Title", artist="Real Artist"))
    first._dirty = True
    first.save()

    second = tags.Reader(tmp_path / "tags.json")
    assert second.cached(song).title == "Real Title"
    assert second.pending([song]) == []


def test_a_changed_file_invalidates_its_cached_tags(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"\0")
    reader = tags.Reader(tmp_path / "tags.json")
    reader.store(song, tags.Tags(title="Before"))
    assert reader.cached(song).title == "Before"

    song.write_bytes(b"\0" * 4096)   # edited in place
    assert reader.cached(song) is None


# -- the library ordering ----------------------------------------------


@pytest.fixture
def library_of(tmp_path):
    config = Config(root=str(tmp_path / "lib"))
    playlist = playlists.Playlist(id="default", name="Library")
    library.init(config, playlist)
    tree = tmp_path / "music"
    for artist, record, songs in (
        ("Vela", "Undertow", ["02 Bell.flac", "01 Anchor.flac"]),
        ("Auro", "Glasshouse", ["01 Zenith.flac"]),
    ):
        folder = tree / artist / record
        folder.mkdir(parents=True)
        for song in songs:
            (folder / song).write_bytes(b"\0")
    library.add_source(config, tree, playlist=playlist)
    library.invalidate_cache()
    return config, playlist, tmp_path


def names(entries):
    return [entry.name for entry in entries]


def test_sorting_by_file_name(library_of, reader):
    config, playlist, _ = library_of
    found = library.entries(config, "music", playlist, sort="name", reader=reader)
    assert names(found) == ["01 Anchor.flac", "01 Zenith.flac", "02 Bell.flac"]


def test_sorting_by_artist_then_album_then_track(library_of, reader):
    config, playlist, _ = library_of
    found = library.entries(config, "music", playlist, sort="artist", reader=reader)
    assert names(found) == ["01 Zenith.flac", "01 Anchor.flac", "02 Bell.flac"]


def test_sorting_by_album(library_of, reader):
    config, playlist, _ = library_of
    found = library.entries(config, "music", playlist, sort="album", reader=reader)
    assert names(found) == ["01 Zenith.flac", "01 Anchor.flac", "02 Bell.flac"]


def test_sorting_by_title(library_of, reader):
    config, playlist, _ = library_of
    found = library.entries(config, "music", playlist, sort="title", reader=reader)
    assert names(found) == ["01 Anchor.flac", "02 Bell.flac", "01 Zenith.flac"]


def test_the_setting_drives_the_default_order(library_of, reader):
    config, playlist, _ = library_of
    config.sort_by = "title"
    assert names(library.entries(config, "music", playlist, reader=reader))[0] == "01 Anchor.flac"


def test_an_unknown_sort_is_refused_by_the_config():
    with pytest.raises(ValueError, match="sort_by"):
        Config(sort_by="vibes").validate()


def test_sorting_also_sets_the_play_order_without_shuffle(library_of, reader):
    config, playlist, _ = library_of
    config.sort_by = "title"
    played = library.tracks(config, "music", playlist)
    assert [path.name for path in played][0] == "01 Anchor.flac"
