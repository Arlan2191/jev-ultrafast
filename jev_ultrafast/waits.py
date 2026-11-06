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


PYTHON_ONLY = {"A": "\\A (use ^)", "Z": "\\Z (use $)"}


def js_incompatible(source):
    """The first Python-only construct in a regex that JavaScript's RegExp would reject or read differently, or None.
    Walks the pattern, so escapes and character classes are not mistaken for syntax."""
    i, in_class, previous = 0, False, ""
    while i < len(source):
        c = source[i]
        if c == "\\":
            nxt = source[i + 1 : i + 2]
            if nxt in PYTHON_ONLY and not in_class:
                return PYTHON_ONLY[nxt]
            i, previous = i + 2, "atom"
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class, previous = True, "atom"
        elif c == "(" and source.startswith("(?", i):
            tail = source[i + 2 : i + 4]
            if tail[:1] == "P":
                return "(?P<name>...) and (?P=name) (use (?<name>...) and \\k<name>)"
            if tail[:1] in {">", "(", "#"} or re.match(r"[aiLmsux-]", tail[:1]):
                return "inline flags, atomic groups, conditionals and comments (pass re flags instead)"
            i, previous = i + 2, ""
            continue
        elif c == "+" and previous == "quantifier":
            return "possessive quantifiers"
        previous = "quantifier" if c in "*+?}" else "atom"
        i += 1
    return None


def pattern(name, value, *, optional=False):
    """A string (exact, or substring for contains/ignore_urls) or a compiled regex, as the page-side matcher spec.
    A regex is evaluated by the page's JavaScript RegExp: only i, s and m flags and JavaScript-compatible syntax work,
    anything else raises ValueError here instead of failing in the page."""
    if value is None and optional:
        return None
    if isinstance(value, re.Pattern):
        if value.flags & (re.ASCII | re.VERBOSE | re.LOCALE):
            raise ValueError(f"{name}: only the re.IGNORECASE, re.DOTALL and re.MULTILINE flags are supported")
        unsupported = js_incompatible(value.pattern)
        if unsupported:
            raise ValueError(f"{name}: {unsupported} is not supported; the page evaluates it as a JavaScript RegExp")
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


def attribute_names(values):
    """wait_ignore_attributes: attribute names (a bare string is one name) whose changes are not DOM activity."""
    if isinstance(values, str):
        values = [values]
    values = tuple(values or ())
    if not all(isinstance(v, str) and v for v in values):
        raise ValueError("wait_ignore_attributes must be attribute names")
    return values


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


def needs_settle(activity):
    """Whether the post-action settle should run a full settled(): a counted request is in flight or started since
    the last read, or a short timer was armed since then. The page-side counters already apply ignore_urls, the timer
    threshold and the epoch (a new document is not a trigger by itself: its load was already awaited)."""
    return bool(activity) and bool(
        activity.get("inflight") or activity.get("timers") or activity.get("new_started")
    )
