"""Run evidence: trace.jsonl, screenshots, console.jsonl, network.jsonl, network.har and report.html.

`Evidence` owns the run directory. Console and network events come from a dedicated CDP websocket that only this
module reads (browser-harness's shared event buffer is one global destructive queue, and wrapping page functions would
change what the page sees). Everything written passes through `clean`, the agent's secret scrub, except screenshots.
"""

import base64
import datetime
import importlib.metadata
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from .report import render_report

MODES = ("actions", "all", "failure", "none")
LIMIT = 5000  # Entries kept per stream (console, network). Later ones are counted, not kept.
THREAD = "jev-evidence-events"
DOMAINS = ("Runtime.enable", "Log.enable", "Network.enable")


def library_version():
    try:
        return importlib.metadata.version("jev-ultrafast")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def resolve_endpoint(environ=None, fetch=None):
    """(browser websocket URL, None), or (None, why it is unknown). From BU_CDP_WS, else /json/version of BU_CDP_URL;
    a browser that browser-harness found by itself has no URL this module can know."""
    environ = os.environ if environ is None else environ
    if environ.get("BU_CDP_WS"):
        return environ["BU_CDP_WS"], None
    base = environ.get("BU_CDP_URL")
    if not base:
        return None, "neither BU_CDP_WS nor BU_CDP_URL is set, so the DevTools endpoint is unknown"
    try:
        fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=5).read())
        return json.loads(fetch(base.rstrip("/") + "/json/version"))["webSocketDebuggerUrl"], None
    except Exception as error:
        return None, f"could not resolve a websocket from BU_CDP_URL: {type(error).__name__}"


def argument_text(argument):
    if "value" in argument:
        value = argument["value"]
        return value if isinstance(value, str) else json.dumps(value)
    return argument.get("unserializableValue") or argument.get("description") or argument.get("type", "")


def top_frame(stack):
    frames = (stack or {}).get("callFrames") or []
    return (frames[0].get("url", ""), frames[0].get("lineNumber", 0) + 1) if frames else ("", None)


class CdpEvents:
    """A websocket of its own, attached to one target, that collects console and network events in a thread.

    `connect(url)` returns an object with send(str), recv(timeout=...) (raising TimeoutError when idle) and close();
    the default is websockets.sync.client.connect. Buffers are bounded: past `limit` entries they only count drops."""

    def __init__(self, url, target, clock=time.perf_counter, started=None, limit=LIMIT, connect=None):
        self.url, self.target, self.clock, self.limit, self.connect = url, target, clock, limit, connect
        self.started = clock() if started is None else started
        self.console, self.network, self.dropped = [], [], {"console": 0, "network": 0}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.requests = {}
        self.ws = self.thread = self.session = None
        self.next_id = 0

    def start(self):
        connect = self.connect
        if connect is None:
            from websockets.sync.client import connect  # noqa: PLC0415 (only needed when evidence is on)
        self.ws = connect(self.url, max_size=None, open_timeout=5, ping_interval=None, compression=None)
        try:
            self.session = self.request("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            for method in DOMAINS:
                self.request(method, session=self.session)
        except BaseException:
            self.close()
            raise
        self.thread = threading.Thread(target=self.pump, name=THREAD, daemon=True)
        self.thread.start()

    def request(self, method, session=None, **params):
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                reply = json.loads(self.ws.recv(timeout=0.5))
            except TimeoutError:
                continue
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error'].get('message', 'failed')}")
                return reply.get("result", {})
            self.handle(reply)
        raise TimeoutError(f"{method} was not answered")

    def pump(self):
        while not self.stopping.is_set():
            try:
                raw = self.ws.recv(timeout=0.2)
            except TimeoutError:
                continue
            except Exception:
                return  # The connection closed (the browser or the tab went away).
            try:
                self.handle(json.loads(raw))
            except Exception:
                continue  # One odd event never ends the capture.

    def close(self):
        self.stopping.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(5)
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.thread = None

    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    def now_ms(self):
        return round((self.clock() - self.started) * 1000)

    def snapshot(self):
        with self.lock:
            clean = lambda items: [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]  # noqa: E731
            return clean(self.console), clean(self.network), dict(self.dropped)

    def keep(self, name, items, item):
        if len(items) >= self.limit:
            self.dropped[name] += 1
            return False
        items.append(item)
        return True

    def handle(self, message):
        if message.get("sessionId") != self.session or "method" not in message:
            return
        method, p = message["method"], message.get("params", {})
        with self.lock:
            if method == "Runtime.consoleAPICalled":
                url, line = top_frame(p.get("stackTrace"))
                self.keep(
                    "console",
                    self.console,
                    {
                        "t_ms": self.now_ms(),
                        "source": "console",
                        "level": p.get("type", "log"),
                        "text": " ".join(argument_text(a) for a in p.get("args", [])),
                        "url": url,
                        "line": line,
                    },
                )
            elif method == "Runtime.exceptionThrown":
                details = p.get("exceptionDetails", {})
                description = (details.get("exception") or {}).get("description") or details.get("text", "")
                url, line = top_frame(details.get("stackTrace"))
                self.keep(
                    "console",
                    self.console,
                    {
                        "t_ms": self.now_ms(),
                        "source": "exception",
                        "level": "error",
                        "text": description.split("\n")[0],
                        "stack_top": f"{url or details.get('url', '')}:{line or details.get('lineNumber', 0) + 1}",
                        "stack": description,
                    },
                )
            elif method == "Log.entryAdded":
                entry = p.get("entry", {})
                self.keep(
                    "console",
                    self.console,
                    {
                        "t_ms": self.now_ms(),
                        "source": "browser",
                        "origin": entry.get("source"),
                        "level": entry.get("level", "info"),
                        "text": entry.get("text", ""),
                        "url": entry.get("url", ""),
                        "line": entry.get("lineNumber"),
                    },
                )
            elif method.startswith("Network."):
                self.on_network(method, p)

    def on_network(self, method, p):
        ident = p.get("requestId")
        if method == "Network.requestWillBeSent":
            before = self.requests.get(ident)
            if before is not None and p.get("redirectResponse"):
                self.respond(before, p["redirectResponse"], p.get("type"))
                self.finish(before, p.get("timestamp"))
                before["redirected_to"] = p["request"]["url"]
            record = {
                "method": p["request"]["method"],
                "url": p["request"]["url"],
                "resource_type": p.get("type"),
                "status": None,
                "mime": None,
                "start_ms": self.now_ms(),
                "ttfb_ms": None,
                "duration_ms": None,
                "encoded_size": None,
                "failure": None,
                "from_cache": False,
                "started_at": datetime.datetime.fromtimestamp(
                    p.get("wallTime") or time.time(), datetime.UTC
                ).isoformat(timespec="milliseconds"),
                "_ts": p.get("timestamp"),
            }
            if self.keep("network", self.network, record):
                self.requests[ident] = record
            else:
                self.requests.pop(ident, None)
            return
        record = self.requests.get(ident)
        if record is None:
            return
        if method == "Network.responseReceived":
            self.respond(record, p["response"], p.get("type"))
        elif method == "Network.requestServedFromCache":
            record["from_cache"] = True
        elif method == "Network.loadingFinished":
            record["encoded_size"] = p.get("encodedDataLength")
            self.finish(record, p.get("timestamp"))
        elif method == "Network.loadingFailed":
            record["failure"] = p.get("errorText") or p.get("blockedReason") or "failed"
            if p.get("canceled"):
                record["failure"] += " (canceled)"
            self.finish(record, p.get("timestamp"))

    @staticmethod
    def respond(record, response, kind):
        record["status"], record["mime"] = response.get("status"), response.get("mimeType")
        record["resource_type"] = kind or record["resource_type"]
        record["from_cache"] = record["from_cache"] or bool(
            response.get("fromDiskCache") or response.get("fromPrefetchCache") or response.get("fromServiceWorker")
        )
        timing = response.get("timing") or {}
        sent, headers = timing.get("sendStart"), timing.get("receiveHeadersEnd")
        if sent is not None and headers is not None and headers >= sent >= 0:
            record["ttfb_ms"] = round(headers - sent, 1)
        if response.get("encodedDataLength") not in (None, -1) and record["encoded_size"] is None:
            record["encoded_size"] = response["encodedDataLength"]

    @staticmethod
    def finish(record, timestamp):
        if timestamp is not None and record.get("_ts") is not None:
            record["duration_ms"] = round(max(timestamp - record["_ts"], 0) * 1000, 1)


def build_har(network, version):
    """HAR 1.2 from network records (no bodies, no headers: none were read)."""
    entries = []
    for r in network:
        wait = r["ttfb_ms"] if r.get("ttfb_ms") is not None else -1
        total = r.get("duration_ms") or 0
        receive = max(total - wait, 0) if wait >= 0 else total
        entry = {
            "startedDateTime": r["started_at"],
            "time": max(wait, 0) + receive,
            "request": {
                "method": r["method"],
                "url": r["url"],
                "httpVersion": "unknown",
                "cookies": [],
                "headers": [],
                "queryString": [],
                "headersSize": -1,
                "bodySize": -1,
            },
            "response": {
                "status": r["status"] or 0,
                "statusText": "",
                "httpVersion": "unknown",
                "cookies": [],
                "headers": [],
                "content": {"size": -1, "mimeType": r.get("mime") or ""},
                "redirectURL": r.get("redirected_to") or "",
                "headersSize": -1,
                "bodySize": r["encoded_size"] if r.get("encoded_size") is not None else -1,
            },
            "cache": {},
            "timings": {"send": 0, "wait": max(wait, 0), "receive": receive},
        }
        if r.get("failure"):
            entry["_error"] = r["failure"]
        if r.get("from_cache"):
            entry["_fromCache"] = True
        entries.append(entry)
    return {
        "log": {"version": "1.2", "creator": {"name": "jev-ultrafast", "version": version}, "entries": entries}
    }


class Evidence:
    """The run directory. Create it, `start` the run, `event` as it goes, `close` to write the rest."""

    def __init__(self, directory, screenshots="actions", clean=lambda value: value, clock=time.perf_counter):
        if screenshots not in MODES:
            raise ValueError(f"screenshots must be one of {', '.join(MODES)}")
        self.dir, self.mode, self.clean, self.clock = Path(directory), screenshots, clean, clock
        if self.dir.exists() and any(self.dir.iterdir()):
            raise ValueError(f"evidence_dir {str(self.dir)!r} already holds files; use a new or empty directory")
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.mode != "none":
            (self.dir / "screenshots").mkdir(exist_ok=True)
        self.started = clock()
        self.events, self.shots, self.ended, self.closed = [], 0, False, False
        self.stream, self.capture = None, {"console": False, "network": False, "reason": "not attached"}
        self.trace = open(self.dir / "trace.jsonl", "w", encoding="utf-8")  # noqa: SIM115 (kept open for the run)

    def now_ms(self):
        return round((self.clock() - self.started) * 1000)

    def attach(self, target, environ=None, events=None):
        """Start the event connection for this tab. Never raises: console and network are then marked unavailable."""
        url, why = resolve_endpoint(environ)
        if url:
            stream = (events or CdpEvents)(url, target, clock=self.clock, started=self.started)
            try:
                stream.start()
                self.stream, self.capture = stream, {"console": True, "network": True, "reason": None}
                return
            except Exception as error:
                why = f"could not attach the evidence connection: {type(error).__name__}"
        self.capture = {"console": False, "network": False, "reason": why}

    def event(self, event_type, /, **fields):
        record = self.clean({"t_ms": self.now_ms(), "type": event_type, **fields})
        self.trace.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        self.trace.flush()
        self.events.append(record)
        return record

    def shot(self, browser, page=None):
        """Save a screenshot and return its path inside the run directory (None if none could be taken)."""
        try:
            data = (page or {}).get("screenshot") or browser.screenshot()
            self.shots += 1
            name = f"screenshots/{self.shots:04d}.jpg"
            (self.dir / name).write_bytes(base64.b64decode(data))
            return name
        except Exception:
            return None

    def observed(self, browser, page, important):
        """The screenshot for an observation: after an action or the first one ('actions'), every one ('all')."""
        wanted = self.mode == "all" or (self.mode == "actions" and important)
        return self.shot(browser, page) if wanted else None

    def final(self, browser, status):
        wanted = self.mode in {"actions", "all"} or (self.mode == "failure" and status != "done")
        return self.shot(browser) if wanted else None

    def dropped(self):
        return self.stream.snapshot()[2] if self.stream else {"console": 0, "network": 0}

    def write_streams(self):
        """console.jsonl, network.jsonl and network.har from what has been collected so far (scrubbed)."""
        if not self.stream:
            return [], [], {"console": 0, "network": 0}
        console, network, dropped = self.stream.snapshot()
        console, network = self.clean(console), self.clean(network)
        for name, rows in (("console.jsonl", console), ("network.jsonl", network)):
            (self.dir / name).write_text(
                "".join(json.dumps(row, default=str, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
            )
        har = self.clean(build_har(network, library_version()))
        (self.dir / "network.har").write_text(json.dumps(har, default=str, indent=1), encoding="utf-8")
        return console, network, dropped

    def close(self):
        """Stop the connection, write the streams and report.html. Safe to call twice."""
        if self.closed:
            return
        self.closed = True
        try:
            if self.stream:
                self.stream.close()
            console, network, _dropped = self.write_streams()
            page = render_report(self.events, console, network, self.dir, self.capture)
            (self.dir / "report.html").write_text(self.clean(page), encoding="utf-8")
        finally:
            self.trace.close()

