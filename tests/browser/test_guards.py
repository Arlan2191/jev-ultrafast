"""Freshness and execution guards against real controls. No model calls or external websites."""

import pytest

from jev_ultrafast.browser import StalePage


def action_named(page, label):
    return next(a for a in page["actions"] if a["label"] == label)


@pytest.fixture
def guards(open_page):
    return open_page("guards.html")


@pytest.fixture
def controls(open_page):
    return open_page("controls.html")


def test_moving_target_clicked_at_its_current_location(guards):
    page = guards.observe(screenshot=False)
    action = action_named(page, "Continue")
    guards.evaluate("document.querySelector('#target').style.transform='translateX(200px)'")
    assert guards.fresh(page), "Movement should use fresh geometry, not another model call"
    guards.act(action, page)
    assert guards.evaluate("window.clicks") == 1


def test_unrelated_offscreen_text_does_not_invalidate(guards):
    page = guards.observe(screenshot=False)
    guards.evaluate("document.querySelector('#outside').textContent='Updated outside the viewport'")
    assert guards.fresh(page)


@pytest.mark.parametrize(
    "expression",
    [
        "document.querySelector('#context').textContent='Cart total: $100'",
        "document.querySelector('#target').setAttribute('aria-label','Delete account')",
        "document.querySelector('#field').value='London'",
        "document.querySelector('#toggle').checked=true",
        "document.querySelector('#target').disabled=true",
        "document.querySelector('#field').readOnly=true",
        "document.querySelector('#target').style.display='none'",
        "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
        "document.querySelector('select').options[1].text='Coastal'",
    ],
    ids=[
        "visible context",
        "accessible label",
        "field property",
        "checkbox property",
        "disabled target",
        "read-only field",
        "hidden target",
        "replaced node",
        "dropdown option",
    ],
)
def test_page_mutation_invalidates(guards, expression):
    page = guards.observe(screenshot=False)
    guards.evaluate(expression)
    assert not guards.fresh(page)


def test_overlay_blocks_click_before_input(guards):
    page = guards.observe(screenshot=False)
    action = action_named(page, "Continue")
    # A textless overlay does not alter the model's semantic state, but must block a click.
    guards.evaluate(
        "const cover=document.createElement('div'); "
        "cover.style.cssText='position:fixed;inset:0;z-index:9999;background:white'; "
        "document.body.append(cover)"
    )
    assert guards.fresh(page)
    with pytest.raises((RuntimeError, StalePage)):
        guards.act(action, page)
    assert not guards.evaluate("window.clicks")


def test_click_guard_accepts_unrelated_updates_terminal_guard_rejects(controls):
    page = controls.observe(screenshot=False)
    buy = action_named(page, "Buy")
    controls.evaluate("document.querySelector('#unrelated').textContent='New unrelated news'")
    assert controls.fresh(page, buy)
    assert not controls.fresh(page)


@pytest.mark.parametrize(
    "expression",
    [
        "document.querySelector('#price').textContent='Total $100'",
        "document.querySelector('#query').value='changed'",
        "document.querySelector('#check').checked=true",
        "document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML",
    ],
    ids=["nearby price", "form value", "form toggle", "target replacement"],
)
def test_action_specific_guard_invalidates(controls, expression):
    page = controls.observe(screenshot=False)
    buy = action_named(page, "Buy")
    controls.evaluate(expression)
    assert not controls.fresh(page, buy)


def test_native_controls_expose_only_supported_operations_and_safe_values(controls):
    actions = controls.observe(screenshot=False)["actions"]
    for role in ("checkbox", "radio"):
        assert {a["kind"] for a in actions if a.get("role") == role} == {"click"}
    assert {a["kind"] for a in actions if a["label"] == "Read only"} == {"click"}
    assert not any(a["label"] == "Disabled" or a.get("value") == "never expose this" for a in actions)
    assert [a["value"] for a in actions if a["kind"] == "select"] == ["Design"]


def test_native_dropdown_selects_an_observed_option(controls):
    page = controls.observe(screenshot=False)
    select = next(a for a in page["actions"] if a["kind"] == "select")
    controls.act(select, page)
    assert controls.evaluate("document.querySelector('#category').value") == "Design"


def test_text_input_waits_for_asynchronous_combobox_suggestions(controls):
    controls.evaluate(
        "document.querySelector('#query').addEventListener('input',()=>setTimeout(()=>{"
        "document.querySelector('#suggestions').innerHTML='<div role=option>Generated</div>'"
        "},60))"
    )
    page = controls.observe(screenshot=False)
    field = next(a for a in page["actions"] if a["kind"] == "fill")
    controls.act(field, page, text="Generated")
    page = controls.observe(screenshot=False)
    assert controls.evaluate("document.querySelector('#query').value") == "Generated"
    assert any(a.get("role") == "option" for a in page["actions"])


def test_navigation_invalidates_the_old_document(controls):
    page = controls.observe(screenshot=False)
    field = next(a for a in page["actions"] if a["kind"] == "fill")
    controls.call("Page.navigate", url="about:blank")
    assert not controls.fresh(page, field)
