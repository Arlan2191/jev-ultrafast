"""Postconditions the caller states for a step. They are checked in code, never by the model.

    from jev_ultrafast import Step, expect

    Step("Mark it as a favourite", expect=[expect.element(role="button", name="Favourite", state={"pressed": True})])

Each constructor returns an Expectation with a human description (shown to the model and recorded). The agent
evaluates it through the condition waits with a bounded timeout, so a check can wait for a UI that updates late. A
check never raises on timeout; a caller callable that raises is recorded as unmet with its message.

Two limits to know before relying on a check. A negative check (`no_text`, `no_element`) holds on a blank or unrelated
page and on a dialog that never opened, so pair it with a positive one. Checks read the whole page, not the element
an action touched: a step whose action navigates away is checked against whatever page is open then (add `url`).
"""

import inspect
import re
import time
from dataclasses import dataclass, field, replace

from .waits import WaitResult, element_state, non_negative, pattern

POLL_S = 0.05


def describe(value):
    return f"/{value.pattern}/" if isinstance(value, re.Pattern) else f'"{value}"'


def describe_of(kind, args):
    """The description of a text, element or url check, built from its (possibly rendered) inputs."""
    if kind == "text":
        value = args["contains"]
        regex = isinstance(value, re.Pattern)
        if args["present"]:
            return f"text {'matches' if regex else 'contains'} {describe(value)}"
        return f"text does not {'match' if regex else 'contain'} {describe(value)}"
    if kind == "url":
        value = args["match"]
        return f"url {'matches' if isinstance(value, re.Pattern) else 'contains'} {describe(value)}"
    parts = [args["role"]] if args["role"] else []
    parts += [describe(args["name"])] if args["name"] is not None else []
    parts += [f"{key}={str(want).lower()}" for key, want in (args["state"] or {}).items()]
    return f"{'element' if args['present'] else 'no element'} {' '.join(parts)}"


def timeout_of(timeout_ms):
    return None if timeout_ms is None else non_negative("timeout_ms", timeout_ms)


class Facade:
    """What a check callable receives instead of the Browser: it can read the page and nothing else. `evaluate` runs
    the JavaScript you give it, so keep that read-only too; this keeps a callable from clicking, typing, navigating
    or closing the tab, and does not sandbox JavaScript."""

    def __init__(self, browser):
        self._browser = browser

    def _unwrap(self):
        return self._browser

    def evaluate(self, expression):
        return self._browser.evaluate(expression)

    def url(self):
        return self._browser.evaluate("location.href")

    def probe(self, kind, timeout_ms=0, **args):
        """A WaitResult for an "element" or "text" condition, read once (timeout_ms=0) or waited for."""
        return self._browser.probe(kind, timeout_ms, **args)

    def element(self, role=None, name=None, state=None, present=True):
        """True when an element with this role, name and state is visible now (False with present=False)."""
        return self.probe("element", role=role, name=name, state=state, present=present).met is True

    def text(self, contains, present=True):
        """True when the visible text contains the string or regex now (False with present=False)."""
        return self.probe("text", contains=contains, present=present).met is True


@dataclass(frozen=True)
class Expectation:
    kind: str
    description: str
    timeout_ms: int | None = None
    args: dict = field(default_factory=dict)
    fn: object = None
    auto: bool = False

    def render(self, params, used):
        """This check with its string inputs (contains, name, url match) filled from params and the description built
        from them; a compiled regex stays literal. A caller-written `check` description is rendered instead. `used`
        collects the param names found."""
        from .steps import render

        if self.kind == "check":
            return replace(self, description=render(self.description, params, used))
        key = {"text": "contains", "url": "match", "element": "name"}[self.kind]
        if not isinstance(self.args.get(key), str):
            return self
        args = {**self.args, key: render(self.args[key], params, used)}
        return replace(self, args=args, description=describe_of(self.kind, args))

    @property
    def cheap(self):
        """Safe to read after every action: everything but a caller callable that did not opt in (auto=True)."""
        return self.kind != "check" or self.auto

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
            outcome = self.fn(Facade(browser))
            if inspect.iscoroutine(outcome):
                outcome.close()  # Never awaited: say so instead of leaving the warning.
                return False, "the check returned a coroutine; check callables must be plain functions, not async"
        except Exception as error:
            return False, f"{type(error).__name__}: {error}"
        met, reason = outcome if isinstance(outcome, tuple) and len(outcome) == 2 else (outcome, None)
        if reason is not None:
            reason = str(reason)
        if met is True:
            return True, reason or "met"
        if reason:
            return False, reason
        if met is False:
            return False, "returned a falsy value"
        return False, f"returned {met!r}, a non-bool value (a check must return True or False)"


def contains(name, value):
    if isinstance(value, str) and not value:
        raise ValueError(f"{name} must not be empty")
    pattern(name, value)  # Same validation as the waits: a string, or a JavaScript-compatible compiled regex.
    return value


def text(contains_, *, timeout_ms=None):
    """The visible document text contains a string or compiled regex."""
    contains("contains", contains_)
    args = {"contains": contains_, "present": True}
    return Expectation("text", describe_of("text", args), timeout_of(timeout_ms), args)


def no_text(contains_, *, timeout_ms=None):
    """The visible document text does not contain a string or compiled regex."""
    contains("contains", contains_)
    args = {"contains": contains_, "present": False}
    return Expectation("text", describe_of("text", args), timeout_of(timeout_ms), args)


def element_args(role, name, state, present):
    if role is None and name is None and not state:
        raise ValueError("element needs a role, a name or a state")
    if role is not None and not isinstance(role, str):
        raise ValueError("role must be a string")
    pattern("name", name, optional=True)
    states = element_state(state)
    return {"role": role, "name": name, "state": states or None, "present": present}


def element(role=None, name=None, state=None, *, timeout_ms=None):
    """An element with this role, accessible name (exact or compiled regex) and state is visible."""
    args = element_args(role, name, state, True)
    return Expectation("element", describe_of("element", args), timeout_of(timeout_ms), args)


def no_element(role=None, name=None, state=None, *, timeout_ms=None):
    """No visible element has this role, accessible name and state."""
    args = element_args(role, name, state, False)
    return Expectation("element", describe_of("element", args), timeout_of(timeout_ms), args)


def url(match, *, timeout_ms=None):
    """The page URL contains a string, or matches a compiled regex (searched, not anchored)."""
    contains("match", match)
    args = {"match": match}
    return Expectation("url", describe_of("url", args), timeout_of(timeout_ms), args)


def check(fn, description, auto=False, *, timeout_ms=None):
    """A caller callable fn(page) -> bool or (bool, reason), where page is a read-only Facade (evaluate, url, probe,
    element, text). It is polled every 50 ms until it holds or the timeout passes, and a callable that raises is
    unmet with the exception as the reason (the exception is not propagated). It must be read-only, idempotent and
    fast: it is never interrupted, it runs dozens of times per DONE, and it must not mutate the page.

    A DONE on the step runs it up to expect_timeout_ms (or timeout_ms) plus one more read after the settle. It is left
    out of the cheap read after every action unless auto=True, and a step with a callable that did not opt in never
    completes without a DONE decision. With auto=True it runs once after every executed action."""
    if not callable(fn):
        raise ValueError("check needs a callable")
    if inspect.iscoroutinefunction(fn):
        raise ValueError("check callables must be plain functions, not async")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("check needs a description")
    if type(auto) is not bool:
        raise ValueError("auto must be True or False")
    return Expectation("check", description.strip(), timeout_of(timeout_ms), {}, fn, auto)
