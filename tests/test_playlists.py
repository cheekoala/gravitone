"""Playlists: switchable sets, each with its own links and removals."""

import json

import pytest

from bgsoundtrack import config as config_module, library, playlists
from bgsoundtrack.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BGSOUNDTRACK_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("BGSOUNDTRACK_ROOT", str(tmp_path / "custom soundtrack"))
    return tmp_path


def make_album(directory, *names):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"\0" * 32)
    return directory


# -- the store ---------------------------------------------------------


def test_there_is_always_a_library_playlist():
    store = playlists.Store()
    assert store.current().id == "default"
    assert store.current().name == "Library"
    with pytest.raises(playlists.PlaylistError):
        store.remove("default")


def test_add_select_rename_remove():
    store = playlists.Store()
    first = store.add("Hollow Kingdom")
    assert first.id == "hollow-kingdom"

    store.select("Hollow Kingdom")           # by name
    assert store.current().id == first.id
    store.rename(first.id, "Hollow Kingdom II")
    assert store.current().name == "Hollow Kingdom II"

    store.remove(first.id)
    assert store.current().id == "default"   # falls back, never dangles


def test_duplicate_names_are_refused_but_similar_ids_are_not():
    store = playlists.Store()
    store.add("Rain")
    with pytest.raises(playlists.PlaylistError):
        store.add("rain")
    assert store.add("Rain!").id == "rain-2"


def test_every_edit_bumps_the_version(tmp_path):
    store = playlists.Store()
    versions = [store.version]
    store.add("A")
    versions.append(store.version)
    store.select("A")
    versions.append(store.version)
    store.rename("A", "B")
    versions.append(store.version)
    assert versions == sorted(set(versions))  # strictly increasing


def test_store_round_trips_through_json(env, tmp_path):
    store = playlists.load(Config())
    store.add("Field Work").music_sources.append(str(tmp_path / "recordings"))
    store.select("Field Work")
    store.save()

    reloaded = playlists.load(Config())
    assert reloaded.current().name == "Field Work"
    assert reloaded.current().music_sources == [str(tmp_path / "recordings")]
    assert json.loads((tmp_path / "playlists.json").read_text())["active"] == "field-work"


def test_legacy_sources_migrate_into_the_library_playlist(env, tmp_path):
    config = Config()
    config.music_sources = [str(tmp_path / "old")]
    store = playlists.load(config)
    assert store.current().music_sources == [str(tmp_path / "old")]
    assert (tmp_path / "playlists.json").exists()


# -- playlists in the library ------------------------------------------


@pytest.fixture
def config(tmp_path):
    cfg = Config(root=str(tmp_path / "custom soundtrack"))
    return cfg


def test_each_playlist_has_its_own_link_folder(config):
    store = playlists.Store()
    other = store.add("Night Drive")
    assert library.section_dir(config, "music") == config.music_dir
    assert library.section_dir(config, "music", other) == (
        config.root_path / "playlists" / "night-drive" / "music"
    )


def test_playlists_see_only_their_own_tracks(config, tmp_path):
    store = playlists.Store()
    one = store.add("One")
    two = store.add("Two")
    library.init(config, one)
    library.init(config, two)

    library.add_source(config, make_album(tmp_path / "a", "a.mp3"), playlist=one)
    library.add_source(config, make_album(tmp_path / "b", "b.mp3"), playlist=two)
    library.invalidate_cache()

    assert [e.name for e in library.entries(config, "music", one)] == ["a.mp3"]
    assert [e.name for e in library.entries(config, "music", two)] == ["b.mp3"]


def test_removing_a_source_track_is_remembered_per_playlist(config, tmp_path):
    store = playlists.Store()
    one, two = store.add("One"), store.add("Two")
    album = make_album(tmp_path / "album", "keep.mp3", "drop.mp3")
    library.add_source(config, album, playlist=one)
    library.add_source(config, album, playlist=two)
    library.invalidate_cache()

    how, target = library.remove_track(config, "drop.mp3", playlist=one)
    assert how == "removed"
    assert target == album / "drop.mp3"
    assert (album / "drop.mp3").exists()        # the file is not touched

    assert [e.name for e in library.entries(config, "music", one)] == ["keep.mp3"]
    assert sorted(e.name for e in library.entries(config, "music", two)) == [
        "drop.mp3",
        "keep.mp3",
    ]


def test_a_removal_survives_a_save_and_load(env, tmp_path):
    config = Config()
    store = playlists.load(config)
    playlist = store.add("Mix")
    album = make_album(tmp_path / "album", "drop.mp3")
    library.add_source(config, album, playlist=playlist)
    library.remove_track(config, "drop.mp3", playlist=playlist)
    store.save()

    reloaded = playlists.load(config).get("Mix")
    assert reloaded.excluded == [str(album / "drop.mp3")]
    library.invalidate_cache()
    assert library.entries(config, "music", reloaded) == []


def test_removing_a_linked_track_unlinks_it(config, tmp_path):
    store = playlists.Store()
    playlist = store.add("Linked")
    library.init(config, playlist)
    song = make_album(tmp_path / "src", "song.mp3") / "song.mp3"
    library.link(config, [song], playlist=playlist)

    how, _ = library.remove_track(config, "song.mp3", playlist=playlist)
    assert how == "unlinked"
    assert song.exists()
    assert library.entries(config, "music", playlist) == []
    assert playlist.excluded == []   # nothing to remember, the link is gone


def test_restore_puts_a_removed_track_back(config, tmp_path):
    store = playlists.Store()
    playlist = store.add("Mix")
    album = make_album(tmp_path / "album", "a.mp3")
    library.add_source(config, album, playlist=playlist)
    library.remove_track(config, "a.mp3", playlist=playlist)
    library.invalidate_cache()
    assert library.entries(config, "music", playlist) == []

    library.restore_track(config, str(album / "a.mp3"), playlist=playlist)
    library.invalidate_cache()
    assert [e.name for e in library.entries(config, "music", playlist)] == ["a.mp3"]

    with pytest.raises(library.LibraryError):
        library.restore_track(config, str(album / "a.mp3"), playlist=playlist)


def test_removing_something_not_in_the_playlist_errors(config):
    store = playlists.Store()
    with pytest.raises(library.LibraryError):
        library.remove_track(config, "ghost.mp3", playlist=store.current())


# -- switching while playing -------------------------------------------


def test_engine_rebuilds_its_queue_when_the_playlist_changes(config, tmp_path, monkeypatch):
    """Switching playlist has to land on the next track, not the next pass."""
    from bgsoundtrack import engine, player

    store = playlists.Store()
    one, two = store.add("One"), store.add("Two")
    library.add_source(config, make_album(tmp_path / "a", "a1.mp3", "a2.mp3"), playlist=one)
    library.add_source(config, make_album(tmp_path / "b", "b1.mp3"), playlist=two)
    library.invalidate_cache()
    store.select("One")

    played = []

    class Done:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        kill = terminate

    def fake_play(backend, path, volume=70, duration=None, **rest):
        played.append(path.name)
        return player.Playback(Done(), path)

    monkeypatch.setattr(engine.player, "play", fake_play)

    controls = engine.Controls()

    switched = []

    def on_event(event):
        if event.kind == "track" and not switched:
            switched.append(True)        # the listener switches during song one
            store.select("Two")
        if len(played) >= 3:
            controls.stop()

    runner = engine.Engine(
        config,
        player.Backend("ffplay", "/bin/true"),
        controls=controls,
        sleep=lambda _s: None,
        monotonic=lambda: 0.0,
        store=store,
    )
    runner.config.gap_min = runner.config.gap_max = 0
    runner.run(on_event)

    assert played[0].startswith("a")     # started in One
    assert played[1] == "b1.mp3"         # switched without waiting for a pass end
