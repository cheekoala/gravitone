# Changelog

## 1.1.0

Defaults and small edges, from watching somebody else use it.

- **Gaps are 3–7 minutes of silence out of the box**, where they were
  12–45 seconds of mostly ambience. Music behind a game should arrive as a
  thing that happens, not a thing that is on. Existing settings are left
  alone; this is what a fresh install starts with.
- **Ambience is a switch**, in Settings, with the chance slider and the
  random-start option folded away behind it. A slider at 0 % meant the same
  thing, but off is not a number anybody wants to set.
- **The ambience percentage has left the Now panel.** It was a read-out of a
  setting, sat next to nothing you could do about it. The gap card stays and
  now says what the quiet actually is.
- **New playlist…** is the last entry in the playlist dropdown, and takes you
  to where one gets named.
- **Buttons hold their size while they work.** Skip and Remove swapped their
  icon for a differently shaped spinner, so they shrank on the click and grew
  back on the answer — two jumps under the cursor for one press.
- `make docs` redraws the README's pictures (`tools/docshots.py`), so they
  cannot quietly drift from the thing they show.
- A gap that crosses the other one now says which of the two to move.

## 1.0.0 — first release

The first version worth handing to somebody else. It was called `bgst` while
it was being built; on the first run it moves `~/.config/bgsoundtrack` to
`~/.config/gravitone` with a single rename, so nothing you built up is lost.

### What it does

- **Plays your own music behind a game**, shuffled, with a random gap after
  each song that is either silence or a bed of ambience — wind, rain, tavern
  noise. 12–45 seconds by default, tunable from nothing to a full hour, and
  hideable if you would rather not know how long the quiet lasts.
- **Two ways to add music, mixed freely.** Link a file and a symlink lands in
  your library folder pointing at where it already lives; or add a folder and
  it plays in place, picking up whatever you drop in later. Nothing is ever
  copied.
- **Playlists**, each with its own links, folders and removals. Switching one
  in lands on the next track.
- **A control panel in your browser** — no Electron, no build step, no
  dependencies, nothing loaded from the internet. Runs on your phone as a
  remote for the machine that is playing.
- **Album art from the files themselves**, never guessed from a `cover.jpg`
  lying in the folder.
- **Listening together, offline.** A 32-character code and a shared clock put
  two machines on the same evening, with nothing running between them: no
  server, no connection, nothing to keep alive. Join late and you land in the
  middle of the track everyone else is in the middle of.
- **Export and import** — a small manifest, or a bundle with the audio in it,
  which can convert to MP3 or Opus on the way so 400 FLACs is a file somebody
  can actually accept.
- **Fades**, so nothing starts or stops with a click.

### Needs

Python 3.9 or newer, and one of ffmpeg, mpv or VLC for playback. The
installer offers to fetch a player if you have none. `ffprobe` (part of
ffmpeg) reads tags, lengths and cover art.

### Known limits

- **macOS**: `afplay` cannot seek, so joining a party late starts the current
  track from the top instead of part-way in. Installing ffmpeg fixes it.
- **Windows**: symlinks need Developer Mode; without it Gravitone uses hard
  links, which cost no extra space but cannot cross drives. Adding a folder
  whole needs neither.
- Live volume while a track plays needs PulseAudio or PipeWire. Elsewhere the
  volume applies from the next track.
