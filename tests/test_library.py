import os
from pathlib import Path

import pytest

from bgsoundtrack import library
from bgsoundtrack.config import Config


@pytest.fixture
def config(tmp_path):
    cfg = Config(root=str(tmp_path / "custom soundtrack"))
    library.init(cfg)
    return cfg


def make_audio(directory: Path, name: str, size: int = 1024) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\0" * size)
    return path


def test_init_creates_music_and_ambient(tmp_path):
    cfg = Config(root=str(tmp_path / "lib"))
    created = library.init(cfg)
    assert cfg.music_dir.is_dir() and cfg.ambient_dir.is_dir()
    assert len(created) == 2
    assert library.init(cfg) == []


def test_link_creates_symlinks_not_copies(config, tmp_path):
    source = make_audio(tmp_path / "music", "song.mp3", size=4096)
    result = library.link(config, [source])
    link = config.music_dir / "song.mp3"
    assert result.linked == [link]
    assert link.is_symlink()
    assert link.resolve() == source.resolve()
    assert os.lstat(link).st_size < 4096  # the link, not the audio


def test_link_directory_recurses_and_filters(config, tmp_path):
    make_audio(tmp_path / "album", "a.mp3")
    make_audio(tmp_path / "album" / "disc2", "b.flac")
    make_audio(tmp_path / "album", "cover.jpg")
    result = library.link(config, [tmp_path / "album"])
    assert sorted(p.name for p in result.linked) == ["a.mp3", "b.flac"]


def test_link_no_recursive_stays_shallow(config, tmp_path):
    make_audio(tmp_path / "album", "a.mp3")
    make_audio(tmp_path / "album" / "disc2", "b.mp3")
    result = library.link(config, [tmp_path / "album"], recursive=False)
    assert [p.name for p in result.linked] == ["a.mp3"]


def test_link_skips_duplicate_targets(config, tmp_path):
    source = make_audio(tmp_path / "music", "song.mp3")
    library.link(config, [source])
    result = library.link(config, [source])
    assert result.linked == []
    assert result.skipped[0][1] == "already in library"


def test_link_renames_on_name_collision(config, tmp_path):
    first = make_audio(tmp_path / "a", "theme.ogg")
    second = make_audio(tmp_path / "b", "theme.ogg")
    library.link(config, [first])
    result = library.link(config, [second])
    assert [p.name for p in result.linked] == ["theme-2.ogg"]
    assert (config.music_dir / "theme-2.ogg").resolve() == second.resolve()


def test_link_relative_survives_moving_the_library(config, tmp_path):
    source = make_audio(config.root_path.parent / "src", "s.mp3")
    library.link(config, [source], relative=True)
    target = os.readlink(config.music_dir / "s.mp3")
    assert not os.path.isabs(target)


def test_ambient_section_is_separate(config, tmp_path):
    song = make_audio(tmp_path / "m", "song.mp3")
    rain = make_audio(tmp_path / "a", "rain.ogg")
    library.link(config, [song])
    library.link(config, [rain], section="ambient")
    assert [p.name for p in library.tracks(config, "music")] == ["song.mp3"]
    assert [p.name for p in library.tracks(config, "ambient")] == ["rain.ogg"]


def test_unknown_section_rejected(config):
    with pytest.raises(library.LibraryError):
        library.tracks(config, "sfx")


def test_broken_links_are_hidden_and_prunable(config, tmp_path):
    source = make_audio(tmp_path / "m", "gone.mp3")
    library.link(config, [source])
    source.unlink()
    assert library.tracks(config, "music") == []
    assert [p.name for p in library.broken(config, "music")] == ["gone.mp3"]
    removed = library.prune(config)
    assert [p.name for p in removed] == ["gone.mp3"]
    assert library.broken(config, "music") == []


def test_unlink_refuses_real_files(config, tmp_path):
    real = make_audio(config.music_dir, "real.mp3")
    with pytest.raises(library.LibraryError):
        library.unlink(config, ["real.mp3"])
    assert real.exists()


def test_unlink_removes_link_only(config, tmp_path):
    source = make_audio(tmp_path / "m", "song.mp3")
    library.link(config, [source])
    library.unlink(config, ["song.mp3"])
    assert not (config.music_dir / "song.mp3").is_symlink()
    assert source.exists()


def test_link_missing_path_errors(config, tmp_path):
    with pytest.raises(library.LibraryError):
        library.link(config, [tmp_path / "nope"])
