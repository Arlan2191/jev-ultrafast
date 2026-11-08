"""Steps: one goal at a time, with optional checked postconditions and caller-provided values."""

import html
import json
import string
import unicodedata
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
    """One goal. `expect` checks (see jev_ultrafast.expect) decide whether it is done; `values` override the agent's.

    `params={name: str}` fills `{name}` placeholders in the goal and in the string inputs of the checks (`contains`,
    `name`, `url`; a compiled regex stays literal) or the description of an `expect.check` (str.format style, `{{` and
    `}}` for literal braces) with text that only exists in this run. `goal`, the check inputs and the descriptions are
    the RENDERED text, which is all the model, the evidence and the hook see; `template` and `check_templates` keep
    the placeholders, and a replay compares those, never the param values. Without `params` (or with an empty dict)
    the goal and the checks are literal. `param_checked` is True when some check uses a param."""

    goal: str
    expect: tuple = ()
    values: dict = field(default_factory=dict)
    params: dict | None = None
    template: str = field(default="", init=False, compare=False)
    check_templates: tuple = field(default=(), init=False, compare=False)
    param_checked: bool = field(default=False, init=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("A step needs a goal")
        expect = (self.expect,) if isinstance(self.expect, Expectation) else tuple(self.expect or ())
        if not all(isinstance(e, Expectation) for e in expect):
            raise ValueError("Step expect must hold jev_ultrafast.expect constructors")
        values = validate_values(self.values)
        params = validate_params(self.params, values)
        template = self.goal.strip()
        templates = tuple(e.description for e in expect)
        checked = False
        if params is not None:
            used = set()
            goal = render(template, params, used)
            rendered = []
            for e in expect:
                found = set()
                rendered.append(e.render(params, found))
                used |= found
                checked = checked or bool(found)
            expect = tuple(rendered)
            if unused := sorted(set(params) - used):
                raise ValueError(f"Step params {', '.join(unused)} are unused by the goal and the checks")
            forms = [f for v in values.values() if v.secret for f in secret_forms(v.text)]
            if any(form in text for text in params.values() for form in forms):
                raise ValueError("A step param contains a declared secret value; params are shown to the model")
        else:
            goal = template
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "expect", expect)
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "params", params or {})
        object.__setattr__(self, "template", template)
        object.__setattr__(self, "check_templates", templates)
        object.__setattr__(self, "param_checked", checked)


def validate_params(params, values):
    """None (no params, or an empty dict: the goal is literal) or {name: single-line text}. Params are data shown to
    the model as part of its instruction."""
    if params is None:
        return None
    if not isinstance(params, dict):
        raise ValueError("params must be a dict of name -> str")
    if not params:
        return None
    for name, text in params.items():
        if not (isinstance(name, str) and name.isidentifier() and len(name) <= 64):
            raise ValueError("A param name must be a plain identifier of at most 64 characters")
        if name in values:
            raise ValueError(f"Param {name!r} is named like a declared value; a name is one or the other")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError(f"Param {name!r} must be a string with text, of at most {MAX_TEXT} characters")
        if any(unicodedata.category(c)[0] == "C" or unicodedata.category(c) in {"Zl", "Zp"} for c in text):
            raise ValueError(f"Param {name!r} must be one line without control characters")
    return dict(params)


def render(text, params, used):
    """`text` with its `{name}` placeholders filled from params; the names found are added to `used`."""
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError as error:
        raise ValueError(f"Bad placeholder in {text!r}: {error}") from None
    for _literal, name, spec, conversion in parts:
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            raise ValueError(f"A placeholder must be a plain name like {{title}}, in {text!r}")
        if name not in params:
            raise ValueError(f"Placeholder {{{name}}} has no param")
        used.add(name)
    return "".join(literal + (params[name] if name is not None else "") for literal, name, _s, _c in parts)


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
