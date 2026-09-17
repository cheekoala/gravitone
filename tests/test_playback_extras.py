"""Long gaps, ambient start offsets, hidden mode, banning, live volume."""

import random
import shutil
import subprocess
from pathlib import Path

import pytest

from gravitone import config as config_module, engine, library, mixer, player, playlists
from gravitone.config import MAX_GAP, Config
from gravitone.service import Session


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
    assert env["PULSE_PROP_application.name"] == "gravitone"
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


# -- ban asks before it bites ------------------------------------------


def test_ban_refuses_when_the_track_has_moved_on(session, tmp_path, monkeypatch):
    """A confirmation can arrive late; it must not ban whatever is on now."""
    album = tmp_path / "album"
    album.mkdir()
    for name in ("first.mp3", "second.mp3"):
        (album / name).write_bytes(b"\0")
    library.add_source(session.config, album, playlist=session.playlist)
    library.invalidate_cache()
    monkeypatch.setattr(session, "skip", lambda: None)

    session._on_event(engine.Event("track", path=album / "first.mp3"))
    session._on_event(engine.Event("track", path=album / "second.mp3"))  # moved on

    with pytest.raises(library.LibraryError, match="no longer playing"):
        session.ban("first.mp3")
    assert session.playlist.excluded == []

    result = session.ban("second.mp3")     # naming what is actually playing
    assert result["name"] == "second.mp3"


def test_ban_without_a_name_still_takes_what_is_playing(session, tmp_path, monkeypatch):
    album = tmp_path / "album"
    album.mkdir()
    (album / "only.mp3").write_bytes(b"\0")
    library.add_source(session.config, album, playlist=session.playlist)
    library.invalidate_cache()
    monkeypatch.setattr(session, "skip", lambda: None)

    session._on_event(engine.Event("track", path=album / "only.mp3"))
    assert session.ban()["name"] == "only.mp3"


def test_gravitone_play_prints_events_without_crashing(tmp_path, monkeypatch, capsys):
    """`gravitone play` walks the event callback for real - a track event has no
    duration, and formatting one used to raise."""
    from gravitone import cli, engine as engine_module, player as player_module

    monkeypatch.setenv("GRAVITONE_ROOT", str(tmp_path / "lib"))
    monkeypatch.setenv("GRAVITONE_CONFIG", str(tmp_path / "config.json"))
    album = tmp_path / "album"
    album.mkdir()
    (album / "song.mp3").write_bytes(b"\0")
    assert cli.main(["init"]) == 0
    assert cli.main(["folder", "add", str(album)]) == 0

    class Done:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        kill = terminate

    monkeypatch.setattr(
        player_module, "detect", lambda preferred=None: player_module.Backend("ffplay", "/bin/true")
    )
    monkeypatch.setattr(
        engine_module.player,
        "play",
        lambda backend, path, volume=70, duration=None, start=None, **rest:
            player_module.Playback(Done(), path),
    )
    monkeypatch.setattr(engine_module.time, "sleep", lambda _s: None)

    assert cli.main(["play", "--no-keys", "--no-loop", "--gap-min", "0", "--gap-max", "0"]) == 0
    out = capsys.readouterr().out
    assert "song.mp3" in out and "stopped" in out


# -- fades: sound arrives and leaves, it does not appear and vanish ------


def test_a_track_is_eased_in_and_out_when_its_length_is_known():
    made = player.Backend("ffplay", "/usr/bin/ffplay").fades(1.5, None, None, 200.0)
    assert made == "afade=t=in:st=0:d=1.500,afade=t=out:st=198.500:d=1.500"


def test_a_bed_fades_over_the_stretch_that_is_actually_played():
    """It is seeked into and cut to the gap, so the end is the gap's end."""
    made = player.Backend("ffplay", "/usr/bin/ffplay").fades(1.5, 30.0, 66.6, 300.0)
    assert made == "afade=t=in:st=0:d=1.500,afade=t=out:st=28.500:d=1.500"


def test_without_a_length_the_sound_still_arrives_gently():
    made = player.Backend("ffplay", "/usr/bin/ffplay").fades(1.5, None, None, None)
    assert made == "afade=t=in:st=0:d=1.500"


def test_two_fades_never_meet_in_the_middle_of_a_short_bed():
    made = player.Backend("ffplay", "/usr/bin/ffplay").fades(1.5, 2.0, None, None)
    assert made == "afade=t=in:st=0:d=0.667,afade=t=out:st=1.333:d=0.667"


def test_a_sliver_of_sound_is_left_alone():
    assert player.Backend("ffplay", "/usr/bin/ffplay").fades(1.5, 0.3, None, None) == ""


def test_fades_can_be_turned_off():
    assert player.Backend("ffplay", "/usr/bin/ffplay").fades(0, 30.0, None, 200.0) == ""


def test_the_player_is_told_to_fade():
    command = player.Backend("ffplay", "/usr/bin/ffplay").command(
        Path("/music/a.flac"), 70, None, None, 1.5, 200.0
    )
    assert "-af" in command
    assert command[command.index("-af") + 1].startswith("afade=t=in")


def test_a_player_without_a_fade_filter_is_left_alone():
    """mpv and the rest get their fade from the mixer instead of a filter."""
    for name in ("mpv", "afplay", "cvlc"):
        command = player.Backend(name, f"/usr/bin/{name}").command(
            Path("/music/a.flac"), 70, None, None, 1.5, 200.0
        )
        assert "-af" not in command
        assert not any("afade" in part for part in command)


class Fading:
    """A process that is still running, with a mixer that takes volumes."""

    def __init__(self, mixer_works=True):
        self.levels = []
        self.stopped = False
        self.mixer_works = mixer_works

    def poll(self):
        return None

    def set_volume(self, level):
        self.levels.append(level)
        return self.mixer_works

    def stop(self):
        self.stopped = True


def test_an_interrupted_sound_is_walked_down_before_it_stops(monkeypatch):
    """Skip has no filter to schedule: the mixer does the fading."""
    playback = player.Playback.__new__(player.Playback)
    fake = Fading()
    playback.process = fake
    playback.set_volume = fake.set_volume
    playback.stop = fake.stop
    slept = []

    assert playback.fade_out(0.8, volume=70, steps=8, sleep=slept.append) is True
    assert fake.levels == [61, 52, 44, 35, 26, 18, 9, 0]
    assert fake.stopped
    assert sum(slept) == pytest.approx(0.8)


def test_without_a_mixer_the_sound_simply_stops(monkeypatch):
    playback = player.Playback.__new__(player.Playback)
    fake = Fading(mixer_works=False)
    playback.process = fake
    playback.set_volume = fake.set_volume
    playback.stop = fake.stop
    slept = []

    assert playback.fade_out(0.8, volume=70, sleep=slept.append) is False
    assert fake.stopped
    assert slept == []          # nothing to wait for


def test_skipping_a_track_fades_it_rather_than_cutting_it(config, monkeypatch):
    faded = []

    class Playing:
        running = True

        def fade_out(self, seconds, volume=100, steps=8, sleep=None):
            faded.append((seconds, volume))
            return True

        def stop(self):
            faded.append(("cut", None))

        def wait(self, timeout=None):
            return 0

    eng = make_engine(config)
    eng.controls.skip()
    eng._wait_for_track(Playing())
    assert faded == [(min(config.fade, engine.Engine.INTERRUPT_FADE), config.volume)]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
def test_the_fade_filter_really_quietens_the_audio(tmp_path):
    """The flag is only a promise; this listens to what comes out."""
    import array
    import wave

    source = tmp_path / "tone.flac"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=8", str(source)],
        check=True,
    )
    made = player.Backend("ffplay", "/usr/bin/ffplay").fades(1.5, None, None, 8.0)
    rendered = {}
    for name, filters in (("plain", None), ("faded", made)):
        out = tmp_path / f"{name}.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(source)]
            + (["-af", filters] if filters else [])
            + [str(out)],
            check=True,
        )
        with wave.open(str(out)) as handle:
            rate, channels = handle.getframerate(), handle.getnchannels()
            raw = array.array("h")
            raw.frombytes(handle.readframes(handle.getnframes()))
        rendered[name] = (raw[::channels], rate)

    def loudest(name, start, end):
        mono, rate = rendered[name]
        piece = mono[int(start * rate):int(end * rate)]
        return max(abs(value) for value in piece) / 32768

    level = loudest("plain", 4.0, 4.1)
    assert level > 0.05, "the test tone should be audible to begin with"
    assert loudest("faded", 0, 0.05) < level / 8          # arrives from nothing
    assert loudest("faded", 7.95, 8.0) < level / 8        # and leaves the same way
    # ...and the stretch in between is untouched.
    assert loudest("faded", 4.0, 4.1) == pytest.approx(level, rel=0.02)


@pytest.mark.skipif(
    not (shutil.which("ffplay") and shutil.which("ffmpeg")),
    reason="ffplay is not installed",
)
def test_ffplay_accepts_the_command_we_build(tmp_path):
    """A filter ffmpeg understands is not automatically one ffplay takes."""
    import os

    source = tmp_path / "tone.flac"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", str(source)],
        check=True,
    )
    backend = player.Backend("ffplay", shutil.which("ffplay"))
    command = backend.command(source, 60, 0.6, 0.2, 1.5, 1.0)
    assert "-af" in command
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "SDL_AUDIODRIVER": "dummy"},
    )
    assert done.returncode == 0, done.stderr
    assert "error" not in done.stderr.lower(), done.stderr
