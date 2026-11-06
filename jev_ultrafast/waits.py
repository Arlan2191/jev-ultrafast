"""Condition waits that cost no model call: result object, argument checks and the post-action trigger."""

import re
from dataclasses import asdict, dataclass

STATES = ("checked", "expanded", "selected", "pressed", "disabled")
MAX_WAITS = 100


@dataclass(frozen=True)
class WaitResult:
    """met is False when the timeout ended the wait. A wait never raises on timeout."""

    met: bool
    waited_ms: int
    reason: str

    def as_dict(self):
        return asdict(self)


def non_negative(name, value):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def positive(name, value):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def pattern(name, value, *, optional=False):
    """A string (exact, or substring for contains/ignore_urls) or a compiled regex, as the page-side matcher spec."""
    if value is None and optional:
        return None
    if isinstance(value, re.Pattern):
        flags = "i" if value.flags & re.IGNORECASE else ""
        flags += "s" if value.flags & re.DOTALL else ""
        flags += "m" if value.flags & re.MULTILINE else ""
        return {"r": value.pattern, "f": flags}
    if isinstance(value, str):
        return {"s": value}
    raise ValueError(f"{name} must be a string or a compiled regex")


def ignore_patterns(values):
    """ignore_urls: a list of substrings or compiled regexes. A bare string is one substring, not a list of letters."""
    if isinstance(values, str | re.Pattern):
        values = [values]
    return [pattern("ignore_urls entry", v) for v in values or ()]


def element_state(state):
    """A state name, a list of names (all true), or {name: bool}."""
    if state is None:
        return {}
    if isinstance(state, str):
        state = [state]
    if not isinstance(state, dict):
        state = {name: True for name in state}
    for key, want in state.items():
        if key not in STATES or type(want) is not bool:
            raise ValueError(f"state must use {', '.join(STATES)} with true or false")
    return dict(state)


def needs_settle(activity, seen):
    """Whether the post-action settle should run a full settled(): requests or short timers are pending, or a
    request started since the last read. A new document is not a trigger by itself (its load was already awaited)."""
    if not activity:
        return False
    if activity.get("inflight") or activity.get("timers"):
        return True
    same_document = bool(seen) and activity.get("epoch") == seen.get("epoch")
    return same_document and activity.get("started", 0) > seen.get("started", 0)
