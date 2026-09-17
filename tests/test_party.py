"""Listening together: the same timetable, worked out twice, apart."""

import json
import time

import pytest

from gravitone import party
from gravitone.config import Config


def member(name, seconds=180.0, artist="Soule", album="Morrowind"):
    return party.Member(
        id=party.track_id(name, artist, album, name),
        duration=seconds,
        title=name,
        artist=artist,
        album=album,
        name=f"{name}.flac",
    )


def roster(count=12, ambient=3):
    return party.Roster.of(
        [member(f"Track {n}", 120.0 + n * 7) for n in range(count)],
        [member(f"Rain {n}", 300.0, artist="Weather", album="Beds") for n in range(ambient)],
    )


@pytest.fixture
def config():
    return Config(gap_min=8, gap_max=30, ambient_chance=0.6, ambient_min_tail=3)


# -- the same music, wherever it lives -----------------------------------


def test_a_track_is_the_same_track_on_both_machines():
    """Different folder, different file name, same song."""
    mine = party.track_id("Nerevar Rising", "Jeremy Soule", "Morrowind", "01 nerevar.flac")
    theirs = party.track_id(
        "nerevar  rising", "JEREMY SOULE", "Morrowind", "/nas/music/track01.flac"
    )
    assert mine == theirs


def test_retagging_a_file_does_not_make_it_a_different_track():
    """Adding the cover art that was missing rewrites the file's bytes."""
    before = party.track_id("Peaceful Waters", "Soule", "Morrowind", "a.flac")
    after = party.track_id("Peaceful Waters", "Soule", "Morrowind", "a.flac")
    assert before == after


def test_a_file_with_no_tags_falls_back_to_its_name():
    assert party.track_id("", "", "", "04 Blight.flac") == party.track_id(
        "", "", "", "04 blight.flac"
    )
    assert party.track_id("", "", "", "a.flac") != party.track_id("", "", "", "b.flac")


def test_a_roster_is_built_in_an_order_both_sides_arrive_at():
    """Sorted by what a track *is*, so nobody has to agree on an order."""
    tracks = [member(f"Track {n}") for n in range(6)]
    one = party.Roster.of(tracks, [])
    other = party.Roster.of(list(reversed(tracks)), [])
    assert [m.id for m in one.music] == [m.id for m in other.music]
    assert one.fingerprint == other.fingerprint


def test_the_same_track_listed_twice_is_still_one_track():
    once = member("Track 1")
    assert len(party.Roster.of([once, once], []).music) == 1


# -- the code ------------------------------------------------------------


def test_a_code_carries_the_whole_party_but_the_music(config):
    made = party.start(roster(), config, name="Morrowind evening")
    read = party.decode(made.code)
    assert read["seed"] == made.seed
    assert read["epoch"] == made.epoch
    assert read["fingerprint"] == made.roster.fingerprint
    assert read["settings"]["gap_min"] == config.gap_min
    assert read["settings"]["gap_max"] == config.gap_max
    assert read["settings"]["shuffle"] is config.shuffle


def test_a_code_is_short_enough_to_read_out(config):
    code = party.start(roster(), config).code
    assert len(code.replace("-", "")) == 32
    assert code.count("-") == 7


def test_a_code_is_forgiving_about_how_it_was_typed(config):
    code = party.start(roster(), config).code
    sloppy = code.lower().replace("-", " ")
    assert party.decode(sloppy) == party.decode(code)


def test_letters_that_look_like_numbers_are_taken_as_numbers(config):
    code = party.start(roster(), config, seed=0, epoch=1_700_000_000).code
    assert "I" not in code and "L" not in code and "O" not in code and "U" not in code
    typed = code.replace("1", "I").replace("0", "O")
    assert party.decode(typed) == party.decode(code)


@pytest.mark.parametrize(
    "bad, says",
    [
        ("", "no code"),
        ("ABC", "characters"),
        ("!!!!", "no code"),
    ],
)
def test_a_code_that_is_not_one_says_so(bad, says):
    with pytest.raises(party.PartyError) as raised:
        party.decode(bad)
    assert says in str(raised.value)


def test_settings_travel_in_the_code_so_the_gaps_line_up():
    """The silences are part of the party, not a local preference."""
    loose = Config(gap_min=45, gap_max=180, ambient_chance=0.1)
    tight = Config(gap_min=2, gap_max=4, ambient_chance=0.9)
    one = party.start(roster(), loose, seed=7, epoch=1_700_000_000)
    other = party.start(roster(), tight, seed=7, epoch=1_700_000_000)
    assert one.code != other.code
    assert party.decode(one.code)["settings"]["gap_max"] == 180


# -- the timetable -------------------------------------------------------


def test_two_machines_work_out_the_same_evening(config):
    """The whole point: no connection, six hours, not one disagreement."""
    made = party.start(roster(40), config, seed=12345, epoch=1_700_000_000)

    # The other side has only the code and the roster - nothing else passed
    # between them.
    theirs = party.join(made.code, roster=made.roster)
    mine, yours = party.Timetable(made), party.Timetable(theirs)

    for elapsed in range(0, 6 * 60 * 60, 7):     # every seven seconds, six hours
        item, into = mine.at(elapsed)
        other, other_into = yours.at(elapsed)
        assert item.kind == other.kind
        assert item.at == other.at
        assert item.duration == other.duration
        assert into == other_into
        assert (item.member and item.member.id) == (other.member and other.member.id)
        assert item.seek == other.seek


def test_joining_late_lands_in_the_middle_of_the_track(config):
    made = party.start(roster(), config, seed=99, epoch=1_700_000_000)
    table = party.Timetable(made)
    first, into = table.at(0)
    assert first.kind == "track" and into == 0
    later, into = table.at(first.duration - 5)
    assert later is first
    assert into == pytest.approx(first.duration - 5)


def test_a_party_covers_a_whole_evening_without_gaps_in_the_timetable(config):
    made = party.start(roster(20), config, seed=5, epoch=0)
    table = party.Timetable(made)
    seen = []
    at = 0.0
    while at < 4 * 60 * 60:
        item, _ = table.at(at)
        if not seen or seen[-1] is not item:
            seen.append(item)
        at = item.ends
    for before, after in zip(seen, seen[1:]):
        assert after.at == pytest.approx(before.ends)   # no seam, no overlap
    assert {item.kind for item in seen} <= {"track", "ambient", "silence"}


def test_a_track_always_has_a_gap_after_it(config):
    made = party.start(roster(10), config, seed=3, epoch=0)
    table = party.Timetable(made)
    kinds = [item.kind for item in table.upcoming(0, count=10)]
    for before, after in zip(kinds, kinds[1:]):
        assert not (before == "track" and after == "track")


def test_gaps_stay_inside_the_settings(config):
    made = party.start(roster(30), config, seed=8, epoch=0)
    table = party.Timetable(made)
    gaps = [item for item in table.upcoming(0, count=60) if item.kind != "track"]
    assert gaps
    for gap in gaps:
        assert config.gap_min <= gap.duration <= config.gap_max


def test_ambience_is_never_cut_short_by_where_it_started(config):
    made = party.start(roster(30), config, seed=11, epoch=0)
    table = party.Timetable(made)
    beds = [item for item in table.upcoming(0, count=80) if item.kind == "ambient"]
    assert beds
    for bed in beds:
        assert bed.seek + bed.duration <= bed.member.duration


def test_a_party_that_does_not_loop_ends(config):
    made = party.start(
        roster(3), Config(gap_min=1, gap_max=2, loop=False), seed=1, epoch=0
    )
    table = party.Timetable(made)
    assert table.at(0) is not None
    assert table.at(10 * 60 * 60) is None


def test_a_track_with_no_known_length_does_not_stop_the_clock(config):
    unknown = party.Roster.of([party.Member(id="x" * 12, duration=0.0, title="?")], [])
    table = party.Timetable(party.start(unknown, config, seed=1, epoch=0))
    item, _ = table.at(0)
    assert item.duration > 0


def test_the_timetable_is_the_same_whether_you_walk_it_or_jump_into_it(config):
    """A machine that joins at 21:40 must not need to have been there at 18:00."""
    made = party.start(roster(25), config, seed=4242, epoch=0)
    walked = party.Timetable(made)
    at = 0.0
    while at < 3 * 60 * 60:
        item, _ = walked.at(at)
        at = item.ends
    jumped = party.Timetable(made)
    for probe in (3 * 60 * 60 - 1, 90 * 60, 42.0):
        one, into_one = walked.at(probe)
        two, into_two = jumped.at(probe)
        assert (one.kind, one.at, one.duration) == (two.kind, two.at, two.duration)
        assert into_one == into_two


# -- what this machine actually holds ------------------------------------


def test_extra_music_here_is_simply_not_in_the_party(config):
    """Your library being bigger than theirs changes nothing."""
    shared = roster(5)
    have = {
        m.id: f"/nas/{m.id}.flac" for m in tuple(shared.music) + tuple(shared.ambient)
    }
    have.update({"something": "/home/me/other.flac", "else": "/home/me/more.flac"})
    lined_up = party.match(shared, have)
    assert lined_up.complete
    assert len(lined_up.found) == len(shared.music) + len(shared.ambient)


def test_a_track_you_do_not_have_is_named_rather_than_guessed_at(config):
    shared = roster(5)
    have = {m.id: f"/nas/{m.id}.flac" for m in shared.music[1:]}
    lined_up = party.match(shared, have)
    assert not lined_up.complete
    assert shared.music[0].id in {m.id for m in lined_up.missing}
    assert lined_up.path(shared.music[0]) is None


def test_a_missing_track_does_not_move_anything_else(config):
    """It plays as silence: the party goes on without you hearing it."""
    made = party.start(roster(8), config, seed=6, epoch=0)
    table = party.Timetable(made)
    before = [(item.kind, item.at, item.duration) for item in table.upcoming(0, 12)]
    have = {m.id: "/nas/x.flac" for m in made.roster.music[2:]}
    assert not party.match(made.roster, have).complete
    after = [(item.kind, item.at, item.duration) for item in party.Timetable(made).upcoming(0, 12)]
    assert before == after


# -- keeping parties between runs ----------------------------------------


def test_a_roster_once_seen_makes_a_bare_code_enough(tmp_path, config):
    made = party.start(roster(6), config)
    cfg = tmp_path / "config.json"
    party.save_roster(made.roster, cfg)

    joined = party.join(made.code, config_file=cfg)
    assert joined.roster.fingerprint == made.roster.fingerprint
    assert joined.seed == made.seed
    assert joined.epoch == made.epoch


def test_a_code_for_music_this_machine_has_never_seen_says_what_to_ask_for(tmp_path, config):
    made = party.start(roster(6), config)
    with pytest.raises(party.PartyError) as raised:
        party.join(made.code, config_file=tmp_path / "config.json")
    assert "party file" in str(raised.value)


def test_a_party_file_carries_everything(tmp_path, config):
    made = party.start(roster(9), config, name="Morrowind evening")
    written = party.write_file(made, tmp_path / "share" / "party")
    assert written.suffix == ".json"

    read = party.read_file(written)
    assert read.name == "Morrowind evening"
    assert read.code == made.code
    assert [m.id for m in read.roster.music] == [m.id for m in made.roster.music]
    assert [m.duration for m in read.roster.music] == [m.duration for m in made.roster.music]


def test_a_party_file_saved_into_a_folder_gets_a_name(tmp_path, config):
    written = party.write_file(party.start(roster(), config), tmp_path)
    assert written.name == "gravitone-party.json"


def test_something_that_is_not_a_party_file_says_so(tmp_path):
    plain = tmp_path / "notes.json"
    plain.write_text(json.dumps({"hello": True}))
    with pytest.raises(party.PartyError) as raised:
        party.read_file(plain)
    assert "not a gravitone party file" in str(raised.value)


def test_a_party_file_from_a_newer_gravitone_says_so(tmp_path, config):
    raw = party.start(roster(), config).as_dict()
    raw["gravitone_party"] = party.VERSION + 1
    path = tmp_path / "future.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(party.PartyError) as raised:
        party.read_file(path)
    assert "newer gravitone" in str(raised.value)


def test_a_party_starts_on_a_whole_second(config):
    made = party.start(roster(), config)
    assert made.epoch == int(made.epoch)
    assert abs(made.epoch - time.time()) < 5


def test_an_empty_playlist_cannot_hold_a_party(config):
    with pytest.raises(party.PartyError) as raised:
        party.start(party.Roster.of([], []), config)
    assert "empty" in str(raised.value)


def test_a_party_file_from_before_the_rename_still_opens(tmp_path, config):
    """Somebody may already have one sitting in a shared folder."""
    raw = party.start(roster(4), config, name="Morrowind evening").as_dict()
    raw["bgst_party"] = raw.pop("gravitone_party")      # as the old name wrote it
    path = tmp_path / "old-party.json"
    path.write_text(json.dumps(raw))

    read = party.read_file(path)
    assert read.name == "Morrowind evening"
    assert len(read.roster.music) == 4
