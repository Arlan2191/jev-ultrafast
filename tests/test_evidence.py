"""Offline contracts for run evidence: trace, screenshots, console and network capture, HAR, report. No browser."""

import base64
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from jev_ultrafast import Agent, Step, Value, trace
from jev_ultrafast import agent as loop
from jev_ultrafast import evidence as evidence_module
from jev_ultrafast.browser import fingerprint
from jev_ultrafast.evidence import CdpEvents, Evidence, build_har, resolve_endpoint
from jev_ultrafast.report import render_report
from jev_ultrafast.steps import secret_forms
from jev_ultrafast.waits import WaitResult

JPEG = base64.b64encode(b"\xff\xd8fake-jpeg\xff\xd9").decode()
SECRET = "hunter2-correct-horse"
LOCATOR = {"role": "button", "name": "Go", "scope": {"role": "dialog", "name": "Sign in"}, "test_id": "go"}


def page(text="Search", value=""):
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": text,
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": value, "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
        "marker": ["m", text, value],
        "page_key": ["k"],
        "guards": {"10": ["g", value], "20": ["g", ""]},
    }
    state["fingerprint"] = fingerprint(state)
    return state


def decision(action="e3", operation="CLICK", value=None):
    return {
        "choice": action,
        "operation": operation,
        "target": "1",
        "confidence": 0.9,
        "probabilities": {action: 0.9, "e1": 0.05},
        "latency_ms": 10,
        "usage": {"input_tokens": 100, "output_tokens": 5},
        "model": "jev-test",
        **({"value": value} if value else {}),
    }


class Fake:
    def __init__(self):
        self.pages = [page()]
        self.browser = Mock()
        self.browser.observe.side_effect = lambda **_kw: self.pages[-1]
        self.browser.fresh.return_value = True
        self.browser.act.return_value = {"executed": "x"}
        self.browser.evaluate.return_value = "https://example.test/"
        self.browser.screenshot.return_value = JPEG
        self.browser.locator.return_value = dict(LOCATOR)
        self.browser.version = {"product": "HeadlessChrome/130", "userAgent": "UA"}
        self.browser.probe.side_effect = lambda kind, timeout_ms=0, **_a: WaitResult(True, 0, f"{kind} reason")


def make(monkeypatch, tmp_path, goals="Find a book", **kwargs):
    fake = Fake()
    monkeypatch.setattr(loop, "Browser", Mock(return_value=fake.browser))
    kwargs.setdefault("evidence_dir", tmp_path / "run")
    agent = Agent("https://example.test/", goals, **kwargs)
    agent.state["started_at"] = time.perf_counter()
    agent.fake = fake
    return agent


def do(agent, action="DONE", operation="DONE", value=None):
    agent.state["decision"] = decision(action, operation, value)
    return agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})


def read(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


# --- the trace file ---------------------------------------------------------------------------------------------


def test_a_run_writes_a_trace_that_reads_back_and_ends_with_totals_that_match_the_state(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path, [Step("click go")], max_steps=5)
    agent.state["decisions"].append(decision())
    do(agent, "e3", "CLICK")
    time.sleep(0.02)
    final = do(agent)
    agent.close()
    events = trace.load_trace(tmp_path / "run" / "trace.jsonl")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end" and kinds.count("run_end") == 1
    assert {"observe", "action", "step"} <= set(kinds)
    start, action, end = events[0], next(e for e in events if e["type"] == "action"), events[-1]
    assert start["trace_schema"] == trace.SCHEMA == 1 and start["browser"]["product"] == "HeadlessChrome/130"
    assert start["steps"][0]["goal"] == "click go" and start["config"]["max_steps"] == 5
    assert action["kind"] == "click" and action["label"] == "Go" and action["locator"] == LOCATOR
    assert action["result"] == {"executed": "x"} and action["index"] == 1
    assert end["status"] == final["status"] == "done" and end["verified"] is False
    assert end["totals"]["actions"] == len(final["history"]) == 1
    assert end["totals"]["jev_calls"] == len(final["decisions"]) and end["totals"]["text_calls"] == 0
    assert end["totals"]["tokens"]["jev"] == {"input_tokens": 100, "output_tokens": 5}
    assert end["totals"]["wall_ms"] == final["elapsed_ms"] > 0
    assert final["history"][0]["locator"] == LOCATOR


def test_a_decision_event_carries_the_top_five_probabilities_with_labels(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    probabilities = {f"p{i}": 1 / (i + 2) for i in range(7)} | {"e3": 0.9}
    chosen = {**decision("e3", "CLICK"), "probabilities": probabilities}
    monkeypatch.setattr(loop, "choose", lambda *_a, **_k: chosen)
    agent.command("predict")
    event = next(e for e in read(tmp_path / "run" / "trace.jsonl") if e["type"] == "decision")
    assert len(event["top"]) == 5 and event["top"][0] == {"id": "e3", "label": "Go", "p": 0.9}
    assert event["model"] == "jev-test" and event["latency_ms"] == 10 and event["usage"]["input_tokens"] == 100


def test_refusals_checks_and_denials_are_events(monkeypatch, tmp_path):
    from jev_ultrafast import expect

    agent = make(monkeypatch, tmp_path, [Step("x", expect=[expect.text("Search")])])
    do(agent)  # DONE with the check met, twice
    agent.close()
    kinds = [e["type"] for e in read(tmp_path / "run" / "trace.jsonl")]
    assert "check" in kinds and kinds[-1] == "run_end"


def test_a_closed_run_still_ends_the_trace(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    agent.close()
    end = trace.load_trace(tmp_path / "run" / "trace.jsonl")[-1]
    assert end["type"] == "run_end" and end["stop_reason"] == "closed"
    assert (tmp_path / "run" / "report.html").exists()


def test_evidence_is_off_by_default_and_history_has_no_locator(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path, evidence_dir=None)
    assert agent.evidence is None
    do(agent, "e3", "CLICK")
    agent.fake.browser.locator.assert_not_called()
    assert "locator" not in agent.state["history"][0]
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("bad", [{"screenshots": "sometimes"}, {"screenshots": 3}, {"locators": "yes"}])
def test_bad_evidence_options_are_rejected(monkeypatch, tmp_path, bad):
    with pytest.raises(ValueError):
        make(monkeypatch, tmp_path, **bad)


def test_a_screenshot_mode_needs_an_evidence_dir(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        make(monkeypatch, tmp_path, evidence_dir=None, screenshots="actions")


def test_an_evidence_dir_that_holds_files_is_refused(monkeypatch, tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "old.txt").write_text("x")
    with pytest.raises(ValueError, match="already holds files"):
        make(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json\n",
        '{"type": "observe", "t_ms": 1}\n',
        '{"type": "run_start", "t_ms": 0, "trace_schema": 2}\n',
    ],
)
def test_the_reader_rejects_what_it_does_not_understand(tmp_path, text):
    path = tmp_path / "trace.jsonl"
    path.write_text(text)
    with pytest.raises(ValueError):
        trace.load_trace(path)


def test_the_validator_names_the_missing_field_and_the_line(tmp_path):
    start = {
        "type": "run_start", "t_ms": 0, "trace_schema": 1, "library_version": "x", "browser": {}, "viewport": {},
        "steps": [], "values": {}, "config": {}, "capture": {},
    }
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps(start) + "\n" + json.dumps({"type": "step", "t_ms": 3}) + "\n")
    with pytest.raises(ValueError, match=r"line 2: step is missing index, status"):
        trace.load_trace(path)
    path.write_text(json.dumps(start) + "\n" + json.dumps({"type": "teleport", "t_ms": 3}) + "\n")
    with pytest.raises(ValueError, match="unknown event type"):
        trace.load_trace(path)
    path.write_text(json.dumps(start) + "\n" + json.dumps({"type": "step", "index": 0, "status": "done"}) + "\n")
    with pytest.raises(ValueError, match="t_ms"):
        trace.load_trace(path)


# --- screenshots ------------------------------------------------------------------------------------------------


def shots(tmp_path):
    return sorted(p.name for p in (tmp_path / "run" / "screenshots").glob("*.jpg"))


def run_two_clicks_then_done(agent):
    for _ in range(2):
        agent.fake.pages.append(page(text="changed " + str(len(agent.fake.pages))))
        do(agent, "e3", "CLICK")
    return do(agent)


@pytest.mark.parametrize(
    "mode, expected",
    [("actions", 4), ("all", 4), ("failure", 0), ("none", 0)],
)
def test_screenshot_modes_on_a_run_that_ends_done(monkeypatch, tmp_path, mode, expected):
    agent = make(monkeypatch, tmp_path, screenshots=mode)
    run_two_clicks_then_done(agent)
    agent.close()
    # initial + after each of two actions + final.
    assert len(shots(tmp_path)) == expected
    events = read(tmp_path / "run" / "trace.jsonl")
    referenced = [e["screenshot"] for e in events if e.get("screenshot")]
    assert sorted(Path(r).name for r in referenced) == shots(tmp_path)
    assert all(not r.startswith("/") for r in referenced)
    assert all((tmp_path / "run" / r).read_bytes().startswith(b"\xff\xd8") for r in referenced)


def test_failure_mode_keeps_only_the_last_page_of_a_run_that_did_not_end_done(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path, screenshots="failure", max_steps=1)
    do(agent, "e3", "CLICK")
    agent.fake.pages.append(page(text="again"))
    agent.state["decision"] = decision("e3", "CLICK")
    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert agent.state["status"] == "budget"
    agent.close()
    assert len(shots(tmp_path)) == 1


def test_all_mode_also_shoots_observations_that_follow_no_action(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path, screenshots="all")
    agent.fake.browser.fresh.return_value = False
    agent.fake.pages.append(page(text="moved"))
    monkeypatch.setattr(loop, "choose", lambda *_a, **_k: decision("e3", "CLICK"))
    agent.command("predict")  # The page was stale, so it is observed again.
    agent.close()
    assert len(shots(tmp_path)) == 3  # first page, the re-observation, the final one


# --- scrub ------------------------------------------------------------------------------------------------------


def test_no_written_file_but_the_screenshots_holds_a_declared_secret(monkeypatch, tmp_path):
    secret = "p@ss w0rd/&<\"x>"
    agent = make(monkeypatch, tmp_path, "type it", values={"pw": Value(secret, secret=True)})
    agent.fake.pages = [page(text=f"Echo {secret}", value=secret)]
    agent.fake.pages.append(page(text=f"Echo {secret}", value=secret))
    agent.fake.browser.locator.return_value = {**LOCATOR, "name": secret, "text": secret}
    stream = CdpEvents("ws://x", "t", connect=lambda *_a, **_k: ScriptedSocket())
    stream.start()
    agent.evidence.stream = stream
    agent.evidence.capture = {"console": True, "network": True, "reason": None}
    for form in secret_forms(secret):
        stream.handle(
            {"sessionId": "S1", "method": "Runtime.consoleAPICalled",
             "params": {"type": "log", "args": [{"type": "string", "value": form}]}}
        )
        stream.handle(
            {"sessionId": "S1", "method": "Network.requestWillBeSent",
             "params": {"requestId": form, "request": {"url": f"https://example.test/?q={form}", "method": "GET"},
                        "type": "Fetch", "timestamp": 1.0, "wallTime": 1.0}}
        )
    do(agent, "e1", "TYPE_TEXT", value="pw")
    do(agent)
    agent.close()
    files = [p for p in (tmp_path / "run").rglob("*") if p.is_file() and p.suffix != ".jpg"]
    assert {p.name for p in files} >= {"trace.jsonl", "console.jsonl", "network.jsonl", "network.har", "report.html"}
    for path in files:
        text = path.read_text()
        for form in secret_forms(secret):
            assert form not in text, (path.name, form)
    for name in ("trace.jsonl", "console.jsonl", "network.jsonl", "network.har", "report.html"):
        assert "secret:pw" in (tmp_path / "run" / name).read_text(), name


def test_the_trace_names_values_and_marks_secrets_without_their_text(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path, values={"term": "cats", "pw": Value(SECRET, secret=True)})
    agent.close()
    start = read(tmp_path / "run" / "trace.jsonl")[0]
    assert start["values"] == {"term": False, "pw": True}
    assert "cats" not in json.dumps(start) and SECRET not in json.dumps(start)


# --- the locator ------------------------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_stable_id_heuristic():
    script = (
        "const a=eval(require('fs').readFileSync(process.argv[1],'utf8'));"
        "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(a.stableId)))"
    )
    ids = {
        "save": True, "email-field": True, "main-nav": True, "item-3": True, "SubmitButton": True,
        "": False, ":r1:": False, "radix-:r2:": False, "«r1»": False, "_r_3_": False, "ember123": False,
        "ember": True, "12345": False, "7": False, "a1b2c3d4e5": False, "550e8400-e29b-41d4-a716-446655440000": False,
        "input-84729": False, "has space": False,
    }
    out = subprocess.run(
        ["node", "-e", script, str(Path(loop.__file__).with_name("a11y.js")), json.dumps(list(ids))],
        capture_output=True, text=True, check=True,
    )
    assert dict(zip(ids, json.loads(out.stdout), strict=True)) == ids


def test_a_locator_is_only_read_for_elements_and_never_for_a_key_or_a_wait(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path, keys=("Control+K",))
    key = {"id": "key:Control+K", "kind": "key", "key": "Control+K", "name": "Control+K", "label": "Press Control+K"}
    agent.fake.pages = [{**page(), "actions": [*page()["actions"], key]}]
    agent.fake.pages[0]["fingerprint"] = fingerprint(agent.fake.pages[0])
    agent.state["page"] = agent.fake.pages[0]
    do(agent, "wait", "WAIT")
    do(agent, "key:Control+K", "PRESS_KEY")
    assert [h["locator"] for h in agent.state["history"]] == [None, None]
    agent.fake.browser.locator.assert_not_called()


# --- HAR, report ------------------------------------------------------------------------------------------------


def record(**kwargs):
    base = {
        "method": "GET", "url": "https://example.test/a", "resource_type": "Fetch", "status": 200, "mime": "text/plain",
        "start_ms": 10, "ttfb_ms": 4.0, "duration_ms": 9.0, "encoded_size": 120, "failure": None, "from_cache": False,
        "started_at": "2026-01-01T00:00:00.000+00:00",
    }
    return {**base, **kwargs}


def test_the_har_has_the_1_2_shape_and_keeps_failures(monkeypatch):
    har = build_har([record(), record(status=None, failure="net::ERR_FAILED", ttfb_ms=None, duration_ms=3.0,
                                      encoded_size=None, url="https://example.test/b")], "9.9")
    log = har["log"]
    assert log["version"] == "1.2" and log["creator"] == {"name": "jev-ultrafast", "version": "9.9"}
    ok, failed = log["entries"]
    for entry in (ok, failed):
        assert {"startedDateTime", "time", "request", "response", "cache", "timings"} <= set(entry)
        assert {"method", "url", "httpVersion", "cookies", "headers", "queryString", "headersSize", "bodySize"} <= set(
            entry["request"])
        assert {"status", "statusText", "httpVersion", "cookies", "headers", "content", "redirectURL", "headersSize",
                "bodySize"} <= set(entry["response"])
        assert entry["time"] == sum(v for v in entry["timings"].values() if v >= 0)
    assert ok["response"]["status"] == 200 and ok["timings"] == {"send": 0, "wait": 4.0, "receive": 5.0}
    assert failed["response"]["status"] == 0 and failed["_error"] == "net::ERR_FAILED"
    json.dumps(har)


def test_the_report_is_self_contained_and_escapes_what_the_page_said(tmp_path):
    (tmp_path / "screenshots").mkdir()
    (tmp_path / "screenshots" / "0001.jpg").write_bytes(b"\xff\xd8x\xff\xd9")
    events = [
        {"type": "run_start", "t_ms": 0, "steps": [{"goal": "Go <b>now</b>", "checks": ["text contains \"ok\""]}],
         "config": {"max_steps": 3}},
        {"type": "observe", "t_ms": 1, "url": "https://example.test/", "elements": 2,
         "screenshot": "screenshots/0001.jpg"},
        {"type": "action", "t_ms": 2, "index": 1, "label": "<script>alert(1)</script>", "kind": "click",
         "text": None, "text_source": None, "locator": LOCATOR},
        {"type": "run_end", "t_ms": 3, "status": "done", "stop_reason": None, "verified": True,
         "totals": {"actions": 1, "jev_calls": 1, "text_calls": 0, "wall_ms": 3}, "screenshot": None},
    ]
    console = [{"level": "error", "text": "boom", "url": "https://example.test/app.js"}]
    network = [record(status=404, url="https://example.test/missing"), record()]
    html_text = render_report(events, console, network, tmp_path, {"console": True, "network": True})
    assert "<script" not in html_text.lower() and "&lt;script&gt;" in html_text and "&lt;b&gt;now" in html_text
    assert "boom" in html_text and "example.test/missing" in html_text and "1 of 2" in html_text
    assert "data:image/jpeg;base64," in html_text
    import re

    for attribute in re.findall(r'(?:src|href)="([^"]*)"', html_text):
        assert attribute.startswith("data:"), attribute
    assert "url(" not in html_text and "@import" not in html_text


# --- the event connection ---------------------------------------------------------------------------------------


class ScriptedSocket:
    """A websocket double: answers the handshake, then yields queued events; recv raises TimeoutError when idle."""

    def __init__(self):
        self.sent, self.incoming, self.closed = [], [], False
        self.lock = threading.Lock()

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        result = {"sessionId": "S1"} if message["method"] == "Target.attachToTarget" else {}
        with self.lock:
            self.incoming.append(json.dumps({"id": message["id"], "result": result}))

    def push(self, message):
        with self.lock:
            self.incoming.append(json.dumps(message))

    def recv(self, timeout=None):
        if self.closed:
            raise ConnectionError("closed")
        with self.lock:
            if self.incoming:
                return self.incoming.pop(0)
        time.sleep(min(timeout or 0.05, 0.02))
        raise TimeoutError

    def close(self):
        self.closed = True


def event(method, **params):
    return {"sessionId": "S1", "method": method, "params": params}


def test_the_connection_attaches_to_its_own_target_enables_domains_and_collects_events():
    socket = ScriptedSocket()
    stream = CdpEvents("ws://browser", "TARGET", connect=lambda url, **_k: socket)
    stream.start()
    try:
        assert [m["method"] for m in socket.sent] == [
            "Target.attachToTarget", "Runtime.enable", "Log.enable", "Network.enable"
        ]
        assert socket.sent[0]["params"] == {"targetId": "TARGET", "flatten": True}
        assert all(m["sessionId"] == "S1" for m in socket.sent[1:])
        frame = {"callFrames": [{"url": "https://example.test/app.js", "lineNumber": 11}]}
        socket.push(event("Runtime.consoleAPICalled", type="warning", stackTrace=frame,
                          args=[{"type": "string", "value": "careful"}, {"type": "number", "value": 3}]))
        socket.push(event("Runtime.exceptionThrown", exceptionDetails={
            "text": "Uncaught", "stackTrace": frame,
            "exception": {"description": "Error: boom\n    at f (app.js:1:1)"}}))
        socket.push(event("Log.entryAdded", entry={"source": "network", "level": "error", "text": "Failed 404",
                                                    "url": "https://example.test/x", "lineNumber": 0}))
        socket.push({"sessionId": "OTHER", "method": "Runtime.consoleAPICalled", "params": {"type": "log", "args": []}})
        socket.push(event("Network.requestWillBeSent", requestId="1", type="Fetch", timestamp=10.0, wallTime=1.7e9,
                          request={"url": "https://example.test/api", "method": "POST"}))
        socket.push(event("Network.responseReceived", requestId="1", type="Fetch", response={
            "status": 404, "mimeType": "text/html", "timing": {"sendStart": 1.0, "receiveHeadersEnd": 6.5}}))
        socket.push(event("Network.loadingFinished", requestId="1", timestamp=10.25, encodedDataLength=77))
        socket.push(event("Network.requestWillBeSent", requestId="2", type="Fetch", timestamp=11.0, wallTime=1.7e9,
                          request={"url": "https://nowhere.invalid/", "method": "GET"}))
        socket.push(event("Network.loadingFailed", requestId="2", timestamp=11.1,
                          errorText="net::ERR_NAME_NOT_RESOLVED"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (socket.incoming == [] and len(stream.snapshot()[0]) >= 3):
            time.sleep(0.01)
        time.sleep(0.1)
        console, network, dropped = stream.snapshot()
        assert [c["level"] for c in console] == ["warning", "error", "error"]
        assert console[0]["text"] == "careful 3" and console[0]["url"].endswith("app.js") and console[0]["line"] == 12
        assert console[1]["source"] == "exception" and console[1]["text"] == "Error: boom"
        assert console[1]["stack_top"].endswith("app.js:12")
        first, second = network
        got = (first["method"], first["status"], first["mime"], first["encoded_size"])
        assert got == ("POST", 404, "text/html", 77)
        assert first["ttfb_ms"] == 5.5 and first["duration_ms"] == 250.0 and first["failure"] is None
        assert second["status"] is None and second["failure"] == "net::ERR_NAME_NOT_RESOLVED"
        assert dropped == {"console": 0, "network": 0}
        assert not any(k.startswith("_") for row in network for k in row)
    finally:
        stream.close()
    assert socket.closed and not stream.alive()
    assert THREAD_NAME not in [t.name for t in threading.enumerate()]


THREAD_NAME = evidence_module.THREAD


def test_a_redirect_is_two_records_and_buffers_are_bounded():
    socket = ScriptedSocket()
    stream = CdpEvents("ws://browser", "T", connect=lambda *_a, **_k: socket, limit=3)
    stream.start()
    try:
        for message in (
            event("Network.requestWillBeSent", requestId="r", type="Document", timestamp=1.0, wallTime=1.7e9,
                  request={"url": "https://example.test/a", "method": "GET"}),
            event("Network.requestWillBeSent", requestId="r", type="Document", timestamp=1.1, wallTime=1.7e9,
                  request={"url": "https://example.test/b", "method": "GET"},
                  redirectResponse={"status": 302, "mimeType": "text/html"}),
            *[event("Runtime.consoleAPICalled", type="log", args=[{"type": "string", "value": str(i)}])
              for i in range(5)],
        ):
            socket.push(message)
        time.sleep(0.4)
        console, network, dropped = stream.snapshot()
    finally:
        stream.close()
    got = [(n["url"][-1], n["status"], n.get("redirected_to", "")[-1:]) for n in network]
    assert got == [("a", 302, "b"), ("b", None, "")]
    assert len(console) == 3 and dropped["console"] == 2


def test_a_failed_handshake_leaves_no_thread_or_open_socket():
    socket = ScriptedSocket()
    socket.send = lambda text: (_ for _ in ()).throw(OSError("boom"))
    stream = CdpEvents("ws://browser", "T", connect=lambda *_a, **_k: socket)
    with pytest.raises(OSError):
        stream.start()
    assert socket.closed and THREAD_NAME not in [t.name for t in threading.enumerate()]


def test_the_endpoint_comes_from_bu_cdp_ws_then_bu_cdp_url_and_says_why_when_unknown():
    assert resolve_endpoint({"BU_CDP_WS": "ws://a"}) == ("ws://a", None)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return json.dumps({"webSocketDebuggerUrl": "ws://b/devtools/browser/1"})

    assert resolve_endpoint({"BU_CDP_URL": "http://127.0.0.1:9222/"}, fetch) == ("ws://b/devtools/browser/1", None)
    assert fetched == ["http://127.0.0.1:9222/json/version"]
    url, why = resolve_endpoint({})
    assert url is None and "BU_CDP" in why
    url, why = resolve_endpoint({"BU_CDP_URL": "http://x"}, lambda _u: (_ for _ in ()).throw(OSError("down")))
    assert url is None and "OSError" in why


def test_unavailable_capture_is_recorded_in_run_start_and_the_run_goes_on(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    agent.evidence.attach("T", environ={})
    assert agent.evidence.capture["console"] is False and "BU_CDP" in agent.evidence.capture["reason"]
    do(agent)
    agent.close()
    assert not (tmp_path / "run" / "console.jsonl").exists()
    assert "Console was not captured" in (tmp_path / "run" / "report.html").read_text()
    assert read(tmp_path / "run" / "trace.jsonl")[0]["capture"]["console"] is False


def test_a_connection_that_cannot_open_is_unavailable_not_an_error(monkeypatch, tmp_path):
    evidence = Evidence(tmp_path / "e")

    class Broken:
        def __init__(self, *_a, **_k):
            pass

        def start(self):
            raise OSError("refused")

    evidence.attach("T", environ={"BU_CDP_WS": "ws://x"}, events=Broken)
    assert evidence.capture["console"] is False and "OSError" in evidence.capture["reason"]
    evidence.close()


def test_closing_the_agent_stops_the_event_thread(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    socket = ScriptedSocket()
    agent.evidence.attach("T", environ={"BU_CDP_WS": "ws://x"}, events=lambda *a, **k: CdpEvents(
        *a, connect=lambda *_a, **_k: socket, **k))
    assert agent.evidence.capture["console"] is True and THREAD_NAME in [t.name for t in threading.enumerate()]
    agent.close()
    assert THREAD_NAME not in [t.name for t in threading.enumerate()] and socket.closed
    for name in ("console.jsonl", "network.jsonl", "network.har", "report.html", "trace.jsonl"):
        assert (tmp_path / "run" / name).exists()
    assert json.loads((tmp_path / "run" / "network.har").read_text())["log"]["version"] == "1.2"


def test_actions_mode_does_not_shoot_an_observation_that_follows_no_action(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path, screenshots="actions")
    agent.fake.browser.fresh.return_value = False
    agent.fake.pages.append(page(text="moved"))
    monkeypatch.setattr(loop, "choose", lambda *_a, **_k: decision("e3", "CLICK"))
    agent.command("predict")
    agent.close()
    assert len(shots(tmp_path)) == 2  # first page and final page only


def test_every_event_and_the_report_pass_through_the_scrub_given_to_evidence(tmp_path):
    evidence = Evidence(tmp_path / "e", clean=lambda value: json.loads(json.dumps(value).replace("TOPSECRET", "<x>")))
    evidence.event("step", index=0, status="active", note="a TOPSECRET b", nested={"k": ["TOPSECRET"]})
    evidence.event("run_end", status="done", stop_reason=None, verified=False, totals={}, screenshot=None)
    evidence.events[0]["note"] = "a TOPSECRET b"  # The report must clean what it renders, whatever was kept.
    evidence.close()
    assert "TOPSECRET" not in (tmp_path / "e" / "trace.jsonl").read_text()
    assert "<x>" in (tmp_path / "e" / "trace.jsonl").read_text()
    assert "TOPSECRET" not in (tmp_path / "e" / "report.html").read_text()
