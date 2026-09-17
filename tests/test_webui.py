import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from gravitone import instance, library, picker, webui
from gravitone.config import Config
from gravitone.service import Session


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
    req.add_header("X-Gravitone-Token", httpd.token if token is None else token)
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
    for route, needle in (("/", b"<title>Gravitone</title>"), ("/app.js", b"gravitone"), ("/style.css", b"--accent")):
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
    from gravitone import tags

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
    assert "gravitone ui --stop" in body["hint"]


# -- covers, labels and server control ----------------------------------


def test_a_cover_is_served_only_for_a_track_in_the_library(server, tmp_path):
    httpd, session, config, _ = server
    album = tmp_path / "album"
    make_audio(album, "one.mp3")
    cover = tmp_path / "extracted.jpg"
    cover.write_bytes(b"\xff\xd8\xffcover-bytes")
    session._art.remember(album, cover)
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
    from gravitone import engine, tags

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
    from gravitone import engine

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
    from gravitone import engine, library, tags

    httpd, session, config, _ = server
    real = tmp_path / "Artist" / "Album" / "01 Song.mp3"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"\0")
    cover = tmp_path / "cover-from-the-file.jpg"
    cover.write_bytes(b"\xff\xd8\xffart")
    # Tags first: they say which record this is, and the cover is filed
    # under the record.
    session._tags.store(real, tags.Tags(title="Song", artist="Artist", album="Album"))
    session._art.remember(real, cover)      # as extraction would
    library.link(config, [real], playlist=session.playlist)

    link = config.music_dir / "01 Song.mp3"
    assert link.is_symlink() and link != real
    session._on_event(engine.Event("track", path=link))

    now = request(httpd, "/api/state")["now"]
    assert now["label"] == "Song — Artist"
    assert now["album"] == "Album"
    assert now["art"] is True      # the album's cover, not the library folder's


def test_the_playing_track_gets_read_without_opening_the_table(server, tmp_path, monkeypatch):
    from gravitone import engine, tags

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
    from gravitone import library

    httpd, session, config, _ = server
    real = tmp_path / "Artist" / "Album" / "song.mp3"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"\0")
    cover = tmp_path / "extracted.jpg"
    cover.write_bytes(b"\xff\xd8\xffart")
    session._art.remember(real, cover)
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
    from gravitone import engine, tags

    httpd, session, *_ = server
    song = tmp_path / "album" / "01 Song.mp3"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"\0")

    session._on_event(engine.Event("track", path=song))
    assert request(httpd, "/api/state")["history"] == ["01 Song.mp3"]

    session._tags.store(song, tags.Tags(title="Song", artist="Artist"))
    assert request(httpd, "/api/state")["history"] == ["Song — Artist"]


# -- parties -------------------------------------------------------------


def with_music(config, session, names=("01 Song.mp3", "02 Other.mp3", "03 Third.mp3")):
    """A playlist with tags already read, as a party needs."""
    from gravitone import library, tags

    made = []
    folder = library.section_dir(config, "music", session.playlist)
    folder.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        real = folder / name
        real.write_bytes(b"\0")
        session._tags.store(
            real,
            tags.Tags(
                title=name.split(" ", 1)[1].removesuffix(".mp3"),
                artist="Jeremy Soule",
                album="Morrowind",
                duration=60.0 + index * 10,
            ),
        )
        made.append(real)
    library.SCANNER.refresh_now(folder)
    library.SCANNER.wait(5)
    return made


def elsewhere(tmp_path):
    """A config dir of its own: the other machine shares nothing but the code."""
    directory = tmp_path / "their machine"
    directory.mkdir(exist_ok=True)
    return directory / "config.json"


def test_no_party_is_the_normal_state(server):
    httpd, session, config, tmp_path = server
    assert request(httpd, "/api/state")["party"] is None


def test_hosting_a_party_gives_a_code_and_says_what_is_in_it(server):
    httpd, session, config, tmp_path = server
    with_music(config, session)
    state = request(httpd, "/api/party", {"action": "host", "name": "Morrowind night"})
    party = state["party"]
    assert party["name"] == "Morrowind night"
    assert len(party["code"].replace("-", "")) == 32
    assert party["tracks"] == 3
    assert party["missing"] == []
    assert party["now"]["here"] is True


def test_a_second_machine_joins_with_the_code_alone(server, tmp_path):
    """Same music, different folders: the code is the whole handshake."""
    from gravitone import library
    from gravitone.config import Config
    from gravitone.service import Session

    httpd, session, config, _ = server
    with_music(config, session)
    code = request(httpd, "/api/party", {"action": "host"})["party"]["code"]

    # Their machine: the same tracks, somewhere else entirely.
    theirs = Config(root=str(tmp_path / "their library"))
    library.init(theirs)
    other = Session(theirs, elsewhere(tmp_path))
    with_music(theirs, other)

    joined = other.join_party(code)
    assert joined["code"] == code
    assert joined["missing"] == []
    assert joined["now"]["label"] == request(httpd, "/api/state")["party"]["now"]["label"]


def test_a_bigger_library_on_the_other_side_still_joins(server, tmp_path):
    """Their extra music is simply not in the party.

    A code alone cannot say *which* three of their four tracks are in it, so
    the party file does - and once it has, the code is enough from then on.
    """
    from gravitone import library
    from gravitone.config import Config
    from gravitone.service import Session

    httpd, session, config, _ = server
    with_music(config, session)
    made = request(httpd, "/api/party", {"action": "host"})["party"]
    saved = tmp_path / "party.json"
    request(httpd, "/api/party", {"action": "save", "path": str(saved)})

    theirs = Config(root=str(tmp_path / "their library"))
    library.init(theirs)
    other = Session(theirs, elsewhere(tmp_path))
    with_music(theirs, other)
    with_music(theirs, other, names=("09 Something Else.mp3",))

    joined = other.join_party(path=str(saved))
    assert joined["missing"] == []
    assert joined["tracks"] == 3            # the party is still the party
    assert joined["code"] == made["code"]

    # And the roster is kept, so the next code needs no file at all.
    again = Session(theirs, other.config_path)
    assert again.join_party(made["code"])["tracks"] == 3


def test_joining_finds_the_playlist_the_party_is_about(server, tmp_path):
    """Their own music is one playlist, the shared library another."""
    from gravitone import library
    from gravitone.config import Config
    from gravitone.service import Session

    httpd, session, config, _ = server
    with_music(config, session)
    code = request(httpd, "/api/party", {"action": "host"})["party"]["code"]

    theirs = Config(root=str(tmp_path / "their library"))
    library.init(theirs)
    other = Session(theirs, elsewhere(tmp_path))
    with_music(theirs, other, names=("77 My Own Thing.mp3",))   # their playlist
    shared = other.add_playlist("Morrowind from the NAS")
    other.select_playlist(shared)
    with_music(theirs, other)                                   # the shared one
    other.select_playlist(other.store.playlists[0].id)          # back to theirs

    joined = other.join_party(code)
    assert joined["missing"] == []
    assert other.playlist.name == "Morrowind from the NAS"


def test_a_smaller_library_joins_and_is_told_what_it_lacks(server, tmp_path):
    from gravitone import library
    from gravitone.config import Config
    from gravitone.service import Session

    httpd, session, config, _ = server
    with_music(config, session)
    made = request(httpd, "/api/party", {"action": "host"})["party"]
    saved = tmp_path / "party.json"
    request(httpd, "/api/party", {"action": "save", "path": str(saved)})

    theirs = Config(root=str(tmp_path / "their library"))
    library.init(theirs)
    other = Session(theirs, elsewhere(tmp_path))
    with_music(theirs, other, names=("01 Song.mp3",))

    joined = other.join_party(path=str(saved))
    assert joined["code"] == made["code"]
    assert len(joined["missing"]) == 2
    assert "Other" in " ".join(joined["missing"])


def test_a_code_for_unknown_music_asks_for_the_party_file(server, tmp_path):
    from gravitone import library
    from gravitone.config import Config
    from gravitone.service import Session

    httpd, session, config, _ = server
    with_music(config, session)
    code = request(httpd, "/api/party", {"action": "host"})["party"]["code"]

    theirs = Config(root=str(tmp_path / "empty library"))
    library.init(theirs)
    (theirs.music_dir / "unrelated.mp3").write_bytes(b"\0")
    other = Session(theirs, elsewhere(tmp_path))
    with pytest.raises(Exception) as raised:
        other.join_party(code)
    assert "party file" in str(raised.value)


def test_leaving_a_party_puts_things_back(server):
    httpd, session, config, _ = server
    with_music(config, session)
    request(httpd, "/api/party", {"action": "host"})
    assert request(httpd, "/api/state")["party"] is not None
    assert request(httpd, "/api/party", {"action": "leave"})["party"] is None


def test_a_party_survives_the_server_restarting(server, tmp_path):
    """The party is still going on; this machine just stepped out."""
    from gravitone.service import Session

    httpd, session, config, _ = server
    with_music(config, session)
    code = request(httpd, "/api/party", {"action": "host"})["party"]["code"]

    again = Session(config, session.config_path)
    assert again.party_state()["code"] == code
    assert again.party_state()["note"] == "rejoined"


def test_the_party_says_what_is_coming(server):
    httpd, session, config, _ = server
    with_music(config, session)
    party = request(httpd, "/api/party", {"action": "host"})["party"]
    assert party["next"]
    assert {item["kind"] for item in party["next"]} <= {"track", "ambient", "silence"}
    assert all("at" in item for item in party["next"])


def test_an_unknown_party_action_is_a_clear_error(server):
    with pytest.raises(urllib.error.HTTPError):
        request(server[0], "/api/party", {"action": "conga"})


def test_starting_a_party_mid_song_does_not_blank_the_player(server, monkeypatch):
    """The engine has to be replaced - the party's schedule is a different
    one - but a page polling through that swap should never be told that
    nothing is playing."""
    import threading
    import time as clock

    from gravitone import engine, player

    httpd, session, config, _ = server
    with_music(config, session)

    def hold(self, on_event=None):
        """An engine that names one track and then just keeps running."""
        if on_event:
            on_event(engine.Event("track", path=config.music_dir / "01 Song.mp3"))
        while not self.controls.stopping:
            clock.sleep(0.02)

    monkeypatch.setattr(
        player, "detect", lambda *a, **k: player.Backend("test", "/bin/true")
    )
    monkeypatch.setattr(engine.Engine, "run", hold)
    # Starting the new engine takes a moment, as it does with a real library:
    # it has to work out which of the party's tracks are on this machine.
    slow = session.start

    def unhurried(*args, **kwargs):
        clock.sleep(0.3)
        return slow(*args, **kwargs)

    monkeypatch.setattr(session, "start", unhurried)

    session.start()
    for _ in range(100):
        if session._now is not None:
            break
        clock.sleep(0.02)
    assert session.running and session._now is not None

    seen = []
    watching = threading.Event()

    def watch() -> None:
        while not watching.is_set():
            seen.append(session.snapshot()["now"])
            clock.sleep(0.01)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        request(httpd, "/api/party", {"action": "host"})
    finally:
        watching.set()
        watcher.join(timeout=2)

    assert seen, "the watcher should have polled through the swap"
    assert all(item is not None for item in seen), "the player went blank"


def test_the_party_says_when_the_current_item_began(server):
    """An instant every machine in the party agrees on - it is what the page
    uses to tell one item from the next."""
    httpd, session, config, _ = server
    with_music(config, session)
    party = request(httpd, "/api/party", {"action": "host"})["party"]
    assert party["now"]["at"] == pytest.approx(party["epoch"], abs=1)
    assert party["now"]["at"] + party["now"]["duration"] > party["now"]["at"]


# -- the file browser ----------------------------------------------------


def test_browsing_says_what_is_inside_each_folder(server, tmp_path):
    """So you can tell a record from a box of records without opening it."""
    tree = tmp_path / "Game Soundtracks"
    (tree / "Morrowind" / "Disc 1").mkdir(parents=True)
    (tree / "Oblivion").mkdir()
    for index in range(3):
        (tree / "Morrowind" / "Disc 1" / f"{index}.flac").write_bytes(b"\0")
    for index in range(5):
        (tree / "Oblivion" / f"{index}.mp3").write_bytes(b"\0")
    (tree / "loose.flac").write_bytes(b"\0")

    httpd, *_ = server
    listing = request(httpd, f"/api/browse?path={urllib.parse.quote(str(tree))}")
    by_name = {d["name"]: d for d in listing["dirs"]}
    assert by_name["Morrowind"] == {
        "name": "Morrowind", "path": str(tree / "Morrowind"), "audio": 0, "folders": 1,
    }
    assert by_name["Oblivion"]["audio"] == 5
    assert by_name["Oblivion"]["folders"] == 0
    assert [f["name"] for f in listing["files"]] == ["loose.flac"]


def test_browsing_hands_back_every_step_of_the_path(server, tmp_path):
    """Every step is a way back, not just the one directly above."""
    deep = tmp_path / "one" / "two" / "three"
    deep.mkdir(parents=True)
    httpd, *_ = server
    listing = request(httpd, f"/api/browse?path={urllib.parse.quote(str(deep))}")

    names = [crumb["name"] for crumb in listing["crumbs"]]
    paths = [crumb["path"] for crumb in listing["crumbs"]]
    assert names[0] == "/" and names[-3:] == ["one", "two", "three"]
    assert paths[-1] == str(deep)
    assert paths[-2] == str(deep.parent)
    # Each one is a real place, and they only ever get longer.
    assert paths == sorted(paths, key=len)


def test_a_hidden_folder_is_not_counted_as_contents(server, tmp_path):
    folder = tmp_path / "album"
    (folder / ".git").mkdir(parents=True)
    (folder / "a.flac").write_bytes(b"\0")
    httpd, *_ = server
    listing = request(httpd, f"/api/browse?path={urllib.parse.quote(str(tmp_path))}")
    assert listing["dirs"][0]["folders"] == 0
    assert listing["dirs"][0]["audio"] == 1
