"""A Playwright-style description of the elements a check looks at. Plain data: the page-side function in waits.js
does the finding, so a Locator is frozen, hashable and serialisable (`as_dict`)."""

import re
from dataclasses import dataclass, fields, replace

DEFAULT_TEST_ID_ATTRIBUTE = "data-testid"
ATTRIBUTE = re.compile(r"^[A-Za-z_][\w:.-]*$")


def text_field(name, value):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class Locator:
    """The elements that match every field you set, in document order (hidden ones included).

    role + name   the ARIA role (explicit, or implied by the tag) and the accessible name.
    test_id       the value of a test-id attribute (`test_id_attribute`, default data-testid), always exact.
    label         the text of aria-label, aria-labelledby, or a `<label>` (for= or wrapping).
    text          the smallest element whose visible text contains it (whitespace-normalised).
    css           a selector the caller wrote; the model never produces one.
    exact         name, label and text must equal the text (case-sensitive) instead of containing it
                  (case-insensitive substring, as in Playwright).
    within        another Locator: the match must be a descendant of one of its matches.
    nth           0-based index into the matches in document order. Visibility is NOT applied before it, so a hidden
                  match counts.
    """

    role: str | None = None
    name: str | None = None
    exact: bool = False
    test_id: str | None = None
    test_id_attribute: str = DEFAULT_TEST_ID_ATTRIBUTE
    label: str | None = None
    text: str | None = None
    css: str | None = None
    within: "Locator | None" = None
    nth: int | None = None

    def __post_init__(self):
        for key in ("role", "name", "test_id", "label", "text", "css"):
            text_field(key, getattr(self, key))
        if type(self.exact) is not bool:
            raise ValueError("exact must be True or False")
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
        """JSON-ready, unset fields left out (also what the page receives)."""
        data = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if value is None or value is False:
                continue
            if item.name == "test_id_attribute" and (self.test_id is None or value == DEFAULT_TEST_ID_ATTRIBUTE):
                continue
            data[item.name] = value.as_dict() if isinstance(value, Locator) else value
        if self.test_id is not None:
            data["test_id_attribute"] = self.test_id_attribute
        return data

    def describe(self):
        parts = [self.role] if self.role else []
        for key in ("name", "test_id", "label", "text", "css"):
            value = getattr(self, key)
            if value is not None:
                parts.append(f'{key}="{value}"' if key != "css" else f"css={value}")
        if self.exact:
            parts.append("exact")
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
