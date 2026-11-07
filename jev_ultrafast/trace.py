"""trace.jsonl: the machine-readable record of one run (see Agent(evidence_dir=...)).

One JSON object per line. Every event has `t_ms` (milliseconds since the run started) and `type`; the first is
`run_start`, which carries `trace_schema`. `load_trace` reads a file back and raises ValueError for anything this
version does not understand, so a reader never guesses at a layout it was not written for.

Schema 2 adds the `heal` event type and fields on `action` events (step, source, name, option, delta, focus) and in
the locator (digest). It reads schema 1 traces too; a `heal` event inside a schema 1 trace is an error, because a
schema 1 writer never produced one.
"""

import json
from pathlib import Path

SCHEMA = 2
READABLE = (1, 2)
SCHEMA_2_EVENTS = frozenset({"heal"})

# Required fields per event type (beyond t_ms and type). Writers may add fields; readers must ignore unknown ones.
EVENTS = {
    "run_start": (
        "trace_schema", "library_version", "browser", "viewport", "steps", "values", "config", "capture",
    ),
    "observe": ("url", "title", "fingerprint", "elements", "focus", "screenshot"),
    "decision": ("operation", "target", "choice", "top", "confidence", "latency_ms", "usage", "model"),
    "action": ("index", "kind", "label", "choice", "locator", "text_source", "text", "result"),
    "wait": ("kind", "met", "waited_ms", "reason"),
    "check": ("step", "description", "met", "reason", "waited_ms"),
    "step": ("index", "status"),
    "refused": ("label", "kind", "reason", "skipped"),
    "denied": ("reason", "action", "kind"),
    "done_rejected": ("failed",),
    "heal": ("step", "cause", "healed", "model_calls"),
    "run_end": ("status", "stop_reason", "verified", "totals", "screenshot"),
}


def validate_event(event, line=None, schema=SCHEMA):
    """Raise ValueError unless `event` is a well-formed event of `schema` (the trace's own, 1 or 2)."""
    where = f"line {line}: " if line else ""
    if not isinstance(event, dict):
        raise ValueError(f"{where}an event must be a JSON object")
    kind = event.get("type")
    if kind not in EVENTS:
        raise ValueError(f"{where}unknown event type {kind!r}")
    if schema < 2 and kind in SCHEMA_2_EVENTS:
        raise ValueError(f"{where}{kind} is a trace_schema 2 event, but this trace declares trace_schema {schema}")
    t_ms = event.get("t_ms")
    if isinstance(t_ms, bool) or not isinstance(t_ms, int | float) or t_ms < 0:
        raise ValueError(f"{where}{kind} needs a non-negative t_ms")
    missing = [field for field in EVENTS[kind] if field not in event]
    if missing:
        raise ValueError(f"{where}{kind} is missing {', '.join(missing)}")


def load_trace(path):
    """Read and validate a trace.jsonl. Returns the events in order; raises ValueError (with the line number) when
    the file is empty, is not valid JSON lines, does not start with run_start, declares a trace_schema this version
    cannot read (1 and 2 are readable), or holds an event newer than its own schema (heal in a schema 1 trace)."""
    events, schema = [], SCHEMA
    for number, text in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not text.strip():
            continue
        try:
            event = json.loads(text)
        except ValueError as error:
            raise ValueError(f"line {number}: not valid JSON ({error})") from None
        if not events:
            if not isinstance(event, dict) or event.get("type") != "run_start":
                raise ValueError(f"line {number}: a trace starts with run_start")
            schema = event.get("trace_schema")
            if type(schema) is not int or schema not in READABLE:
                raise ValueError(
                    f"line {number}: unknown trace_schema {schema!r}; readable: {', '.join(map(str, READABLE))}"
                )
        validate_event(event, number, schema)
        events.append(event)
    if not events:
        raise ValueError("the trace is empty")
    return events
