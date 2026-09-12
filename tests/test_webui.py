import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from bgsoundtrack import library, webui
from bgsoundtrack.config import Config
from bgsoundtrack.service import Session


@pytest.fixture
def server(tmp_path):
    config = Config(root=str(tmp_path / "custom soundtrack"), gap_min=1, gap_max=2)
    library.init(config)
    session = Session(config, tmp_path / "config.json")
    httpd = webui.serve(session, host="127.0.0.1", port=0, open_browser=False)
    yield httpd, session, config, tmp_path
    session.stop()
    httpd.shutdown()


def request(httpd, route, body=None, token=None):
    url = f"http://127.0.0.1:{httpd.server_port}{route}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("X-BGST-Token", httpd.token if token is None else token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as response:
        payload = response.read()
    return json.loads(payload) if payload[:1] in (b"{", b"[") else payload


def make_audio(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\0" * 64)
    return path


def test_state_needs_the_token(server):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/state", token="wrong")
    assert caught.value.code == 403


def test_state_reports_library_and_config(server):
    httpd, _, config, tmp_path = server
    make_audio(config.music_dir, "song.mp3")
    state = request(httpd, "/api/state")
    assert state["running"] is False
    assert [t["name"] for t in state["library"]["music"]] == ["song.mp3"]
    assert state["config"]["gap_min"] == 1


def test_config_post_persists_to_disk(server):
    httpd, _, _, tmp_path = server
    state = request(httpd, "/api/config", {"gap_max": 90, "shuffle": False})
    assert state["config"]["gap_max"] == 90
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["gap_max"] == 90 and saved["shuffle"] is False


def test_config_rejects_impossible_values(server):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/config", {"ambient_chance": 4})
    assert caught.value.code == 400


def test_link_and_unlink_through_the_api(server):
    httpd, _, config, tmp_path = server
    source = make_audio(tmp_path / "album", "theme.mp3")
    state = request(httpd, "/api/link", {"path": str(source.parent), "section": "music"})
    assert state["result"]["linked"] == ["theme.mp3"]
    assert (config.music_dir / "theme.mp3").is_symlink()
    assert source.exists()

    state = request(httpd, "/api/unlink", {"name": "theme.mp3", "section": "music"})
    assert state["library"]["music"] == []
    assert source.exists()  # the original is untouched


def test_link_without_a_path_is_a_bad_request(server):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/link", {"section": "music"})
    assert caught.value.code == 400


def test_browse_lists_folders_and_audio(server, tmp_path):
    httpd, *_ = server
    make_audio(tmp_path / "album", "a.mp3")
    (tmp_path / "album" / "notes.txt").write_text("x")
    (tmp_path / "album" / ".hidden").mkdir()
    listing = request(httpd, f"/api/browse?path={tmp_path / 'album'}")
    assert [f["name"] for f in listing["files"]] == ["a.mp3"]
    assert listing["dirs"] == []
    assert listing["parent"] == str(tmp_path)


def test_browse_rejects_a_missing_directory(server, tmp_path):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, f"/api/browse?path={tmp_path / 'nope'}")
    assert caught.value.code == 400


def test_play_without_a_player_reports_the_problem(server, monkeypatch):
    httpd, *_ = server
    monkeypatch.setattr(webui.player.shutil, "which", lambda name: None)
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/play", {})
    assert caught.value.code == 503


def test_static_files_are_served(server):
    httpd, *_ = server
    for route, needle in (("/", b"<title>bgst</title>"), ("/app.js", b"bgst"), ("/style.css", b"--accent")):
        assert needle in request(httpd, route)


def test_static_refuses_paths_outside_the_ui_folder(server):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/../config.py")
    assert caught.value.code == 404
