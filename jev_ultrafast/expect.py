"""Postconditions the caller states for a step. They are checked in code, never by the model.

    from jev_ultrafast import Step, expect

    Step("Mark it as a favourite", expect=[expect.element(role="button", name="Favourite", state={"pressed": True})])

Each constructor returns an Expectation with a human description (shown to the model and recorded). The agent
evaluates it through the condition waits with a bounded timeout, so a check can wait for a UI that updates late. A
check never raises on timeout; a caller callable that raises is recorded as unmet with its message.
"""

import re
import time
from dataclasses import dataclass, field

from .waits import WaitResult, element_state, non_negative, pattern

POLL_S = 0.05


def describe(value):
    return f"/{value.pattern}/" if isinstance(value, re.Pattern) else f'"{value}"'


def timeout_of(timeout_ms):
    return None if timeout_ms is None else non_negative("timeout_ms", timeout_ms)


@dataclass(frozen=True)
class Expectation:
    kind: str
    description: str
    timeout_ms: int | None = None
    args: dict = field(default_factory=dict)
    fn: object = None

    def evaluate(self, browser, timeout_ms):
        """A WaitResult, waiting at most timeout_ms (0 checks once). The caller picks it: see `timeout_for`."""
        wait = timeout_ms
        if self.kind in {"text", "element"}:
            return browser.probe(self.kind, wait, **self.args)
        start = time.monotonic()
        deadline = start + wait / 1000
        reason = "not checked"
        while True:
            met, reason = self.once(browser)
            if met or time.monotonic() >= deadline:
                break
            time.sleep(POLL_S)
        return WaitResult(met, round((time.monotonic() - start) * 1000), reason)

    def timeout_for(self, default_ms):
        return self.timeout_ms if self.timeout_ms is not None else default_ms

    def once(self, browser):
        try:
            if self.kind == "url":
                href = browser.evaluate("location.href")
                spec = self.args["match"]
                found = bool(spec.search(href)) if isinstance(spec, re.Pattern) else spec in href
                return found, f"url is {href}"
            outcome = self.fn(browser)
        except Exception as error:
            return False, f"{type(error).__name__}: {error}"
        met, reason = outcome if isinstance(outcome, tuple) and len(outcome) == 2 else (outcome, None)
        return met is True, reason or ("met" if met is True else "returned a falsy value")


def contains(name, value):
    if isinstance(value, str) and not value:
        raise ValueError(f"{name} must not be empty")
    pattern(name, value)  # Same validation as the waits: a string, or a JavaScript-compatible compiled regex.
    return value


def text(contains_, *, timeout_ms=None):
    """The visible document text contains a string or compiled regex."""
    contains("contains", contains_)
    kind = "matches" if isinstance(contains_, re.Pattern) else "contains"
    return Expectation(
        "text", f"text {kind} {describe(contains_)}", timeout_of(timeout_ms), {"contains": contains_, "present": True}
    )


def no_text(contains_, *, timeout_ms=None):
    """The visible document text does not contain a string or compiled regex."""
    contains("contains", contains_)
    kind = "match" if isinstance(contains_, re.Pattern) else "contain"
    return Expectation(
        "text",
        f"text does not {kind} {describe(contains_)}",
        timeout_of(timeout_ms),
        {"contains": contains_, "present": False},
    )


def element_args(role, name, state, present):
    if role is None and name is None and not state:
        raise ValueError("element needs a role, a name or a state")
    if role is not None and not isinstance(role, str):
        raise ValueError("role must be a string")
    pattern("name", name, optional=True)
    states = element_state(state)
    parts = [role] if role else []
    parts += [describe(name)] if name is not None else []
    parts += [f"{key}={str(want).lower()}" for key, want in states.items()]
    return {"role": role, "name": name, "state": states or None, "present": present}, " ".join(parts)


def element(role=None, name=None, state=None, *, timeout_ms=None):
    """An element with this role, accessible name (exact or compiled regex) and state is visible."""
    args, described = element_args(role, name, state, True)
    return Expectation("element", f"element {described}", timeout_of(timeout_ms), args)


def no_element(role=None, name=None, state=None, *, timeout_ms=None):
    """No visible element has this role, accessible name and state."""
    args, described = element_args(role, name, state, False)
    return Expectation("element", f"no element {described}", timeout_of(timeout_ms), args)


def url(match, *, timeout_ms=None):
    """The page URL contains a string, or matches a compiled regex (searched, not anchored)."""
    contains("match", match)
    kind = "matches" if isinstance(match, re.Pattern) else "contains"
    return Expectation("url", f"url {kind} {describe(match)}", timeout_of(timeout_ms), {"match": match})


def check(fn, description, *, timeout_ms=None):
    """A caller callable fn(browser) -> bool or (bool, reason). It is polled until it holds or the timeout passes; one
    that raises is unmet with the exception as the reason, and the exception is not propagated."""
    if not callable(fn):
        raise ValueError("check needs a callable")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("check needs a description")
    return Expectation("check", description.strip(), timeout_of(timeout_ms), {}, fn)
