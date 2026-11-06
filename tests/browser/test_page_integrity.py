"""The tracker must not change what the page sees or does: fetch bodies, cancellation, backpressure, XHR, timers."""

import time

import pytest


def run(browser, expression):
    """Evaluate in the page and await the promise it returns."""
    response = browser.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
    assert not response.get("exceptionDetails"), response["exceptionDetails"]
    return response["result"].get("value")


def until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def inflight(browser):
    return browser.evaluate("window.__jevWait.inflight.size")


@pytest.fixture
def blank(open_page):
    return open_page("blank.html")


# fetch


def test_cancelling_a_reader_reaches_the_server(blank, server_observed):
    run(
        blank,
        "(async () => { const r = await fetch('/endless?id=cancel'); const rd = r.body.getReader();"
        " await rd.read(); await rd.cancel(); return 1; })()",
    )
    assert until(lambda: "cancel" in server_observed["closed"], 3), "the server kept generating after cancel()"
    assert server_observed["sent"]["cancel"] < 100 * 1024


def test_a_slow_reader_does_not_let_the_server_run_ahead(blank, server_observed):
    run(
        blank,
        "(async () => { const r = await fetch('/big?id=slow'); window.rd = r.body.getReader();"
        " await window.rd.read(); return 1; })()",
    )
    time.sleep(1.5)
    ahead = server_observed["sent"]["slow"]
    run(blank, "window.rd.cancel().then(() => 1)")
    # The page reads one chunk and stops. Only what the browser and the sockets buffer may be sent, not 256 MB.
    assert ahead < 64 * 1024 * 1024, ahead


def test_aborting_a_streaming_fetch_ends_it_everywhere(blank, server_observed):
    outcome = run(
        blank,
        "(async () => { window.ac = new AbortController();"
        " const r = await fetch('/endless?id=abort', {signal: window.ac.signal}); const rd = r.body.getReader();"
        " await rd.read(); window.ac.abort(); try { await rd.read(); } catch (e) { return e.name; } })()",
    )
    assert outcome == "AbortError"
    assert until(lambda: "abort" in server_observed["closed"], 3)
    assert until(lambda: inflight(blank) == 0, 3), "an aborted fetch stayed in flight"


def test_aborting_before_the_headers_rejects_and_ends_the_request(blank):
    outcome = run(
        blank,
        "(async () => { const ac = new AbortController(); const p = fetch('/hang', {signal: ac.signal});"
        " ac.abort(); try { await p; } catch (e) { return e.name; } })()",
    )
    assert outcome == "AbortError" and inflight(blank) == 0


def test_json_and_text_bodies_arrive_intact_and_end_the_request(blank):
    assert run(blank, "fetch('/json').then(r => r.json()).then(j => j.a)") == 1
    assert run(blank, "fetch('/api/0').then(r => r.text())") == "ok"
    assert until(lambda: inflight(blank) == 0, 2)


def test_an_unread_small_body_does_not_hang_the_counter(blank):
    run(blank, "fetch('/api/0').then(() => 1)")
    assert until(lambda: inflight(blank) == 0, 3), "a response nobody reads kept the request in flight"


def test_the_response_is_the_native_one(blank):
    value = run(
        blank,
        "(async () => { const r = await fetch('/redirect'); const p = fetch('/api/0');"
        " return [r.redirected, r.url.endsWith('/api/0'), r.status, r instanceof Response,"
        " r.headers.get('content-type'), p instanceof Promise, fetch.name,"
        " Function.prototype.toString.call(fetch).includes('[native code]')]; })()",
    )
    assert value == [True, True, 200, True, "text/plain", True, "fetch", True]


def test_a_streamed_fetch_whose_body_completes_releases_the_counter(open_page):
    stream = open_page("stream.html")
    stream.evaluate("start(); 1")
    result = stream.network_idle(idle_ms=100, timeout_ms=8000)
    assert result.met is True and 2500 <= result.waited_ms <= 4500, result
    # Released by the browser's resource-timing entry, not by the hard cap.
    assert stream.evaluate("window.__jevWait.stats.entry") >= 1


# XMLHttpRequest


def test_xhr_behaviour_is_preserved(blank):
    value = run(
        blank,
        """(async () => {
          const once = (x, send) => new Promise(resolve => {
            const seen = [];
            for (const type of ['load', 'error', 'abort', 'loadend']) x.addEventListener(type, () => {
              seen.push(type); if (type === 'loadend') resolve(seen);
            });
            send();
          });
          const out = {};
          let x = new XMLHttpRequest(); x.open('GET', '/json'); x.responseType = 'json';
          out.json = await once(x, () => x.send()).then(() => x.response.a);
          x = new XMLHttpRequest(); x.open('GET', '/api/0'); x.responseType = 'arraybuffer';
          out.buffer = await once(x, () => x.send()).then(() => x.response.byteLength);
          x = new XMLHttpRequest(); x.open('GET', '/hang');
          out.abort = await once(x, () => { x.send(); x.abort(); });
          x = new XMLHttpRequest(); x.open('GET', 'http://127.0.0.1:1/');
          out.error = await once(x, () => x.send());
          x = new XMLHttpRequest(); x.open('GET', '/api/0', false); x.send();
          out.sync = [x.status, x.responseText, window.__jevWait.inflight.size];
          return out;
        })()""",
    )
    assert value["json"] == 1 and value["buffer"] == 2
    assert value["abort"][-1] == "loadend" and "abort" in value["abort"]
    assert value["error"][-1] == "loadend" and "error" in value["error"]
    assert value["sync"] == [200, "ok", 0]
    assert until(lambda: inflight(blank) == 0, 2)


# setTimeout


def test_timers_behave_like_native_timers(blank):
    value = run(
        blank,
        """(async () => {
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          const out = {id: typeof setTimeout(() => {}, 1)};
          out.self = await new Promise(resolve => setTimeout(function () { resolve(this === window); }, 0));
          out.args = await new Promise(resolve => setTimeout((a, b) => resolve([a, b]), 0, 1, 'two'));
          let fired = false; const id = setTimeout(() => { fired = true; }, 20); clearTimeout(id);
          await sleep(60); out.cleared = !fired;
          window.addEventListener('error', event => { window.__err = event.message; event.preventDefault(); });
          setTimeout(() => { throw new Error('boom'); }, 0); await sleep(60); out.threw = window.__err;
          setTimeout('window.__str = 7', 0); await sleep(60); out.string = window.__str;
          const timer = setTimeout(() => {}, 1500); clearInterval(timer);
          out.dropped = window.__jevWait.timers.size;
          return out;
        })()""",
    )
    assert value["id"] == "number" and value["self"] is True and value["args"] == [1, "two"]
    assert value["cleared"] is True and "boom" in value["threw"] and value["string"] == 7
    assert value["dropped"] == 0
