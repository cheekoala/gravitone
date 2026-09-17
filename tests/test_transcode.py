"""Making the copy smaller: a bundle nobody can download is not a bundle."""

import json
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from gravitone import library, playlists, tags, transcode, transfer
from gravitone.config import Config

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")


def music(path: Path, seconds: float = 3.0, art: bool = True) -> Path:
    """A real audio file, with tags and a cover, for a real conversion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
    ]
    if art:
        # A finished picture rather than a second lavfi stream: limiting a
        # generated video with -frames:v ends the whole encode, audio too.
        cover = path.parent / "cover.jpg"
        if not cover.exists():
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                 "-i", "color=c=orange:s=120x120:d=1", "-frames:v", "1", str(cover)],
                check=True,
            )
        command += ["-i", str(cover), "-map", "0:a", "-map", "1:v",
                    "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
    command += [
        "-metadata", "title=A Song", "-metadata", "artist=Someone",
        "-metadata", "album=A Record", str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return path


def probe(path: Path) -> dict:
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True,
    ).stdout)


# -- the settings themselves ---------------------------------------------


def test_keeping_the_original_is_the_default():
    assert transcode.get(None).copies
    assert transcode.get("original").suffix is None
    assert transcode.available(transcode.get("original"))


def test_a_setting_that_does_not_exist_says_what_does():
    with pytest.raises(transcode.TranscodeError) as raised:
        transcode.get("wav-please")
    assert "mp3" in str(raised.value)


def test_converting_changes_the_extension():
    assert transcode.renamed("01 Song.flac", transcode.get("mp3")) == "01 Song.mp3"
    assert transcode.renamed("01 Song.flac", transcode.get("opus")) == "01 Song.opus"
    assert transcode.renamed("01 Song.flac", transcode.get("original")) == "01 Song.flac"


def test_the_guess_is_length_times_bitrate():
    preset = transcode.get("mp3")            # 245 kbps
    # Ten minutes of music at 245 kbps is about 18 MB.
    assert transcode.estimate([0], [600.0], preset) == pytest.approx(18.4e6, rel=0.02)


def test_keeping_the_original_guesses_the_size_exactly():
    assert transcode.estimate([100, 200], [1, 2], transcode.get("original")) == 300


def test_a_track_of_unknown_length_is_not_left_out_of_the_guess():
    """Guessing three minutes beats pretending it weighs nothing."""
    preset = transcode.get("mp3")
    assert transcode.estimate([0], [0.0], preset) > 5e6


# -- the conversion -------------------------------------------------------


@needs_ffmpeg
def test_an_mp3_keeps_its_tags_and_its_cover(tmp_path):
    source = music(tmp_path / "song.flac")
    made = transcode.convert(source, tmp_path / "song.mp3", transcode.get("mp3"))

    info = probe(made)
    assert info["format"]["tags"]["title"] == "A Song"
    assert info["format"]["tags"]["album"] == "A Record"
    kinds = [stream["codec_type"] for stream in info["streams"]]
    assert "video" in kinds, "the cover should ride along"


@needs_ffmpeg
def test_opus_keeps_its_tags(tmp_path):
    """Ogg puts them on the stream rather than the container."""
    source = music(tmp_path / "song.flac")
    made = transcode.convert(source, tmp_path / "song.opus", transcode.get("opus"))

    audio = [s for s in probe(made)["streams"] if s["codec_type"] == "audio"][0]
    assert audio["tags"]["title"] == "A Song"
    assert audio["tags"]["artist"] == "Someone"


@needs_ffmpeg
def test_the_original_is_never_touched(tmp_path):
    source = music(tmp_path / "song.flac")
    before = source.read_bytes()
    transcode.convert(source, tmp_path / "out.mp3", transcode.get("mp3"))
    assert source.read_bytes() == before


@needs_ffmpeg
def test_something_that_is_not_audio_is_refused_by_name(tmp_path):
    rubbish = tmp_path / "notes.flac"
    rubbish.write_bytes(b"this is not a flac")
    with pytest.raises(transcode.TranscodeError) as raised:
        transcode.convert(rubbish, tmp_path / "out.mp3", transcode.get("mp3"))
    assert "notes.flac" in str(raised.value)
    assert not (tmp_path / "out.mp3").exists()


# -- bundles --------------------------------------------------------------


@pytest.fixture
def packed(tmp_path):
    """A playlist of real audio, ready to bundle."""
    album = tmp_path / "album"
    for index in range(4):
        music(album / f"{index:02d} Song.flac")
    config = Config(root=str(tmp_path / "custom soundtrack"))
    store = playlists.Store()
    library.init(config, store.current())
    library.link(config, [album], playlist=store.current())
    library.invalidate_cache()
    reader = tags.Reader(tmp_path / "tags.json")
    reader.read_all([e.target for e in library.entries(config, "music", store.current())])
    return config, store, reader


@needs_ffmpeg
def test_a_bundle_can_be_converted_on_the_way_in(packed, tmp_path):
    config, store, _ = packed
    out = transfer.export_bundle(config, store, tmp_path / "share.zip", audio="mp3")

    with zipfile.ZipFile(out.path) as bundle:
        audio = [n for n in bundle.namelist() if n.startswith("audio/")]
    assert len(audio) == 4
    assert all(name.endswith(".mp3") for name in audio), audio
    assert out.tracks == 4


@needs_ffmpeg
def test_a_converted_bundle_can_be_imported_again(packed, tmp_path):
    """The manifest has to name the files that are actually in there."""
    config, store, _ = packed
    made = transfer.export_bundle(config, store, tmp_path / "share.zip", audio="mp3")

    other = Config(root=str(tmp_path / "theirs"))
    theirs = playlists.Store()
    library.init(other, theirs.current())
    report = transfer.import_bundle(other, theirs, made.path, "Theirs")
    assert report.tracks == 4
    library.invalidate_cache()
    names = [
        entry.name
        for playlist in theirs.playlists
        for entry in library.entries(other, "music", playlist)
    ]
    assert sorted(names) == ["00 Song.mp3", "01 Song.mp3", "02 Song.mp3", "03 Song.mp3"]


@needs_ffmpeg
def test_the_guess_is_close_to_what_comes_out(packed, tmp_path):
    config, store, reader = packed
    plan = transfer.bundle_plan(config, store, audio="mp3", reader=reader)
    made = transfer.export_bundle(config, store, tmp_path / "share.zip", audio="mp3")
    # A sine tone is the easiest thing in the world to encode, so this is a
    # loose bound; on real music it lands within a few percent.
    assert plan["tracks"] == 4
    assert plan["bytes"] > 0
    assert made.path.stat().st_size < plan["bytes"] * 3


def test_the_plan_for_keeping_the_original_is_the_size_on_disk(packed):
    config, store, reader = packed
    plan = transfer.bundle_plan(config, store, audio="original", reader=reader)
    on_disk = sum(
        entry.target.stat().st_size
        for entry in library.entries(config, "music", store.current())
    )
    assert plan["bytes"] == on_disk == plan["original"]


@needs_ffmpeg
def test_stopping_half_way_leaves_no_half_a_bundle(packed, tmp_path):
    config, store, _ = packed
    state = {"stop": False}
    out = tmp_path / "share.zip"
    with pytest.raises(transfer.TransferError) as raised:
        transfer.export_bundle(
            config, store, out, audio="mp3",
            on_progress=lambda done, total, name: state.update(stop=True),
            should_stop=lambda: state["stop"],
        )
    assert "stopped" in str(raised.value)
    assert not out.exists()


@needs_ffmpeg
def test_one_unreadable_track_does_not_lose_the_bundle(packed, tmp_path):
    config, store, _ = packed
    (Path(config.music_dir) / "broken.flac").write_bytes(b"not audio at all")
    library.invalidate_cache()

    made = transfer.export_bundle(config, store, tmp_path / "share.zip", audio="mp3")
    assert made.tracks == 4
    assert [name for name, _ in made.skipped] == ["broken.flac"]


def test_a_setting_this_machine_cannot_manage_says_so(packed, tmp_path, monkeypatch):
    config, store, _ = packed
    monkeypatch.setattr(transcode, "available", lambda preset: preset.copies)
    with pytest.raises(transfer.TransferError) as raised:
        transfer.export_bundle(config, store, tmp_path / "share.zip", audio="mp3")
    assert "ffmpeg" in str(raised.value)
