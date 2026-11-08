"""Locator checks in `expect` on a real page: every locator field, scoping, ordinals, strictness, counts, focus."""

import pytest

from jev_ultrafast import expect
from jev_ultrafast.expect import Locator


@pytest.fixture
def page(open_page):
    browser = open_page("expect_locators.html")

    def run(check, timeout_ms=0):
        return check.evaluate(browser, timeout_ms)

    run.browser = browser
    return run


def met(page, check):
    result = page(check)
    assert result.met is True, result.reason
    return result


def unmet(page, check, reason=None):
    result = page(check)
    assert result.met is False
    if reason:
        assert reason in result.reason
    return result


def test_role_and_name_are_a_case_insensitive_substring_unless_exact(page):
    met(page, expect.visible(Locator(role="button", name="save")))
    met(page, expect.visible(Locator(role="button", name="Save draft", exact=True)))
    unmet(page, expect.visible(Locator(role="button", name="save", exact=True)))
    met(page, expect.visible(Locator(role="heading", name="Orders")))


def test_test_id_uses_the_default_attribute_or_a_configured_one(page):
    met(page, expect.visible(Locator(test_id="save")))
    unmet(page, expect.visible(Locator(test_id="alt-save")))
    met(page, expect.visible(Locator(test_id="alt-save", test_id_attribute="data-cy")))
    unmet(page, expect.visible(Locator(test_id="sav")))  # A test id is exact.


def test_label_by_for_attribute_wrapping_labelledby_and_aria_label(page):
    for label, selector in (
        ("Email address", "#email"),
        ("Phone number", "#phone"),
        ("Nickname", "#nick"),
        ("Search site", "#search"),
    ):
        met(page, expect.count(Locator(label=label), eq=1))
        page.browser.evaluate(f"document.querySelector({selector!r}).focus()")
        met(page, expect.focused(Locator(label=label)))
    unmet(page, expect.focused(Locator(label="Email address")))


def test_text_matches_the_smallest_element_and_normalises_whitespace(page):
    met(page, expect.count(Locator(text="order #42"), eq=1))
    met(page, expect.count(Locator(text="Total: 12 items", exact=True), eq=1))
    unmet(page, expect.count(Locator(text="Order #42", exact=True), eq=1))  # The paragraph has more text.


def test_css_is_caller_authored(page):
    met(page, expect.element_text(Locator(css=".badge"), equals="New"))
    met(page, expect.count(Locator(css="li"), eq=3))


def test_within_scopes_to_descendants_and_nth_is_document_order(page):
    remove = Locator(role="button", name="Remove")
    met(page, expect.count(remove, eq=3))
    met(page, expect.count(Locator(role="button", name="Remove", within=Locator(label="Cart")), eq=2))
    met(page, expect.count(Locator(role="button", name="Remove", within=Locator(label="Wishlist")), eq=1))
    second = Locator(role="button", name="Remove", nth=1)
    met(page, expect.count(second, eq=1))
    page.browser.evaluate("document.querySelectorAll('li button')[1].focus()")
    met(page, expect.focused(second))
    unmet(page, expect.focused(Locator(role="button", name="Remove", nth=0)))
    nested = Locator(css="li", within=Locator(label="Cart"), nth=1)
    met(page, expect.element_text(nested, contains="Ink"))


def test_nth_counts_hidden_matches_too(page):
    page.browser.evaluate("document.querySelector('li').hidden = true")
    met(page, expect.hidden(Locator(css="li", nth=0)))
    met(page, expect.visible(Locator(css="li", nth=1)))
    met(page, expect.count(Locator(css="li"), eq=3))


def test_hidden_is_true_for_absent_and_for_not_visible_but_visible_is_not(page):
    for loc in (Locator(text="Ghost text"), Locator(text="Hidden text"), Locator(text="No such thing")):
        met(page, expect.hidden(loc))
        unmet(page, expect.visible(loc))
    unmet(page, expect.hidden(Locator(text="Order #42")))
    # The two cases differ in the count: a not-visible element still exists.
    met(page, expect.count(Locator(text="Ghost text"), eq=1))
    met(page, expect.count(Locator(text="No such thing"), eq=0))


def test_count_operators(page):
    items = Locator(css="li")
    met(page, expect.count(items, eq=3))
    met(page, expect.count(items, gte=3))
    met(page, expect.count(items, gte=2, lte=3))
    met(page, expect.count(items, lte=5))
    unmet(page, expect.count(items, eq=2), "3")
    unmet(page, expect.count(items, gte=4))
    unmet(page, expect.count(items, lte=2))


def test_a_check_that_needs_one_element_fails_when_the_locator_is_ambiguous(page):
    unmet(page, expect.element_text(Locator(css="li"), contains="Pen"), "ambiguous: 3 matches")
    unmet(page, expect.focused(Locator(css="input")), "ambiguous: 5 matches")
    met(page, expect.element_text(Locator(css="li", nth=0), contains="Pen"))
    unmet(page, expect.element_text(Locator(text="No such thing"), contains="x"), "no matching element")


def test_element_text_conditions_normalise_whitespace_and_combine(page):
    long = Locator(css="#long")
    met(page, expect.element_text(long, equals="Total: 12 items"))
    met(page, expect.element_text(long, contains="12 items", not_contains="13"))
    unmet(page, expect.element_text(long, contains="12 items", not_contains="Total"))
    unmet(page, expect.element_text(long, equals="Total"))


def test_focus_checks_one_element_at_a_time(page):
    save = Locator(test_id="save")
    unmet(page, expect.focused(save))
    page.browser.evaluate("document.querySelector('[data-testid=save]').focus()")
    met(page, expect.focused(save))


def test_a_check_waits_for_the_page_to_catch_up(page):
    page.browser.evaluate("setTimeout(() => document.querySelector('.gone').classList.remove('gone'), 300)")
    met_after = page(expect.visible(Locator(text="Ghost text")), timeout_ms=3000)
    assert met_after.met is True and met_after.waited_ms >= 200


def test_no_horizontal_scroll_is_true_until_something_overflows(page):
    met(page, expect.no_horizontal_scroll())
    page.browser.evaluate("document.getElementById('wide').style.display = 'block'")
    unmet(page, expect.no_horizontal_scroll(), "horizontal")
    page.browser.evaluate("document.getElementById('wide').style.display = 'none'")
    met(page, expect.no_horizontal_scroll())
