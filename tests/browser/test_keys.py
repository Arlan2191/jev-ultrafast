"""Key presses and navigation on real pages. The model is stubbed, so no paid call is made."""

import pytest

from jev_ultrafast import ActionDenied, Agent
from jev_ultrafast import agent as loop


def stub(monkeypatch, plan):
    """plan: [(operation, action id)], one entry per step; the last repeats."""

    def choose(page, goal, history, refused=()):
        operation, choice = plan[min(len(history), len(plan) - 1)]
        assert any(a["id"] == choice for a in page["actions"]), choice
        return {
            "choice": choice,
            "operation": operation,
            "target": "1",
            "confidence": 1.0,
            "probabilities": {choice: 1.0},
            "latency_ms": 1,
            "usage": {},
        }

    monkeypatch.setattr(loop, "choose", choose)


@pytest.fixture
def start(open_page, fixture_base_url):
    open_page("keys_tab.html")  # Skips (or fails in CI) when no browser is configured.
    agents = []

    def start_(name, **kwargs):
        agent = Agent(f"{fixture_base_url}/{name}", "Use the page", **kwargs)
        agents.append(agent)
        return agent

    yield start_
    for agent in agents:
        agent.close()


def key(name):
    return ("PRESS_KEY", f"key:{name}")


def test_focused_element_is_marked_and_history_length_is_exposed(start):
    agent = start("keys_tab.html")
    page = agent.state["page"]
    first = next(a for a in page["actions"] if a["label"] == "First" and a["kind"] == "fill")
    second = next(a for a in page["actions"] if a["label"] == "Second" and a["kind"] == "fill")
    assert first["focused"] is True and "focused" not in second
    assert page["focus"]["label"] == "First" and page["history_length"] >= 1


def test_enter_submits_a_focused_field_without_a_submit_button(start, monkeypatch):
    stub(monkeypatch, [key("Enter")])
    agent = start("keys_enter.html", max_steps=1)
    list(agent.run())
    assert agent.browser.evaluate("window.submitted") == "a book"
    assert agent.state["history"][0]["kind"] == "key"


def test_escape_closes_a_dialog_that_listens_for_keydown(start, monkeypatch):
    stub(monkeypatch, [key("Escape")])
    agent = start("keys_escape.html", max_steps=1)
    assert agent.browser.evaluate("document.getElementById('d').hidden") is False
    list(agent.run())
    assert agent.browser.evaluate("window.dismissed") is True


def test_tab_and_shift_tab_move_focus_between_inputs(start, monkeypatch):
    stub(monkeypatch, [key("Tab"), key("Shift+Tab")])
    agent = start("keys_tab.html", max_steps=2)
    agent.command("tick")
    assert agent.browser.evaluate("document.activeElement.id") == "b"
    assert agent.state["page"]["focus"]["label"] == "Second"
    agent.command("tick")
    assert agent.browser.evaluate("document.activeElement.id") == "a"


def test_a_declared_control_k_opens_a_palette_that_checks_ctrl_key(start, monkeypatch):
    stub(monkeypatch, [key("Control+K")])
    agent = start("keys_shortcut.html", max_steps=1, keys=("Control+K",))
    list(agent.run())
    assert agent.browser.evaluate("window.opened") == 1
    assert agent.browser.evaluate("document.getElementById('palette').hidden") is False


def test_space_toggles_a_focused_checkbox(start, monkeypatch):
    stub(monkeypatch, [key("Space")])
    agent = start("keys_space.html", max_steps=1)
    list(agent.run())
    assert agent.browser.evaluate("document.getElementById('c').checked") is True


def test_back_returns_to_the_previous_page_and_is_offered_only_with_history(start, monkeypatch):
    agent = start("nav_one.html", max_steps=2)
    link = next(a for a in agent.state["page"]["actions"] if a["label"] == "Next page")
    stub(monkeypatch, [("CLICK", link["id"]), ("BACK", "back")])
    list(agent.run())
    assert agent.state["history"][0]["url"].endswith("nav_two.html")
    assert agent.browser.evaluate("document.title") == "Page one"
    assert [h["kind"] for h in agent.state["history"]] == ["click", "back"]


def test_reload_resets_an_in_page_counter(start, monkeypatch):
    stub(monkeypatch, [("RELOAD", "reload")])
    agent = start("nav_one.html", max_steps=1)
    agent.browser.evaluate("window.counter = 5")
    list(agent.run())
    assert agent.browser.evaluate("window.counter") == 0


def test_goto_opens_a_declared_page(start, monkeypatch, fixture_base_url):
    stub(monkeypatch, [("GOTO", "goto:Second")])
    agent = start("nav_one.html", max_steps=1, urls={"Second": f"{fixture_base_url}/nav_two.html"})
    list(agent.run())
    assert agent.state["page"]["url"].endswith("nav_two.html")
    assert agent.state["history"][0]["kind"] == "goto"


def test_a_deny_hook_refuses_enter_while_delete_has_focus(start, monkeypatch):
    stub(monkeypatch, [key("Enter")])

    def hook(action, page, text):
        if action["kind"] == "key" and "delete" in action["focused"]["label"].lower():
            raise ActionDenied("no keys on Delete")

    agent = start("keys_delete.html", before_action=hook)
    states = list(agent.run())
    assert states[-1]["status"] == "denied"
    assert agent.browser.evaluate("window.deleted") == 0
    assert agent.state["history"] == []


def test_enter_on_a_focused_button_activates_it_when_allowed(start, monkeypatch):
    stub(monkeypatch, [key("Enter")])
    agent = start("keys_delete.html", max_steps=1)
    list(agent.run())
    assert agent.browser.evaluate("window.deleted") == 1


def test_overlays_of_every_kind_list_none_of_their_buttons(start):
    agent = start("overlays.html")
    labels = {a["label"] for a in agent.state["page"]["actions"]}
    assert labels & {"Plain hover button"}  # A plain transparent toolbar still lists.
    assert not labels & {
        "Native dialog button",
        "Modal button",
        "Popover button",
        "Alert button",
        "Role dialog button",
        "Fixed button",
    }
