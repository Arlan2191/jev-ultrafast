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


def test_a_step_runs_on_the_callers_tab_and_leaves_it_open(owned_tab, monkeypatch):
    stub(monkeypatch, "key:Enter")
    before = page_targets()
    assert owned_tab in before
    agent = Agent(goals="Submit the form", target_id=owned_tab, max_steps=1)
    try:
        list(agent.run())
        assert agent.browser.evaluate("window.submitted") == "a book"
        assert page_targets() == before
    finally:
        agent.close()
    assert page_targets() == before
