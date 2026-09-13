import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from bgsoundtrack import instance, library, picker, webui
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
    assert state["counts"]["music"] == 1
    assert state["config"]["gap_min"] == 1
    listing = request(httpd, "/api/library?section=music")
    assert [t["name"] for t in listing["tracks"]] == ["song.mp3"]


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
    assert state["counts"]["music"] == 0
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


# -- source folders through the API ------------------------------------


def test_source_add_and_remove_through_the_api(server, tmp_path):
    httpd, _, config, _ = server
    album = tmp_path / "album"
    make_audio(album, "a.mp3")

    state = request(httpd, "/api/source", {"path": str(album), "section": "music"})
    assert state["sources"]["music"][0]["path"] == str(album.resolve())
    assert state["sources"]["music"][0]["count"] == 1
    assert state["counts"]["music"] == 1
    assert list(config.music_dir.iterdir()) == []  # played in place, not linked

    listing = request(httpd, "/api/library?section=music")
    assert listing["tracks"][0]["origin"] == "source"

    state = request(httpd, "/api/source", {"path": str(album), "remove": True})
    assert state["sources"]["music"] == []
    assert (album / "a.mp3").exists()


def test_source_needs_a_real_folder(server, tmp_path):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/source", {"path": str(tmp_path / "nope")})
    assert caught.value.code == 400


def test_library_route_rejects_unknown_sections(server):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/library?section=sfx")
    assert caught.value.code == 400


def test_pick_reports_when_no_chooser_is_available(server, monkeypatch):
    httpd, session, *_ = server
    monkeypatch.setattr(
        webui.Session, "pick",
        lambda self, kind="folder", title="", suggested="": picker.PickResult(
            [], False, "no Tk here"
        ),
    )
    result = request(httpd, "/api/pick", {"kind": "folder"})
    assert result == {"paths": [], "available": False, "reason": "no Tk here"}


# -- playlists through the API -----------------------------------------


def test_playlist_create_select_and_remove(server, tmp_path):
    httpd, session, config, _ = server
    album = tmp_path / "night"
    make_audio(album, "drive.mp3")

    state = request(httpd, "/api/playlist", {"action": "new", "name": "Night Drive",
                                             "source": str(album)})
    assert [p["name"] for p in state["playlists"]] == ["Library", "Night Drive"]
    assert state["playlist"] == "default"          # created, not selected

    state = request(httpd, "/api/playlist", {"action": "select", "id": "night-drive"})
    assert state["playlist"] == "night-drive"
    assert state["counts"]["music"] == 1
    assert [p["active"] for p in state["playlists"]] == [False, True]

    state = request(httpd, "/api/playlist", {"action": "rename", "id": "night-drive",
                                             "name": "Night"})
    assert state["playlists"][1]["name"] == "Night"

    state = request(httpd, "/api/playlist", {"action": "remove", "id": "night-drive"})
    assert [p["name"] for p in state["playlists"]] == ["Library"]
    assert state["playlist"] == "default"
    assert (album / "drive.mp3").exists()


def test_the_library_playlist_cannot_be_removed(server):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/playlist", {"action": "remove", "id": "default"})
    assert caught.value.code == 400


def test_remove_track_is_remembered_and_restorable(server, tmp_path):
    httpd, session, config, _ = server
    album = tmp_path / "album"
    make_audio(album, "a.mp3")
    make_audio(album, "b.mp3")
    request(httpd, "/api/source", {"path": str(album)})

    state = request(httpd, "/api/remove-track", {"name": "b.mp3"})
    assert state["result"]["how"] == "removed"
    assert state["counts"]["music"] == 1
    assert [item["name"] for item in state["removed"]] == ["b.mp3"]
    assert (album / "b.mp3").exists()

    # and it stays removed across a restart of the session
    reborn = Session(config, session.config_path)
    assert [t["name"] for t in reborn.library("music")["tracks"]] == ["a.mp3"]

    state = request(httpd, "/api/restore-track", {"target": str(album / "b.mp3")})
    assert state["counts"]["music"] == 2
    assert state["removed"] == []


def test_remove_track_unlinks_a_linked_track(server, tmp_path):
    httpd, _, config, _ = server
    source = make_audio(tmp_path / "src", "song.mp3")
    request(httpd, "/api/link", {"path": str(source.parent)})

    state = request(httpd, "/api/remove-track", {"name": "song.mp3"})
    assert state["result"]["how"] == "unlinked"
    assert state["counts"]["music"] == 0
    assert state["removed"] == []      # nothing to remember - the link is gone
    assert source.exists()


def test_unknown_playlist_action_is_rejected(server):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/playlist", {"action": "explode"})
    assert caught.value.code == 400


# -- starting up twice --------------------------------------------------


def test_a_busy_port_moves_to_the_next_one(tmp_path):
    """An older instance holding 8765 must not stop a new one starting."""
    config = Config(root=str(tmp_path / "lib"))
    library.init(config)
    first = webui.serve(Session(config), host="127.0.0.1", port=0, open_browser=False)
    try:
        second = webui.serve(
            Session(config),
            host="127.0.0.1",
            port=first.server_port,
            open_browser=False,
        )
    finally:
        pass
    try:
        assert second.server_port != first.server_port
    finally:
        first.shutdown()
        second.shutdown()


def test_an_explicit_busy_port_is_a_clear_error_not_a_traceback(tmp_path):
    config = Config(root=str(tmp_path / "lib"))
    library.init(config)
    first = webui.serve(Session(config), host="127.0.0.1", port=0, open_browser=False)
    try:
        with pytest.raises(webui.PortInUse, match="already in use"):
            webui.serve(
                Session(config),
                host="127.0.0.1",
                port=first.server_port,
                open_browser=False,
                fallback=False,
            )
    finally:
        first.shutdown()


def test_a_running_instance_is_found_and_a_stale_note_is_cleared(tmp_path):
    config_file = tmp_path / "config.json"
    live = instance.Instance(
        pid=os.getpid() + 1 if os.getpid() > 1 else 99999,
        host="127.0.0.1",
        port=9,          # nothing listens on discard
        url="http://127.0.0.1:9/#x",
        token="x",
    )
    instance.write(live, config_file)
    assert instance.read(config_file).url == live.url
    assert instance.running(config_file) is None          # not actually alive
    assert not instance.state_path(config_file).exists()  # and the note is gone


def test_the_note_points_at_a_real_server(tmp_path):
    config = Config(root=str(tmp_path / "lib"))
    library.init(config)
    httpd = webui.serve(Session(config), host="127.0.0.1", port=0, open_browser=False)
    config_file = tmp_path / "config.json"
    try:
        instance.write(
            instance.Instance(
                pid=os.getpid(),
                host="127.0.0.1",
                port=httpd.server_port,
                url=httpd.url,
                token=httpd.token,
            ),
            config_file,
        )
        found = instance.read(config_file)
        assert instance.alive(found) is True
        assert instance.state_path(config_file).stat().st_mode & 0o077 == 0  # token file
    finally:
        httpd.shutdown()


# -- the table's data ---------------------------------------------------


def test_the_library_carries_what_the_table_shows(server, tmp_path, monkeypatch):
    from bgsoundtrack import tags

    httpd, session, config, _ = server
    album = tmp_path / "album"
    make_audio(album, "01 Anchor.mp3")
    request(httpd, "/api/source", {"path": str(album)})
    session._tags.store(
        album / "01 Anchor.mp3",
        tags.Tags(title="Anchor", artist="Vela", album="Undertow", track=1, duration=35.0),
    )

    track = request(httpd, "/api/library?section=music")["tracks"][0]
    assert track["title"] == "Anchor"
    assert track["artist"] == "Vela"
    assert track["album"] == "Undertow"
    assert track["track"] == 1
    assert track["duration"] == 35.0
    assert track["guessed"] is False


def test_sorting_and_direction_are_settings_the_ui_can_set(server):
    httpd, *_ = server
    state = request(httpd, "/api/config", {"sort_by": "album", "sort_desc": True})
    assert state["sort"] == "album"
    assert state["config"]["sort_desc"] is True
    assert "length" in state["sorts"]

    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/config", {"sort_by": "vibes"})
    assert caught.value.code == 400


def test_a_finished_tag_read_bumps_the_version(server, tmp_path):
    httpd, session, *_ = server
    before = request(httpd, "/api/state")["tags_version"]
    session._tags_version += 1          # what the background reader does
    assert request(httpd, "/api/state")["tags_version"] == before + 1


# -- upgraded while running ---------------------------------------------


def test_a_process_older_than_the_installed_code_is_stale(monkeypatch):
    monkeypatch.setattr(instance, "package_mtime", lambda: instance.PROCESS_START + 60)
    instance.forget_staleness()
    assert instance.process_is_stale() is True
    monkeypatch.setattr(instance, "package_mtime", lambda: instance.PROCESS_START - 60)
    instance.forget_staleness()
    assert instance.process_is_stale() is False


def test_a_running_instance_from_before_an_upgrade_is_stale(monkeypatch, tmp_path):
    older = instance.Instance(
        pid=os.getpid(), host="127.0.0.1", port=1, url="u", token="t", started=1000.0
    )
    monkeypatch.setattr(instance, "package_mtime", lambda: 2000.0)
    assert instance.is_stale(older) is True
    monkeypatch.setattr(instance, "package_mtime", lambda: 500.0)
    assert instance.is_stale(older) is False
    instance.forget_staleness()


def test_state_says_when_the_running_code_is_out_of_date(server, monkeypatch):
    httpd, *_ = server
    instance.forget_staleness()
    assert request(httpd, "/api/state")["stale"] is False
    monkeypatch.setattr(instance, "package_mtime", lambda: instance.PROCESS_START + 60)
    instance.forget_staleness()
    assert request(httpd, "/api/state")["stale"] is True


def test_an_unexpected_error_answers_with_json_and_a_hint(server, monkeypatch):
    """A dead socket tells the page nothing; an error with a hint does."""
    httpd, session, *_ = server

    def boom(self):
        raise AttributeError("'Config' object has no attribute 'sort_desc'")

    monkeypatch.setattr(type(session), "snapshot", boom)
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/state")
    assert caught.value.code == 500
    body = json.loads(caught.value.read())
    assert "sort_desc" in body["error"]
    assert "bgst ui --stop" in body["hint"]


# -- covers, labels and server control ----------------------------------


def test_a_cover_is_served_only_for_a_track_in_the_library(server, tmp_path):
    httpd, session, config, _ = server
    album = tmp_path / "album"
    make_audio(album, "one.mp3")
    (album / "folder.jpg").write_bytes(b"\xff\xd8\xffcover-bytes")
    request(httpd, "/api/source", {"path": str(album)})

    url = f"http://127.0.0.1:{httpd.server_port}/api/cover?track={album / 'one.mp3'}&t={httpd.token}"
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.read().endswith(b"cover-bytes")
        assert response.headers["Content-Type"].startswith("image/")

    # a file that is not in the library gets nothing, token or no token
    outside = tmp_path / "secret.mp3"
    outside.write_bytes(b"\0")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(
            f"http://127.0.0.1:{httpd.server_port}/api/cover?track={outside}&t={httpd.token}",
            timeout=5,
        )
    assert caught.value.code == 404


def test_a_cover_needs_the_token(server, tmp_path):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(
            f"http://127.0.0.1:{httpd.server_port}/api/cover?track=/x.mp3", timeout=5
        )
    assert caught.value.code == 403


def test_now_playing_is_named_from_its_tags(server, tmp_path):
    from bgsoundtrack import engine, tags

    httpd, session, *_ = server
    song = tmp_path / "album" / "01 Anchor.mp3"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"\0")
    session._tags.store(song, tags.Tags(title="Anchor", artist="Vela", album="Undertow"))
    session._on_event(engine.Event("track", path=song))

    now = request(httpd, "/api/state")["now"]
    assert now["label"] == "Anchor — Vela"
    assert now["name"] == "01 Anchor.mp3"       # the file is still named


def test_an_untagged_track_keeps_its_file_name(server, tmp_path):
    from bgsoundtrack import engine

    httpd, session, *_ = server
    song = tmp_path / "album" / "mystery.mp3"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"\0")
    session._on_event(engine.Event("track", path=song))
    assert request(httpd, "/api/state")["now"]["label"] == "mystery.mp3"


def test_an_unknown_server_action_is_refused(server):
    httpd, *_ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(httpd, "/api/server", {"action": "explode"})
    assert caught.value.code == 400


def test_state_reports_what_the_server_is_doing(server):
    httpd, *_ = server
    state = request(httpd, "/api/state")
    for key in ("pid", "started", "indexing", "tags_pending", "art_pending", "version"):
        assert key in state


def test_a_linked_track_shows_its_own_tags_and_art(server, tmp_path):
    """A link plays by its own path; its tags and cover belong to the file."""
    from bgsoundtrack import engine, library, tags

    httpd, session, config, _ = server
    real = tmp_path / "Artist" / "Album" / "01 Song.mp3"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"\0")
    (real.parent / "cover.jpg").write_bytes(b"\xff\xd8\xffart")
    session._tags.store(real, tags.Tags(title="Song", artist="Artist", album="Album"))
    library.link(config, [real], playlist=session.playlist)

    link = config.music_dir / "01 Song.mp3"
    assert link.is_symlink() and link != real
    session._on_event(engine.Event("track", path=link))

    now = request(httpd, "/api/state")["now"]
    assert now["label"] == "Song — Artist"
    assert now["album"] == "Album"
    assert now["art"] is True      # the album's cover, not the library folder's


def test_the_playing_track_gets_read_without_opening_the_table(server, tmp_path, monkeypatch):
    from bgsoundtrack import engine, tags

    httpd, session, *_ = server
    song = tmp_path / "album" / "01 Song.mp3"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"\0")
    monkeypatch.setattr(
        tags, "_probe", lambda path: tags.Tags(title="Song", artist="Artist")
    )

    assert session._tags.cached(song) is None       # nothing known yet
    session._on_event(engine.Event("track", path=song))
    for _ in range(50):
        if session._tags.cached(song) is not None:
            break
        time.sleep(0.05)
    assert request(httpd, "/api/state")["now"]["label"] == "Song — Artist"


def test_a_cover_can_be_asked_for_by_the_link_or_the_file(server, tmp_path):
    from bgsoundtrack import library

    httpd, session, config, _ = server
    real = tmp_path / "Artist" / "Album" / "song.mp3"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"\0")
    (real.parent / "cover.jpg").write_bytes(b"\xff\xd8\xffart")
    library.link(config, [real], playlist=session.playlist)
    link = config.music_dir / "song.mp3"

    for asked in (real, link):
        url = (
            f"http://127.0.0.1:{httpd.server_port}/api/cover"
            f"?track={urllib.parse.quote(str(asked))}&t={httpd.token}"
        )
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.read().endswith(b"art")


def test_history_catches_up_when_tags_arrive(server, tmp_path):
    from bgsoundtrack import engine, tags

    httpd, session, *_ = server
    song = tmp_path / "album" / "01 Song.mp3"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"\0")

    session._on_event(engine.Event("track", path=song))
    assert request(httpd, "/api/state")["history"] == ["01 Song.mp3"]

    session._tags.store(song, tags.Tags(title="Song", artist="Artist"))
    assert request(httpd, "/api/state")["history"] == ["Song — Artist"]
