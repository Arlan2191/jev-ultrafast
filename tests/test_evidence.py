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
             "params": {"requestId": form, "request": {"url": f"https://example.test/p/{form}", "method": "GET"},
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
        "save": True, "email-field": True, "main-nav": True, "item-3": False, "SubmitButton": True,
        "": False, ":r1:": False, "radix-:r2:": False, "«r1»": False, "_r_3_": False, "ember123": False,
        "ember": True, "12345": False,
        # Counter-generated ids that frameworks produce most.
        "mat-input-3": False, "cdk-overlay-12": False, "react-select-2-input": False, "radix-0": False,
        "downshift-1-input": False, "ui-id-4": False, "rc_select_0": False, "gwt-uid-5": False, "mui-12": False,
        "input-1": False, "item-3.2": False, "email-v2-field": True, "step2": True, "h1-title": True,
        "7": False, "a1b2c3d4e5": False, "550e8400-e29b-41d4-a716-446655440000": False,
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
            "Target.attachToTarget", "Runtime.enable", "Log.enable", "Network.enable", "Inspector.enable"
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
        assert dropped == {"console": 0, "network": 0, "errors": 0}
        assert stream.lost() is None
        assert socket.sent[3]["params"] == evidence_module.BUFFERS  # Network.enable keeps no response bodies.
        assert not any(k.startswith("_") for row in network for k in row)
    finally:
        stream.close()
    assert socket.closed and not stream.alive()
    assert THREAD_NAME not in [t.name for t in threading.enumerate()]


THREAD_NAME = evidence_module.THREAD


def test_a_redirect_is_two_records():
    socket = ScriptedSocket()
    stream = CdpEvents("ws://browser", "T", connect=lambda *_a, **_k: socket)
    stream.start()
    try:
        for message in (
            event("Network.requestWillBeSent", requestId="r", type="Document", timestamp=1.0, wallTime=1.7e9,
                  request={"url": "https://example.test/a", "method": "GET"}),
            event("Network.requestWillBeSent", requestId="r", type="Document", timestamp=1.1, wallTime=1.7e9,
                  request={"url": "https://example.test/b", "method": "GET"},
                  redirectResponse={"status": 302, "mimeType": "text/html"}),
        ):
            socket.push(message)
        settle(stream, lambda: len(stream.snapshot()[1]) == 2)
        network = stream.snapshot()[1]
    finally:
        stream.close()
    got = [(n["url"][-1], n["status"], n.get("redirected_to", "")[-1:]) for n in network]
    assert got == [("a", 302, "b"), ("b", None, "")]


def settle(stream, done, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not done():
        time.sleep(0.01)
    time.sleep(0.05)


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
    socket.push(event("Runtime.consoleAPICalled", type="error", args=[{"type": "string", "value": "kaboom"}]))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not agent.evidence.stream.snapshot()[0]:
        time.sleep(0.01)
    agent.close()
    assert "kaboom" in (tmp_path / "run" / "report.html").read_text()
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
    evidence.close()
    assert "TOPSECRET" not in (tmp_path / "e" / "trace.jsonl").read_text()
    assert "<x>" in (tmp_path / "e" / "trace.jsonl").read_text()
    assert "TOPSECRET" not in (tmp_path / "e" / "report.html").read_text()


# --- fix round 1: URLs, lost capture, bounds, locator errors, report ------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.test/verify?token=zzz&state=abc#access_token=q",
         "https://example.test/verify?token=<redacted>&state=<redacted>#<redacted>"),
        ("https://example.test/a/b", "https://example.test/a/b"),
        ("/verify?token=zzz", "/verify?token=<redacted>"),
        ("https://example.test/?abc123", "https://example.test/?<redacted>"),
        ("https://example.test/?a=1&a=2&b", "https://example.test/?a=<redacted>&a=<redacted>&<redacted>"),
        ("https://user:pw@example.test:8080/x?k=v", "https://example.test:8080/x?k=<redacted>"),
        ("data:image/png;base64,AAAA", "data:image/png"),
        ("data:,secret", "data:"),
        ("blob:https://example.test/0b1c", "blob:"),
        ("/a//b@c?x=1", "/a//b@c?x=<redacted>"),
        ("about:blank", "about:blank"),
    ],
)
def test_redact_url_keeps_origin_path_and_query_keys_only(url, expected):
    assert evidence_module.redact_url(url) == expected
    assert evidence_module.redact_url(expected) == expected  # Idempotent: every layer may apply it.


def test_redact_text_redacts_each_url_inside_a_message():
    text = "GET https://example.test/cb?code=zzz failed; see data:image/png;base64,AAAA and metadata:keep"
    out = evidence_module.redact_text(text)
    assert "zzz" not in out and "AAAA" not in out
    assert out == "GET https://example.test/cb?code=<redacted> failed; see data:image/png and metadata:keep"


def test_an_undeclared_token_in_a_url_is_in_no_written_file(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path, "open it")
    agent.fake.pages = [{**page(), "url": "https://example.test/verify?token=zzz#frag-zzz"}]
    agent.fake.pages.append({**page(text="next"), "url": "https://example.test/done?session=zzz"})
    agent.state["page"] = agent.fake.pages[0]
    agent.fake.browser.locator.return_value = {**LOCATOR, "href": "/verify?token=zzz"}  # The page-side half is a11y.js.
    socket = ScriptedSocket()
    stream = CdpEvents("ws://x", "t", connect=lambda *_a, **_k: socket)
    stream.start()
    agent.evidence.stream, agent.evidence.capture = stream, {"console": True, "network": True, "reason": None}
    stream.handle(event("Network.requestWillBeSent", requestId="1", type="Document", timestamp=1.0, wallTime=1.0,
                        request={"url": "https://example.test/verify?token=zzz#frag-zzz", "method": "GET"}))
    stream.handle(event("Network.requestWillBeSent", requestId="1", type="Document", timestamp=1.1, wallTime=1.0,
                        request={"url": "https://example.test/next?code=zzz", "method": "GET"},
                        redirectResponse={"status": 302}))
    stream.handle(event("Network.loadingFailed", requestId="1", timestamp=1.2, errorText="net::ERR_FAILED"))
    stream.handle(event("Runtime.consoleAPICalled", type="log", args=[
        {"type": "string", "value": "at https://example.test/x?token=zzz"}],
        stackTrace={"callFrames": [{"url": "https://example.test/app.js?v=zzz", "lineNumber": 0}]}))
    stream.handle(event("Log.entryAdded", entry={"source": "network", "level": "error", "text": "Failed",
                                                 "url": "https://example.test/img?sig=zzz"}))
    stream.handle(event("Runtime.exceptionThrown", exceptionDetails={
        "text": "Uncaught", "url": "https://example.test/boot.js?auth=zzz",
        "exception": {"description": "Error: bad https://example.test/a?token=zzz"}}))
    do(agent, "e3", "CLICK")
    do(agent)
    agent.close()
    files = [p for p in (tmp_path / "run").rglob("*") if p.is_file() and p.suffix != ".jpg"]
    assert {p.name for p in files} >= {"trace.jsonl", "console.jsonl", "network.jsonl", "network.har", "report.html"}
    for path in files:
        assert "zzz" not in path.read_text(), path.name
    network = read(tmp_path / "run" / "network.jsonl")
    assert network[0]["url"] == "https://example.test/verify?token=<redacted>#<redacted>"
    assert network[0]["redirected_to"] == "https://example.test/next?code=<redacted>"
    har = json.loads((tmp_path / "run" / "network.har").read_text())
    assert har["log"]["entries"][0]["request"]["queryString"] == [{"name": "token", "value": "<redacted>"}]
    observed = [e["url"] for e in read(tmp_path / "run" / "trace.jsonl") if e["type"] == "observe"]
    assert "https://example.test/done?session=<redacted>" in observed


def test_evidence_urls_full_opts_out_and_a_bad_mode_is_refused(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path, evidence_urls="full")
    agent.fake.pages = [{**page(), "url": "https://example.test/verify?token=zzz"}]
    agent.state["page"] = agent.fake.pages[0]
    agent.trace_observe(agent.state["page"])
    do(agent)
    agent.close()
    assert "token=zzz" in (tmp_path / "run" / "trace.jsonl").read_text()
    agent.fake.browser.locator.assert_not_called()
    with pytest.raises(ValueError, match="evidence_urls"):
        make(monkeypatch, tmp_path / "other", evidence_urls="partial")


def test_the_locator_is_read_with_the_url_mode(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    do(agent, "e3", "CLICK")
    agent.fake.browser.locator.assert_called_with(20, full_urls=False)
    agent.close()
    agent = make(monkeypatch, tmp_path / "full", evidence_urls="full")
    do(agent, "e3", "CLICK")
    agent.fake.browser.locator.assert_called_with(20, full_urls=True)
    agent.close()


def test_evidence_capture_false_opens_no_second_connection(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path, evidence_capture=False)
    agent.evidence.attach("T", environ={"BU_CDP_WS": "ws://x"}, events=lambda *_a, **_k: pytest.fail("connected"))
    assert agent.evidence.capture == {"console": False, "network": False, "reason": "evidence_capture=False"}
    do(agent, "e3", "CLICK")
    do(agent)
    agent.close()
    assert agent.evidence.stream is None and agent.state["history"][0]["locator"] == LOCATOR
    assert "Console was not captured: evidence_capture=False" in (tmp_path / "run" / "report.html").read_text()
    with pytest.raises(ValueError, match="evidence_capture"):
        make(monkeypatch, tmp_path / "other", evidence_capture="no")


def test_a_connection_that_dies_mid_run_is_recorded_not_reported_as_a_clean_page(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    socket = ScriptedSocket()
    agent.evidence.attach("T", environ={"BU_CDP_WS": "ws://x"}, events=lambda *a, **k: CdpEvents(
        *a, connect=lambda *_a, **_k: socket, **k))
    time.sleep(0.05)
    socket.closed = True  # The tab crashed.
    settle(agent.evidence.stream, lambda: agent.evidence.stream.lost() is not None)
    assert agent.evidence.lost()["reason"] == "ConnectionError"
    do(agent)
    agent.close()
    end = read(tmp_path / "run" / "trace.jsonl")[-1]
    assert end["capture_lost"]["reason"] == "ConnectionError" and end["capture_lost"]["t_ms"] >= 0
    page_text = (tmp_path / "run" / "report.html").read_text()
    assert "capture ended at" in page_text and "ConnectionError" in page_text


def test_a_normal_close_is_not_a_lost_capture(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    agent.evidence.attach("T", environ={"BU_CDP_WS": "ws://x"}, events=lambda *a, **k: CdpEvents(
        *a, connect=lambda *_a, **_k: ScriptedSocket(), **k))
    do(agent)
    agent.close()
    end = read(tmp_path / "run" / "trace.jsonl")[-1]
    assert end["capture_lost"] is None and "capture ended" not in (tmp_path / "run" / "report.html").read_text()


def test_events_that_cannot_be_read_are_counted(monkeypatch, tmp_path):
    socket = ScriptedSocket()
    stream = CdpEvents("ws://x", "T", connect=lambda *_a, **_k: socket)
    stream.start()
    try:
        socket.push(event("Network.requestWillBeSent", requestId="1"))  # No request: the handler raises.
        socket.push("not json")
        socket.push(event("Runtime.consoleAPICalled", type="log", args=[{"type": "string", "value": "still here"}]))
        settle(stream, lambda: stream.snapshot()[0])
        console, _network, dropped = stream.snapshot()
    finally:
        stream.close()
    assert [c["text"] for c in console] == ["still here"] and dropped["errors"] == 2


def test_run_end_dropped_is_taken_after_the_connection_closed(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    socket = ScriptedSocket()
    agent.evidence.attach("T", environ={"BU_CDP_WS": "ws://x"}, events=lambda *a, **k: CdpEvents(
        *a, connect=lambda *_a, **_k: socket, head=1, tail=1, reserved=1, **k))
    do(agent)  # run_end is written here ...
    for i in range(4):  # ... and more arrives before close().
        socket.push(event("Runtime.consoleAPICalled", type="log", args=[{"type": "string", "value": str(i)}]))
    settle(agent.evidence.stream, lambda: socket.incoming == [])
    agent.close()
    end = read(tmp_path / "run" / "trace.jsonl")[-1]
    assert end["type"] == "run_end" and end["dropped"] == {"console": 2, "network": 0, "errors": 0}
    assert "between the first" in end["dropped_part"]
    assert [c["text"] for c in read(tmp_path / "run" / "console.jsonl")] == ["0", "3"]


def test_past_the_cap_the_first_the_last_and_every_error_are_kept():
    socket = ScriptedSocket()
    stream = CdpEvents("ws://x", "T", connect=lambda *_a, **_k: socket, head=2, tail=2, reserved=2)
    stream.start()
    try:
        for i in range(10):
            socket.push(event("Runtime.consoleAPICalled", type="error" if i in (4, 5, 6) else "log",
                              args=[{"type": "string", "value": f"m{i}"}]))
        socket.push(event("Runtime.exceptionThrown",
                          exceptionDetails={"text": "late", "exception": {"description": "E: late"}}))
        for i in range(10, 14):  # Noise after the errors must not push them out of the reserved ring.
            socket.push(event("Runtime.consoleAPICalled", type="log", args=[{"type": "string", "value": f"m{i}"}]))
        settle(stream, lambda: socket.incoming == [])
        console, _network, dropped = stream.snapshot()
    finally:
        stream.close()
    # head m0 m1; tail m12 m13; reserved ring keeps the last two errors (m6 and the exception).
    assert [c["text"] for c in console] == ["m0", "m1", "m6", "E: late", "m12", "m13"]
    assert dropped["console"] == 15 - 6


def test_network_failures_outlive_the_tail_ring():
    socket = ScriptedSocket()
    stream = CdpEvents("ws://x", "T", connect=lambda *_a, **_k: socket, head=1, tail=2, reserved=5)
    stream.start()
    try:
        for i in range(8):
            socket.push(event("Network.requestWillBeSent", requestId=str(i), type="Fetch", timestamp=1.0, wallTime=1.0,
                              request={"url": f"https://example.test/{i}", "method": "GET"}))
            if i == 3:
                socket.push(event("Network.responseReceived", requestId="3", type="Fetch", response={"status": 404}))
        settle(stream, lambda: socket.incoming == [])
        _console, network, dropped = stream.snapshot()
    finally:
        stream.close()
    assert [n["url"][-1] for n in network] == ["0", "3", "6", "7"] and dropped["network"] == 4
    assert len(stream.requests) <= 3  # Records that fell out of the ring are no longer tracked.


def test_fields_are_clipped_with_a_truncated_flag_and_frames_have_a_ceiling():
    socket = ScriptedSocket()
    seen = {}
    stream = CdpEvents("ws://x", "T", connect=lambda url, **kw: seen.update(kw) or socket)
    stream.start()
    try:
        huge = "x" * 50_000
        socket.push(event("Runtime.consoleAPICalled", type="log", args=[{"type": "string", "value": huge}]))
        socket.push(event("Runtime.exceptionThrown", exceptionDetails={"exception": {"description": "E: " + huge}}))
        socket.push(event("Network.requestWillBeSent", requestId="1", type="Fetch", timestamp=1.0, wallTime=1.0,
                          request={"url": "https://example.test/" + huge, "method": "GET"}))
        settle(stream, lambda: len(stream.snapshot()[1]) == 1 and len(stream.snapshot()[0]) == 2)
        console, network, _dropped = stream.snapshot()
    finally:
        stream.close()
    limits = evidence_module.CLIP
    assert len(console[0]["text"]) == limits["text"] and console[0]["truncated"] == ["text"]
    assert len(console[1]["stack"]) == limits["stack"] and "stack" in console[1]["truncated"]
    assert len(network[0]["url"]) == limits["url"] and network[0]["truncated"] == ["url"]
    assert 0 < seen["max_size"] <= 16 * 1024 * 1024


def test_network_enable_falls_back_when_the_buffer_parameters_are_unknown():
    class Picky(ScriptedSocket):
        def send(self, text):
            message = json.loads(text)
            self.sent.append(message)
            if message["method"] == "Network.enable" and message["params"]:
                reply = {"id": message["id"], "error": {"message": "invalid params"}}
            else:
                reply = {"id": message["id"], "result": {"sessionId": "S1"}}
            with self.lock:
                self.incoming.append(json.dumps(reply))

    socket = Picky()
    stream = CdpEvents("ws://x", "T", connect=lambda *_a, **_k: socket)
    stream.start()
    try:
        enables = [m["params"] for m in socket.sent if m["method"] == "Network.enable"]
        assert enables == [evidence_module.BUFFERS, {}] and stream.alive()
    finally:
        stream.close()


def test_a_skipped_locator_says_why_and_the_totals_count_it(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    agent.fake.browser.locator.side_effect = TimeoutError("slow")
    do(agent, "e3", "CLICK")
    agent.fake.browser.locator.side_effect = None
    agent.fake.browser.locator.return_value = None  # The node is gone.
    do(agent, "e3", "CLICK")
    do(agent)
    agent.close()
    events = read(tmp_path / "run" / "trace.jsonl")
    actions = [e for e in events if e["type"] == "action"]
    assert [(a["locator"], a["locator_error"]) for a in actions] == [(None, "TimeoutError"), (None, "ElementGone")]
    assert [h["locator_error"] for h in agent.state["history"]] == ["TimeoutError", "ElementGone"]
    assert events[-1]["totals"]["locator_missing"] == 2
    assert "2 executed actions have no locator" in (tmp_path / "run" / "report.html").read_text()
    assert "no locator: TimeoutError" in (tmp_path / "run" / "report.html").read_text()


def test_a_run_with_all_its_locators_counts_none_missing(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    do(agent, "e3", "CLICK")
    do(agent)
    agent.close()
    end = read(tmp_path / "run" / "trace.jsonl")[-1]
    assert end["totals"]["locator_missing"] == 0
    assert "locator_error" not in agent.state["history"][0]


def test_endpoints_must_be_http_or_websocket_urls():
    assert resolve_endpoint({"BU_CDP_URL": "file:///etc/passwd"})[0] is None
    assert "http" in resolve_endpoint({"BU_CDP_URL": "ftp://host"})[1]
    assert resolve_endpoint({"BU_CDP_WS": "http://host"})[0] is None
    assert resolve_endpoint({"BU_CDP_WS": "wss://host/devtools/browser/1"}) == ("wss://host/devtools/browser/1", None)


# --- report: CSP and inert rendering -------------------------------------------------------------------------------


def test_the_report_has_a_csp_and_renders_hostile_text_inert(tmp_path):
    hostile = ['"><img src=x onerror=alert(1)>', "javascript:alert(1)", "</style><script>alert(2)</script>"]
    events = [
        {"type": "run_start", "t_ms": 0, "steps": [{"goal": hostile[0], "checks": [hostile[2]]}],
         "config": {"g": hostile[2]}},
        {"type": "observe", "t_ms": 1, "url": f"https://example.test/{hostile[1]}", "elements": 1, "screenshot": None},
        {"type": "action", "t_ms": 2, "index": 1, "label": hostile[0], "kind": "click", "text": hostile[2],
         "text_source": hostile[1], "locator": {**LOCATOR, "name": hostile[0], "href": hostile[1]}},
        {"type": "refused", "t_ms": 3, "label": hostile[2], "kind": "click", "reason": hostile[0], "skipped": False},
    ]
    console = [{"level": "error", "text": hostile[0], "url": hostile[1]}]
    network = [record(status=500, url=f"https://example.test/{hostile[2]}")]
    html_text = render_report(events, console, network, tmp_path, {"console": True, "network": True})
    assert "Content-Security-Policy" in html_text and "default-src 'none'" in html_text
    lowered = html_text.lower()
    assert "<img" not in lowered and "<script" not in lowered and lowered.count("</style>") == 1
    import re

    assert not re.search(r"<[a-z]+[^>]* on\w+=", lowered)
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_text


# --- HAR shape ---------------------------------------------------------------------------------------------------


HAR_ENTRY = {
    "startedDateTime": str, "time": (int, float),
    "request": {"method": str, "url": str, "httpVersion": str, "cookies": list, "headers": list, "queryString": [
        {"name": str, "value": str}], "headersSize": int, "bodySize": int},
    "response": {"status": int, "statusText": str, "httpVersion": str, "cookies": list, "headers": list,
                 "content": {"size": int, "mimeType": str}, "redirectURL": str, "headersSize": int, "bodySize": int},
    "cache": dict, "timings": {"send": (int, float), "wait": (int, float), "receive": (int, float)},
}


def conforms(value, shape, where="entry"):
    """A minimal structural check of a HAR 1.2 entry: required keys with their types (no external validator)."""
    if isinstance(shape, dict):
        assert isinstance(value, dict), where
        for key, inner in shape.items():
            assert key in value, f"{where}.{key} is missing"
            conforms(value[key], inner, f"{where}.{key}")
    elif isinstance(shape, list):
        assert isinstance(value, list), where
        for index, item in enumerate(value):
            conforms(item, shape[0], f"{where}[{index}]")
    else:
        assert isinstance(value, shape) and not isinstance(value, bool), f"{where} is {type(value).__name__}"


def test_har_entries_conform_to_the_1_2_field_types_and_carry_the_query_keys():
    har = build_har(
        [record(url="https://example.test/a?token=<redacted>&q=<redacted>"), record(status=None, failure="x")], "1"
    )
    assert set(har["log"]) == {"version", "creator", "entries"} and set(har["log"]["creator"]) == {"name", "version"}
    for entry in har["log"]["entries"]:
        conforms(entry, HAR_ENTRY)
    first = har["log"]["entries"][0]["request"]["queryString"]
    assert first == [{"name": "token", "value": "<redacted>"}, {"name": "q", "value": "<redacted>"}]
    assert har["log"]["entries"][1]["request"]["queryString"] == []


# --- fix round 2: non-http URLs, IPv6 and relative URLs in text, path parameters, a tab that dies -----------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("javascript:doIt('zzz')", "javascript:"),
        ("JavaScript:alert(1)", "javascript:"),
        ("mailto:person@example.com?subject=zzz", "mailto:"),
        ("tel:+15550100", "tel:"),
        ("data:text/plain;base64,QUJD-zzz", "data:text/plain"),
        ("https://h/p;jsessionid=ABC123?x=1", "https://h/p;jsessionid=<redacted>?x=<redacted>"),
        ("/p;token=SECRET/x;a=1", "/p;token=<redacted>/x;a=<redacted>"),
        ("http://[::1]:8080/cb?code=zzz", "http://[::1]:8080/cb?code=<redacted>"),
        ("localhost:8080/x?k=v", "localhost:8080/x?k=<redacted>"),
    ],
)
def test_redact_url_cuts_other_schemes_and_path_parameters(url, expected):
    assert evidence_module.redact_url(url) == expected
    assert evidence_module.redact_url(expected) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("TypeError at http://[::1]:8080/cb?code=zzz now", "TypeError at http://[::1]:8080/cb?code=<redacted> now"),
        ("Fetch failed: /api/me?token=zzz", "Fetch failed: /api/me?token=<redacted>"),
        ("bad ?key=zzz here", "bad ?key=<redacted> here"),
        ("href javascript:doIt('zzz') ok", "href javascript: ok"),
        ("write mailto:p@e.com?subject=zzz now", "write mailto: now"),
        ("who? nobody", "who? nobody"),
    ],
)
def test_redact_text_covers_ipv6_hosts_relative_urls_and_script_urls(text, expected):
    out = evidence_module.redact_text(text)
    assert out == expected and "zzz" not in out


def test_a_locator_href_is_clipped_to_the_url_limit():
    assert len(evidence_module.clipped("x" * 5000, "url")[0]) == evidence_module.CLIP["url"]


def test_a_tab_that_crashes_while_the_socket_stays_open_is_a_lost_capture():
    socket = ScriptedSocket()
    stream = CdpEvents("ws://browser", "TARGET", connect=lambda url, **_k: socket)
    stream.start()
    try:
        socket.push({"method": "Target.detachedFromTarget", "params": {"sessionId": "OTHER"}})
        socket.push(event("Log.entryAdded", entry={"source": "other", "level": "info", "text": "still here"}))
        settle(stream, lambda: len(stream.snapshot()[0]) == 1)
        assert stream.lost() is None  # Another session going away is not ours.
        socket.push({"method": "Target.detachedFromTarget", "params": {"sessionId": "S1"}})
        settle(stream, lambda: stream.lost() is not None)
        assert stream.lost()["reason"] == "TargetDetached"
    finally:
        stream.close()
    assert stream.lost()["reason"] == "TargetDetached"  # The first reason stays; closing does not clear it.


def test_a_renderer_crash_event_is_a_lost_capture():
    socket = ScriptedSocket()
    stream = CdpEvents("ws://browser", "TARGET", connect=lambda url, **_k: socket)
    stream.start()
    try:
        socket.push(event("Inspector.targetCrashed"))
        settle(stream, lambda: stream.lost() is not None)
        assert stream.lost()["reason"] == "TargetCrashed"
    finally:
        stream.close()


def test_an_element_outside_the_document_is_a_counted_missing_locator(monkeypatch, tmp_path):
    agent = make(monkeypatch, tmp_path)
    agent.fake.browser.locator.return_value = {"error": "NotInDocument"}  # A shadow root or a frame.
    do(agent, "e3", "CLICK")
    do(agent)
    agent.close()
    events = read(tmp_path / "run" / "trace.jsonl")
    action = next(e for e in events if e["type"] == "action")
    assert (action["locator"], action["locator_error"]) == (None, "NotInDocument")
    assert events[-1]["totals"]["locator_missing"] == 1
