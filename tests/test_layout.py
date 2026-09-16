"""Layout regressions, checked in a real browser.

The table walking out of its panel was invisible to every other test here -
it is a CSS fact, so it needs a browser to see it. Skipped wherever one is
not available, which is most places.
"""

import os
import shutil
from pathlib import Path

import pytest

from bgsoundtrack import library, playlists, webui
from bgsoundtrack.config import Config
from bgsoundtrack.service import Session

playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CHROME = os.environ.get("BGST_TEST_CHROME") or shutil.which("chromium") or shutil.which(
    "google-chrome"
) or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

pytestmark = pytest.mark.skipif(
    not Path(CHROME).exists(), reason="no chromium to drive"
)

LONG_ALBUM = "An Album With An Unreasonably Long Title (Deluxe Anniversary Edition)"


@pytest.fixture
def served(tmp_path):
    from bgsoundtrack import tags

    album = tmp_path / "music" / "Artist" / "Album"
    album.mkdir(parents=True)
    for index in range(12):
        (album / f"{index:02d} Track.mp3").write_bytes(b"\0")

    config = Config(root=str(tmp_path / "custom soundtrack"))
    store = playlists.Store()
    library.init(config, store.current())
    library.add_source(config, album, playlist=store.current())
    session = Session(config, tmp_path / "config.json", store=store)
    for path in album.iterdir():
        session._tags.store(
            path,
            tags.Tags(
                title=f"A Song Whose Title Also Refuses To Stop ({path.stem})",
                artist="Very Long Artist Name That Goes On And On",
                album=LONG_ALBUM,
                duration=61.0,
            ),
        )
    httpd = webui.serve(session, host="127.0.0.1", port=0, open_browser=False)
    yield f"http://127.0.0.1:{httpd.server_port}/#{httpd.token}"
    session.stop()
    httpd.shutdown()


@pytest.mark.parametrize("width", [1600, 1100, 940, 700, 390])
def test_the_table_stays_inside_its_panel(served, width):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": width, "height": 700})
        try:
            page.goto(served)
            page.click("[data-panel=music]")
            page.wait_for_selector("#music-list tr", timeout=15000)
            measured = page.evaluate(
                """() => {
                  const table = document.querySelector("#music-table");
                  const wrap = table.closest(".table-wrap");
                  return {
                    spill: table.getBoundingClientRect().right
                         - wrap.getBoundingClientRect().right,
                    sideways: document.body.scrollWidth > window.innerWidth,
                    clipped: [...document.querySelectorAll("td.col-album")]
                      .every(cell => cell.scrollWidth >= cell.clientWidth),
                  };
                }"""
            )
        finally:
            browser.close()

    assert measured["spill"] <= 1, f"the table hangs {measured['spill']:.0f}px outside its panel"
    assert measured["sideways"] is False, "the page scrolls sideways"
    assert measured["clipped"] is True


def test_the_header_sticks_while_scrolling(served):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1100, "height": 400})
        try:
            page.goto(served)
            page.click("[data-panel=music]")
            page.wait_for_selector("#music-list tr", timeout=15000)
            before = page.evaluate(
                "document.querySelector('#music-table th.col-title').getBoundingClientRect().top"
            )
            page.eval_on_selector(".stage", "el => el.scrollTop = 600")
            page.wait_for_timeout(300)
            after = page.evaluate(
                "document.querySelector('#music-table th.col-title').getBoundingClientRect().top"
            )
        finally:
            browser.close()

    assert after < before, "the header did not move up with the scroll"
    assert 0 <= after <= 120, f"the header scrolled away (top={after:.0f})"


def test_the_view_stays_put_while_the_table_updates(served):
    """Covers and tags arrive while you are reading; the list must not jump."""
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1100, "height": 420})
        try:
            page.goto(served)
            page.click("[data-panel=music]")
            page.wait_for_selector("#music-list tr", timeout=15000)
            page.wait_for_timeout(400)
            page.evaluate("document.querySelector('.stage').scrollTop = 220")
            page.wait_for_timeout(300)
            start = page.evaluate("document.querySelector('.stage').scrollTop")
            # several poll cycles, each of which re-renders the table
            page.wait_for_timeout(3500)
            settled = page.evaluate("document.querySelector('.stage').scrollTop")

            page.click("[data-panel=now]")
            page.wait_for_timeout(400)
            page.click("[data-panel=music]")
            page.wait_for_timeout(800)
            returned = page.evaluate("document.querySelector('.stage').scrollTop")
        finally:
            browser.close()

    assert start > 0, "the list should have been scrollable"
    assert abs(settled - start) <= 2, f"the view drifted from {start} to {settled}"
    assert abs(returned - start) <= 2, f"coming back landed at {returned}, not {start}"


def test_the_table_header_is_filled_to_its_rounded_corners(served):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1100, "height": 600})
        try:
            page.goto(served)
            page.click("[data-panel=music]")
            page.wait_for_selector("#music-list tr", timeout=15000)
            corners = page.evaluate(
                """() => {
                  const wrap = document.querySelector(".table-wrap");
                  const th = document.querySelector("#music-table thead th:first-child");
                  const last = document.querySelector("#music-table thead th:last-child");
                  const px = (value) => parseFloat(value) || 0;
                  return {
                    wrap: px(getComputedStyle(wrap).borderTopLeftRadius),
                    left: px(getComputedStyle(th).borderTopLeftRadius),
                    right: px(getComputedStyle(last).borderTopRightRadius),
                  };
                }"""
            )
        finally:
            browser.close()

    assert corners["left"] >= corners["wrap"] - 1, "the header's fill squares off the panel corner"
    assert corners["right"] >= corners["wrap"] - 1


# -- the party panel -----------------------------------------------------


def test_starting_a_party_shows_a_code_you_can_read_out(served):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        problems = []
        page.on("pageerror", lambda exc: problems.append(str(exc)))
        try:
            page.goto(served)
            page.click("[data-panel=party]")
            assert page.is_visible("#party-off")
            page.click("#party-host")
            page.wait_for_selector("#party-on:not([hidden])", timeout=20000)

            code = page.inner_text("#party-code-out")
            assert len(code.replace("-", "")) == 32
            assert page.inner_text("#party-since").startswith("Started")
            # It says what is on and what is next, without anyone asking.
            assert page.inner_text("#party-now-title").strip()
            assert page.locator("#party-next li").count() >= 1
            # And the rail says a party is on from any panel.
            page.click("[data-panel=now]")
            assert "live-on" in page.get_attribute("[data-panel=party]", "class")

            page.click("[data-panel=party]")
            page.click("#party-leave")
            page.wait_for_selector("#party-off:not([hidden])", timeout=20000)
            assert problems == []
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1400, 900, 390])
def test_the_party_code_never_leaves_its_card(served, width):
    """A 32-character code on a phone is exactly the sort of thing that
    pushes a page sideways."""
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": width, "height": 800})
        try:
            page.goto(served)
            page.click("[data-panel=party]")
            page.click("#party-host")
            page.wait_for_selector("#party-on:not([hidden])", timeout=20000)
            measured = page.evaluate(
                """() => {
                    const code = document.getElementById('party-code-out');
                    const card = document.getElementById('party-on');
                    return {
                      code: code.getBoundingClientRect().right,
                      card: card.getBoundingClientRect().right,
                      sideways: document.documentElement.scrollWidth
                                > document.documentElement.clientWidth,
                    };
                }"""
            )
            assert measured["code"] <= measured["card"] + 1
            assert measured["sideways"] is False
        finally:
            browser.close()
