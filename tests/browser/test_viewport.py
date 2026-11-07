"""The viewport option against a real tab: responsive layout and the wheel at a small size."""

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
