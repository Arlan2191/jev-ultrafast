"""A Playwright-style description of the elements a check looks at. Plain data: the page-side function in waits.js
does the finding, so a Locator is frozen, hashable and serialisable (`as_dict`)."""

import re
from dataclasses import dataclass, fields, replace

from .waits import pattern

DEFAULT_TEST_ID_ATTRIBUTE = "data-testid"
ATTRIBUTE = re.compile(r"^[A-Za-z_][\w:.-]*$")


def text_field(name, value):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} must be a non-empty string")
    return value


def matching_field(name, value):
    """A non-empty string, or a JavaScript-compatible compiled regex (the same rule as the other checks)."""
    if isinstance(value, re.Pattern):
        pattern(name, value)
        return value
    return text_field(name, value)


def wire(value):
    """What the page receives for a name, label or text: the string, or the `{r, f}` regex form of `pattern()`."""
    return pattern("pattern", value) if isinstance(value, re.Pattern) else value


def shown(value):
    """A name, label or text in a description: quoted, or /regex/flags."""
    if isinstance(value, re.Pattern):
        spec = wire(value)
        return f"/{spec['r']}/{spec['f']}"
    return f'"{value}"'


@dataclass(frozen=True)
class Locator:
    """The elements that match every field you set, in document order (hidden ones included).

    role + name   the ARIA role (explicit, or implied by the tag) and the accessible name. Like Playwright's
                  getByRole, elements hidden from the accessibility tree (display:none, visibility:hidden, inside
                  aria-hidden="true") are left out unless `include_hidden=True`.
    test_id       the value of a test-id attribute (`test_id_attribute`, default data-testid), always exact.
    label         the text of aria-label, aria-labelledby, or a `<label>` (for= or wrapping).
    text          the smallest element whose text content contains it (whitespace-normalised, hidden text included).
    css           a selector the caller wrote; the model never produces one.
    exact         name, label and text must equal the text (case-sensitive) instead of containing it
                  (case-insensitive substring, as in Playwright). Ignored for a compiled regex, which is searched
                  in the whitespace-normalised text with its own flags.
    include_hidden  role locators also match elements hidden from the accessibility tree.
    within        another Locator: the match must be a descendant of one of its matches.
    nth           0-based index into the matches in document order. Visibility is NOT applied before it, so a hidden
                  match counts.
    """

    role: str | None = None
    name: "str | re.Pattern | None" = None
    exact: bool = False
    test_id: str | None = None
    test_id_attribute: str = DEFAULT_TEST_ID_ATTRIBUTE
    label: "str | re.Pattern | None" = None
    text: "str | re.Pattern | None" = None
    css: str | None = None
    within: "Locator | None" = None
    nth: int | None = None
    include_hidden: bool = False

    def __post_init__(self):
        for key in ("role", "test_id", "css"):
            text_field(key, getattr(self, key))
        for key in ("name", "label", "text"):
            matching_field(key, getattr(self, key))
        for key in ("exact", "include_hidden"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"{key} must be True or False")
        if not isinstance(self.test_id_attribute, str) or not ATTRIBUTE.match(self.test_id_attribute):
            raise ValueError("test_id_attribute must be an attribute name")
        if self.within is not None and not isinstance(self.within, Locator):
            raise ValueError("within must be a Locator")
        if self.nth is not None and (type(self.nth) is not int or self.nth < 0):
            raise ValueError("nth must be a non-negative integer")
        if self.name is not None and self.role is None:
            raise ValueError("a name belongs to a role: pass role as well")
        if not (self.role or self.test_id or self.label or self.text or self.css):
            raise ValueError("a Locator needs a role, test_id, label, text or css")

    def as_dict(self):
        """JSON-ready, unset fields left out (also what the page receives). A regex is `{r, f}`."""
        data = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if value is None or value is False or (item.name == "test_id_attribute" and self.test_id is None):
                continue
            if item.name == "within":
                value = value.as_dict()
            data[item.name] = wire(value)
        return data

    def describe(self):
        """Every field that changes which elements match, so a changed check is a replay mismatch."""
        parts = [self.role] if self.role else []
        for key in ("name", "test_id", "label", "text"):
            value = getattr(self, key)
            if value is not None:
                parts.append(f"{key}={shown(value)}")
        if self.css is not None:
            parts.append(f"css={self.css}")
        if self.test_id is not None and self.test_id_attribute != DEFAULT_TEST_ID_ATTRIBUTE:
            parts.append(f"attribute={self.test_id_attribute}")
        if self.exact:
            parts.append("exact")
        if self.include_hidden:
            parts.append("include_hidden")
        if self.within is not None:
            parts.append(f"within ({self.within.describe()})")
        if self.nth is not None:
            parts.append(f"nth {self.nth}")
        return " ".join(parts)

    def render(self, params, used):
        """This locator with its name, test_id, label and text filled from params (css is left alone)."""
        from .steps import render

        changes = {
            key: render(getattr(self, key), params, used)
            for key in ("name", "test_id", "label", "text")
            if isinstance(getattr(self, key), str)
        }
        if self.within is not None:
            changes["within"] = self.within.render(params, used)
        return replace(self, **changes)
