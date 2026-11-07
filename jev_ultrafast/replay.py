"""Replay a recorded run (trace.jsonl, see jev_ultrafast.trace) without model calls.

Pure functions only: load and check a trace against the steps the caller passes, extract each step's executed actions,
and map a re-resolved locator to the action the current page offers. The agent loop (agent.py) drives them.
"""

from dataclasses import dataclass
from pathlib import Path

from .evidence import redact_url
from .trace import READABLE, load_trace, validate_event

ELEMENT = {"click", "fill", "select", "hover"}
CONTROL = {"key", "back", "reload", "goto", "scroll", "wait"}
REPLAYABLE = ELEMENT | CONTROL
SECRET_MARK = "<secret:"
OPERATIONS = {
    "click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT", "hover": "HOVER", "key": "PRESS_KEY", "goto": "GOTO",
}
ACCEPTED_RULES = {"role_name_scope", "test_id", "id"}
FOCUS_FIELDS = ("role", "label", "tag", "type")


def normalise(text):
    return " ".join(str(text).split())


@dataclass(frozen=True)
class Planned:
    """One executed action of the recorded run, as far as a replay needs it."""

    kind: str
    choice: str
    locator: dict | None = None
    text_source: str | None = None
    text: str | None = None
    value_name: str | None = None
    option: str | None = None
    truncated: tuple = ()
    url: str | None = None
    delta: int | float | None = None
    focus: dict | None = None
    focus_recorded: bool = False  # False for a schema 1 trace: it never wrote the focus of a key.

    @property
    def operation(self):
        return OPERATIONS.get(self.kind, self.choice.upper())


def load(source):
    """The validated events of a trace given as a path or as already loaded events."""
    if isinstance(source, str | Path):
        return load_trace(source)
    if not isinstance(source, list | tuple) or not source:
        raise ValueError("replay must be a trace.jsonl path or a list of loaded trace events")
    events = list(source)
    schema = events[0].get("trace_schema") if isinstance(events[0], dict) else None
    if not isinstance(events[0], dict) or events[0].get("type") != "run_start" or schema not in READABLE:
        raise ValueError(
            f"a trace starts with run_start and trace_schema {' or '.join(map(str, READABLE))}"
        )
    for number, event in enumerate(events, 1):
        validate_event(event, number, schema)
    return events


def require_verified(events):
    end = events[-1]
    if end["type"] != "run_end" or end["status"] != "done" or end["verified"] is not True:
        found = end["status"] if end["type"] == "run_end" else "no run_end"
        raise ValueError(
            f"replay_requires_verified: only a trace that ended done with every step verified can be replayed ({found})"
        )


def secrecy(values):
    """{name: secret flag}: a trace stores the flag, the caller's Value has `.secret`."""
    return {name: bool(getattr(v, "secret", v)) for name, v in (values or {}).items()}


def match(events, steps, values):
    """Raise ValueError unless the trace was recorded for these steps (goals, check descriptions) and value names."""
    start = events[0]
    recorded = start["steps"]
    if len(recorded) != len(steps):
        raise ValueError(f"replay_mismatch: the trace has {len(recorded)} steps, the run has {len(steps)}")
    for number, (old, step) in enumerate(zip(recorded, steps, strict=True), 1):
        if normalise(old["goal"]) != normalise(step.goal):
            raise ValueError(f"replay_mismatch: step {number} goal differs from the trace")
        checks = [normalise(e.description) for e in step.expect]
        if [normalise(c) for c in old["checks"]] != checks:
            raise ValueError(f"replay_mismatch: step {number} checks differ from the trace")
        if set(old.get("values") or {}) != set(step.values):
            raise ValueError(f"replay_mismatch: step {number} value names differ from the trace")
        if secrecy(old.get("values")) != secrecy(step.values):
            raise ValueError(f"replay_mismatch: step {number} value secrecy differs from the trace")
    if set(start["values"]) != set(values):
        raise ValueError("replay_mismatch: value names differ from the trace")
    if secrecy(start["values"]) != secrecy(values):
        raise ValueError("replay_mismatch: value secrecy differs from the trace")


def plan(events, count):
    """[[Planned, ...] per step]: the executed actions of the run, grouped by the step they ran in."""
    steps, current = [[] for _ in range(count)], 0
    for event in events:
        if event["type"] == "step" and event["status"] == "active":
            current = event["index"]
        elif event["type"] == "action" and event["kind"] in REPLAYABLE:
            index = event.get("step", current)
            if type(index) is int and 0 <= index < count:
                steps[index].append(
                    Planned(
                        kind=event["kind"],
                        choice=event["choice"],
                        locator=event.get("locator"),
                        text_source=event.get("text_source"),
                        text=event.get("text"),
                        value_name=event.get("value_name"),
                        option=event.get("option"),
                        truncated=tuple(event.get("truncated") or ()),
                        url=event.get("url"),
                        delta=event.get("delta"),
                        focus=event.get("focus"),
                        focus_recorded="focus" in event,
                    )
                )
    return steps


def prepare(source, steps, values):
    """(plan, number of recorded actions without a locator): the whole check at construction."""
    events = load(source)
    require_verified(events)
    match(events, steps, values)
    planned = plan(events, len(steps))
    return planned, events[-1]["totals"].get("locator_missing", 0)


def unresolved(found):
    """Why a resolve result cannot be used (None when it can)."""
    if found is None:
        return "not_found"
    rule = found.get("rule")
    if rule == "ambiguous":
        return "ambiguous"
    if rule == "hint":
        return "hint_only"
    if rule not in ACCEPTED_RULES or not found.get("verified"):
        return "unverified"
    if type(found.get("node")) is not int:
        return "not_observed"
    return None


def hoverable(action):
    return action["kind"] == "click" and bool(
        action.get("revealed_by") or action.get("hover_signal") or "expanded" in action
    )


def element_action(planned, actions, node):
    """(the page's action, None) for a recorded element action on the node the locator resolved to, or (None, cause)."""
    same = [a for a in actions if a.get("node") == node]
    if planned.kind == "hover":
        found = [a for a in same if hoverable(a)]
    elif planned.kind == "select":
        if planned.option is None:
            return None, "option_unavailable"  # A schema 1 trace did not record which option.
        found = [a for a in same if a["kind"] == "select" and a.get("value") == planned.option]
    else:
        found = [a for a in same if a["kind"] == planned.kind]
    if len(found) != 1:
        return None, "kind_not_offered" if not found else "ambiguous"
    return found[0], None


def focus_of(focus):
    """The fields of a focused element a replay compares (the recorded `focus` and the page's `focus` alike)."""
    return {k: (focus or {}).get(k) for k in FOCUS_FIELDS} if focus else None


def control_action(planned, actions, focus=None):
    """(the page's action, None) for a recorded key, back, reload, goto, scroll or wait, or (None, cause). The id
    carries the key chord, the declared page name or the direction, so one lookup by id checks the declaration. A
    goto must open the URL it opened (query values aside), a scroll the distance it scrolled, and a key goes to the
    element that had focus when it was recorded: `focus` is the page's focus now."""
    found = [a for a in actions if a["id"] == planned.choice and a["kind"] == planned.kind]
    if len(found) != 1:
        return None, "control_not_offered"
    action = found[0]
    if planned.kind == "goto" and redact_url(planned.url) != redact_url(action.get("url")):
        return None, "goto_url_changed"
    if planned.kind == "scroll" and planned.delta != action.get("delta"):
        return None, "scroll_changed"
    if planned.kind == "key":
        if not planned.focus_recorded:
            return None, "focus_changed"
        if focus_of(planned.focus) != focus_of(focus):
            return None, "focus_changed"
    return action, None


def fill_text(planned, values):
    """(decision fields, None) for the text a recorded fill typed, or (None, cause). A caller value comes from the
    caller (a secret was never recorded); a generated text is replayed only when whole and without a secret marker."""
    if planned.text_source and planned.text_source.startswith("value:"):
        name = planned.value_name or planned.text_source[len("value:") :]
        return ({"value": name}, None) if name in values else (None, "value_missing")
    if "text" in planned.truncated or not isinstance(planned.text, str):
        return None, "text_unavailable"
    if SECRET_MARK in planned.text:
        return None, "generated_secret"
    return {"text": planned.text}, None


class Replay:
    """What one replaying Agent keeps: the recorded plan per step, where it is, and which steps were healed."""

    def __init__(self, source, steps, values, heal):
        self.plan, self.locator_missing = prepare(source, steps, values)
        self.heal = heal
        self.cursor, self.failures = [-1, 0], 0
        self.healing = None  # The step handed to the model; replay resumes at the next one.
        self.causes = {}  # step index -> why it diverged

    def describe(self):
        return {"heal": self.heal, "actions": [len(p) for p in self.plan], "locator_missing": self.locator_missing}

    def counts(self, state=None, model_calls=0):
        done = sum(r["status"] == "done" for r in (state or {}).get("steps", []))
        replayed = sum(h.get("source") == "replay" for h in (state or {}).get("history", []))
        return {
            "steps_replayed": max(done - len(self.causes), 0),
            "steps_healed": len(self.causes),
            "actions_replayed": replayed,
            "model_calls": model_calls,
        }
