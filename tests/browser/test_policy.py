"""A before_action hook on a real page. The model is stubbed, so no paid call is made."""

import pytest

from jev_ultrafast import ActionDenied, Agent
from jev_ultrafast import agent as loop


def stub_choice(monkeypatch, label):
    def choose(page, goal, history):
        action = next(a for a in page["actions"] if a["label"] == label)
        return {
            "choice": action["id"],
            "operation": "CLICK",
            "target": "1",
            "confidence": 1.0,
            "probabilities": {action["id"]: 1.0},
            "latency_ms": 1,
            "usage": {},
        }

    monkeypatch.setattr(loop, "choose", choose)


@pytest.fixture
def start(open_page, fixture_base_url):
    open_page("policy.html")  # Skips (or fails in CI) when no browser is configured.
    agents = []

    def start_(**kwargs):
        agent = Agent(f"{fixture_base_url}/policy.html", "Press the button", **kwargs)
        agents.append(agent)
        return agent

    yield start_
    for agent in agents:
        agent.close()


def test_deny_hook_leaves_the_real_click_counter_at_zero(start, monkeypatch):
    stub_choice(monkeypatch, "Delete account")

    def hook(action, page, text):
        if "delete" in action["label"].lower():
            raise ActionDenied("deletes are not allowed")

    agent = start(before_action=hook)
    states = list(agent.run())
    assert states[-1]["status"] == "denied"
    assert states[-1]["denied"]["action"] == "Delete account"
    assert agent.state["history"] == []
    assert agent.browser.evaluate("window.deleted") == 0


def test_allowed_click_executes(start, monkeypatch):
    stub_choice(monkeypatch, "Save")
    agent = start(before_action=lambda action, page, text: None, max_steps=1)
    next(agent.run())
    assert agent.browser.evaluate("window.saved") == 1
