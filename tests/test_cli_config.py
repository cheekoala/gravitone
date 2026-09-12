import json

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
    assert "no music" in capsys.readouterr().err


def test_cli_prune_removes_broken_links(env, tmp_path, capsys):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"\0")
    main(["init"])
    main(["link", str(song)])
    song.unlink()
    capsys.readouterr()
    assert main(["prune"]) == 0
    assert "1 broken link(s) removed" in capsys.readouterr().out
