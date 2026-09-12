# custom_bg_game_soundtrack_player

Play a custom list of music files, with a customizable random silence / ambient
track duration in between.

Point it at music you already have, and it plays a shuffled soundtrack behind
whatever game you're in — with a random gap after each song that is either
silence or a bed of ambience (wind, rain, tavern noise, crickets).

- **Playlists.** Two folders you switch between — one per game, per mood, per
  session. Each keeps its own links, its own source folders, and remembers
  which tracks you took out of it. Switching lands on the next track.
- **Two ways in, mixed freely.** *Link* a file and a symlink lands in your
  `custom soundtrack` folder, pointing at where it already lives — a 40 GB
  collection costs a few KB of directory entries. Or set a whole folder as a
  **source** and it plays in place, picking up whatever you drop in later.
  Nothing is ever copied.
- **A separate `ambient` folder and ambient sources** for loops and atmosphere.
- **Random gaps with sane defaults** — 12–45 s, 65 % of them ambient, the rest
  silence — all tunable.
- **A tiny control panel** (`bgst ui`) that runs in your browser, or on your
  phone as a remote — no Electron, no build step, no dependencies.
- **No Python dependencies.** Playback goes through `ffplay`, `mpv`, `afplay`
  or `vlc`, whichever you have.

![The bgst control panel, playing a track](docs/ui-now.png)

## Install

One script per platform. Each installs into a private virtualenv (or pipx if
you have it), puts `bgst` on your PATH, and offers to install an audio player
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

Uninstall with `./install.sh --uninstall` / `.\install.ps1 -Uninstall`; both
leave your library and config alone. `make help` lists the same tasks for
developers.

You also need one player: `ffmpeg` (for `ffplay`), `mpv`, or `vlc` — the
installers offer to fetch one. Check any time with `bgst doctor`.

## Use

```sh
bgst ui                                    # the control panel, in your browser
```

`bgst ui` starts the server *and* opens the page. Opening
`bgsoundtrack/ui/index.html` from the folder by hand gives you a dead page —
it has no server to talk to, so no library, no file browser, no playback. The
page says so if you land there.

Or from the terminal:

```sh
bgst init                                    # create the custom soundtrack folder
bgst playlist new "Hollow Kingdom" --source ~/Music/Nier --use
bgst playlist new "Field Work" --source ~/Sounds/recordings
bgst source add --ambient ~/Sounds/weather   # into the selected playlist
bgst link ~/Music/Outer\ Wilds               # or link track by track
bgst play
```

While playing in a terminal: `n` skips to the next track, `q` quits.

```
♪ Ashes.flac
  ~ rain.ogg (0:34)
♪ Village.mp3
  . silence (0:19)
```

## The UI

`bgst ui` serves a small control panel on `127.0.0.1:8765` and opens it. It is
plain HTML, CSS and JavaScript served by Python's own HTTP server — no
Electron, no build step, no dependencies, nothing loaded from the internet.

| | |
| --- | --- |
| ![The music library, mixing linked tracks with one from a source folder](docs/ui-library.png) | ![Settings, including source folders](docs/ui-settings.png) |
| ![Adding files: link them, or play a folder in place](docs/ui-add.png) | |

- **Top bar** — the playlist selector; switching it switches what plays.
- **Now** — what is playing or how long the current gap runs, with history.
- **Music / Ambient** — the two libraries; `✕` removes a link, never a file.
- **Add** — *Choose a folder…* opens your desktop's own folder dialog (needs
  Tk; `bgst doctor` says whether you have it). Otherwise browse from the
  built-in file browser or paste a path — a web file picker hands over file
  *contents*, never paths, and paths are what linking needs. Every folder row
  offers both **Link all** (symlinks each file) and **Source** (play the
  folder in place).
- **Config** — gaps, levels, shuffle, loop, your playlists (rename, delete,
  create) and this playlist's source folders and removed tracks. Changes save
  immediately and take effect from the next gap; no need to restart playback.

Keys: `space` play/stop, `n` next. Every API call needs the token in the URL,
so another page in your browser cannot drive your player or read your disk.

Run it as a phone remote for the machine that's playing:

```sh
bgst ui --host 0.0.0.0        # prints a LAN URL with the token
```

Anyone who has that link can control playback, so use it on networks you
trust.

## Playlists

Already have two folders you think of as two playlists? Make them two:

```sh
bgst playlist new "Hollow Kingdom" --source ~/Music/hollow-kingdom --use
bgst playlist new "Night Drive"    --source ~/Music/night-drive
bgst playlist list
bgst playlist use "Night Drive"
```

Or pick them from the selector in the top bar of the UI. A playlist owns:

- its **source folders**, played in place;
- its own **links**, in `custom soundtrack/playlists/<id>/`;
- its **removals** — take a track out with `✕` (or `bgst unlink NAME`) and it
  stays out of *this* playlist, remembered across restarts. The same file
  keeps playing in any other playlist that points at it, and the file itself
  is never touched. Put it back from Config → *Removed from this playlist*,
  or `bgst playlist restore --all`.

Removing a **linked** track deletes that playlist's link instead — there is
nothing to remember, and the other playlists keep theirs.

Switching playlist while music is playing takes effect at the next track, not
the next full pass — as does linking, removing, or adding a source.

The playlist called **Library** is the plain `custom soundtrack/music` and
`/ambient` folders, so an install from before playlists keeps working exactly
as it did, source folders and all.

Playlists live in `~/.config/bgsoundtrack/playlists.json` — plain JSON, easy
to read, back up, or edit by hand.

## The library

```
custom soundtrack/
├── music/      symlinks -> your songs, wherever they live
└── ambient/    symlinks -> your ambience

plus any source folders, played where they stand
```

**Linked** files are curated one by one and stay put even if you reorganise
the original folder later. **Source folders** are the low-effort option: point
at `~/Music/Soundtracks`, and every audio file under it plays, including
whatever you add next week. A file reachable both ways is only played once.

```sh
bgst source add ~/Music/Soundtracks      # music source
bgst source add --ambient ~/Sounds       # ambience source
bgst source list                         # with per-folder track counts
bgst source remove ~/Music/Soundtracks   # the folder itself is untouched
bgst ui --pick                           # pick a music source from a dialog
```

Because entries are plain symlinks, you can also manage the folder by hand —
drag links in with your file manager, rename them to change sort order, delete
one to drop a track. The player ignores non-audio files and dangling links.

| Command | |
| --- | --- |
| `bgst ui` | open the control panel (`--host 0.0.0.0` for a phone remote) |
| `bgst play` | play in the terminal (`n` next, `q` quit) |
| `bgst link PATH...` | symlink files/folders in (`--ambient`, `--no-recursive`, `--relative`) |
| `bgst source add\|remove\|list` | play whole folders in place (`--ambient`) |
| `bgst playlist list\|new\|use\|rename\|remove` | switch between sets of music |
| `bgst playlist removed\|restore` | see and undo removals in this playlist |
| `bgst unlink NAME...` | remove entries (only ever deletes symlinks, never real files) |
| `bgst list --targets` | show the library and what each link points at |
| `bgst prune` | drop links whose target moved or was deleted |
| `bgst doctor` | check folders, tracks and available players |

Any command takes `--playlist NAME` to act on a playlist without selecting it.

`--relative` writes relative symlinks, which keep working if the library and
your music move together (e.g. both on one external drive).

On Windows, symlinks need Developer Mode (Settings → System → For developers).
Without it `bgst` falls back to hard links, which also cost no extra space but
cannot cross drives.

## Gaps between songs

Every song is followed by a gap of a random length between `gap_min` and
`gap_max`. Each gap independently becomes either ambience (probability
`ambient_chance`, a random track from `ambient/`, cut to the gap length) or
plain silence. Gaps shorter than `ambient_min_tail` stay silent — a two-second
smear of rain sounds like a mistake.

| Setting | Default | |
| --- | --- | --- |
| `gap_min` / `gap_max` | `12` / `45` | gap length range, seconds |
| `ambient_chance` | `0.65` | share of gaps that get ambience |
| `ambient_min_tail` | `3.0` | shortest gap worth filling with ambience |
| `volume` / `ambient_volume` | `70` / `45` | ambience sits under the music |
| `shuffle` | `true` | shuffled passes; no repeat until all have played |
| `loop` | `true` | start a new pass when the list is exhausted |
| `root` | `~/.local/share/custom soundtrack` | library location |

Change them for good, or just for one session:

```sh
bgst config gap_min=30 gap_max=120 ambient_chance=0.8   # saved
bgst config                                             # show everything
bgst play --gap-min 60 --gap-max 180 --no-ambient       # this run only
```

Other `play` flags: `--volume`, `--ambient-volume`, `--no-shuffle`, `--no-loop`,
`--player ffplay|mpv|afplay|vlc`, `--seed N` (reproducible shuffle and gaps).

Settings live in `~/.config/bgsoundtrack/config.json`. `BGSOUNDTRACK_ROOT` and
`BGSOUNDTRACK_CONFIG` override the paths, and `--root` / `--config` override
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

## Tests

```sh
python -m pytest
```

No test needs an audio device: playback is faked and the clock is injected,
so the suite runs in about a second anywhere.

## License

MIT — see [LICENSE](LICENSE). Free software, no telemetry, no network calls
beyond the local UI you start yourself.
