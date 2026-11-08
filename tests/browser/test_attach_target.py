"""Driving a tab the caller opened: the agent acts on it, creates no tab and leaves it open. The model is stubbed."""

import os

import pytest
from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from jev_ultrafast import Agent
from jev_ultrafast import agent as loop


def page_targets():
    return {t["targetId"] for t in cdp("Target.getTargets", session_id=None)["targetInfos"] if t["type"] == "page"}


def stub(monkeypatch, choice):
    def choose(page, goal, history, refused=()):
        assert any(a["id"] == choice for a in page["actions"]), choice
        return {
            "choice": choice, "operation": "PRESS_KEY", "target": "1", "confidence": 1.0,
            "probabilities": {choice: 1.0}, "latency_ms": 1, "usage": {},
        }

    monkeypatch.setattr(loop, "choose", choose)


@pytest.fixture
def owned_tab(fixture_base_url):
    if not os.environ.get("BU_CDP_URL"):
        message = "BU_CDP_URL is not set"
        if os.environ.get("JEV_REQUIRE_BROWSER") == "1":
            pytest.fail(message)
        pytest.skip(message)
    ensure_daemon()
    target = cdp("Target.createTarget", session_id=None, url=f"{fixture_base_url}/keys_enter.html")["targetId"]
    yield target
    try:
        cdp("Target.closeTarget", session_id=None, targetId=target)
    except Exception:
        pass


def test_a_step_runs_on_the_callers_tab_and_leaves_it_open(owned_tab, monkeypatch, tmp_path):
    stub(monkeypatch, "key:Enter")
    before = page_targets()
    assert owned_tab in before
    run = tmp_path / "ev"
    agent = Agent(goals="Submit the form", target_id=owned_tab, evidence_dir=run, max_steps=1)
    try:
        list(agent.run())
        assert agent.browser.evaluate("window.submitted") == "a book"
        assert page_targets() == before
        # The size the caller gave the page is measured, not overridden.
        assert agent.browser.viewport == tuple(agent.browser.evaluate("[innerWidth, innerHeight]"))
    finally:
        agent.close()
    assert page_targets() == before
    assert (run / "trace.jsonl").stat().st_size > 0
    assert list((run / "screenshots").glob("*.jpg"))


def test_a_second_agent_attaches_to_the_same_tab_after_the_first_closed(owned_tab, monkeypatch):
    stub(monkeypatch, "key:Enter")
    before = page_targets()
    first = Agent(goals="Submit the form", target_id=owned_tab, max_steps=1)
    try:
        list(first.run())
    finally:
        first.close()
    assert page_targets() == before
    second = Agent(goals="Submit the form", target_id=owned_tab, max_steps=1)
    try:
        assert page_targets() == before
        # The tracker is installed once and still answers waits (the second attach re-evaluated it).
        assert second.browser.evaluate("!!window.__jevWait") is True
        assert second.wait_for("dom_quiet", quiet_ms=100, timeout_ms=5000).met is True
        assert second.wait_for("network_idle", idle_ms=100, timeout_ms=5000).met is True
    finally:
        second.close()
    assert page_targets() == before
