"""The viewport option against a real tab: responsive layout and scrolling at a small size.

The wheel scroll test is a smoke test: Chrome still scrolls the page for a wheel event outside the viewport, so it does
not pin the wheel point. The offline tests in tests/test_viewport_option.py are the guard for the wheel point and step.
"""

import time


def labels(page):
    return [a["label"] for a in page["actions"] if "node" in a]


def test_a_sidebar_link_that_needs_a_wide_viewport_is_missing_at_the_default(open_page):
    page = open_page("viewport.html").observe(screenshot=False)
    assert (page["w"], page["h"]) == (1120, 780)
    assert "Quarterly reports" not in labels(page) and "Open menu" in labels(page)


def test_a_wide_viewport_shows_the_sidebar_link(open_page):
    page = open_page("viewport.html", viewport=(1470, 920)).observe(screenshot=False)
    assert (page["w"], page["h"]) == (1470, 920)
    assert "Quarterly reports" in labels(page) and "Open menu" not in labels(page)


def test_the_wheel_scrolls_at_a_small_viewport(open_page):
    browser = open_page("viewport.html", viewport=(800, 400))
    page = browser.observe(screenshot=False)
    assert (page["w"], page["h"]) == (800, 400) and page["scroll"]["y"] == 0
    down = next(a for a in page["actions"] if a["id"] == "scroll_down")
    browser.act(down, page)
    # The wheel event is applied by the compositor a moment after dispatch.
    deadline = time.monotonic() + 5
    while browser.evaluate("scrollY") == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert browser.evaluate("scrollY") > 0


def scroll_down_and_wait(browser, page):
    before = page["scroll"]["y"]
    browser.act(next(a for a in page["actions"] if a["id"] == "scroll_down"), page)
    deadline = time.monotonic() + 5
    while browser.evaluate("scrollY") == before and time.monotonic() < deadline:
        time.sleep(0.05)
    return browser.observe(screenshot=False)


def test_two_scroll_downs_leave_no_unseen_band_at_a_short_viewport(open_page):
    browser = open_page("tall_list.html", viewport=(800, 400))
    page = browser.observe(screenshot=False)
    seen = {line for line in page["text"].splitlines()}
    for _ in range(2):
        page = scroll_down_and_wait(browser, page)
        seen |= set(page["text"].splitlines())
    assert {f"Row {i:02d}" for i in range(1, 25)} <= seen
