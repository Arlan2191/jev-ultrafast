"""Run evidence: trace.jsonl, screenshots, console.jsonl, network.jsonl, network.har and report.html.

`Evidence` owns the run directory. Console and network events come from a dedicated CDP websocket that only this
module reads (browser-harness's shared event buffer is one global destructive queue, and wrapping page functions would
change what the page sees). Everything written passes through `clean`, the agent's secret scrub, except screenshots.
"""

import base64
import collections
import datetime
import importlib.metadata
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .report import render_report

MODES = ("actions", "all", "failure", "none")
URL_MODES = ("redacted", "full")
# Per stream: the first HEAD entries, a ring of the last TAIL, and a ring of the last RESERVED errors.
HEAD, TAIL, RESERVED = 3000, 1000, 500
# Characters kept per field; a clipped record lists the fields in `truncated`.
CLIP = {"text": 2048, "stack": 8192, "url": 2048}
MAX_FRAME = 8 * 1024 * 1024  # A larger websocket frame closes the connection (reported as capture_lost).
BUFFERS = {"maxTotalBufferSize": 262144, "maxResourceBufferSize": 65536}  # Network.enable: do not retain bodies.
THREAD = "jev-evidence-events"
DOMAINS = ("Runtime.enable", "Log.enable", "Network.enable", "Inspector.enable")
DROPPED_PART = f"entries between the first {HEAD} and the last {TAIL} of a stream that are not errors"
REDACTED = "<redacted>"
URL_KEYS = frozenset({"url", "href", "redirected_to"})


WEB_SCHEMES = {"http", "https", "ws", "wss"}
PATH_PARAM = re.compile(r";([^=;/]*)=[^;/]*")


def redact_url(url):
    """The URL with every query value, every ;path parameter value and the fragment replaced by <redacted> (keys,
    origin and path kept) and the userinfo dropped. A URL that is not http(s) or ws(s) is cut to its scheme (a
    javascript: payload, a mailto: address); data: keeps its mime type."""
    if not isinstance(url, str):
        return url
    url = url.lstrip()
    scheme = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):(?!\d+(?:/|$))", url)
    if scheme and scheme.group(1).lower() not in WEB_SCHEMES and not url[scheme.end() :].startswith("//"):
        name = scheme.group(1).lower()
        if name == "about":
            return url.split("#", 1)[0].split("?", 1)[0]
        return "data:" + url[5:].split(",", 1)[0].split(";", 1)[0] if name == "data" else name + ":"
    base, hash_, fragment = url.partition("#")
    base, mark, query = base.partition("?")
    origin = re.match(r"(?:[A-Za-z][A-Za-z0-9+.-]*:)?//", base)
    if origin:
        authority, slash, path = base[origin.end() :].partition("/")
        if "@" in authority:
            authority = authority.rsplit("@", 1)[1]
        base = f"{origin.group(0)}{authority}{slash}{PATH_PARAM.sub(rf';\1={REDACTED}', path)}"
    else:
        base = PATH_PARAM.sub(rf";\1={REDACTED}", base)
    if mark:
        pairs = [part.partition("=") for part in query.split("&") if part]
        query = "&".join(f"{key}={REDACTED}" if equals else REDACTED for key, equals, _value in pairs)
        base += "?" + query
    return base + (f"#{REDACTED}" if hash_ and fragment else "")


def query_pairs(url):
    """[(name, value)] of a URL's query string, as stored in the HAR (values are <redacted> in a redacted URL)."""
    parts = (part.partition("=") for part in url.partition("#")[0].partition("?")[2].split("&") if part)
    return [
        (urllib.parse.unquote_plus(key), urllib.parse.unquote_plus(value) if equals else "")
        for key, equals, value in parts
    ]


# In free text: an absolute URL (an IPv6 host in brackets is part of it), a data:/blob:/javascript:/mailto:/tel: URL
# (whole rest of the word: the payload may contain quotes), or a relative URL that carries a query (/api/me?token=x).
URL_IN_TEXT = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://(?:[^\s\"'<>/?#\[\]@]*@)?(?:\[[^\]\s]*\])?[^\s\"'<>)\]]*"
    r"|(?<!\w)(?:data|blob|javascript|mailto|tel):\S+"
    r"|[\w/.~%-]*\?[\w%.~-]+=[^\s\"'<>]*"
)


def redact_text(text):
    """Free text (a console line, a stack) with every URL in it redacted like redact_url."""
    return URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), text) if isinstance(text, str) else text


def redact_tree(value):
    """A copy of a JSON-like value with every url, href and redirected_to string redacted."""
    if isinstance(value, dict):
        return {
            key: redact_url(item) if key in URL_KEYS and isinstance(item, str) else redact_tree(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    return value


def clipped(text, field):
    """(text cut to CLIP[field], whether it was cut)."""
    text = "" if text is None else str(text)
    limit = CLIP[field]
    return (text[:limit], True) if len(text) > limit else (text, False)


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
        if urllib.parse.urlsplit(environ["BU_CDP_WS"]).scheme not in {"ws", "wss"}:
            return None, "BU_CDP_WS must be a ws: or wss: URL"
        return environ["BU_CDP_WS"], None
    base = environ.get("BU_CDP_URL")
    if not base:
        return None, "neither BU_CDP_WS nor BU_CDP_URL is set, so the DevTools endpoint is unknown"
    if urllib.parse.urlsplit(base).scheme not in {"http", "https"}:
        return None, "BU_CDP_URL must be an http: or https: URL"
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


class Bounded:
    """One stream's records: the first `head`, a ring of the last `tail`, and a ring of the last `reserved` errors
    (so log noise never pushes out the failure that explains the end of a run). Anything else is counted."""

    def __init__(self, head=HEAD, tail=TAIL, reserved=RESERVED):
        self.head, self.tail, self.errors = [], collections.deque(), collections.deque(maxlen=reserved)
        self.head_limit, self.tail_limit, self.seen = head, tail, 0

    def add(self, item, error=False):
        """Keep `item`; returns the records that fell out of the tail ring (they are no longer updated)."""
        item["_seq"] = self.seen
        self.seen += 1
        evicted = []
        if len(self.head) < self.head_limit:
            self.head.append(item)
        else:
            self.tail.append(item)
            while len(self.tail) > self.tail_limit:
                evicted.append(self.tail.popleft())
        if error:
            self.flag(item)
        return evicted

    def flag(self, item):
        if not item.get("_reserved"):
            item["_reserved"] = True
            self.errors.append(item)

    def items(self):
        return sorted({id(i): i for i in (*self.head, *self.errors, *self.tail)}.values(), key=lambda i: i["_seq"])

    def dropped(self):
        return self.seen - len(self.items())


def cut(record, **kinds):
    """Clip the named fields of `record` (field=kind of CLIP); `truncated` lists the fields that were cut."""
    cutting = []
    for field, kind in kinds.items():
        if isinstance(record.get(field), str):
            record[field], was = clipped(record[field], kind)
            if was:
                cutting.append(field)
    if cutting:
        record["truncated"] = cutting
    return record


class CdpEvents:
    """A websocket of its own, attached to one target, that collects console and network events in a thread.

    `connect(url)` returns an object with send(str), recv(timeout=...) (raising TimeoutError when idle) and close();
    the default is websockets.sync.client.connect. Memory is bounded by count (see Bounded), by field size (CLIP) and
    by frame size (MAX_FRAME). If the connection ends before `close()`, `lost()` says when and why."""

    def __init__(
        self, url, target, clock=time.perf_counter, started=None, head=HEAD, tail=TAIL, reserved=RESERVED,
        connect=None, urls="redacted",
    ):
        self.url, self.target, self.clock, self.connect, self.urls = url, target, clock, connect, urls
        self.started = clock() if started is None else started
        self.console_log, self.network_log = Bounded(head, tail, reserved), Bounded(head, tail, reserved)
        self.swallowed, self.lost_info = 0, None
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.requests = {}
        self.ws = self.thread = self.session = None
        self.next_id = 0

    def start(self):
        connect = self.connect
        if connect is None:
            from websockets.sync.client import connect  # noqa: PLC0415 (only needed when evidence is on)
        self.ws = connect(self.url, max_size=MAX_FRAME, open_timeout=5, ping_interval=None, compression=None)
        try:
            self.session = self.request("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            for method in DOMAINS:
                if method != "Network.enable":
                    self.request(method, session=self.session)
                    continue
                try:
                    self.request(method, session=self.session, **BUFFERS)
                except RuntimeError:  # A Chrome that does not know the buffer parameters.
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
            self.process(reply)
        raise TimeoutError(f"{method} was not answered")

    def pump(self):
        while not self.stopping.is_set():
            try:
                raw = self.ws.recv(timeout=0.2)
            except TimeoutError:
                continue
            except Exception as error:
                if not self.stopping.is_set():  # The browser or the tab went away: say so, do not look clean.
                    self.mark_lost(type(error).__name__)
                return
            self.process(raw)

    def process(self, raw):
        """Handle one message; one odd event never ends the capture, but it is counted."""
        try:
            self.handle(json.loads(raw) if isinstance(raw, str | bytes) else raw)
        except Exception:
            with self.lock:
                self.swallowed += 1

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

    def lost(self):
        """{"reason", "t_ms"} when the connection ended before close(), else None."""
        with self.lock:
            return dict(self.lost_info) if self.lost_info else None

    def snapshot(self):
        """(console, network, dropped): the records kept, in order, and what was not kept."""
        with self.lock:
            clean = lambda items: [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]  # noqa: E731
            dropped = {
                "console": self.console_log.dropped(), "network": self.network_log.dropped(), "errors": self.swallowed,
            }
            return clean(self.console_log.items()), clean(self.network_log.items()), dropped

    def link(self, url):
        return redact_url(url) if self.urls == "redacted" else url

    def prose(self, text):
        return redact_text(text) if self.urls == "redacted" else text

    def console(self, record, error):
        self.console_log.add(cut(record, text="text", stack="stack", url="url"), error)

    def mark_lost(self, reason):
        with self.lock:
            if self.lost_info is None:
                self.lost_info = {"reason": reason, "t_ms": self.now_ms()}

    def handle(self, message):
        method = message.get("method")
        # The tab went away while the socket stays open: nothing more will arrive, and "0 errors" would be a lie.
        if method == "Target.detachedFromTarget" and (message.get("params") or {}).get("sessionId") == self.session:
            return self.mark_lost("TargetDetached")
        if method == "Inspector.targetCrashed" and message.get("sessionId") == self.session:
            return self.mark_lost("TargetCrashed")
        if message.get("sessionId") != self.session or method is None:
            return
        method, p = message["method"], message.get("params", {})
        with self.lock:
            if method == "Runtime.consoleAPICalled":
                url, line = top_frame(p.get("stackTrace"))
                level = p.get("type", "log")
                text = " ".join(argument_text(a)[: CLIP["text"] + 1] for a in p.get("args", []))
                self.console(
                    {
                        "t_ms": self.now_ms(),
                        "source": "console",
                        "level": level,
                        "text": self.prose(text),
                        "url": self.link(url),
                        "line": line,
                    },
                    level in {"error", "assert"},
                )
            elif method == "Runtime.exceptionThrown":
                details = p.get("exceptionDetails", {})
                description = (details.get("exception") or {}).get("description") or details.get("text", "")
                description = description[: CLIP["stack"] + 1]
                url, line = top_frame(details.get("stackTrace"))
                where = self.link(url or details.get("url", ""))
                self.console(
                    {
                        "t_ms": self.now_ms(),
                        "source": "exception",
                        "level": "error",
                        "text": self.prose(description.split("\n")[0]),
                        "stack_top": f"{where}:{line or details.get('lineNumber', 0) + 1}",
                        "stack": self.prose(description),
                    },
                    True,
                )
            elif method == "Log.entryAdded":
                entry = p.get("entry", {})
                level = entry.get("level", "info")
                self.console(
                    {
                        "t_ms": self.now_ms(),
                        "source": "browser",
                        "origin": entry.get("source"),
                        "level": level,
                        "text": self.prose(entry.get("text", "")),
                        "url": self.link(entry.get("url", "")),
                        "line": entry.get("lineNumber"),
                    },
                    level in {"error", "assert"},
                )
            elif method.startswith("Network."):
                self.on_network(method, p)

    def settled(self, record):
        """A finished record that failed or answered 4xx/5xx is reserved, so it outlives the tail ring."""
        if record.get("failure") or (record.get("status") or 0) >= 400:
            self.network_log.flag(record)

    def on_network(self, method, p):
        ident = p.get("requestId")
        if method == "Network.requestWillBeSent":
            before = self.requests.get(ident)
            if before is not None and p.get("redirectResponse"):
                self.respond(before, p["redirectResponse"], p.get("type"))
                self.finish(before, p.get("timestamp"))
                before["redirected_to"] = clipped(self.link(p["request"]["url"]), "url")[0]
                self.settled(before)
            record = cut(
                {
                    "method": p["request"]["method"],
                    "url": self.link(p["request"]["url"]),
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
                    "_id": ident,
                },
                url="url",
            )
            self.requests[ident] = record
            for gone in self.network_log.add(record):
                if self.requests.get(gone.get("_id")) is gone:
                    del self.requests[gone["_id"]]
            return
        record = self.requests.get(ident)
        if record is None:
            return
        if method == "Network.responseReceived":
            self.respond(record, p["response"], p.get("type"))
            self.settled(record)
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
            self.settled(record)

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
                "queryString": [{"name": name, "value": value} for name, value in query_pairs(r["url"])],
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
    """The run directory. Create it, `start` the run, `event` as it goes, `close` to write the rest.

    urls="redacted" (default) writes every URL as origin + path with query values and fragment replaced by
    <redacted>; "full" writes them as they were (private use only). capture=False never opens the event connection."""

    def __init__(
        self, directory, screenshots="actions", clean=lambda value: value, clock=time.perf_counter,
        urls="redacted", capture=True,
    ):
        if screenshots not in MODES:
            raise ValueError(f"screenshots must be one of {', '.join(MODES)}")
        if urls not in URL_MODES:
            raise ValueError(f"evidence_urls must be one of {', '.join(URL_MODES)}")
        self.dir, self.mode, self.clean, self.clock, self.urls = Path(directory), screenshots, clean, clock, urls
        self.capture_enabled = capture
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

    def sanitize(self, value):
        return redact_tree(value) if self.urls == "redacted" else value

    def attach(self, target, environ=None, events=None):
        """Start the event connection for this tab. Never raises: console and network are then marked unavailable."""
        if not self.capture_enabled:
            self.capture = {"console": False, "network": False, "reason": "evidence_capture=False"}
            return
        url, why = resolve_endpoint(environ)
        if url:
            stream = (events or CdpEvents)(url, target, clock=self.clock, started=self.started, urls=self.urls)
            try:
                stream.start()
                self.stream, self.capture = stream, {"console": True, "network": True, "reason": None}
                return
            except Exception as error:
                why = f"could not attach the evidence connection: {type(error).__name__}"
        self.capture = {"console": False, "network": False, "reason": why}

    def event(self, event_type, /, **fields):
        record = self.clean(self.sanitize({"t_ms": self.now_ms(), "type": event_type, **fields}))
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
        return self.stream.snapshot()[2] if self.stream else {"console": 0, "network": 0, "errors": 0}

    def lost(self):
        """{"reason", "t_ms"} when the event connection ended before the run did, else None."""
        return self.stream.lost() if self.stream else None

    def write_streams(self):
        """console.jsonl, network.jsonl and network.har from what has been collected so far (scrubbed)."""
        if not self.stream:
            return [], [], {"console": 0, "network": 0, "errors": 0}
        console, network, dropped = self.stream.snapshot()
        console, network = self.clean(self.sanitize(console)), self.clean(self.sanitize(network))
        for name, rows in (("console.jsonl", console), ("network.jsonl", network)):
            (self.dir / name).write_text(
                "".join(json.dumps(row, default=str, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
            )
        har = self.clean(self.sanitize(build_har(network, library_version())))
        (self.dir / "network.har").write_text(json.dumps(har, default=str, indent=1), encoding="utf-8")
        return console, network, dropped

    def settle_end(self):
        """The connection is closed now: put the final dropped counts and any lost capture into run_end."""
        end = next((e for e in reversed(self.events) if e["type"] == "run_end"), None)
        if end is None or not self.stream:
            return
        end["dropped"], end["dropped_part"], end["capture_lost"] = self.dropped(), DROPPED_PART, self.lost()
        self.trace.seek(0)
        self.trace.truncate()
        self.trace.write("".join(json.dumps(e, default=str, ensure_ascii=False) + "\n" for e in self.events))
        self.trace.flush()

    def close(self):
        """Stop the connection, write the streams and report.html. Safe to call twice."""
        if self.closed:
            return
        self.closed = True
        try:
            if self.stream:
                self.stream.close()
                self.capture = {**self.capture, "lost": self.lost()}
            self.settle_end()
            console, network, _dropped = self.write_streams()
            page = render_report(self.events, console, network, self.dir, self.capture)
            (self.dir / "report.html").write_text(self.clean(page), encoding="utf-8")
        finally:
            self.trace.close()
