"""Long gaps, ambient start offsets, hidden mode, banning, live volume."""

import random
import subprocess
from pathlib import Path

import pytest

from bgsoundtrack import config as config_module, engine, library, mixer, player, playlists
from bgsoundtrack.config import MAX_GAP, Config
from bgsoundtrack.service import Session


@pytest.fixture
def config(tmp_path):
    cfg = Config(root=str(tmp_path / "lib"), gap_min=1, gap_max=2)
    library.init(cfg)
    for name in ("a.mp3", "b.mp3"):
        (cfg.music_dir / name).write_bytes(b"\0")
    (cfg.ambient_dir / "rain.ogg").write_bytes(b"\0")
    return cfg


def make_engine(config, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("monotonic", lambda: 0.0)
    kwargs.setdefault("rng", random.Random(0))
    return engine.Engine(config, player.Backend("ffplay", "/usr/bin/ffplay"), **kwargs)


# -- gaps up to an hour -------------------------------------------------


def test_gaps_may_run_to_an_hour():
    cfg = Config(gap_min=600, gap_max=MAX_GAP)
    cfg.validate()
    assert MAX_GAP == 3600


def test_gaps_longer_than_an_hour_are_refused():
    with pytest.raises(ValueError, match="one hour"):
        Config(gap_min=0, gap_max=MAX_GAP + 1).validate()


def test_long_gaps_are_still_drawn_from_the_whole_range(config):
    config.gap_min, config.gap_max = 60, 3600
    eng = make_engine(config)
    draws = [eng.gap_length() for _ in range(500)]
    assert min(draws) >= 60 and max(draws) <= 3600
    assert max(draws) > 1800  # the top half is reachable, not just the floor


# -- ambient start offset ----------------------------------------------


def test_ambient_starts_somewhere_that_still_covers_the_gap(config, monkeypatch):
    monkeypatch.setattr(engine.player, "probe_duration", lambda path: 300.0)
    eng = make_engine(config)
    starts = [eng.ambient_start(Path("rain.ogg"), 30.0) for _ in range(200)]
    assert all(0 <= s <= 270 for s in starts)
    assert max(starts) > 10          # it really does move around


def test_ambient_starts_at_the_top_when_the_flag_is_off(config, monkeypatch):
    monkeypatch.setattr(engine.player, "probe_duration", lambda path: 300.0)
    config.ambient_random_start = False
    assert make_engine(config).ambient_start(Path("rain.ogg"), 30.0) == 0.0


def test_ambient_starts_at_the_top_without_ffprobe(config, monkeypatch):
    monkeypatch.setattr(engine.player, "probe_duration", lambda path: None)
    assert make_engine(config).ambient_start(Path("rain.ogg"), 30.0) == 0.0


def test_a_short_ambient_track_is_not_seeked(config, monkeypatch):
    monkeypatch.setattr(engine.player, "probe_duration", lambda path: 20.0)
    assert make_engine(config).ambient_start(Path("rain.ogg"), 30.0) == 0.0


def test_the_offset_reaches_the_player_command():
    backend = player.Backend("ffplay", "/usr/bin/ffplay")
    command = backend.command(Path("rain.ogg"), 40, 30.0, 12.5)
    assert "-ss" in command and "12.500" in command
    assert command.index("-ss") < command.index("-t")   # seek before length
    assert "--start=12.500" in player.Backend("mpv", "/usr/bin/mpv").command(
        Path("rain.ogg"), 40, 30.0, 12.5
    )
    assert "-ss" not in backend.command(Path("rain.ogg"), 40, 30.0, None)


# -- hidden mode --------------------------------------------------------


@pytest.fixture
def session(config, tmp_path):
    return Session(config, tmp_path / "config.json", store=playlists.Store())


def playing(session, kind, duration):
    session._on_event(engine.Event(kind, path=Path(f"/x/{kind}.ogg"), duration=duration))


def test_gap_times_are_withheld_in_hidden_mode(session):
    session.config.hide_gaps = True
    playing(session, "silence", 42.0)
    now = session.snapshot()["now"]
    assert now["duration"] is None and now["elapsed"] is None
    assert session.snapshot()["hidden"] is True


def test_hidden_mode_still_shows_the_song(session):
    session.config.hide_gaps = True
    playing(session, "track", 180.0)
    now = session.snapshot()["now"]
    assert now["duration"] == 180.0 and now["elapsed"] is not None


def test_gap_times_are_shown_when_not_hidden(session):
    playing(session, "ambient", 42.0)
    now = session.snapshot()["now"]
    assert now["duration"] == 42.0 and now["elapsed"] is not None


# -- ban ----------------------------------------------------------------


def test_ban_removes_the_playing_track_and_skips(session, tmp_path, monkeypatch):
    album = tmp_path / "album"
    album.mkdir()
    (album / "song.mp3").write_bytes(b"\0")
    library.add_source(session.config, album, playlist=session.playlist)
    library.invalidate_cache()

    skipped = []
    monkeypatch.setattr(session, "skip", lambda: skipped.append(True))
    session._on_event(engine.Event("track", path=album / "song.mp3"))

    result = session.ban()
    assert result["how"] == "removed" and skipped == [True]
    assert (album / "song.mp3").exists()
    library.invalidate_cache()
    names = [e.name for e in library.entries(session.config, "music", session.playlist)]
    assert "song.mp3" not in names


def test_ban_during_ambience_bans_the_ambient_track(session, tmp_path, monkeypatch):
    monkeypatch.setattr(session, "skip", lambda: None)
    real = tmp_path / "weather" / "storm.ogg"
    real.parent.mkdir()
    real.write_bytes(b"\0")
    library.link(session.config, [real], section="ambient", playlist=session.playlist)

    session._on_event(engine.Event("ambient", path=session.config.ambient_dir / "storm.ogg"))
    result = session.ban()
    assert result["how"] == "unlinked"
    assert not (session.config.ambient_dir / "storm.ogg").is_symlink()
    assert real.exists()   # the file it pointed at is untouched


def test_banning_a_real_file_in_the_library_folder_only_hides_it(session):
    """Someone who copied files in instead of linking must not lose them."""
    real = session.config.music_dir / "a.mp3"
    result = library.remove_track(session.config, "a.mp3", playlist=session.playlist)
    assert result[0] == "removed"
    assert real.exists()
    library.invalidate_cache()
    assert "a.mp3" not in [
        e.name for e in library.entries(session.config, "music", session.playlist)
    ]


def test_ban_with_nothing_playing_is_an_error(session):
    with pytest.raises(library.LibraryError):
        session.ban()


def test_ban_during_silence_is_an_error(session):
    session._on_event(engine.Event("silence", duration=30))
    with pytest.raises(library.LibraryError):
        session.ban()


# -- live volume --------------------------------------------------------


def test_the_engine_pushes_volume_at_what_is_playing(config, monkeypatch):
    asked = []

    class FakePlayback:
        def set_volume(self, volume):
            asked.append(volume)
            return True

    eng = make_engine(config)
    assert eng.live_volume() is False        # nothing playing yet

    eng._hold(FakePlayback(), "track")
    config.volume = 33
    assert eng.live_volume() is True
    eng._hold(FakePlayback(), "ambient")
    config.ambient_volume = 12
    eng.live_volume()
    assert asked == [33, 12]


def test_players_are_tagged_for_the_desktop_mixer():
    env = mixer.environment()
    assert env["PULSE_PROP_application.name"] == "bgst"
    assert env["PULSE_PROP_media.role"] == "music"


def test_the_mixer_finds_our_stream_and_sets_it(monkeypatch, tmp_path):
    """pactl is the interface, so drive a fake one and check both calls."""
    calls = []
    listing = (
        '[{"index": 7, "properties": {"application.process.id": "4242"}},'
        ' {"index": 9, "properties": {"application.process.id": "1"}}]'
    )

    class Done:
        returncode = 0

        def __init__(self, stdout=""):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        calls.append(command[1:])
        return Done(listing if "list" in command else "")

    monkeypatch.setattr(mixer.shutil, "which", lambda name: "/usr/bin/pactl")
    monkeypatch.setattr(mixer.subprocess, "run", fake_run)

    assert mixer.sink_inputs_for(4242) == ["7"]
    assert mixer.set_volume(4242, 55) is True
    assert calls[-1] == ["set-sink-input-volume", "7", "55%"]


def test_the_mixer_is_a_no_op_without_pactl(monkeypatch):
    monkeypatch.setattr(mixer.shutil, "which", lambda name: None)
    assert mixer.available() is False
    assert mixer.set_volume(1, 50) is False
