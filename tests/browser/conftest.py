"""Real-browser fixtures: a Chrome reached over BU_CDP_URL and local pages from tests/fixtures/.

Run these tests with `pytest -m browser`; the default run deselects them. Without BU_CDP_URL they skip,
unless JEV_REQUIRE_BROWSER=1 is set (as in CI), which turns the skip into a failure.
"""

import functools
import http.server
import os
import re
import threading
import time
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
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            pass

        def stream(self, chunks, pause, content_type="text/plain"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for chunk in chunks:
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
                    time.sleep(pause)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            # /stream: six chunks, half a second apart. /events: a server-sent event every 200 ms, until the client
            # leaves. /api/<ms>: a short answer after <ms>. /hang: a long poll that stays open for 20 s.
            route = self.path.split("?")[0]
            if route == "/stream":
                return self.stream([f"chunk {i} " for i in range(1, 7)], 0.5)
            if route == "/events":
                return self.stream((f"data: tick {i}\n\n" for i in range(100)), 0.2, "text/event-stream")
            if route == "/hang":
                return self.stream([" "], 20)
            api = re.match(r"/api/(\d+)", self.path)
            if api:
                time.sleep(int(api[1]) / 1000)
                return self.stream(["ok"], 0)
            # /delay/<ms>/<path> answers <path> after <ms>: a form or link whose server is slow.
            match = re.match(r"/delay/(\d+)(/.*)", self.path)
            if match:
                time.sleep(int(match[1]) / 1000)
                self.path = match[2]
            super().do_GET()

    handler = functools.partial(Handler, directory=str(FIXTURES))
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
