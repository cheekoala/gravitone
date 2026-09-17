<img src="docs/icon.svg" width="72" align="left" alt="">

# Gravitone

**Your own soundtrack for any game.** Point it at music you already have and
it plays a shuffled soundtrack behind whatever you're in — with a random gap
after each song that is either silence or a bed of ambience (wind, rain,
tavern noise, crickets).

<br clear="left">

A graviton is the particle that would carry gravity, if we ever catch one;
a tone is what you'd hear if it arrived. The mark is a mass with the waves
coming off it.

- **Playlists.** Two folders you switch between — one per game, per mood, per
  session. Each keeps its own links, its own folders, and remembers which
  tracks you took out of it. Switching lands on the next track.
- **Two ways in, mixed freely.** *Link* a file and a symlink lands in your
  `custom soundtrack` folder, pointing at where it already lives — a 40 GB
  collection costs a few KB of directory entries. Or set a whole folder as a
  **folder** to a playlist and it plays in place, picking up whatever you drop
  in later.
  Nothing is ever copied.
- **A separate `ambient` folder and ambient folders** for loops and atmosphere.
- **Random gaps with sane defaults** — 12–45 s, 65 % of them ambient, the rest
  silence — tunable anywhere from none to a full hour, and hideable if you
  would rather not know how long the quiet lasts.
- **Listening together, offline.** A 32-character code and a shared clock put
  two machines on the same evening, with nothing running between them.
- **A tiny control panel** (`gravitone ui`) that runs in your browser, or on
  your phone as a remote — no Electron, no build step, no dependencies.
- **No Python dependencies.** Playback goes through `ffplay`, `mpv`, `afplay`
  or `vlc`, whichever you have.

The command is `gravitone`, and `grav` for short.

![The gravitone control panel, with Remove armed and its confirm dropped below](docs/ui-now.png)

## Install

One script per platform. Each installs into a private virtualenv (or pipx if
you have it), puts `gravitone` on your PATH, and offers to install an audio player
if you have none.

**Linux, macOS, *BSD, WSL**

```sh
git clone https://github.com/cheekoala/custom_bg_game_soundtrack_player
cd custom_bg_game_soundtrack_player
./install.sh                 # add --with-player to install ffmpeg without asking
```

**Windows** (PowerShell)

```powershell
git clone https://github.com/cheekoala/custom_bg_game_soundtrack_player
cd custom_bg_game_soundtrack_player
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**Any platform, by hand**

```sh
pipx install .          # or: pip install --user .
```

The installer offers to add a menu entry and a desktop shortcut (`--shortcut`
/ `--no-shortcut` to decide up front; `-Shortcut` / `-NoShortcut` on Windows),
and to put `~/.local/bin` on your PATH if it isn't already (`--path` /
`--no-path`) — otherwise `gravitone` installs fine and then `command not found`.
It writes one line to your shell's own profile (`.zshrc`, `.bashrc`,
`config.fish`, `.profile`), once; open a new terminal afterwards. Until then
the full path works: `~/.local/bin/gravitone ui`.

When it offers to open the control panel at the end, it starts it **detached**
(`setsid`), so the UI keeps running after that terminal window closes —
logging to `~/.local/state/gravitone/ui.log`.

**Double-clicking `install.sh` in Dolphin, Nautilus or Thunar** used to look
like nothing happened: a file manager runs an executable script with no
terminal attached, so everything it prints goes to a pipe nobody reads, and
its questions have nowhere to appear. It now notices it has no terminal and
reopens itself in one (konsole, gnome-terminal, xfce4-terminal, kitty,
alacritty, foot, xterm — whichever you have), so you can watch the install and
answer its two questions. `--no-terminal` keeps it in place.

Uninstall with `./install.sh --uninstall` / `.\install.ps1 -Uninstall`; both
leave your library and config alone. `make help` lists the same tasks for
developers.

You also need one player: `ffmpeg` (for `ffplay`), `mpv`, or `vlc` — the
installers offer to fetch one. Check any time with `gravitone doctor`.

## Use

```sh
gravitone ui                                    # the control panel, in your browser
```

`gravitone ui` starts the server *and* opens the page. Opening
`gravitone/ui/index.html` from the folder by hand gives you a dead page —
it has no server to talk to, so no library, no file browser, no playback. The
page says so if you land there.

Only one UI runs at a time: start it again (or click the shortcut again) and
it opens the browser at the one already running instead of fighting it for the
port. `gravitone ui --stop` ends it, `gravitone ui --new` starts a second one anyway. If
port 8765 is taken by something else, it moves to the next free port and says
so; with an explicit `--port` it reports the clash instead of moving.

Or from the terminal:

```sh
gravitone init                                    # create the custom soundtrack folder
gravitone playlist new "Hollow Kingdom" --folder ~/Music/Nier --use
gravitone playlist new "Field Work" --folder ~/Sounds/recordings
gravitone folder add --ambient ~/Sounds/weather   # into the selected playlist
gravitone link ~/Music/Outer\ Wilds               # or link track by track
gravitone play
```

While playing in a terminal: `n` skips, `b` bans (skip and drop it from this
playlist), `q` quits.

```
♪ Ashes.flac
  ~ rain.ogg (0:34)
♪ Village.mp3
  . silence (0:19)
```

## Speed and stability

The page is polled once a second, but the **playing clock does not wait for
it**: each answer is a fix, and between fixes the progress bar and the times
run on their own at one second per second, snapping back only when the server
says something different. A late answer no longer shows up as the bar sitting
still and then lurching four seconds.

Skip and Remove both fade the sound out before stopping it, so the button
keeps its spinner until the music has actually moved on rather than until the
request returns — and while it waits, the page asks more often than once a
second so it catches the change as it happens.


Everything that walks the disk happens on one background worker, never on a
request. The folder index is written to `~/.config/gravitone/index.json`,
so a restart starts with answers rather than work, and the state the UI polls
once a second is worked out only when something actually changes.

On a 2000-file library: a poll costs ~0.02 ms (it used to re-walk every
folder), and the server starts answering immediately instead of after the
first scan. Tag reads and cover extraction also happen in the background —
the Server card in Config shows what is being worked on.

The track table builds 400 rows at a time and has a **search box**; with
thousands of tracks the page stays responsive instead of parking tens of
thousands of nodes in the DOM. *Show all* is there when you want it.

A refresh that would draw the same rows patches them in place instead of
rebuilding the table, so **where you scrolled to stays where you scrolled
to** while the tags and covers fill in around you.

Anything in flight shows a thin sweeping bar across the top of the window,
and buttons that take a moment (exporting a bundle, finding art) spin while
they work. A library that takes a second to arrive shows pulsing placeholder
rows rather than an empty table, and cover cells pulse while art is being
read.

![Placeholder rows while a large library loads](docs/ui-loading.png)

## Server controls

Config → **Server** shows the address, the process id, how long it has been
up and its version, with **Restart** and **Stop** buttons. Restart replaces
the process in place, keeping the same port *and the same token*, so the page
you clicked it from reconnects by itself.

From a terminal:

```sh
gravitone ui --status      # is one running, where, and since when
gravitone ui --stop        # stop it
gravitone ui --new         # a second one anyway
```

## The UI

`gravitone ui` serves a small control panel on `127.0.0.1:8765` and opens it. It is
plain HTML, CSS and JavaScript served by Python's own HTTP server — no
Electron, no build step, no dependencies, nothing loaded from the internet.

| | |
| --- | --- |
| ![The track table, sorted by album, with one row playing](docs/ui-library.png) | ![Settings: gaps, levels, fade, playlists](docs/ui-settings.png) |
| ![The file browser, with a folder opened where it stands](docs/ui-browse.png) | ![Adding: a whole folder, or just the files in it](docs/ui-add.png) |

- **Top bar** — the playlist selector; switching it switches what plays. The
  button says what pressing it does — **Play**, then **Stop** — and a green
  **Live** badge next to it says something is sounding. The button is never
  the status: you should not have to press the word "Live" to make things
  stop.
- **Now** — what is playing, named from its tags (`Anchor — Vela`, with the
  album underneath and the cover beside it) or how long the current gap runs,
  with history. Tags for the playing track are read on the spot, so this
  works whether or not you have opened the library table.
- **Playlist / Ambient** — what this playlist plays, and what fills the gaps;
  `✕` removes a link, never a file.
- **Add** — *Choose a folder…* opens your desktop's own folder dialog (needs
  Tk; `gravitone doctor` says whether you have it). Otherwise use the built-in
  file browser, or paste a path — a web file picker hands over file
  *contents*, never paths, and paths are what linking needs.

  The browser behaves like a file manager rather than a list: **Back** (where
  you were), **Up** (the folder above), **Home**, and a breadcrumb where every
  step of the path is clickable. Each folder says what is in it — *5 tracks ·
  2 folders* — and the chevron **opens it where it stands**, indenting its
  contents underneath rather than taking you somewhere else, so a box of
  albums can be read without walking in and out of it. Every folder row offers
  **Add folder** (the whole folder joins this playlist, live) and **Link
  files** (just the files in it now, as symlinks).
- **Config** — gaps, levels, fade, shuffle, loop, your playlists (rename,
  delete, create), this playlist's folders and removed tracks, and
  export/import. Changes save immediately and take effect from the next gap;
  no need to restart playback.

On a wide screen the artwork is given room: the cover in Now is 288px and the
table's thumbnails are 68px, dropping back to 148/34 on a tablet and 96/34 on
a phone. It is a music player; the records should be visible.

Keys: `space` play/stop, `n` next, `r` remove (`b` still works). Every API call needs the token in the URL,
so another page in your browser cannot drive your player or read your disk.

Run it as a phone remote for the machine that's playing:

```sh
gravitone ui --host 0.0.0.0        # prints a LAN URL with the token
```

Anyone who has that link can control playback, so use it on networks you
trust.

## Playlists

Already have two folders you think of as two playlists? Make them two:

```sh
gravitone playlist new "Hollow Kingdom" --folder ~/Music/hollow-kingdom --use
gravitone playlist new "Night Drive"    --folder ~/Music/night-drive
gravitone playlist list
gravitone playlist use "Night Drive"
```

Or pick them from the selector in the top bar of the UI. A playlist owns:

- its **folders**, played in place;
- its own **links**, in `custom soundtrack/playlists/<id>/`;
- its **removals** — take a track out with `✕` (or `gravitone unlink NAME`) and it
  stays out of *this* playlist, remembered across restarts. The same file
  keeps playing in any other playlist that points at it, and the file itself
  is never touched. Put it back from Config → *Removed from this playlist*,
  or `gravitone playlist restore --all`.

Removing a **linked** track deletes that playlist's link instead — there is
nothing to remember, and the other playlists keep theirs.

Switching playlist while music is playing takes effect at the next track, not
the next full pass — as does linking, removing, or adding a source.

The playlist called **Library** is the plain `custom soundtrack/music` and
`/ambient` folders, so an install from before playlists keeps working exactly
as it did, folders and all.

Playlists live in `~/.config/gravitone/playlists.json` — plain JSON, easy
to read, back up, or edit by hand.

## Album art

Covers come from **the picture inside the file**, and only from there — a
`cover.jpg` lying in the folder is someone else's idea of what the record
looks like, so it is ignored. Tag the files and gravitone will find it.

Plenty of rips carry the picture on one track and not the rest, so a record
is read across its first few files before it is called coverless. Both
answers — the cover and the "there is none" — are written to
`~/.config/gravitone/covers/index.json`, so a restart asks nothing again.

**A record is a folder and an album tag, not a folder.** Keeping a whole
game's music in one directory is normal, and one answer per directory meant
whichever album happened to be listed first decided for everything below it:
six coverless tracks at the top of the folder hid the art on the rest. Tracks
are grouped by what their album tag says, and only the tracks of that album
are read looking for its picture.

The picture is lifted out byte for byte and the format read off the bytes,
not off the type the tagger wrote down. A JPEG filed as `image/png` is
common, and decoding it by the label fails outright — the wrong decoder is
picked and refuses a picture that is perfectly good. It is shrunk to 600px
afterwards, from the file rather than from the label.

Before running ffmpeg at all, the file's header is checked for an attached
picture: 45 ms to ask versus about 3 s for ffmpeg to scan a whole file and
find nothing.

**Config → Find album art** goes through the playlist in the background,
reading the records nobody has asked about yet; **Look again** throws every
answer away and re-reads the lot, for after you have added art to files. It
reads the tags first, since they are what says where one record ends and the
next begins. Art is cached in `~/.config/gravitone/covers/`, one file per
record, since a record shares its cover, and `gravitone doctor` says how many
records have art and how many are known not to.

A cover is fetched **once per record, not once per track**, and served with
an ETag and a week of cache headers. The token the page uses is kept in
`~/.config/gravitone/token` rather than made up at every start, so a
restart reuses the browser's cached art instead of re-fetching all of it;
`gravitone ui --new-token` throws the old one away when you want that.

Now shows the cover of what is playing at full size, next to the title,
artist and album; the table shows a thumbnail per row.

## The track table

Each library is a table — track number, title, artist, album, length — and
**clicking a column header sorts by it**, the same header again reverses it.
The order sticks (it is a setting) and it is also the play order when shuffle
is off.

The **File names** switch decides what the first column is: the file on disk,
or the title tag. Whichever it shows is what its header sorts by, so the
column and the sort never disagree.

```sh
gravitone list --sort album        # name, title, artist, album, length
gravitone config sort_by=artist sort_desc=true
```

Tags come from `ffprobe` (part of ffmpeg). Anything it cannot read falls back
to the path — `Artist/Album/03 Title.flac` is a convention for a reason —
and a guessed artist or album is shown in italics rather than passed off as a
tag. Tags are read in the background and cached in
`~/.config/gravitone/tags.json`, so a listing never waits on a probe: the
table fills in the moment the read lands, and says how many are left while it
works.

## Searching

Every list has a **search box** above it. Type and the table narrows as you
go, matching across the file name, title, artist, album and path; several
words all have to match, in any order and any field, so `bell vela` finds the
Vela track by Bell. It says `3 of 7` beside the box so you know what you are
looking at. `/` (or Ctrl-F) jumps to it from anywhere on the page, Escape
clears it.

The header stays put while you scroll. Columns keep their share of the width
and truncate with an ellipsis (the full value is in the tooltip), so a
sprawling album title can never push the table out of its panel. On a phone
the artist, album and length columns fold away, leaving the track and its
title, and the filter box takes its own line.

The window is 1440px wide at most, so a desktop gets a properly wide table
without the settings cards stretching into something silly.

## Export and import

Two shapes, for two jobs:

```sh
gravitone export ~/gravitone-library.json                    # a manifest: what is in each playlist
gravitone export ~/library.csv --format csv             # the same, flat, one row per track
gravitone export ~/share.zip --bundle --only "Night Drive"   # a zip with the audio inside
gravitone import ~/share.zip                            # adds playlists, never overwrites
```

A **manifest** is small, readable JSON listing each playlist's folders, links
and removals. It points at files rather than carrying them — right for your
own backup, or moving to a machine that has the same music on it. Import
reports anything that isn't there.

A **bundle** is a zip with the audio in it, so someone else can unpack it and
hear what you hear. Importing one unpacks to
`custom soundtrack/imported/<name>/` and makes playlists that play it.
(Paths inside a bundle are checked before extraction — a zip cannot write
outside that folder.)

Either can be driven from Config → *Export & import*, which uses your
desktop's save/open dialog where there is one and a path box where there
isn't. Import always **adds**: a name that already exists becomes
`Hollow Kingdom (2)` rather than replacing anything.

## Listening together

Two machines, the same music at the same moment, with **nothing between
them** — no server, no connection, nothing to keep alive. gravitone does not
stream and it does not follow: both ends work out the same evening from the
same code.

```sh
gravitone party new --save /nas/music/gravitone-party.json   # start one, write the file
gravitone party join 07PW-8BJW-01NA-NA8J-RN5W-W03R-0714-27G7
gravitone party status                                  # where it has got to
gravitone party leave
```

or Party in the control panel: **Start a party** gives you a code to read
out; **Join a party** takes one.

![A party running: the code, what is on, and what is coming](docs/ui-party.png)

### How it can possibly work

A session is not a stream, it is a **timetable**, and the timetable is a pure
function of three things: a **seed** (every choice the engine makes — the
shuffle, how long a gap runs, whether it is silence or wind, where the wind
starts — comes from one generator), a **roster** (the tracks, in an order
both sides arrive at independently, with the lengths the party was planned
against), and an **epoch** (the instant the first track started).

Everything else is arithmetic. "What is playing at 21:47:12" has one answer,
and two machines that agree on the clock agree on the music without ever
speaking. Join at nine for a party that started at seven and you drop into
the middle of the track everyone else is in the middle of. Close the lid for
ten minutes and you come back *in step*, not ten minutes behind.

Nothing drifts, either, because every item is pinned to an absolute instant
rather than started when the last one happened to finish. A track that runs
a second short just leaves a second more quiet — and since the gap between
tracks is silence or weather anyway, the slop is inaudible. That is the one
real advantage of a player built around long random gaps: it has shock
absorbers.

### The code, and the file

The code is 32 characters in [Crockford's base32](https://www.crockford.com/base32.html)
(no I, L, O or U, so nothing reads as something else down a voice call). It
carries the seed, the epoch, a fingerprint of the roster, and **the settings
that shape the gaps** — `gap_min`, `gap_max`, `ambient_chance`,
`ambient_min_tail`, `ambient_random_start`, `shuffle` and `loop`. Those have
to travel: the silences are part of the party, not a local preference.
Volume, fade, hidden mode and which player you use stay yours — they change
nothing about when the next track starts.

What the code cannot carry is 300 tracks, so the **party file** does. Put it
next to the music you are already sharing; whoever takes it keeps the roster
by fingerprint, and **from then on the code alone is enough**. You only need
the file again when the set of tracks changes.

If both libraries hold exactly the same playlist, even the first code works
on its own — joining searches every playlist on the machine for one that
matches, and switches to it.

### Matching music, not paths

A track is identified by what it *is*: its title, artist and album,
normalised for case, accents and punctuation (and its file name when it has
no tags). Not its path — the other person keeps their music somewhere else
entirely — and not its bytes either, since re-tagging a file rewrites them
without changing a note.

So the two libraries need nothing in common but the music:

- **Bigger on their side?** Their extra tracks are simply not in the party.
- **Smaller?** What they lack plays as **silence** for them, for exactly as
  long as it plays for you, and gravitone names what is missing so they can copy
  it over. Drop the files in mid-party and they join in at their next turn.
- **Different folder layout, different file names, different tagger?** Fine.

### Skipping, banning, and the lid

In a party **skip sits a track out**: the sound stops, your place does not,
and you are back with everyone at the next track. It cannot put you out of
step, so there is no warning to click through — but a button whose whole
effect is *silence* looks broken, so the page says what it did and **keeps
saying it** until the party moves on by itself: a note that stays put rather
than fading after three seconds, and "Sitting this one out" where the player
would normally name what is on. Click the note to wave it away. Remove is off for a track that
is not on your machine, and a track you have banned simply plays as silence
for you — the same rule as one you never had.

Stopping and starting, restarting the server, or closing the laptop all
leave the party alone: it is a timetable, and it is still running. `gravitone
party leave` is the way out.

### Clocks

Both ends normally run NTP and need nothing. The party's tolerance is the
gap between tracks, so being a few hundred milliseconds out is not something
anyone can hear. If a machine is known to be off, nudge it:

```sh
gravitone config party_offset=-0.5      # this machine's clock reads half a second late
```

or the **Clock ±0.5s** buttons on the Party card.

### What it does not do

There is no way to skip *for everyone*, change the queue, or chat — that
needs a connection, and the whole appeal here is that there isn't one. What
you get instead is a party that survives a flat battery, a dropped VPN and a
train tunnel.

## The library

```
custom soundtrack/
├── music/                       the Library playlist's symlinks
├── ambient/                     its ambience
└── playlists/
    └── survival-soundtrack/     every other playlist gets its own pair,
        ├── music/               named after the playlist
        └── ambient/

plus any folders you added, played where they stand
```

**Linked** files are curated one by one and stay put even if you reorganise
the original folder later. **Folders** are the low-effort option: point a playlist
at `~/Music/Soundtracks`, and every audio file under it plays, including
whatever you add next week. A file reachable both ways is only played once.

### The folder is the playlist

A linked track is a real symlink, so the playlist is browsable in Dolphin,
Finder or Explorer as well as in here — sort it, open a track, drag one out.
Two things surprise people:

- **A named playlist does not live in `custom soundtrack/music`.** That
  folder belongs to the *Library* playlist. Everything else is under
  `playlists/<name>/`. Config → *This playlist on disk* shows the exact path,
  with a button to copy it.
- **A folder added whole links nothing.** It plays in place by design — that
  is the whole point of it — so its folder on disk is empty. Press
  **Mirror as links** (Config → *This playlist on disk*) and every track the
  playlist plays gets a symlink of its own, without copying a byte or
  changing what plays. Running it twice does nothing the second time.

```sh
gravitone playlist list      # every playlist, with what it holds
```

```sh
gravitone folder add ~/Music/Soundtracks      # a music folder for this playlist
gravitone folder add --ambient ~/Sounds       # an ambience folder
gravitone folder list                         # with per-folder track counts
gravitone folder remove ~/Music/Soundtracks   # the folder itself is untouched
gravitone ui --pick                           # pick one from a system dialog
```

(`gravitone source …` still works — same command, older name.)

Because entries are plain symlinks, you can also manage the folder by hand —
drag links in with your file manager, rename them to change sort order, delete
one to drop a track. The player ignores non-audio files and dangling links.

| Command | |
| --- | --- |
| `gravitone ui` | open the control panel (`--host 0.0.0.0` for a phone remote) |
| `gravitone play` | play in the terminal (`n` next, `q` quit) |
| `gravitone link PATH...` | symlink files/folders in (`--ambient`, `--no-recursive`, `--relative`) |
| `gravitone folder add\|remove\|list` | put whole folders in this playlist (`--ambient`) |
| `gravitone playlist list\|new\|use\|rename\|remove` | switch between sets of music |
| `gravitone playlist removed\|restore` | see and undo removals in this playlist |
| `gravitone export PATH [--bundle\|--format csv]` | write playlists out, with or without the audio |
| `gravitone import PATH` | read one back in as new playlists |
| `gravitone list --sort album` | order by name, title, artist, album or length |
| `gravitone play --hidden` | play without ever showing gap lengths |
| `gravitone ui --stop\|--new` | stop the running control panel, or start a second |
| `gravitone unlink NAME...` | remove entries (only ever deletes symlinks, never real files) |
| `gravitone list --targets` | show the library and what each link points at |
| `gravitone prune` | drop links whose target moved or was deleted |
| `gravitone doctor` | check folders, tracks and available players |

Any command takes `--playlist NAME` to act on a playlist without selecting it.

`--relative` writes relative symlinks, which keep working if the library and
your music move together (e.g. both on one external drive).

On Windows, symlinks need Developer Mode (Settings → System → For developers).
Without it `gravitone` falls back to hard links, which also cost no extra space but
cannot cross drives.

## Remove

**Remove** sits beside skip and never moves. Press it (or `r`) and a confirm
button drops down *on its own layer* naming the track — the buttons
underneath stay exactly where they were, so the thing under your cursor is
still the thing you were aiming at. Press the confirm (or `r` again) to go
through with it; click anywhere else, press `Escape`, or wait eight seconds
and it forgets.

Confirming skips the track *and* takes it out of the current playlist — the
same removal the `✕` does, so a track from a folder is only remembered as
gone and the file is never touched. Undo it in Config → *Removed from this
playlist*.

The confirmation names the track it armed on, and the server checks that name
before acting: if the music moved on while the confirm was sitting there,
nothing is removed. Remove during ambience takes out the ambient track
instead; during silence there is nothing to remove.

## Gaps between songs

Every song is followed by a gap of a random length between `gap_min` and
`gap_max`. Each gap independently becomes either ambience (probability
`ambient_chance`, a random track from `ambient/`, cut to the gap length) or
plain silence. Gaps shorter than `ambient_min_tail` stay silent — a two-second
smear of rain sounds like a mistake.

| Setting | Default | |
| --- | --- | --- |
| `gap_min` / `gap_max` | `12` / `45` | gap length range, seconds — up to `3600` (an hour) |
| `ambient_chance` | `0.65` | share of gaps that get ambience |
| `ambient_min_tail` | `3.0` | shortest gap worth filling with ambience |
| `ambient_random_start` | `true` | drop into an ambient track at a random point |
| `hide_gaps` | `false` | hidden mode: never show how long a gap runs |
| `sort_by` | `name` | list and play order: name, title, artist, album or length |
| `sort_desc` | `false` | reverse that order |
| `show_filenames` | `true` | first column is the file on disk, not the title tag |
| `volume` / `ambient_volume` | `70` / `45` | ambience sits under the music |
| `shuffle` | `true` | shuffled passes; no repeat until all have played |
| `loop` | `true` | start a new pass when the list is exhausted |
| `root` | `~/.local/share/custom soundtrack` | library location |

The gap sliders reach an hour on a curved scale, so the first third of the
travel still covers 0–60 s. Type an exact value into the box beside them
instead if you prefer: `90`, `90s`, `3m`, `2m30`, `1:30` all work.

**Hidden mode** (`hide_gaps`, or `gravitone play --hidden`) withholds gap lengths
*server-side* — the browser is never told how long the quiet is or how much is
left, so there is no countdown to watch and nothing to peek at in the network
tab. Songs still show their progress.

**Ambient start points** are randomised by default, so a twenty-minute rain
recording doesn't open on the same three seconds every time. It needs
`ffprobe` (part of ffmpeg) to know how long the file is; without it, ambience
starts at the top. The offset always leaves enough track to cover the whole
gap.

Change them for good, or just for one session:

```sh
gravitone config gap_min=30 gap_max=120 ambient_chance=0.8   # saved
gravitone config                                             # show everything
gravitone play --gap-min 60 --gap-max 180 --no-ambient       # this run only
```

Other `play` flags: `--volume`, `--ambient-volume`, `--no-shuffle`, `--no-loop`,
`--player ffplay|mpv|afplay|vlc`, `--seed N` (reproducible shuffle and gaps).

Settings live in `~/.config/gravitone/config.json`. `GRAVITONE_ROOT` and
`GRAVITONE_CONFIG` override the paths, and `--root` / `--config` override
them per command — handy for a separate library per game.

## Cross-platform

| | |
| --- | --- |
| Linux, macOS, *BSD, WSL | `./install.sh` |
| Windows 10/11 | `install.ps1` |
| Playback | ffplay, mpv, afplay or vlc — whichever is installed |
| Runtime | Python 3.9+, standard library only |
| File chooser | your desktop's own, when Tk is installed; a built-in browser otherwise |
| UI | any browser, including a phone on the same network |

## Fades

Sound arrives and leaves rather than appearing and vanishing. **Config →
Fade** sets how long that takes (1.5s by default, 0 turns it off), and it
applies to tracks and to ambient beds alike.

It is done two different ways, because there are two different endings:

- **An ending that is known in advance** — a track playing out, a bed cut to
  the length of its gap — is faded by the player itself, with an ffmpeg
  `afade` filter worked out from the length. That is exact, sample-accurate
  and costs nothing. It needs a player that takes filters, which means
  **ffplay** (part of ffmpeg); mpv, afplay and cvlc play flat.
- **An ending nobody scheduled** — you pressed skip, or stop — has no filter
  to arrange, so the level is walked down through the system mixer instead
  and the process is ended quiet. That works with any player, wherever the
  mixer does (PulseAudio or PipeWire); without one the sound simply stops as
  it always did. This fade is capped at 0.8s however long the setting is: a
  skip still has to feel like a skip.

```sh
gravitone config fade=2.5      # a long, slow swell
gravitone config fade=0        # straight in, straight out
```

## Volume

The in-app sliders drive the **system mixer** (PulseAudio / PipeWire, via
`pactl`) for the track that is playing right now, and fall back to the
player's own volume flag for the next one. That matters because `ffplay`
reads `-volume` once at startup and offers no way to change it afterwards —
which is why, before this, moving the app slider mid-song did nothing on
Fedora/Plasma while the desktop's own mixer worked fine.

Players are also tagged as `gravitone` (`PULSE_PROP_application.name`), so your
desktop's volume mixer shows one **gravitone** entry to ride rather than a new
`ffplay` appearing for every song.

If `pactl` is missing (a pure-ALSA box, macOS, Windows), a volume change
applies from the next track and the UI says so.

## After an upgrade

Reinstalling replaces the files on disk, but a UI that is **already running**
keeps the old code in memory while serving the new page from disk — so the
page asks for things the server has never heard of (`unknown setting
'sort_desc'`), or columns come up empty. gravitone now notices:

- the page shows a banner saying it was updated and what to run;
- `gravitone ui` refuses to quietly hand you the stale one: in a terminal it offers
  to restart it, and from a shortcut it says so in a dialog;
- `gravitone doctor` reports it too.

The fix is always the same:

```sh
gravitone ui --stop && gravitone ui
```

## When the shortcut seems to do nothing

A desktop shortcut runs with no terminal, so anything printed is lost. gravitone
now puts failures on screen instead (kdialog, zenity, xmessage or a Tk dialog,
whichever exists), including "running, but no browser opened — go to
`http://…`". The three things that used to fail silently:

| | |
| --- | --- |
| Port 8765 already taken | moves to the next free port, or says so with `--port` |
| A UI already running | opens the browser at that one; `--stop` to end it |
| Upgraded while running | banner in the page, offer to restart from the terminal |
| `gravitone: command not found` | `~/.local/bin` is not on PATH — see Install, or use the full path |
| Started from the installer, terminal closed | now launched detached, survives it |

Its log, when started from the installer or a shortcut, is
`~/.local/state/gravitone/ui.log`.

## Tests

```sh
python -m pytest
```

Most of it needs nothing but Python. `tests/test_layout.py` drives a real
browser to check the table stays inside its panel and the header sticks —
CSS facts that no amount of Python can see — and skips itself when there is
no chromium to drive.

No test needs an audio device: playback is faked and the clock is injected,
so the suite runs in about a second anywhere.

## Credits

The icons are [Lucide](https://lucide.dev) (ISC), inlined rather than loaded
from a CDN so the page works with no network at all. The Gravitone mark was
drawn for this project. Full notices in [docs/CREDITS.md](docs/CREDITS.md).

## Upgrading from bgst

It used to be called `bgst`. On first run the old `~/.config/bgsoundtrack`
folder is moved to `~/.config/gravitone` — one rename, nothing copied — so
your playlists, tags, cover art and party rosters come with you. The old
`bgst` command is gone; it is `gravitone` now, or `grav`.

## License

MIT — see [LICENSE](LICENSE). Free software, no telemetry, no network calls
beyond the local UI you start yourself.
