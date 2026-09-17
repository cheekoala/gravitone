"""Export and import: a manifest that points at files, a bundle that carries them."""

import csv
import json
import zipfile

import pytest

from gravitone import library, playlists, transfer
from gravitone.config import Config


def album(directory, *names):
    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names, start=1):
        (directory / name).write_bytes(b"\0" * (512 * index))
    return directory


@pytest.fixture
def setup(tmp_path):
    config = Config(root=str(tmp_path / "custom soundtrack"))
    store = playlists.Store(path=tmp_path / "playlists.json")
    playlist = store.add("Hollow Kingdom")
    store.select(playlist.id)
    library.init(config, playlist)

    folder = album(tmp_path / "music" / "kingdom", "one.mp3", "two.mp3")
    library.add_source(config, folder, playlist=playlist)
    rain = album(tmp_path / "weather", "rain.ogg") / "rain.ogg"
    library.link(config, [rain], section="ambient", playlist=playlist)
    library.invalidate_cache()
    return config, store, playlist, tmp_path


# -- manifest -----------------------------------------------------------


def test_manifest_describes_folders_links_and_removals(setup):
    config, store, playlist, tmp_path = setup
    library.remove_track(config, "two.mp3", playlist=playlist)
    data = transfer.manifest(config, store, ["Hollow Kingdom"])

    assert data["gravitone"] == transfer.FORMAT
    item = data["playlists"][0]
    assert item["name"] == "Hollow Kingdom"
    assert item["folders"]["music"] == [str(tmp_path / "music" / "kingdom")]
    assert [link["name"] for link in item["links"]["ambient"]] == ["rain.ogg"]
    assert item["removed"] == [str(tmp_path / "music" / "kingdom" / "two.mp3")]
    assert "root" not in data["settings"]       # paths are this machine's business


def test_export_json_then_import_rebuilds_the_playlist(setup, tmp_path):
    config, store, playlist, _ = setup
    out = tmp_path / "library.json"
    report = transfer.export(config, store, out, ["Hollow Kingdom"])
    assert report.total == 3 and report.tracks == 1 and report.folders == 1

    fresh_config = Config(root=str(tmp_path / "elsewhere"))
    fresh_store = playlists.Store(path=tmp_path / "elsewhere.json")
    library.init(fresh_config)
    result = transfer.import_manifest(fresh_config, fresh_store, out)

    assert result.playlists == ["Hollow Kingdom"]
    imported = fresh_store.get("Hollow Kingdom")
    library.invalidate_cache()
    names = sorted(e.name for e in library.entries(fresh_config, "music", imported))
    assert names == ["one.mp3", "two.mp3"]
    assert [e.name for e in library.entries(fresh_config, "ambient", imported)] == ["rain.ogg"]


def test_import_never_overwrites_an_existing_playlist(setup, tmp_path):
    config, store, playlist, _ = setup
    out = tmp_path / "library.json"
    transfer.export(config, store, out, ["Hollow Kingdom"])

    transfer.import_manifest(config, store, out)
    assert [p.name for p in store.playlists] == [
        "Library",
        "Hollow Kingdom",
        "Hollow Kingdom (2)",
    ]


def test_import_reports_what_is_not_on_this_machine(setup, tmp_path):
    config, store, playlist, _ = setup
    out = tmp_path / "library.json"
    transfer.export(config, store, out, ["Hollow Kingdom"])

    data = json.loads(out.read_text())
    data["playlists"][0]["links"]["ambient"].append(
        {"name": "ghost.ogg", "target": "/nowhere/ghost.ogg"}
    )
    data["playlists"][0]["folders"]["music"].append("/nowhere/music")
    out.write_text(json.dumps(data))

    result = transfer.import_manifest(config, store, out)
    reasons = [reason for _, reason in result.skipped]
    assert "file not found here" in reasons
    assert "folder not on this machine" in reasons


def test_csv_export_is_one_row_per_track(setup, tmp_path):
    config, store, playlist, _ = setup
    out = tmp_path / "library.csv"
    transfer.export(config, store, out, ["Hollow Kingdom"], fmt="csv")
    listed = list(csv.DictReader(out.read_text().splitlines()))
    assert sorted(row["name"] for row in listed) == ["one.mp3", "rain.ogg", "two.mp3"]
    assert {row["origin"] for row in listed} == {"source", "link"}


def test_an_unknown_format_is_refused(setup, tmp_path):
    config, store, _, _ = setup
    with pytest.raises(transfer.TransferError):
        transfer.export(config, store, tmp_path / "x.yaml", fmt="yaml")


# -- bundle -------------------------------------------------------------


def test_bundle_carries_the_audio(setup, tmp_path):
    config, store, playlist, _ = setup
    out = tmp_path / "share.zip"
    report = transfer.export_bundle(config, store, out, ["Hollow Kingdom"])
    assert report.tracks == 3

    with zipfile.ZipFile(out) as bundle:
        names = bundle.namelist()
        assert transfer.MANIFEST_NAME in names
        assert "audio/hollow-kingdom/music/one.mp3" in names
        assert "audio/hollow-kingdom/ambient/rain.ogg" in names
        assert bundle.read("audio/hollow-kingdom/music/one.mp3") == b"\0" * 512


def test_a_bundle_imports_where_the_music_does_not_exist(setup, tmp_path):
    config, store, playlist, _ = setup
    out = tmp_path / "share.zip"
    transfer.export_bundle(config, store, out, ["Hollow Kingdom"])

    elsewhere = tmp_path / "other-machine"
    fresh_config = Config(root=str(elsewhere / "custom soundtrack"))
    fresh_store = playlists.Store(path=elsewhere / "playlists.json")
    library.init(fresh_config)
    result = transfer.import_bundle(fresh_config, fresh_store, out)

    assert result.tracks == 3
    imported = fresh_store.get("Hollow Kingdom")
    library.invalidate_cache()
    assert sorted(e.name for e in library.entries(fresh_config, "music", imported)) == [
        "one.mp3",
        "two.mp3",
    ]
    # the audio really is there, not a link back to the original machine
    played = library.entries(fresh_config, "music", imported)[0].target
    assert elsewhere in played.parents
    assert played.read_bytes()


def test_a_bundle_with_a_traversing_path_is_refused(setup, tmp_path):
    config, store, _, _ = setup
    nasty = tmp_path / "nasty.zip"
    with zipfile.ZipFile(nasty, "w") as bundle:
        bundle.writestr("audio/../../escape.mp3", b"\0")
        bundle.writestr(
            transfer.MANIFEST_NAME,
            json.dumps({"gravitone": 1, "playlists": [{"id": "x", "name": "X"}]}),
        )
    result = transfer.import_bundle(config, store, nasty, into=tmp_path / "unpack")
    assert result.tracks == 0
    assert result.skipped and "unsafe" in result.skipped[0][1]
    assert not (tmp_path / "escape.mp3").exists()


def test_reading_a_file_that_is_not_an_export(tmp_path):
    plain = tmp_path / "notes.json"
    plain.write_text('{"hello": "world"}')
    with pytest.raises(transfer.TransferError, match="not a gravitone export"):
        transfer.read_manifest(plain)


def test_an_export_from_a_newer_gravitone_is_refused(tmp_path):
    ahead = tmp_path / "future.json"
    ahead.write_text(json.dumps({"gravitone": transfer.FORMAT + 5, "playlists": []}))
    with pytest.raises(transfer.TransferError, match="newer gravitone"):
        transfer.read_manifest(ahead)
