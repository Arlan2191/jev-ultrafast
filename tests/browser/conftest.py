"""Real-browser fixtures: a Chrome reached over BU_CDP_URL and local pages from tests/fixtures/.

Run these tests with `pytest -m browser`; the default run deselects them. Without BU_CDP_URL they skip,
unless JEV_REQUIRE_BROWSER=1 is set (as in CI), which turns the skip into a failure.
"""

import functools
import http.server
import os
import threading
from pathlib import Path

import pytest

from jev_ultrafast.browser import Browser

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def pytest_collection_modifyitems(items):
    for item in items:
        if Path(__file__).parent in item.path.parents:
            item.add_marker(pytest.mark.browser)


@pytest.fixture(scope="session")
def fixture_base_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *args, **kwargs: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def open_page(fixture_base_url):
    """Open a page from tests/fixtures/ in a fresh tab; every tab is closed at teardown."""
    if not os.environ.get("BU_CDP_URL"):
        message = "BU_CDP_URL is not set; start Chrome with --remote-debugging-port and export BU_CDP_URL"
        if os.environ.get("JEV_REQUIRE_BROWSER") == "1":
            pytest.fail(message)
        pytest.skip(message)
    opened = []

    def open_(name):
        browser = Browser(f"{fixture_base_url}/{name}")
        opened.append(browser)
        return browser

    yield open_
    for browser in opened:
        browser.close()
