"""Offline contracts for step goal parameters: a goal names something that only exists in this run."""

import pytest

from jev_ultrafast import Step, Value, expect


def test_the_goal_and_check_descriptions_are_rendered():
    step = Step(
        "Open the row titled {title}",
        expect=[expect.check(lambda _p: True, "the row {title} is open")],
        params={"title": "Order 17"},
    )
    assert step.goal == "Open the row titled Order 17"
    assert step.expect[0].description == "the row Order 17 is open"
    assert step.template == "Open the row titled {title}"
    assert step.check_templates == ("the row {title} is open",)


def test_braces_are_escaped_by_doubling():
    step = Step("Type {{x}} into the box of {name}", params={"name": "Ada"})
    assert step.goal == "Type {x} into the box of Ada"
    assert step.template == "Type {{x}} into the box of {name}"


def test_a_step_without_params_is_literal():
    step = Step("Type {x} into the box")
    assert step.goal == "Type {x} into the box" and step.template == step.goal and step.params == {}


def test_a_param_value_with_braces_is_not_formatted_again():
    assert Step("Open {title}", params={"title": "{a}"}).goal == "Open {a}"


@pytest.mark.parametrize(
    "goal, params, message",
    [
        ("Open {title}", {}, "no param"),
        ("Open {title}", {"title": "A", "extra": "B"}, "unused"),
        ("Open {title}", {"title": ""}, "non-empty string"),
        ("Open {title}", {"title": 5}, "non-empty string"),
        ("Open {title}", {"title": "x" * 2001}, "non-empty string"),
        ("Open {0}", {"0": "A"}, "plain"),
        ("Open {title.x}", {"title": "A"}, "plain"),
        ("Open {title!r}", {"title": "A"}, "plain"),
        ("Open {title:>5}", {"title": "A"}, "plain"),
        ("Open {title", {"title": "A"}, "placeholder"),
        ("Open title", "A", "dict"),
    ],
)
def test_params_are_validated_at_construction(goal, params, message):
    with pytest.raises(ValueError, match=message):
        Step(goal, params=params)


def test_a_check_placeholder_needs_a_param():
    with pytest.raises(ValueError, match="no param"):
        Step("Open it", expect=[expect.check(lambda _p: True, "{title} is open")], params={"other": "A"})


def test_a_param_may_be_used_only_by_a_check():
    step = Step("Open it", expect=[expect.check(lambda _p: True, "{title} is open")], params={"title": "A"})
    assert step.goal == "Open it" and step.expect[0].description == "A is open"


def test_a_param_cannot_be_named_like_a_declared_value():
    with pytest.raises(ValueError, match="declared value"):
        Step("Open {query}", values={"query": "dune"}, params={"query": "A"})


def test_a_param_value_cannot_hold_a_declared_secret():
    secret = "hunter2-correct-horse"
    with pytest.raises(ValueError, match="secret"):
        Step("Open {title}", values={"pw": Value(secret, secret=True)}, params={"title": f"x {secret}"})
