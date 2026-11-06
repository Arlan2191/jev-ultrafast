"""Hover and hover-revealed controls on real CSS. The model is stubbed, so no paid call is made."""

import pytest

from jev_ultrafast import Agent
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import StalePage


def find(page, label):
    return next(a for a in page["actions"] if a["label"] == label)


def labels(page):
    return {a["label"] for a in page["actions"]}


def counts(browser):
    return browser.evaluate("window.counts")


def test_hover_revealed_button_is_listed_and_pressed_once(open_page):
    browser = open_page("hover.html")  # Copy fades in over .15s once its row is hovered.
    page = browser.observe(screenshot=False)
    copy = find(page, "Copy")
    assert copy["revealed_by"] == "hover" and copy["kind"] == "click"
    assert "revealed_by" not in find(page, "Reply") and "revealed_by" not in find(page, "More")
    browser.act(copy, page)
    assert counts(browser)["copy"] == 1


def test_focus_within_revealed_button_keeps_its_guard_when_the_opacity_flips(open_page):
    browser = open_page("hover.html")
    page = browser.observe(screenshot=False)
    edit = find(page, "Edit")
    assert edit["revealed_by"] == "hover"
    browser.evaluate("document.querySelector('input').focus()")  # The row now reveals it by :focus-within.
    browser.act(edit, page)
    assert counts(browser)["edit"] == 1


def test_a_control_that_never_reveals_is_surfaced_but_never_pressed(open_page):
    browser = open_page("hover.html")
    browser.reveal_timeout_ms = 100
    page = browser.observe(screenshot=False)
    honeypot = find(page, "Remove everything")
    assert honeypot["revealed_by"] == "hover"
    with pytest.raises(StalePage, match="hidden"):
        browser.act(honeypot, page)
    assert counts(browser)["honeypot"] == 0


def test_hidden_rows_and_hover_menus_are_absent_until_hovered(open_page):
    browser = open_page("hover.html")
    page = browser.observe(screenshot=False)
    assert "Ghost" not in labels(page) and "Archive" not in labels(page)
    more = find(page, "More")
    browser.act({**more, "kind": "hover"}, page)
    opened = browser.observe(screenshot=False)
    assert "Archive" in labels(opened) and "revealed_by" not in find(opened, "Archive")
    assert counts(browser)["archive"] == 0
    browser.act(find(opened, "Archive"), opened)
    assert counts(browser)["archive"] == 1
    assert "Ghost" not in labels(opened)


def test_reveal_candidates_never_displace_visible_controls_at_the_cap(open_page):
    browser = open_page("hover_cap.html")
    page = browser.observe(screenshot=False)
    controls = [a for a in page["actions"] if a["kind"] == "click"]
    assert len(controls) == 250
    assert not any("revealed_by" in a for a in controls)
    assert page["omitted_actions"] == 35  # 285 candidates, 250 kept, all visible ones first


@pytest.fixture
def agent(open_page, fixture_base_url):
    open_page("hover.html")  # Skips (or fails in CI) when no browser is configured.
    agents = []

    def start(**kwargs):
        started = Agent(f"{fixture_base_url}/hover.html", "Use the page", **kwargs)
        agents.append(started)
        return started

    yield start
    for started in agents:
        started.close()


def stub(monkeypatch, plan):
    def choose(page, goal, history):
        operation, label = plan[min(len(history), len(plan) - 1)]
        action = find(page, label)
        return {
            "choice": action["id"],
            "operation": operation,
            "target": "1",
            "confidence": 1.0,
            "probabilities": {action["id"]: 1.0},
            "latency_ms": 1,
            "usage": {},
        }

    monkeypatch.setattr(loop, "choose", choose)


def test_agent_hovers_then_clicks_a_hover_menu_item(agent, monkeypatch):
    stub(monkeypatch, [("HOVER", "More"), ("CLICK", "Archive")])
    run = agent(max_steps=2)
    list(run.run())
    assert [h["kind"] for h in run.state["history"]] == ["hover", "click"]
    assert run.browser.evaluate("window.counts.archive") == 1


def test_agent_click_on_a_reveal_candidate_hovers_first(agent, monkeypatch):
    stub(monkeypatch, [("CLICK", "Copy")])
    run = agent(max_steps=1)
    list(run.run())
    assert [h["action"] for h in run.state["history"]] == ["Copy"]
    assert run.browser.evaluate("window.counts.copy") == 1


def test_agent_refused_honeypot_logs_nothing_as_executed(agent, monkeypatch):
    stub(monkeypatch, [("CLICK", "Remove everything")])
    run = agent(max_steps=1, reveal_timeout_ms=50, max_model_calls=2)
    list(run.run())
    assert run.state["history"] == []
    assert run.browser.evaluate("window.counts.honeypot") == 0
