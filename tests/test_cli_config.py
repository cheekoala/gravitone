import json
import os

import pytest

from bgsoundtrack import config as config_module
from bgsoundtrack.cli import main
from bgsoundtrack.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BGSOUNDTRACK_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("BGSOUNDTRACK_ROOT", str(tmp_path / "custom soundtrack"))
    return tmp_path


def test_defaults_are_sane():
    cfg = Config()
    cfg.validate()
    assert cfg.gap_min < cfg.gap_max
    assert 0.0 <= cfg.ambient_chance <= 1.0
    assert cfg.ambient_volume <= cfg.volume  # ambience sits under the music


def test_root_is_the_custom_soundtrack_folder(env):
    cfg = Config()
    assert cfg.root_path.name == "custom soundtrack"
    assert cfg.music_dir.name == "music"
    assert cfg.ambient_dir.name == "ambient"


def test_validate_rejects_bad_values():
    for kwargs in (
        {"gap_min": -1},
        {"gap_min": 30, "gap_max": 10},
        {"ambient_chance": 1.5},
        {"volume": 300},
    ):
        with pytest.raises(ValueError):
            Config(**kwargs).validate()


def test_round_trip_save_load(env, tmp_path):
    cfg = Config()
    config_module.set_value(cfg, "gap_max", "90")
    config_module.set_value(cfg, "shuffle", "off")
    path = config_module.save(cfg)
    assert json.loads(path.read_text())["gap_max"] == 90.0
    loaded = config_module.load()
    assert loaded.gap_max == 90.0 and loaded.shuffle is False


def test_unknown_config_key_rejected(env):
    with pytest.raises(KeyError):
        config_module.set_value(Config(), "tempo", "fast")


def test_cli_init_then_link_then_list(env, tmp_path, capsys):
    source = tmp_path / "songs"
    source.mkdir()
    (source / "theme.mp3").write_bytes(b"\0")
    rain = tmp_path / "rain.ogg"
    rain.write_bytes(b"\0")

    assert main(["init"]) == 0
    assert main(["link", str(source)]) == 0
    assert main(["link", "--ambient", str(rain)]) == 0
    capsys.readouterr()
    assert main(["list", "--targets"]) == 0
    out = capsys.readouterr().out
    assert "theme.mp3 ->" in out and "rain.ogg ->" in out


def test_cli_config_set_persists(env, capsys):
    assert main(["config", "gap_min=5", "gap_max=20"]) == 0
    out = capsys.readouterr().out
    assert "gap_min = 5.0" in out
    assert config_module.load().gap_max == 20.0


def test_cli_config_rejects_bad_assignment(env, capsys):
    assert main(["config", "gap_min"]) == 2
    assert main(["config", "nonsense=1"]) == 2


def test_cli_play_without_music_fails_cleanly(env, capsys, monkeypatch):
    from bgsoundtrack import player

    monkeypatch.setattr(
        player, "detect", lambda preferred=None: player.Backend("ffplay", "/bin/true")
    )
    main(["init"])
    assert main(["play", "--no-keys"]) == 1
    assert "nothing to play" in capsys.readouterr().err


def test_cli_prune_removes_broken_links(env, tmp_path, capsys):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"\0")
    main(["init"])
    main(["link", str(song)])
    song.unlink()
    capsys.readouterr()
    assert main(["prune"]) == 0
    assert "1 broken link(s) removed" in capsys.readouterr().out


# -- source folders ----------------------------------------------------


def test_legacy_config_sources_become_the_library_playlist(env, tmp_path):
    """Upgrading from a version without playlists must not lose the folders."""
    cfg = Config()
    config_module.set_value(cfg, "music_sources", f"{tmp_path / 'a'}{os.pathsep}{tmp_path / 'b'}")
    config_module.save(cfg)

    store = _store()
    assert store.current().name == "Library"
    assert store.current().sources("music") == [tmp_path / "a", tmp_path / "b"]
    assert store.current().sources("ambient") == []


def test_cli_folder_add_list_remove(env, tmp_path, capsys):
    album = tmp_path / "album"
    album.mkdir()
    (album / "a.mp3").write_bytes(b"\0")

    assert main(["folder", "add", str(album)]) == 0
    assert _store().current().music_sources == [str(album.resolve())]
    assert main(["source", "add", "--ambient", str(album)]) == 0
    capsys.readouterr()

    assert main(["source", "list"]) == 0   # the old name still works
    out = capsys.readouterr().out
    assert "music folders (1)" in out and "ambient folders (1)" in out

    assert main(["folder", "remove", str(album)]) == 0   # the new name
    assert _store().current().music_sources == []
    assert (album / "a.mp3").exists()


def _store():
    """The playlist store as it is on disk right now."""
    from bgsoundtrack import playlists

    return playlists.load(config_module.load())


def test_cli_list_marks_folder_tracks(env, tmp_path, capsys):
    album = tmp_path / "album"
    album.mkdir()
    (album / "a.mp3").write_bytes(b"\0")
    main(["init"])
    main(["folder", "add", str(album)])
    capsys.readouterr()
    assert main(["list", "--music"]) == 0
    assert "a.mp3  (folder)" in capsys.readouterr().out


def test_picker_reports_unavailable_without_tk(monkeypatch):
    from bgsoundtrack import picker

    monkeypatch.setattr(picker.subprocess, "run", _no_tk)
    result = picker.pick("folder")
    assert result.available is False and result.paths == []

    with pytest.raises(ValueError):
        picker.pick("everything")


def _no_tk(*args, **kwargs):
    class Done:
        returncode = 1
        stdout = ""
        stderr = "ModuleNotFoundError: No module named 'tkinter'"

    return Done()
