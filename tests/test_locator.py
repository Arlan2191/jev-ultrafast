"""Offline contracts for expect locators: validation, serialisation and the check constructors. No browser."""

import json

import pytest

from jev_ultrafast import Step, expect
from jev_ultrafast.expect import Locator


def test_a_locator_needs_at_least_one_selecting_field():
    with pytest.raises(ValueError, match="needs"):
        Locator()
    with pytest.raises(ValueError, match="needs"):
        Locator(exact=True, nth=0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"role": 3},
        {"role": "button", "name": 3},
        {"name": "x"},  # A name alone is not a locator: it belongs to a role.
        {"test_id": ""},
        {"label": ""},
        {"text": ""},
        {"css": ""},
        {"css": 3},
        {"text": "x", "exact": "yes"},
        {"text": "x", "nth": -1},
        {"text": "x", "nth": True},
        {"text": "x", "nth": "1"},
        {"text": "x", "within": "dialog"},
        {"text": "x", "test_id_attribute": ""},
    ],
)
def test_invalid_fields_are_refused_at_construction(kwargs):
    with pytest.raises(ValueError):
        Locator(**kwargs)


def test_a_locator_is_frozen_and_hashable_so_it_can_be_a_dict_key():
    loc = Locator(role="button", name="Save", within=Locator(css="dialog"), nth=1)
    assert {loc: 1}[Locator(role="button", name="Save", within=Locator(css="dialog"), nth=1)] == 1
    with pytest.raises(AttributeError):
        loc.nth = 2


def test_as_dict_is_json_serialisable_and_nests_within():
    loc = Locator(test_id="row", test_id_attribute="data-cy", within=Locator(label="Cart", exact=True), nth=2)
    data = loc.as_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["test_id"] == "row" and data["test_id_attribute"] == "data-cy" and data["nth"] == 2
    assert data["within"]["label"] == "Cart" and data["within"]["exact"] is True
    assert "css" not in data  # Unset fields are left out.


def test_the_description_names_every_field():
    loc = Locator(role="button", name="Remove", within=Locator(label="Cart"), nth=0)
    text = loc.describe()
    for part in ("button", "Remove", "Cart", "within", "nth 0"):
        assert part in text


def test_count_needs_exactly_the_operators_it_is_given_and_at_least_one():
    loc = Locator(css="li")
    with pytest.raises(ValueError, match="eq, gte or lte"):
        expect.count(loc)
    with pytest.raises(ValueError, match="eq"):
        expect.count(loc, eq=-1)
    with pytest.raises(ValueError, match="eq"):
        expect.count(loc, eq=True)
    with pytest.raises(ValueError, match="gte"):
        expect.count(loc, gte=3, lte=2)  # Unsatisfiable.
    assert expect.count(loc, gte=1, lte=3).args["gte"] == 1


def test_element_text_needs_a_condition_and_a_non_empty_string():
    loc = Locator(css="p")
    with pytest.raises(ValueError, match="contains"):
        expect.element_text(loc)
    with pytest.raises(ValueError):
        expect.element_text(loc, contains="")
    with pytest.raises(ValueError):
        expect.element_text(loc, equals=3)


def test_the_checks_take_a_locator_only():
    for build in (expect.visible, expect.hidden, expect.focused):
        with pytest.raises(ValueError, match="Locator"):
            build("button")


def test_check_constructors_describe_themselves_and_keep_their_timeout():
    loc = Locator(role="button", name="Save")
    assert expect.visible(loc, timeout_ms=10).timeout_ms == 10
    for check in (
        expect.visible(loc),
        expect.hidden(loc),
        expect.focused(loc),
        expect.count(loc, eq=2),
        expect.element_text(loc, contains="x"),
        expect.no_horizontal_scroll(),
    ):
        assert check.description and check.cheap
    assert "2" in expect.count(loc, eq=2).description


def test_a_step_accepts_locator_checks_and_renders_them_unchanged():
    check = expect.visible(Locator(text="{title}"))
    step = Step("Open it", expect=[check], params={"title": "T"})
    assert step.expect[0].kind == check.kind


def test_regex_name_text_and_label_are_accepted_validated_and_serialised():
    import re

    loc = Locator(role="button", name=re.compile("^save", re.I | re.S), within=Locator(label=re.compile("cart")))
    data = loc.as_dict()
    assert data["name"] == {"r": "^save", "f": "is"} and data["within"]["label"] == {"r": "cart", "f": ""}
    assert json.loads(json.dumps(data)) == data
    assert hash(loc) == hash(
        Locator(role="button", name=re.compile("^save", re.I | re.S), within=Locator(label=re.compile("cart")))
    )
    assert Locator(text=re.compile("x")).as_dict() == {"text": {"r": "x", "f": ""}}
    with pytest.raises(ValueError, match="flags"):
        Locator(text=re.compile("x", re.ASCII))
    with pytest.raises(ValueError, match="JavaScript"):
        Locator(text=re.compile("(?P<n>x)(?P=n)"))
    with pytest.raises(ValueError):
        Locator(role="button", name=3)


def test_regex_fields_are_not_rendered_but_string_fields_are():
    import re

    loc = Locator(text=re.compile("{x}"), label="{x}")
    out = loc.render({"x": "v"}, set())
    assert out.text.pattern == "{x}" and out.label == "v"


def test_include_hidden_is_validated_and_serialised():
    assert Locator(role="button", include_hidden=True).as_dict() == {"role": "button", "include_hidden": True}
    assert "include_hidden" not in Locator(role="button").as_dict()
    with pytest.raises(ValueError, match="include_hidden"):
        Locator(role="button", include_hidden="yes")


def test_as_dict_carries_the_test_id_attribute_only_with_a_test_id():
    assert Locator(css="p").as_dict() == {"css": "p"}
    assert Locator(test_id="a").as_dict() == {"test_id": "a", "test_id_attribute": "data-testid"}


def test_every_field_that_changes_the_result_changes_the_description():
    import re

    loc = Locator(role="button", name="Save")
    variants = [
        expect.count(loc, eq=1),
        expect.count(loc, eq=1, visible_only=True),
        expect.count(Locator(role="button", name="Save", include_hidden=True), eq=1),
        expect.count(Locator(role="button", name="Save", exact=True), eq=1),
        expect.count(Locator(role="button", name=re.compile("Save")), eq=1),
        expect.count(Locator(role="button", name=re.compile("Save", re.I)), eq=1),
        expect.count(Locator(test_id="a"), eq=1),
        expect.count(Locator(test_id="a", test_id_attribute="data-cy"), eq=1),
    ]
    descriptions = [check.description for check in variants]
    assert len(set(descriptions)) == len(descriptions)
