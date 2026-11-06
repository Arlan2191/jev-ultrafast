"""Steps: one goal at a time, with optional checked postconditions and caller-provided values."""

import html
import json
import urllib.parse
from dataclasses import dataclass, field

from .expect import Expectation
from .questions import GENERATE, NONE  # NONE: "no declared value fits", offered when values but no text helper exist.

MAX_TEXT = 2000
MIN_SECRET = 4
STATUSES = ("pending", "active", "done", "failed", "blocked")


def redaction(name):
    return f"<secret:{name}>"


def secret_forms(text):
    """The ways a page can echo a secret back: as typed, percent-encoded (a GET form), plus-encoded, JSON-escaped and
    HTML-escaped. Case changes, collapsed whitespace and a cut-off tail are not covered (see the README)."""
    forms = {
        text,
        urllib.parse.quote(text),
        urllib.parse.quote(text, safe=""),
        urllib.parse.quote_plus(text),
        json.dumps(text)[1:-1],
        html.escape(text),
        html.escape(text, quote=False),
    }
    return sorted(forms, key=lambda form: -len(form))


@dataclass(frozen=True)
class Value:
    """Text the caller already knows. The model picks it by name and never sees it. secret=True also keeps it out of
    history, state, hook arguments and the page the model reads (a secret shorter than 4 characters is refused: it
    could not be redacted without mangling other text; a short one still replaces every occurrence, so "1234" also
    changes "$1234.00" in what the model reads)."""

    text: str
    secret: bool = False

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text or len(self.text) > MAX_TEXT:
            raise ValueError(f"A value must be a non-empty string of at most {MAX_TEXT} characters")
        if type(self.secret) is not bool:
            raise ValueError("secret must be True or False")
        if self.secret and len(self.text) < MIN_SECRET:
            raise ValueError(f"A secret value needs at least {MIN_SECRET} characters to be redacted safely")


@dataclass(frozen=True)
class Step:
    """One goal. `expect` checks (see jev_ultrafast.expect) decide whether it is done; `values` override the agent's."""

    goal: str
    expect: tuple = ()
    values: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("A step needs a goal")
        expect = (self.expect,) if isinstance(self.expect, Expectation) else tuple(self.expect or ())
        if not all(isinstance(e, Expectation) for e in expect):
            raise ValueError("Step expect must hold jev_ultrafast.expect constructors")
        object.__setattr__(self, "goal", self.goal.strip())
        object.__setattr__(self, "expect", expect)
        object.__setattr__(self, "values", validate_values(self.values))


def validate_values(values):
    """{name: str | Value} -> {name: Value}. Names are shown to the model, so they must be short and plain."""
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError("values must be a dict of name -> str or Value")
    checked = {}
    for name, value in values.items():
        plain = isinstance(name, str) and name.strip() and name == name.strip() and len(name) <= 64
        if not plain or name in {GENERATE, NONE}:
            raise ValueError(
                f"A value name must be a plain non-empty string of at most 64 characters, not {GENERATE} or {NONE}"
            )
        checked[name] = value if isinstance(value, Value) else Value(value) if isinstance(value, str) else None
        if checked[name] is None:
            raise ValueError(f"Value {name!r} must be a string or a Value")
    return checked


def parse_goals(goals):
    """A string, or a list of strings and Steps, as a non-empty list of Steps."""
    if isinstance(goals, str) and not goals.strip():
        raise ValueError("Supply a task")
    items = [goals] if isinstance(goals, str | Step) else list(goals) if isinstance(goals, list | tuple) else None
    if items is None or not items:
        raise ValueError("Supply a task")
    steps = [item if isinstance(item, Step) else Step(item) if isinstance(item, str) else None for item in items]
    if None in steps:
        raise ValueError("goals must be a string, or a list of strings and Steps")
    return steps
