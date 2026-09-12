import random
import time
from pathlib import Path

import pytest

from bgsoundtrack import engine, library, player
from bgsoundtrack.config import Config


class FakeProcess:
    """A child process that 'finishes' after a fixed number of polls."""

    def __init__(self, polls=1):
        self._left = polls

    def poll(self):
        if self._left > 0:
            self._left -= 1
            return None
        return 0

    def wait(self, timeout=None):
        self._left = 0
        return 0

    def terminate(self):
        self._left = 0

    def kill(self):
        self._left = 0


@pytest.fixture
def fake_player(monkeypatch):
    calls = []

    def fake_play(backend, path, volume=70, duration=None):
        calls.append({"path": path, "volume": volume, "duration": duration})
        return player.Playback(FakeProcess(), path)

    monkeypatch.setattr(player, "play", fake_play)
    monkeypatch.setattr(engine.player, "play", fake_play)
    return calls


@pytest.fixture
def config(tmp_path):
    cfg = Config(root=str(tmp_path / "lib"), gap_min=1, gap_max=2)
    library.init(cfg)
    for name in ("a.mp3", "b.mp3"):
        (cfg.music_dir / name).write_bytes(b"\0")
    (cfg.ambient_dir / "rain.ogg").write_bytes(b"\0")
    return cfg


class FakeClock:
    """Wall clock the test drives, so gaps cost no real time."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


def make_engine(config, **kwargs):
    backend = player.Backend(name="ffplay", executable="/usr/bin/ffplay")
    clock = FakeClock()
    kwargs.setdefault("sleep", clock.sleep)
    kwargs.setdefault("monotonic", clock.monotonic)
    kwargs.setdefault("rng", random.Random(0))
    return engine.Engine(config, backend, **kwargs)


def test_gap_length_within_bounds(config):
    eng = make_engine(config)
    for _ in range(200):
        assert config.gap_min <= eng.gap_length() <= config.gap_max


def test_ambient_chance_zero_means_always_silence(config):
    config.ambient_chance = 0.0
    eng = make_engine(config)
    ambient = [Path("rain.ogg")]
    assert all(eng.pick_ambient(ambient, 30) is None for _ in range(50))


def test_ambient_chance_one_means_always_ambience(config):
    config.ambient_chance = 1.0
    eng = make_engine(config)
    ambient = [Path("rain.ogg"), Path("wind.ogg")]
    assert all(eng.pick_ambient(ambient, 30) in ambient for _ in range(50))


def test_very_short_gaps_stay_silent(config):
    config.ambient_chance = 1.0
    config.ambient_min_tail = 5.0
    eng = make_engine(config)
    assert eng.pick_ambient([Path("rain.ogg")], 2.0) is None


def test_no_ambient_library_means_silence(config):
    config.ambient_chance = 1.0
    eng = make_engine(config)
    assert eng.pick_ambient([], 30) is None


def test_single_pass_plays_every_track_once(config, fake_player):
    config.loop = False
    events = []
    make_engine(config).run(events.append)
    played = [e.path.name for e in events if e.kind == "track"]
    assert sorted(played) == ["a.mp3", "b.mp3"]
    assert events[-1].kind == "done"


def test_gap_follows_every_track(config, fake_player):
    config.loop = False
    events = []
    make_engine(config).run(events.append)
    kinds = [e.kind for e in events if e.kind in ("track", "ambient", "silence")]
    assert kinds[0] == "track"
    assert len(kinds) == 4  # track, gap, track, gap
    assert kinds[1] in ("ambient", "silence")


def test_ambient_is_cut_to_the_gap_and_uses_ambient_volume(config, fake_player):
    config.loop = False
    config.ambient_chance = 1.0
    config.ambient_min_tail = 0.0
    config.volume = 80
    config.ambient_volume = 30
    events = []
    make_engine(config).run(events.append)

    music_calls = [c for c in fake_player if c["path"].name.endswith(".mp3")]
    ambient_calls = [c for c in fake_player if c["path"].name == "rain.ogg"]
    assert all(c["volume"] == 80 and c["duration"] is None for c in music_calls)
    assert ambient_calls
    for call in ambient_calls:
        assert call["volume"] == 30
        assert config.gap_min <= call["duration"] <= config.gap_max


def test_shuffle_off_preserves_order(config, fake_player):
    config.loop = False
    config.shuffle = False
    events = []
    make_engine(config).run(events.append)
    assert [e.path.name for e in events if e.kind == "track"] == ["a.mp3", "b.mp3"]


def test_shuffled_cycle_is_a_permutation():
    tracks = [Path(f"{i}.mp3") for i in range(20)]
    order = engine.shuffled_cycle(tracks, random.Random(1))
    assert sorted(order) == sorted(tracks)


def test_stop_ends_the_session(config, fake_player):
    controls = engine.Controls()
    events = []

    def on_event(event):
        events.append(event)
        if len([e for e in events if e.kind == "track"]) == 1:
            controls.stop()

    make_engine(config, controls=controls).run(on_event)
    assert len([e for e in events if e.kind == "track"]) == 1
    assert events[-1].kind == "done"


def test_empty_music_library_is_an_error(tmp_path):
    cfg = Config(root=str(tmp_path / "empty"))
    library.init(cfg)
    with pytest.raises(RuntimeError, match="nothing to play"):
        make_engine(cfg).run()


def test_loop_keeps_going_past_one_pass(config, fake_player):
    controls = engine.Controls()
    events = []

    def on_event(event):
        events.append(event)
        if len([e for e in events if e.kind == "track"]) == 5:
            controls.stop()

    make_engine(config, controls=controls).run(on_event)
    assert len([e for e in events if e.kind == "track"]) == 5


def test_backend_commands_carry_volume_and_duration():
    path = Path("/tmp/rain.ogg")
    ffplay = player.Backend("ffplay", "/usr/bin/ffplay").command(path, 40, 12.0)
    assert "-volume" in ffplay and "40" in ffplay and "-t" in ffplay
    mpv = player.Backend("mpv", "/usr/bin/mpv").command(path, 40, 12.0)
    assert "--volume=40" in mpv and "--length=12.000" in mpv
    no_limit = player.Backend("mpv", "/usr/bin/mpv").command(path, 40, None)
    assert not any(arg.startswith("--length") for arg in no_limit)
    assert no_limit[-1] == str(path)


def test_detect_reports_missing_player(monkeypatch):
    monkeypatch.setattr(player.shutil, "which", lambda name: None)
    with pytest.raises(player.PlaybackError, match="no audio player"):
        player.detect()


def test_detect_prefers_requested_backend(monkeypatch):
    monkeypatch.setattr(player.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert player.detect("mpv").name == "mpv"
    assert player.detect("vlc").name == "cvlc"


# -- session service ---------------------------------------------------


def test_session_reports_what_is_playing(config, fake_player, monkeypatch):
    from bgsoundtrack import service

    monkeypatch.setattr(service.player, "detect", lambda: player.Backend("ffplay", "/bin/true"))
    monkeypatch.setattr(service.player, "probe_duration", lambda path: 180.0)
    session = service.Session(config)
    session.start()
    for _ in range(100):
        if session.snapshot()["now"]:
            break
        time.sleep(0.02)
    state = session.snapshot()
    assert state["running"] is True
    assert state["now"]["name"].endswith(".mp3")
    assert state["backend"] == "ffplay"
    session.stop()
    assert session.snapshot()["running"] is False


def test_session_start_without_a_player_raises(config, monkeypatch):
    from bgsoundtrack import service

    monkeypatch.setattr(service.player, "detect", _raise_no_player)
    session = service.Session(config)
    with pytest.raises(player.PlaybackError):
        session.start()
    assert session.snapshot()["error"]


def _raise_no_player():
    raise player.PlaybackError("no audio player found")
