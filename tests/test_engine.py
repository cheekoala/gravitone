import random
import time
from pathlib import Path

import pytest

from gravitone import engine, library, player
from gravitone.config import Config


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

    def fake_play(
        backend, path, volume=70, duration=None, start=None, fade=0.0, length=None
    ):
        calls.append(
            {
                "path": path,
                "volume": volume,
                "duration": duration,
                "start": start,
                "fade": fade,
            }
        )
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
    from gravitone import service

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
    from gravitone import service

    monkeypatch.setattr(service.player, "detect", _raise_no_player)
    session = service.Session(config)
    with pytest.raises(player.PlaybackError):
        session.start()
    assert session.snapshot()["error"]


def _raise_no_player():
    raise player.PlaybackError("no audio player found")


# -- parties: the same evening, worked out separately --------------------


def party_roster(names=("a", "b", "c", "d"), seconds=30.0):
    from gravitone import party

    return party.Roster.of(
        [
            party.Member(
                id=party.track_id(name, "Artist", "Album"),
                duration=seconds,
                title=name,
                artist="Artist",
                album="Album",
                name=f"{name}.mp3",
            )
            for name in names
        ],
        [],
    )


def run_party(config, made, clock_start, seconds, have=None, controls=None):
    """Play a party from `clock_start` for `seconds`, recording what sounds."""
    backend = player.Backend(name="ffplay", executable="/usr/bin/ffplay")
    clock = FakeClock()
    clock.now = clock_start
    heard = []
    eng = engine.Engine(
        config,
        backend,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        party=made,
        finder=lambda: dict(have or {}),
        now=lambda: made.epoch + clock.now,
        controls=controls,
    )

    def watch(event):
        if event.kind == "done":
            return
        heard.append(
            (
                round(clock.now, 3),
                event.kind,
                event.path.name if event.path else (event.label or "-"),
                round(event.duration or 0, 3),
                round(event.start or 0, 3),
            )
        )
        if clock.now - clock_start >= seconds:
            eng.controls.stop()

    eng.run(on_event=watch)
    return heard


def test_two_machines_hear_the_same_thing_at_the_same_time(config, fake_player):
    """One starts at the beginning, one walks in twenty minutes late."""
    from gravitone import party

    config.gap_min, config.gap_max, config.ambient_chance = 5, 15, 0.0
    made = party.start(party_roster(), config, seed=4242, epoch=1_700_000_000)
    have = {m.id: Path(f"/music/{m.name}") for m in made.roster.music}

    host = run_party(config, made, 0.0, 1800, have)
    guest = run_party(config, made, 1200.0, 600, have)

    # The guest's first item is half over when they arrive; from the next one
    # on, every single thing matches the host - same instant, same track,
    # same length of quiet - with nothing having passed between them.
    overlap = [row for row in host if row[0] >= guest[1][0] - 0.02]
    shared = min(len(guest) - 1, len(overlap))
    assert shared >= 10
    for theirs, mine in zip(guest[1:1 + shared], overlap[:shared]):
        assert theirs[1:3] == mine[1:3]              # same kind, same track
        assert theirs[0] == pytest.approx(mine[0], abs=0.02)
        assert theirs[3] == pytest.approx(mine[3], abs=0.02)
    # ...and the guest really did walk in mid-item.
    assert guest[0][4] > 0 or guest[0][3] < 30


def test_a_track_you_do_not_have_holds_its_slot(config, fake_player):
    """Silence for you, music for them, and the next track still on time."""
    from gravitone import party

    config.gap_min = config.gap_max = 10
    config.ambient_chance = 0.0
    made = party.start(party_roster(seconds=30.0), config, seed=7, epoch=1_700_000_000)
    missing = made.roster.music[0]
    have = {m.id: Path(f"/music/{m.name}") for m in made.roster.music if m.id != missing.id}

    heard = run_party(config, made, 0.0, 300, have)
    absent = [row for row in heard if row[1] == "absent"]
    assert absent, "the missing track should still take its turn"
    assert absent[0][2] == missing.label
    assert absent[0][3] == 30.0                      # its whole length
    starts = [row[0] for row in heard]
    assert starts == sorted(starts)
    # Every item begins where the one before it ended: nothing slid.
    for (at, _, _, length, _), (next_at, *_) in zip(heard, heard[1:]):
        assert next_at == pytest.approx(at + length)


def test_skipping_in_a_party_sits_the_track_out(config, fake_player):
    """The sound stops; the slot does not, so you rejoin at the next one."""
    from gravitone import party

    config.gap_min = config.gap_max = 10
    config.ambient_chance = 0.0
    made = party.start(party_roster(seconds=30.0), config, seed=5, epoch=1_700_000_000)
    have = {m.id: Path(f"/music/{m.name}") for m in made.roster.music}

    controls = engine.Controls()
    controls.skip()                    # skip the moment it starts
    heard = run_party(config, made, 0.0, 120, have, controls=controls)
    for (at, _, _, length, _), (next_at, *_) in zip(heard, heard[1:]):
        assert next_at == pytest.approx(at + length)


def test_joining_late_seeks_into_the_track(config, fake_player):
    from gravitone import party

    config.gap_min = config.gap_max = 10
    config.ambient_chance = 0.0
    made = party.start(party_roster(seconds=60.0), config, seed=9, epoch=1_700_000_000)
    have = {m.id: Path(f"/music/{m.name}") for m in made.roster.music}

    run_party(config, made, 25.0, 5, have)
    first = fake_player[0]
    assert first["start"] == pytest.approx(25.0)
    assert first["duration"] == pytest.approx(35.0)   # only what is left of it


def test_a_party_ignores_the_local_shuffle_and_gap_settings(config, fake_player):
    """The party's settings are the party's, or the gaps would not line up."""
    from gravitone import party

    config.gap_min = config.gap_max = 10
    config.ambient_chance = 0.0
    made = party.start(party_roster(seconds=30.0), config, seed=3, epoch=1_700_000_000)
    have = {m.id: Path(f"/music/{m.name}") for m in made.roster.music}

    config.gap_min, config.gap_max = 120, 240        # changed after joining
    heard = run_party(config, made, 0.0, 200, have)
    gaps = [row[3] for row in heard if row[1] == "silence"]
    assert gaps and all(gap == 10 for gap in gaps)
