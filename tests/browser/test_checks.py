"""Steps, checked postconditions and caller-provided values on real pages. The model is stubbed: no paid call."""

import json
import statistics
import time

import pytest

from jev_ultrafast import Agent, Step, Value, expect
from jev_ultrafast import agent as loop

SECRET = "tok-4f9c1d7a-secret"


class Script:
    """A stubbed model. `policy(executed, history)` returns None for DONE, else (kind, label, operation, value)."""

    def __init__(self, monkeypatch, policy):
        self.policy, self.pages, self.choices = policy, [], []
        monkeypatch.setattr(loop, "choose", self.choose)

    def choose(self, page, goal, history, **extra):
        self.pages.append(json.dumps(page, default=str))
        executed = sum(h["kind"] != "done_rejected" for h in history)
        want = self.policy(executed, history)
        if want is None:
            choice, operation, value = "DONE", "DONE", None
        else:
            kind, label, operation, value = (*want, None)[:4] if len(want) == 3 else want
            choice = next(a["id"] for a in page["actions"] if a["kind"] == kind and a["label"] == label)
        self.choices.append(choice)
        return {
            "choice": choice,
            "operation": operation,
            "target": "1",
            "value": value,
            "confidence": 1.0,
            "probabilities": {choice: 1.0},
            "latency_ms": 1,
            "usage": {},
        }

    def dones(self):
        return self.choices.count("DONE")


@pytest.fixture
def start(open_page, fixture_base_url):
    open_page("smoke.html")  # Skips (or fails in CI) when no browser is configured.
    agents = []

    def start_(name, goals, **kwargs):
        agent = Agent(f"{fixture_base_url}/{name}", goals, **kwargs)
        agents.append(agent)
        return agent

    yield start_
    for agent in agents:
        agent.close()


def run(agent):
    return list(agent.run())[-1]


def pressed(agent):
    return agent.browser.evaluate("document.querySelector('button').getAttribute('aria-pressed')")


def upvote_policy(executed, history):
    rejected = any(h["kind"] == "done_rejected" for h in history)
    if executed == 0 and not rejected:
        return None  # The model claims DONE before it pressed anything.
    return ("click", "Upvote", "CLICK") if executed == 0 else None


def test_a_done_before_the_toggle_is_pressed_is_rejected_then_accepted_after_it_is(start, monkeypatch):
    script = Script(monkeypatch, upvote_policy)
    pressed_check = expect.element(role="button", name="Upvote", state={"pressed": True})
    agent = start("steps_toggle.html", [Step("Upvote it", expect=[pressed_check])], auto_complete=False)
    final = run(agent)
    assert final["status"] == "done" and final["verified"] is True
    kinds = [h["kind"] for h in final["history"]]
    assert kinds == ["done_rejected", "click"]
    assert final["history"][0]["failed"][0]["description"] == 'element button "Upvote" pressed=true'
    assert pressed(agent) == "true" and script.dones() == 2


def test_a_three_step_journey_completes_by_auto_complete_with_no_done_decision(start, monkeypatch):
    plan = [
        ("fill", "Query", "TYPE_TEXT", "term"),
        ("click", "Search", "CLICK"),
        ("click", "Open Cats", "CLICK"),
        ("click", "Favourite", "CLICK"),
    ]
    script = Script(monkeypatch, lambda executed, history: plan[executed])
    steps = [
        Step("Search for the term", expect=[expect.text("Results for cats")]),
        Step("Open the Cats result", expect=[expect.url("#cats"), expect.text("Cats detail")]),
        Step("Favourite it", expect=[expect.element(role="button", name="Favourite", state="pressed")]),
    ]
    agent = start("steps_journey.html", steps, values={"term": "cats"})
    final = run(agent)
    assert final["status"] == "done" and final["verified"] is True and final["plan_index"] == 3
    assert script.dones() == 0 and len(script.choices) == 4
    assert [s["status"] for s in final["steps"]] == ["done"] * 3
    assert [s["actions"] for s in final["steps"]] == [2, 1, 1]
    assert agent.browser.evaluate("document.getElementById('q').value") == "cats"
    assert final["text_calls"] == [] and final["history"][0]["text_source"] == "value:term"


def test_a_check_that_needs_the_page_to_catch_up_passes_within_the_expect_timeout(start, monkeypatch):
    Script(monkeypatch, lambda executed, history: ("click", "Upvote", "CLICK") if executed == 0 else None)
    steps = [Step("Upvote it", expect=[expect.element(role="button", name="Upvote", state={"pressed": True})])]
    agent = start(
        "steps_toggle.html?late=400", steps, auto_complete=False, action_settle_timeout_ms=0, expect_timeout_ms=5000
    )
    final = run(agent)
    assert final["status"] == "done" and final["verified"] is True
    assert final["steps"][0]["rejections"] == 0
    assert 100 <= final["steps"][0]["checks"][0]["waited_ms"] < 5000


def test_a_check_that_is_never_met_is_rejected_after_waiting_its_timeout(start, monkeypatch):
    Script(monkeypatch, lambda executed, history: None)
    steps = [Step("Upvote it", expect=[expect.element(role="button", name="Upvote", state="pressed", timeout_ms=300)])]
    agent = start("steps_toggle.html", steps, max_rejections=1)
    final = run(agent)
    assert final["status"] == "failed" and final["stop_reason"] == "expectations"
    assert final["steps"][0]["checks"][0]["waited_ms"] >= 250


def test_no_element_waits_for_a_banner_to_close(start, monkeypatch):
    script = Script(
        monkeypatch, lambda executed, history: ("click", "Accept cookies", "CLICK") if executed == 0 else None
    )
    steps = [Step("Accept", expect=[expect.no_element(role="button", name="Accept cookies"), expect.text("Content")])]
    # The banner closes 400 ms after the click and the post-action settle is off, so the first read still sees it.
    agent = start("steps_banner.html?late=400", steps, action_settle_timeout_ms=0, expect_timeout_ms=5000)
    final = run(agent)
    assert final["status"] == "done" and final["verified"] is True
    assert final["steps"][0]["rejections"] == 0 and script.dones() == 1
    assert 150 <= final["steps"][0]["checks"][0]["waited_ms"] < 5000  # the DONE waited for the banner to go


def test_a_press_that_is_taken_back_after_the_click_does_not_end_done_or_verified(start, monkeypatch):
    script = Script(monkeypatch, lambda executed, history: ("click", "Upvote", "CLICK") if executed == 0 else None)
    pressed_check = expect.element(role="button", name="Upvote", state={"pressed": True})
    # Off: the check is read at once after the click, while the optimistic press is still showing.
    agent = start(
        "steps_flaky.html?revert=300",
        [Step("Upvote it", expect=[pressed_check])],
        action_settle_timeout_ms=0,
        expect_timeout_ms=400,
        max_rejections=2,
    )
    final = run(agent)
    assert final["status"] == "failed" and final["stop_reason"] == "expectations" and final["verified"] is False
    assert final["steps"][0]["status"] == "failed" and final["steps"][0]["verified"] is False
    kinds = [h["kind"] for h in final["history"]]
    assert kinds[0] == "click" and kinds.count("done_rejected") == 2
    assert pressed(agent) == "false" and script.dones() == 1  # the stable pass rejected the auto-complete, not a DONE


def test_a_press_that_stays_does_pass_the_stable_read(start, monkeypatch):
    Script(monkeypatch, lambda executed, history: ("click", "Upvote", "CLICK") if executed == 0 else None)
    agent = start(
        "steps_toggle.html",
        [Step("Upvote it", expect=[expect.element(role="button", name="Upvote", state={"pressed": True})])],
    )
    final = run(agent)
    assert final["status"] == "done" and final["verified"] is True and final["steps"][0]["rejections"] == 0


def test_url_after_a_navigation(start, monkeypatch):
    Script(monkeypatch, lambda executed, history: ("click", "Next page", "CLICK") if executed == 0 else None)
    steps = [Step("Go on", expect=[expect.url("nav_two.html")])]
    final = run(start("nav_one.html", steps, auto_complete=False))
    assert final["status"] == "done" and final["verified"] is True


def test_a_raising_check_is_a_failed_check_not_an_exception(start, monkeypatch):
    Script(monkeypatch, lambda executed, history: None)
    boom = expect.check(lambda browser: 1 / 0, "the impossible holds", timeout_ms=1)
    final = run(start("steps_toggle.html", [Step("x", expect=[boom])], max_rejections=1))
    assert final["status"] == "failed"
    check = final["steps"][0]["checks"][0]
    assert check["met"] is False and "ZeroDivisionError" in check["reason"]


def test_a_check_callable_can_read_the_page(start, monkeypatch):
    Script(monkeypatch, lambda executed, history: ("click", "Upvote", "CLICK") if executed == 0 else None)
    voted = expect.check(lambda b: b.evaluate("document.getElementById('status').textContent") == "Voted", "voted")
    final = run(start("steps_toggle.html", [Step("vote", expect=[voted])]))
    assert final["status"] == "done" and final["verified"] is True


def test_a_secret_is_typed_for_real_and_absent_from_everything_the_agent_keeps(start, monkeypatch):
    plan = [("fill", "Access code", "TYPE_TEXT", "code"), ("click", "Save", "CLICK")]
    script = Script(monkeypatch, lambda executed, history: plan[executed])
    hooked = []
    steps = [
        Step(
            "Enter the code",
            values={"code": Value(SECRET, secret=True)},
            expect=[
                expect.check(
                    lambda b: b.evaluate("document.getElementById('code').value") == SECRET, "typed", auto=True
                )
            ],
        ),
        Step("Save it", expect=[expect.check(lambda b: b.evaluate("window.saved") == SECRET, "saved", auto=True)]),
    ]
    agent = start(
        "steps_code.html", steps, before_action=lambda action, page, text: hooked.append((text, action["kind"]))
    )
    final = run(agent)
    assert final["status"] == "done" and final["verified"] is True
    assert agent.browser.evaluate("document.getElementById('code').value") == SECRET  # The field has the real text.
    assert agent.browser.evaluate("document.getElementById('echo').textContent") == f"Code is {SECRET}"
    assert hooked[0] == ("<secret:code>", "fill")
    assert SECRET not in json.dumps(final, default=str)
    assert SECRET not in json.dumps({k: v for k, v in agent.state.items() if k != "browser"}, default=str)
    assert all(SECRET not in page for page in script.pages)
    assert "<secret:code>" in agent.state["page"]["text"]


def test_auto_complete_adds_little_to_an_action_that_does_not_complete_the_step(start, monkeypatch):
    Script(monkeypatch, lambda executed, history: None)
    unmet = expect.element(role="button", name="Upvote", state="pressed")
    agent = start("steps_toggle.html", [Step("x", expect=[unmet])])
    plain = start("steps_toggle.html", [Step("x")])
    samples, baseline = [], []
    for _ in range(5):
        began = time.perf_counter()
        assert agent.run_checks(True)  # Unmet: it returns the failing check.
        samples.append((time.perf_counter() - began) * 1000)
        began = time.perf_counter()
        plain.browser.evaluate("1")
        baseline.append((time.perf_counter() - began) * 1000)
    median = statistics.median(samples)
    print(f"auto-complete check, unmet: median {median:.1f} ms of {[round(s, 1) for s in samples]};")
    print(f"one bare browser round trip: median {statistics.median(baseline):.1f} ms")
    assert median < 100
