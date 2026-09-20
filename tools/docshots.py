"""Redraw the pictures in the README, so they cannot quietly go stale.

    python3 tools/docshots.py            # everything
    python3 tools/docshots.py now demo   # just those

It stands up a real server against a throwaway library of three generated
tracks - with real cover art, because a placeholder note in every slot is
not what the thing looks like - drives it in a real browser, and writes
into docs/. Needs ffmpeg and playwright's chromium; both are what the
tests need anyway.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time as clock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOCS = ROOT / "docs"
# Twice the CSS pixels, which is what the existing pictures are: a 1180px
# window at 2x, so text stays sharp on the sort of screen that reads it.
WINDOW = {"width": 1180, "height": 760}
SCALE = 2

TRACKS = [
    ("01 Hollow Kingdom.mp3", "Hollow Kingdom", "Ori Sallows", 220, ("0x1d2b4f", "0x7fd1ff")),
    ("02 The Long Dark.mp3", "The Long Dark", "Ori Sallows", 196, ("0x3a1f4d", "0xffb37f")),
    ("03 Second Wind.mp3", "Second Wind", "Marta Vey", 243, ("0x14332b", "0x9cf0b0")),
]


def chromium() -> str:
    found = shutil.which("chromium") or shutil.which("google-chrome")
    if found:
        return found
    pinned = sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
    if not pinned:
        raise SystemExit("no chromium to drive - install playwright's browsers")
    return str(pinned[-1])


def make_album(into: Path) -> None:
    """Three tracks with covers, so the pictures show a library, not blanks."""
    for name, title, artist, seconds, (bg, fg) in TRACKS:
        art = into / f"{name}.png"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={bg}:s=600x600",
            "-vf", (f"drawbox=x=150:y=150:w=300:h=300:color={fg}@0.9:t=fill,"
                    f"drawbox=x=210:y=210:w=180:h=180:color={bg}:t=fill,"
                    f"drawbox=x=255:y=255:w=90:h=90:color={fg}@0.9:t=fill"),
            "-frames:v", "1", str(art),
        ], check=True)
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
            "-i", str(art), "-map", "0:a", "-map", "1:v",
            "-c:a", "libmp3lame", "-q:a", "9", "-c:v", "copy",
            "-id3v2_version", "3", "-disposition:v", "attached_pic",
            "-metadata", f"title={title}", "-metadata", f"artist={artist}",
            "-metadata", "album=Deep Field", str(into / name),
        ], check=True)
        art.unlink()


def main(wanted: set) -> int:
    from playwright.sync_api import sync_playwright

    from gravitone import engine, library, player, playlists, webui
    from gravitone.config import Config
    from gravitone.service import Session

    room = Path(tempfile.mkdtemp(prefix="gravitone-docs-"))
    album = room / "music"
    album.mkdir()
    make_album(album)
    weather = room / "weather"
    weather.mkdir()
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=240:c=pink",
        "-c:a", "libmp3lame", "-q:a", "9", "-metadata", "title=Rain on canvas",
        str(weather / "rain.mp3"),
    ], check=True)

    # A path that reads like somebody's machine rather than like a temp dir.
    home = room / "home" / "you"
    home.mkdir(parents=True)
    config = Config(root=str(home / "custom soundtrack"))
    store = playlists.Store()
    current = store.current()
    current.name = "Hollow Kingdom"
    library.init(config, current)
    library.link(config, sorted(album.iterdir()), playlist=current)
    library.add_source(config, weather, section="ambient", playlist=current)
    library.invalidate_cache()
    session = Session(config, room / "config.json", store=store)

    # Hold whatever is playing where it is: these are pictures of a state,
    # not a recording of a run.
    def hold(self, on_event=None):
        while not self.controls.stopping:
            clock.sleep(0.02)

    engine.Engine.run = hold
    player.detect = lambda *a, **k: player.Backend("docs", "/bin/true")
    session.start()
    session.find_covers()
    session.wait_for_covers(20)

    names = sorted(path.name for path in config.music_dir.iterdir())
    session._on_event(engine.Event("track", path=config.music_dir / names[0]))
    session._on_event(engine.Event("track", path=config.music_dir / names[1]))
    session._now.duration = 196.0

    httpd = webui.serve(session, host="127.0.0.1", port=0, open_browser=False)
    url = f"http://127.0.0.1:{httpd.server_port}/#{httpd.token}"
    frames = Path(tempfile.mkdtemp(prefix="gravitone-frames-"))

    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(executable_path=chromium())
            page = browser.new_page(viewport=WINDOW, device_scale_factor=SCALE)
            page.goto(url)
            page.wait_for_selector("#now-elapsed", timeout=20000)
            page.wait_for_timeout(1500)

            if "now" in wanted:
                page.screenshot(path=str(DOCS / "ui-now.png"))
                print("docs/ui-now.png")

            if "settings" in wanted:
                page.click('.rail-btn[data-panel="settings"]')
                page.wait_for_timeout(500)
                page.screenshot(path=str(DOCS / "ui-settings.png"))
                print("docs/ui-settings.png")
                page.click('.rail-btn[data-panel="now"]')
                page.wait_for_timeout(500)

            if "demo" in wanted:
                shot = 0

                def run(count, every=200):
                    nonlocal shot
                    for _ in range(count):
                        page.screenshot(path=str(frames / f"{shot:04d}.png"))
                        shot += 1
                        page.wait_for_timeout(every)

                run(14)                                    # a song playing
                session._on_event(engine.Event(
                    "ambient", path=config.ambient_dir / "rain.mp3", duration=26.0))
                run(10)                                    # the gap after it
                session._on_event(engine.Event(
                    "track", path=config.music_dir / names[2]))
                session._now.duration = 243.0
                run(14)                                    # and the next one
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y", "-framerate", "5",
                    "-i", str(frames / "%04d.png"),
                    "-vf", (f"crop={WINDOW['width'] * SCALE}:{632 * SCALE}:0:0,"
                            "scale=900:-1:flags=lanczos,split[a][b];"
                            "[a]palettegen=max_colors=96[p];"
                            "[b][p]paletteuse=dither=bayer:bayer_scale=3"),
                    "-loop", "0", str(DOCS / "demo.gif"),
                ], check=True)
                print("docs/demo.gif")
            browser.close()
    finally:
        httpd.shutdown()
        session.stop()
        shutil.rmtree(frames, ignore_errors=True)
        shutil.rmtree(room, ignore_errors=True)
    return 0


if __name__ == "__main__":
    every = {"now", "settings", "demo"}
    asked = set(sys.argv[1:]) or every
    unknown = asked - every
    if unknown:
        raise SystemExit(f"nothing called {', '.join(sorted(unknown))} - try: {', '.join(sorted(every))}")
    raise SystemExit(main(asked))
