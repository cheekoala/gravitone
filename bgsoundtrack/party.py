"""Listening together, without anything in the middle.

Two people hear the same tracks, the same silences and the same ambience at
the same moments, with no server, no connection between them and nothing to
keep alive. It works because a session is not a stream to follow but a
**timetable**, and the timetable is a pure function of three things:

* a **seed** - every choice the engine makes (the shuffle, how long a gap
  runs, whether it is silence or wind, where the wind starts) is drawn from
  one generator, so the same seed makes the same choices forever;
* a **roster** - the tracks, in an agreed order, with the lengths the party
  was planned against;
* an **epoch** - the instant the first track starts.

Everything else follows. "What is playing at 21:47:12" is arithmetic, so two
machines that agree on the clock agree on the music without ever speaking,
and a player that joins late, restarts, or closes its laptop for ten minutes
lands exactly where the party already is instead of ten minutes behind.

Slop does not accumulate, either: every item is anchored to an absolute
instant rather than started when the last one happened to finish. A track
that runs a little short just leaves a little more quiet, and the gap between
tracks - which is silence or weather anyway - absorbs it unheard.

The whole party fits in a 32-character code. The roster travels once, as a
small file, alongside the music it describes.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import struct
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from bgsoundtrack.config import Config, config_path

VERSION = 1
# Crockford's base32: no I, L, O or U, so nothing reads as something else
# when it is typed off a screen or read down a voice call.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CONFUSABLE = {"I": "1", "L": "1", "O": "0", "U": "V"}
CODE_BYTES = 20          # 160 bits, exactly 32 base32 characters
SETTINGS = (
    "gap_min",
    "gap_max",
    "ambient_chance",
    "ambient_min_tail",
    "ambient_random_start",
    "shuffle",
    "loop",
)


class PartyError(Exception):
    pass


# -- who is on the list --------------------------------------------------


def _plain(text: str) -> str:
    """A string two libraries can agree on.

    Case, accents, punctuation and doubled spaces are all things one tagger
    writes differently from the next; none of them make it a different song.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def track_id(title: str, artist: str, album: str, name: str = "") -> str:
    """What makes this track *this track*, wherever it lives.

    Not the path - the other person keeps their music somewhere else
    entirely - and not the bytes either, since re-tagging a file (adding the
    cover art that was missing, say) rewrites them without changing a note.
    Tags, and the file name when there are no tags.
    """
    parts = [_plain(title) or _plain(Path(name).stem), _plain(artist), _plain(album)]
    if not any(parts):
        parts = [_plain(name)]
    return hashlib.sha1("\0".join(parts).encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Member:
    """One track on the roster, as the party knows it."""

    id: str
    duration: float
    title: str = ""
    artist: str = ""
    album: str = ""
    name: str = ""

    @property
    def label(self) -> str:
        if self.title and self.artist:
            return f"{self.title} — {self.artist}"
        return self.title or self.name or self.id

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "duration": round(self.duration, 3),
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Member":
        return cls(
            id=str(raw.get("id") or ""),
            duration=float(raw.get("duration") or 0.0),
            title=str(raw.get("title") or ""),
            artist=str(raw.get("artist") or ""),
            album=str(raw.get("album") or ""),
            name=str(raw.get("name") or ""),
        )


@dataclass(frozen=True)
class Roster:
    """The tracks a party plays, in the order it plays them in.

    Sorted by id rather than by name or path, so two people who hold the same
    music build the same roster without having agreed on anything first.
    """

    music: tuple = ()
    ambient: tuple = ()

    @staticmethod
    def of(music: list, ambient: list) -> "Roster":
        def ordered(members: list) -> tuple:
            seen, out = set(), []
            for member in sorted(members, key=lambda m: m.id):
                if member.id in seen:
                    continue        # the same track twice is still one track
                seen.add(member.id)
                out.append(member)
            return tuple(out)

        return Roster(music=ordered(music), ambient=ordered(ambient))

    @property
    def digest(self) -> bytes:
        """Three bytes that say "this roster and not another one"."""
        joined = "\n".join(
            [m.id for m in self.music] + ["-"] + [m.id for m in self.ambient]
        )
        return hashlib.sha1(joined.encode("utf-8")).digest()[:3]

    @property
    def fingerprint(self) -> str:
        return self.digest.hex()

    def as_dict(self) -> dict:
        return {
            "music": [m.as_dict() for m in self.music],
            "ambient": [m.as_dict() for m in self.ambient],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Roster":
        return Roster(
            music=tuple(Member.from_dict(m) for m in raw.get("music") or []),
            ambient=tuple(Member.from_dict(m) for m in raw.get("ambient") or []),
        )


# -- the party itself ----------------------------------------------------


@dataclass(frozen=True)
class Party:
    """A seed, a start time, the settings that shape the gaps, and a roster."""

    seed: int
    epoch: float
    settings: dict
    roster: Roster
    name: str = ""

    @property
    def code(self) -> str:
        return encode(self)

    def as_dict(self) -> dict:
        return {
            "bgst_party": VERSION,
            "name": self.name,
            "seed": self.seed,
            "epoch": self.epoch,
            "settings": dict(self.settings),
            "fingerprint": self.roster.fingerprint,
            "roster": self.roster.as_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Party":
        if int(raw.get("bgst_party") or 0) > VERSION:
            raise PartyError(
                "this party file was made by a newer bgst - update and try again"
            )
        return cls(
            seed=int(raw.get("seed") or 0),
            epoch=float(raw.get("epoch") or 0.0),
            settings=settings_from(raw.get("settings") or {}),
            roster=Roster.from_dict(raw.get("roster") or {}),
            name=str(raw.get("name") or ""),
        )


def settings_of(config: Config) -> dict:
    """The settings that shape the timetable, and only those.

    Volume, hidden mode and which player you use are yours alone: they change
    nothing about when the next track starts.
    """
    return {name: getattr(config, name) for name in SETTINGS}


def settings_from(raw: dict) -> dict:
    fallback = Config()
    out = {}
    for name in SETTINGS:
        value = raw.get(name, getattr(fallback, name))
        if isinstance(getattr(fallback, name), bool):
            out[name] = bool(value)
        else:
            out[name] = float(value)
    return out


def start(
    roster: Roster,
    config: Config,
    seed: int | None = None,
    epoch: float | None = None,
    name: str = "",
) -> Party:
    """Open a party: now, with these settings, on these tracks."""
    if not roster.music:
        raise PartyError("a party needs music - this playlist is empty")
    return Party(
        seed=random.SystemRandom().getrandbits(32) if seed is None else int(seed),
        # Whole seconds: the code carries no room for more, and a party that
        # starts within a second of when you pressed the button is on time.
        epoch=float(int(time.time() if epoch is None else epoch)),
        settings=settings_of(config),
        roster=roster,
        name=name,
    )


# -- the code ------------------------------------------------------------


def _pack(party: Party) -> bytes:
    settings = party.settings
    flags = (
        (1 if settings["ambient_random_start"] else 0)
        | (2 if settings["shuffle"] else 0)
        | (4 if settings["loop"] else 0)
    )
    epoch = int(party.epoch)
    return (
        bytes([VERSION])
        + struct.pack(">I", party.seed & 0xFFFFFFFF)
        + epoch.to_bytes(5, "big")
        + party.roster.digest
        + struct.pack(">H", min(65535, round(settings["gap_min"] * 10)))
        + struct.pack(">H", min(65535, round(settings["gap_max"] * 10)))
        + bytes([max(0, min(100, round(settings["ambient_chance"] * 100)))])
        + bytes([max(0, min(255, round(settings["ambient_min_tail"] * 10)))])
        + bytes([flags])
    )


def _unpack(raw: bytes) -> dict:
    if len(raw) != CODE_BYTES:
        raise PartyError("that code is the wrong length")
    if raw[0] != VERSION:
        raise PartyError(
            f"that code is from bgst party version {raw[0]}, and this is {VERSION}"
        )
    seed = struct.unpack(">I", raw[1:5])[0]
    epoch = int.from_bytes(raw[5:10], "big")
    digest = raw[10:13]
    gap_min = struct.unpack(">H", raw[13:15])[0] / 10
    gap_max = struct.unpack(">H", raw[15:17])[0] / 10
    chance = raw[17] / 100
    tail = raw[18] / 10
    flags = raw[19]
    return {
        "seed": seed,
        "epoch": float(epoch),
        "fingerprint": digest.hex(),
        "settings": {
            "gap_min": gap_min,
            "gap_max": gap_max,
            "ambient_chance": chance,
            "ambient_min_tail": tail,
            "ambient_random_start": bool(flags & 1),
            "shuffle": bool(flags & 2),
            "loop": bool(flags & 4),
        },
    }


def encode(party: Party) -> str:
    """The whole party, minus the roster, as something you can read aloud."""
    raw = _pack(party)
    bits = int.from_bytes(raw, "big")
    digits = []
    for index in range(CODE_BYTES * 8 // 5):
        digits.append(ALPHABET[(bits >> (5 * index)) & 31])
    text = "".join(reversed(digits))
    return "-".join(text[at:at + 4] for at in range(0, len(text), 4))


def decode(code: str) -> dict:
    """Read a code back. Forgiving about how it was typed."""
    cleaned = "".join(
        CONFUSABLE.get(ch, ch) for ch in re.sub(r"[^0-9A-Za-z]", "", code or "").upper()
    )
    if not cleaned:
        raise PartyError("no code given")
    expected = CODE_BYTES * 8 // 5
    if len(cleaned) != expected:
        raise PartyError(
            f"a party code is {expected} characters, and that one is {len(cleaned)}"
        )
    bits = 0
    for ch in cleaned:
        if ch not in ALPHABET:
            raise PartyError(f"{ch!r} is not part of a party code")
        bits = (bits << 5) | ALPHABET.index(ch)
    return _unpack(bits.to_bytes(CODE_BYTES, "big"))


# -- the timetable -------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One thing the party does, at a fixed moment.

    `at` is seconds since the party's epoch, so it is the same number on
    every machine in the party.
    """

    kind: str            # "track" | "ambient" | "silence"
    at: float
    duration: float
    member: Member | None = None
    seek: float = 0.0    # where an ambient bed is dropped into

    @property
    def ends(self) -> float:
        return self.at + self.duration


class Timetable:
    """Everything the party will do, worked out from the seed.

    The engine reads this instead of rolling its own dice, so there is one
    account of what happens and no way for the two to disagree.
    """

    # A party left running for a week is still only tens of thousands of
    # items, but the line has to be somewhere.
    LIMIT = 200_000

    def __init__(self, party: Party):
        self.party = party
        self._items: list = []
        self._rng = random.Random(party.seed)
        self._queue: list = []
        self._clock = 0.0
        self._done = False
        self._started = False

    # The engine's own choices, made here instead. The order of the draws is
    # what has to match, so they live in one place.
    def _gap(self) -> float:
        s = self.party.settings
        return self._rng.uniform(s["gap_min"], s["gap_max"])

    def _bed(self, gap: float) -> Member | None:
        s = self.party.settings
        ambient = self.party.roster.ambient
        if not ambient or gap < s["ambient_min_tail"]:
            return None
        if self._rng.random() >= s["ambient_chance"]:
            return None
        return self._rng.choice(list(ambient))

    def _seek(self, bed: Member, gap: float) -> float:
        if not self.party.settings["ambient_random_start"]:
            return 0.0
        room = bed.duration - gap
        if room <= 1.0:
            return 0.0
        return self._rng.uniform(0.0, room)

    def _grow(self) -> bool:
        """Add the next track and the gap after it. False when the party ends."""
        if self._done:
            return False
        settings = self.party.settings
        if not self._queue:
            if self._started and not settings["loop"]:
                self._done = True
                return False
            order = list(self.party.roster.music)
            if settings["shuffle"]:
                self._rng.shuffle(order)
            self._queue = order
            self._started = True
        member = self._queue.pop(0)
        # A track of no known length would make the rest of the timetable a
        # guess, so it is given a nominal three minutes rather than zero.
        length = member.duration if member.duration > 0 else 180.0
        self._items.append(Item("track", self._clock, length, member))
        self._clock += length
        gap = self._gap()
        if gap > 0:
            bed = self._bed(gap)
            if bed is None:
                self._items.append(Item("silence", self._clock, gap))
            else:
                self._items.append(
                    Item("ambient", self._clock, gap, bed, self._seek(bed, gap))
                )
            self._clock += gap
        return True

    def at(self, elapsed: float) -> tuple | None:
        """What is on at this many seconds past the epoch, and how far in.

        `None` once the party is over (a run with looping off), and the first
        item for anything before it began.
        """
        if elapsed < 0:
            elapsed = 0.0
        while self._clock <= elapsed and len(self._items) < self.LIMIT:
            if not self._grow():
                break
        for item in reversed(self._items):
            if item.at <= elapsed < item.ends:
                return (item, elapsed - item.at)
        if self._items and elapsed < self._items[0].at:
            return (self._items[0], 0.0)
        return None

    def upcoming(self, elapsed: float, count: int = 8) -> list:
        """The next few things, for showing what is coming."""
        found = self.at(elapsed)
        out = []
        index = self._items.index(found[0]) if found else 0
        while len(out) < count:
            while index >= len(self._items):
                if not self._grow():
                    return out
            out.append(self._items[index])
            index += 1
        return out


# -- matching a party to the music actually on this machine --------------


@dataclass
class Match:
    """What this machine can and cannot play of a party's roster."""

    found: dict = field(default_factory=dict)      # track id -> file
    missing: list = field(default_factory=list)    # Members with no file here

    @property
    def complete(self) -> bool:
        return not self.missing

    def path(self, member: Member | None):
        return self.found.get(member.id) if member else None


def match(roster: Roster, have: dict) -> Match:
    """Line a roster up against what is here.

    Anything extra on this machine is simply not in the party, and anything
    of the party's that is missing here plays as silence: the timetable is
    the same either way, so a library that is a superset, a subset or merely
    overlapping all stay in step.
    """
    found, missing = {}, []
    for member in tuple(roster.music) + tuple(roster.ambient):
        path = have.get(member.id)
        if path is None:
            missing.append(member)
        else:
            found[member.id] = path
    return Match(found=found, missing=missing)


# -- reading a party off the library on this machine ---------------------


def _real(path: Path) -> Path:
    """The file itself: a library entry is usually a link to it."""
    try:
        return path.resolve()
    except OSError:
        return path


def members(config: Config, playlist, reader, section: str = "music") -> list:
    """A section of a playlist, named the way a party names things."""
    from bgsoundtrack import library

    found = []
    for entry in library.entries(config, section, playlist, reader=reader):
        known = reader.known(_real(entry.target))
        found.append(
            Member(
                id=track_id(known.title, known.artist, known.album, entry.name),
                duration=known.duration,
                title=known.title,
                artist=known.artist,
                album=known.album,
                name=entry.name,
            )
        )
    return found


def roster_of(config: Config, playlist, reader) -> Roster:
    """A playlist as a roster - the same one on any machine holding it."""
    return Roster.of(
        members(config, playlist, reader, "music"),
        members(config, playlist, reader, "ambient"),
    )


def here(config: Config, playlist, reader) -> dict:
    """Which of a party's tracks are on this machine, and the file to play.

    Keyed the way the roster is, so a match needs nothing in common but the
    music itself - not a path, not a file name, not a folder layout.
    """
    from bgsoundtrack import library

    found = {}
    for section in library.SECTIONS:
        for entry in library.entries(config, section, playlist, reader=reader):
            known = reader.known(_real(entry.target))
            key = track_id(known.title, known.artist, known.album, entry.name)
            found.setdefault(key, entry.path)
    return found


def read_tags(config: Config, playlist, reader) -> int:
    """Read every tag in a playlist now, not in the background.

    A party is planned against track lengths; a guessed length would put the
    two machines on different timetables.
    """
    from bgsoundtrack import library

    targets = [
        entry.target
        for section in library.SECTIONS
        for entry in library.entries(config, section, playlist)
    ]
    waiting = reader.pending(targets)
    if waiting:
        reader.read_all(waiting)
        reader.save()
        reader.forget_memo()
    return len(waiting)


def active_path(config_file: Path | None = None) -> Path:
    base = config_file or config_path()
    return base.parent / "party.json"


def load_active(config_file: Path | None = None) -> "Party | None":
    """The party this machine is in, if any. It outlives the process."""
    try:
        raw = json.loads(active_path(config_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Party.from_dict(raw)
    except PartyError:
        return None


def save_active(made: "Party | None", config_file: Path | None = None) -> None:
    path = active_path(config_file)
    if made is None:
        path.unlink(missing_ok=True)
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(made.as_dict()), encoding="utf-8")
    except OSError:
        pass


# -- where parties are kept ----------------------------------------------


def parties_dir(config_file: Path | None = None) -> Path:
    base = config_file or config_path()
    return base.parent / "parties"


def save_roster(roster: Roster, config_file: Path | None = None) -> Path:
    """Keep a roster by its fingerprint, so a code alone is enough next time."""
    directory = parties_dir(config_file)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{roster.fingerprint}.json"
    target.write_text(json.dumps(roster.as_dict()), encoding="utf-8")
    return target


def load_roster(fingerprint: str, config_file: Path | None = None) -> Roster | None:
    target = parties_dir(config_file) / f"{fingerprint}.json"
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    roster = Roster.from_dict(raw)
    return roster if roster.fingerprint == fingerprint else None


def write_file(party: Party, path: Path) -> Path:
    """The party as a file to hand over, roster and all."""
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / "bgst-party.json"
    if not path.suffix:
        path = path.with_suffix(".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(party.as_dict(), indent=2), encoding="utf-8")
    return path


def read_file(path: Path) -> Party:
    path = Path(path).expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PartyError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PartyError(f"{path.name} is not a party file: {exc}") from exc
    if "bgst_party" not in raw:
        raise PartyError(f"{path.name} is not a bgst party file")
    return Party.from_dict(raw)


def join(code: str, roster: Roster | None = None, config_file: Path | None = None) -> Party:
    """Turn a code into a party, finding the roster it refers to.

    A roster is offered, then one saved from an earlier party with the same
    fingerprint. Without either, the code cannot say what the music is.
    """
    header = decode(code)
    if roster is not None and roster.fingerprint != header["fingerprint"]:
        roster = None
    if roster is None:
        roster = load_roster(header["fingerprint"], config_file)
    if roster is None:
        raise PartyError(
            "this code is for a set of tracks this machine has not seen. "
            "Ask for the party file that goes with it (bgst party save), "
            "then join again - the code alone works from then on."
        )
    return Party(
        seed=header["seed"],
        epoch=header["epoch"],
        settings=header["settings"],
        roster=roster,
    )
