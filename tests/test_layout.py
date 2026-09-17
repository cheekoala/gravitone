"""Layout regressions, checked in a real browser.

The table walking out of its panel was invisible to every other test here -
it is a CSS fact, so it needs a browser to see it. Skipped wherever one is
not available, which is most places.
"""

import os
import shutil
from pathlib import Path

import pytest

from gravitone import library, playlists, webui
from gravitone.config import Config
from gravitone.service import Session

playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CHROME = os.environ.get("GRAVITONE_TEST_CHROME") or shutil.which("chromium") or shutil.which(
    "google-chrome"
) or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

pytestmark = pytest.mark.skipif(
    not Path(CHROME).exists(), reason="no chromium to drive"
)

LONG_ALBUM = "An Album With An Unreasonably Long Title (Deluxe Anniversary Edition)"


@pytest.fixture
def served(tmp_path):
    from gravitone import tags

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


def test_a_slow_poll_cannot_undo_a_fresh_answer(served):
    """A poll already on the wire when an action lands describes the world
    before it. Painting one of those puts "no party" back on the screen."""
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        try:
            page.goto(served)
            page.click("[data-panel=party]")
            page.wait_for_selector("#party-off:not([hidden])", timeout=15000)
            # Hold every state poll on the wire for three seconds, so the one
            # in flight is guaranteed to land after the party has started.
            page.evaluate(
                """() => {
                    const real = window.fetch;
                    window.fetch = (url, opts) => real(url, opts).then(async (res) => {
                        if (String(url).indexOf('/api/state') !== -1) {
                            await new Promise((go) => setTimeout(go, 3000));
                        }
                        return res;
                    });
                    window.__swaps = [];
                    const watch = (id) => new MutationObserver(() => {
                        window.__swaps.push(id + (document.getElementById(id).hidden
                            ? ':hidden' : ':shown'));
                    }).observe(document.getElementById(id),
                        { attributes: true, attributeFilter: ['hidden'] });
                    watch('party-off');
                    watch('party-on');
                }"""
            )
            # Wait past a tick of the one-second poll, so a request carrying
            # the pre-party world is certainly on the wire when we click.
            page.wait_for_timeout(1200)
            page.click("#party-host")
            page.wait_for_selector("#party-on:not([hidden])", timeout=20000)
            page.wait_for_timeout(4500)     # the stale answers land in here
            swaps = page.evaluate("window.__swaps")
            assert swaps.count("party-on:shown") == 1, swaps
            assert swaps.count("party-off:shown") == 0, swaps
        finally:
            browser.close()


def test_the_coming_up_list_is_not_rebuilt_every_second(served):
    """It is polled once a second and rarely changes; replacing the rows
    each time is what makes the card twitch."""
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        try:
            page.goto(served)
            page.click("[data-panel=party]")
            page.click("#party-host")
            page.wait_for_selector("#party-on:not([hidden])", timeout=20000)
            page.wait_for_selector("#party-next li", timeout=20000)
            page.evaluate(
                """() => {
                    window.__rebuilds = 0;
                    new MutationObserver((records) => {
                        window.__rebuilds += records.length;
                    }).observe(document.getElementById('party-next'),
                               { childList: true });
                }"""
            )
            page.wait_for_timeout(4500)      # four polls or so
            assert page.evaluate("window.__rebuilds") == 0
        finally:
            browser.close()


def test_sitting_a_track_out_says_so_until_the_party_moves_on(served):
    """Skip in a party stops the sound without moving your place. With
    nothing said, it looks like the button did nothing at all."""
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        try:
            page.goto(served)
            page.click("[data-panel=party]")
            page.click("#party-host")
            page.wait_for_selector("#party-on:not([hidden])", timeout=20000)

            page.click("[data-panel=now]")
            page.evaluate("document.getElementById('skip').disabled = false")
            page.click("#skip")
            assert page.is_visible("#toast")
            assert "Sitting this one out" in page.inner_text("#toast")
            # The kicker is upper-cased by the stylesheet.
            assert page.inner_text("#now-kind").lower() == "sitting this one out"

            # Still there after several polls - a timer must not take it away.
            page.wait_for_timeout(5000)
            assert page.is_visible("#toast")
            assert "stay" in page.get_attribute("#toast", "class")

            # And it can be waved away by hand.
            page.click("#toast")
            assert page.is_hidden("#toast")
        finally:
            browser.close()


def test_the_sit_out_clears_when_the_party_moves_on(served):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        try:
            page.goto(served)
            page.click("[data-panel=party]")
            page.click("#party-host")
            page.wait_for_selector("#party-on:not([hidden])", timeout=20000)
            page.click("[data-panel=now]")
            page.evaluate("document.getElementById('skip').disabled = false")
            page.click("#skip")
            assert page.is_visible("#toast")
            # Leaving the party is the party moving on as far as this page is
            # concerned: there is no item to sit out any more.
            page.click("[data-panel=party]")
            page.click("#party-leave")
            page.wait_for_selector("#party-off:not([hidden])", timeout=20000)
            # The sticky note is gone (what replaces it is the ordinary
            # "left the party" toast, which times out by itself).
            assert "stay" not in (page.get_attribute("#toast", "class") or "")
            assert "sitting" not in page.inner_text("#toast").lower()
            assert page.inner_text("#now-kind").lower() != "sitting this one out"
        finally:
            browser.close()


def test_the_button_says_what_pressing_it_does(served):
    """Pressing the word "Live" to stop things read as a state, not a verb."""
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        try:
            page.goto(served)
            page.wait_for_selector("#transport", timeout=15000)
            assert page.inner_text("#transport").strip() == "Play"
            assert page.is_hidden("#live-badge")

            # Tell the page the server is playing, and let its own rendering
            # decide what the button and the badge say.
            page.evaluate(
                """() => {
                    const real = window.fetch;
                    window.fetch = (url, opts) => real(url, opts).then(async (res) => {
                        if (String(url).indexOf('/api/state') === -1) return res;
                        const data = await res.json();
                        data.running = true;
                        return new Response(JSON.stringify(data), {
                            status: 200,
                            headers: { 'Content-Type': 'application/json' },
                        });
                    });
                }"""
            )
            page.wait_for_function(
                "() => document.getElementById('transport-label').textContent === 'Stop'",
                timeout=15000,
            )
            assert page.is_visible("#live-badge")
            assert "Live" in page.inner_text("#live-badge")
            assert "stop" in page.get_attribute("#transport", "title").lower()
        finally:
            browser.close()


def test_the_remove_button_does_not_say_ban(served):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        try:
            page.goto(served)
            page.wait_for_selector("#ban", timeout=15000)
            assert page.inner_text("#ban").strip() == "Remove"
            assert "ban" not in (page.get_attribute("#ban", "title") or "").lower()
        finally:
            browser.close()


@pytest.mark.parametrize(
    "width, cover, thumb",
    [(1400, 288, 68), (900, 288, 68), (760, 148, 34), (400, 96, 34)],
)
def test_the_artwork_grows_where_there_is_room(served, width, cover, thumb):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": width, "height": 900})
        try:
            page.goto(served)
            page.wait_for_selector("#now-cover", timeout=15000)
            measured = page.evaluate(
                """() => Math.round(document.querySelector(
                    '#panel-now .cover, #panel-now .cover-holder'
                ).getBoundingClientRect().width)"""
            )
            assert measured == cover
            page.click("[data-panel=music]")
            page.wait_for_selector("#music-list tr", timeout=15000)
            cell = page.evaluate(
                """() => Math.round(document.querySelector(
                    '#music-list .col-cover').getBoundingClientRect().width)"""
            )
            assert cell >= thumb
            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            )
        finally:
            browser.close()


@pytest.fixture
def served_tree(tmp_path):
    """A server plus a folder tree worth exploring."""
    tree = tmp_path / "Game Soundtracks"
    (tree / "Morrowind" / "Disc 1").mkdir(parents=True)
    (tree / "Oblivion").mkdir()
    for index in range(4):
        (tree / "Morrowind" / "Disc 1" / f"{index:02d} song.flac").write_bytes(b"\0")
        (tree / "Oblivion" / f"{index:02d} song.flac").write_bytes(b"\0")
    (tree / "loose.flac").write_bytes(b"\0")

    config = Config(root=str(tmp_path / "custom soundtrack"))
    store = playlists.Store()
    library.init(config, store.current())
    session = Session(config, tmp_path / "config.json", store=store)
    httpd = webui.serve(session, host="127.0.0.1", port=0, open_browser=False)
    yield f"http://127.0.0.1:{httpd.server_port}/#{httpd.token}", tree
    session.stop()
    httpd.shutdown()


def open_browser_at(page, url, where):
    page.goto(url)
    page.click("[data-panel=add]")
    page.fill("#path-input", str(where))
    page.press("#path-input", "Enter")
    page.wait_for_selector(".browser li.dir", timeout=15000)


def test_a_folder_opens_where_it_stands(served_tree):
    """Cascading downward: you can see inside without leaving where you are."""
    url, tree = served_tree
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        try:
            open_browser_at(page, url, tree)
            before = page.inner_text("#crumb")
            page.click(".browser li.dir:has-text('Morrowind') .twisty")
            page.wait_for_selector(".browser li.branch-of", timeout=15000)

            # The child is there, indented, and we have not gone anywhere.
            child = page.locator(".browser li.dir:has-text('Disc 1')")
            assert child.count() == 1
            assert page.inner_text("#crumb") == before
            depth = page.evaluate(
                """() => document.querySelector(".browser li.dir:nth-of-type(3)")
                        .style.getPropertyValue('--depth')"""
            )
            assert depth == "1"
            indent = page.evaluate(
                """() => {
                    const rows = [...document.querySelectorAll('#browser li')];
                    const parent = rows.find((r) => r.innerText.includes('Morrowind'));
                    const child = rows.find((r) => r.innerText.includes('Disc 1'));
                    return child.getBoundingClientRect().left
                         - parent.getBoundingClientRect().left;
                }"""
            )
            assert indent > 10, "a branch should visibly step right"

            # And it folds away again.
            page.click(".browser li.dir:has-text('Morrowind') .twisty")
            page.wait_for_timeout(200)
            assert page.locator(".browser li.dir:has-text('Disc 1')").count() == 0
        finally:
            browser.close()


def test_back_and_up_are_different_journeys(served_tree):
    """Up goes to the parent; Back goes where you actually were."""
    url, tree = served_tree
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        try:
            page.goto(url)
            page.click("[data-panel=add]")
            page.wait_for_selector("#browse-back", timeout=15000)
            assert page.is_disabled("#browse-back")      # nowhere to go back to yet

            page.fill("#path-input", str(tree))
            page.press("#path-input", "Enter")
            page.wait_for_selector(".browser li.dir", timeout=15000)
            page.click(".browser li.dir:has-text('Oblivion') .t-body")
            page.wait_for_function(
                "() => document.getElementById('crumb').innerText.includes('Oblivion')",
                timeout=15000,
            )
            assert not page.is_disabled("#browse-back")

            page.click("#browse-up")                     # to the parent
            page.wait_for_function(
                "() => !document.getElementById('crumb').innerText.includes('Oblivion')",
                timeout=15000,
            )
            page.click("#browse-back")                   # back to Oblivion
            page.wait_for_function(
                "() => document.getElementById('crumb').innerText.includes('Oblivion')",
                timeout=15000,
            )
        finally:
            browser.close()


def test_the_path_is_offered_a_step_at_a_time(served_tree):
    url, tree = served_tree
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        try:
            open_browser_at(page, url, tree)
            steps = page.locator("#crumb .crumb-step")
            assert steps.count() >= 3
            assert "here" in (steps.last.get_attribute("class") or "")
            # The root is a slash already; it must not read as "//".
            assert "//" not in page.inner_text("#crumb")

            # Clicking a step goes there.
            page.locator("#crumb .crumb-step").nth(steps.count() - 2).click()
            page.wait_for_function(
                "(name) => !document.getElementById('crumb').innerText.endsWith(name)",
                arg="Game Soundtracks",
                timeout=15000,
            )
        finally:
            browser.close()
