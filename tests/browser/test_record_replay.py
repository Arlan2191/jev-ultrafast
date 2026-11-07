"""Record a run once, then replay it on real pages without the model, and heal where the page changed. The model is a
stub that knows the answers while recording and fails the test when it is asked during a replay: no paid call."""

import os
import statistics
import time

import pytest

from jev_ultrafast import ActionDenied, Agent, Step, Value, expect
from jev_ultrafast import agent as loop
from jev_ultrafast.trace import load_trace

SECRET = "tok-4f9c1d7a-secret"
PAGE = "replay_journey.html"
SECOND = "Open the second Dune"
ROW_B = "window.__jevFast.ids.get(document.querySelector('[data-row=b] button'))"


def steps():
    return [
        Step("Search for a book", expect=expect.text("Results for")),
        Step(SECOND, expect=expect.text("Opened row b")),
        Step("Pin it", expect=expect.element(role="button", name="Pin", state={"pressed": True})),
    ]


class Model:
    """mode "record": answers every step. mode "heal": answers only `allowed` goals and fails on any other."""

    def __init__(self, monkeypatch, mode="record", allowed=()):
        self.mode, self.allowed, self.calls, self.agent = mode, set(allowed), [], None
        monkeypatch.setattr(loop, "choose", self.choose)

    def pick(self, page, kind, label=None, node=None):
        return next(
            a["id"] for a in page["actions"]
            if a["kind"] == kind and (label is None or a["label"] == label) and (node is None or a.get("node") == node)
        )

    def choose(self, page, goal, history, **extra):
        self.calls.append(goal)
        if self.mode == "forbid" or (self.mode == "heal" and goal not in self.allowed):
            raise AssertionError(f"the model was asked about {goal!r}")
        value = None
        if goal == "Search for a book":
            typed = any(h["kind"] == "fill" for h in history)
            choice = self.pick(page, "click", "Search") if typed else self.pick(page, "fill", "Search")
            operation, value = ("CLICK", None) if typed else ("TYPE_TEXT", "query")
        elif goal == SECOND:
            node = self.agent.browser.evaluate(ROW_B)
            choice, operation = self.pick(page, "click", node=node), "CLICK"
        else:
            label = next(a["label"] for a in page["actions"] if a["label"].startswith("Pin"))
            choice, operation = self.pick(page, "click", label), "CLICK"
        return {
            "choice": choice, "operation": operation, "target": "1", "value": value, "confidence": 1.0,
            "probabilities": {choice: 1.0}, "latency_ms": 1, "usage": {},
        }


@pytest.fixture
def lab(open_page, fixture_base_url, monkeypatch, tmp_path):
    open_page("smoke.html")  # Skips (or fails in CI) when no browser is configured.
    agents, counter = [], iter(range(1000))

    class Lab:
        def run(self, model_mode="record", allowed=(), url=PAGE, goals=None, chooser=None, **kwargs):
            model = Model(monkeypatch, model_mode, allowed)
            if chooser:
                model.calls = chooser.calls = []
                monkeypatch.setattr(loop, "choose", chooser)
            kwargs.setdefault("values", {"query": "dune"})
            kwargs.setdefault("evidence_dir", tmp_path / f"run{next(counter)}")
            kwargs.setdefault("evidence_capture", False)
            kwargs.setdefault("screenshots", "none")
            started = time.perf_counter()
            agent = Agent(f"{fixture_base_url}/{url}", goals or steps(), **kwargs)
            agents.append(agent)
            model.agent = agent
            if chooser:
                chooser.agent = agent
            for _ in agent.run():
                pass
            wall = (time.perf_counter() - started) * 1000
            return agent, agent.snapshot(), model, wall

        def trace(self, agent):
            return agent.evidence.dir / "trace.jsonl"

        def clicked(self, agent):
            return agent.browser.evaluate("window.__clicked")

    yield Lab()
    for agent in agents:
        agent.close()


def sources(state):
    return [h["source"] for h in state["history"]]


def test_a_replay_on_the_same_page_is_done_verified_and_free(lab):
    agent, state, model, _wall = lab.run()
    assert state["status"] == "done" and state["verified"] and len(model.calls) >= 3
    assert lab.clicked(agent) == ["b"]
    agent.close()

    again, replayed, forbidden, _wall = lab.run("forbid", replay=lab.trace(agent))
    assert replayed["status"] == "done" and replayed["verified"] is True, replayed["stop_reason"]
    assert forbidden.calls == [] and replayed["decisions"] == [] and replayed["text_calls"] == []
    assert sources(replayed) == ["replay"] * 4
    assert replayed["replay"] == {"steps_replayed": 3, "steps_healed": 0, "actions_replayed": 4, "model_calls": 0}
    assert lab.clicked(again) == ["b"]
    assert again.browser.evaluate("document.getElementById('q').value") == "dune"
    again.close()
    end = load_trace(lab.trace(again))[-1]
    assert end["totals"]["jev_calls"] == 0 and end["totals"]["replay_actions"] == 4


def test_replay_wall_time_next_to_the_recorded_run(lab):
    recorded, replayed = [], []
    for _ in range(5):
        agent, state, _model, wall = lab.run()
        assert state["status"] == "done"
        agent.close()
        recorded.append(wall)
        again, state, model, wall = lab.run("forbid", replay=lab.trace(agent))
        assert state["status"] == "done" and state["verified"] and model.calls == []
        replayed.append(wall)
    report = (
        "### Record and replay wall time (3 steps, 4 actions, stubbed model with no latency)\n"
        f"- recorded runs ms: {', '.join(f'{t:.0f}' for t in recorded)}; median {statistics.median(recorded):.0f}\n"
        f"- replayed runs ms: {', '.join(f'{t:.0f}' for t in replayed)}; median {statistics.median(replayed):.0f}\n"
        "- model calls: recorded 4 or more per run, replayed 0\n"
    )
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(report)
    assert statistics.median(replayed) < 30000


def test_a_renamed_button_heals_only_its_own_step(lab):
    agent, state, _model, _wall = lab.run()
    agent.close()
    again, healed, model, _wall = lab.run("heal", [SECOND], url=PAGE + "?variant=renamed", replay=lab.trace(agent))
    assert healed["status"] == "done" and healed["verified"] is True, healed["stop_reason"]
    assert model.calls == [SECOND]  # Steps 1 and 3 asked nobody.
    assert sources(healed) == ["replay", "replay", "model", "replay"]
    assert healed["replay"]["steps_healed"] == 1 and healed["replay"]["steps_replayed"] == 2
    assert lab.clicked(again) == ["b"]
    again.close()
    heal = next(e for e in load_trace(lab.trace(again)) if e["type"] == "heal")
    assert heal["step"] == 1 and heal["healed"] is True and heal["model_calls"] == 1
    assert heal["cause"] in {"not_found", "hint_only"}

    # The healed run's own trace replays the renamed page without the model.
    third, replayed, forbidden, _wall = lab.run(
        "forbid", url=PAGE + "?variant=renamed", replay=lab.trace(again)
    )
    assert replayed["status"] == "done" and forbidden.calls == [] and sources(replayed) == ["replay"] * 4


def test_a_row_inserted_above_the_target_heals_and_never_opens_the_wrong_row(lab):
    agent, _state, _model, _wall = lab.run()
    agent.close()
    again, healed, model, _wall = lab.run("heal", [SECOND], url=PAGE + "?variant=inserted", replay=lab.trace(agent))
    assert healed["status"] == "done" and healed["verified"] is True, healed["stop_reason"]
    assert model.calls == [SECOND]
    assert lab.clicked(again) == ["b"]  # Not the inserted row, not the row that moved into the recorded position.
    again.close()
    heal = next(e for e in load_trace(lab.trace(again)) if e["type"] == "heal")
    assert heal["cause"] == "ambiguous" and heal["step"] == 1


def test_without_heal_the_renamed_page_diverges_and_nothing_more_runs(lab):
    agent, _state, _model, _wall = lab.run()
    agent.close()
    again, state, model, _wall = lab.run("forbid", url=PAGE + "?variant=renamed", replay=lab.trace(agent), heal=False)
    assert state["status"] == "diverged" and state["verified"] is False
    assert state["stop_reason"] in {"not_found at step 2", "hint_only at step 2"}
    assert model.calls == [] and len(state["history"]) == 2  # The search only.
    assert lab.clicked(again) == []
    assert again.browser.evaluate("document.querySelector('#detail').hidden") is True
    again.close()
    assert load_trace(lab.trace(again))[-1]["status"] == "diverged"


def test_a_deny_hook_stops_a_replayed_press_like_a_model_action(lab):
    agent, _state, _model, _wall = lab.run()
    agent.close()

    def deny(action, page, text):
        if action["label"] == "Pin":
            raise ActionDenied("pinning is not allowed")

    again, state, model, _wall = lab.run("forbid", replay=lab.trace(agent), before_action=deny)
    assert state["status"] == "denied" and state["denied"]["action"] == "Pin"
    assert model.calls == [] and len(state["history"]) == 3
    assert again.browser.evaluate("document.querySelector('#detail button').getAttribute('aria-pressed')") == "false"


def test_a_secret_stays_out_of_the_trace_and_is_typed_again_from_the_caller(lab):
    secret = {"query": Value(SECRET, secret=True)}
    agent, state, _model, _wall = lab.run(values=secret)
    assert state["status"] == "done"
    assert agent.browser.evaluate("document.getElementById('q').value") == SECRET
    agent.close()
    assert SECRET not in lab.trace(agent).read_text()

    again, replayed, model, _wall = lab.run("forbid", values=secret, replay=lab.trace(agent))
    assert replayed["status"] == "done" and model.calls == []
    assert again.browser.evaluate("document.getElementById('q').value") == SECRET
    again.close()
    assert SECRET not in lab.trace(again).read_text()


def test_a_reordered_list_heals_and_never_opens_the_row_now_in_the_recorded_position(lab):
    agent, _state, _model, _wall = lab.run()
    agent.close()
    # Rows a and b swap: the count, the names and the scopes are as recorded, and position 2 holds another row.
    again, healed, model, _wall = lab.run("heal", [SECOND], url=PAGE + "?variant=swapped", replay=lab.trace(agent))
    assert healed["status"] == "done" and healed["verified"] is True, healed["stop_reason"]
    assert model.calls == [SECOND]
    assert lab.clicked(again) == ["b"]  # Not "a", which sits where "b" was.
    again.close()
    heal = next(e for e in load_trace(lab.trace(again)) if e["type"] == "heal")
    assert heal["cause"] == "digest_changed" and heal["step"] == 1


def test_a_key_is_not_pressed_on_another_element_than_the_recorded_one(lab):
    goals = [Step("Submit the query", expect=expect.text("Submitted"))]

    def chooser(page, goal, history, **extra):
        chooser.calls.append(goal)
        if chooser.forbid:
            raise AssertionError("the model was asked during a replay")
        choice = next(a["id"] for a in page["actions"] if a["id"] == "key:Enter")
        return {
            "choice": choice, "operation": "PRESS_KEY", "target": "1", "confidence": 1.0,
            "probabilities": {choice: 1.0}, "latency_ms": 1, "usage": {},
        }

    chooser.forbid = False
    agent, state, _model, _wall = lab.run(url="replay_focus.html", goals=goals, chooser=chooser)
    assert state["status"] == "done" and agent.browser.evaluate("window.__deleted") == 0
    agent.close()
    key = next(e for e in load_trace(lab.trace(agent)) if e["type"] == "action")
    assert key["kind"] == "key" and key["focus"]["label"] == "Query"

    chooser.forbid = True
    same, replayed, _model, _wall = lab.run(
        url="replay_focus.html", goals=goals, chooser=chooser, replay=lab.trace(agent)
    )
    assert replayed["status"] == "done" and sources(replayed) == ["replay"]
    same.close()

    # Focus is on Delete now: Enter would delete. The replay stops before pressing it.
    moved, stopped, _model, _wall = lab.run(
        url="replay_focus.html?variant=moved", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert stopped["status"] == "diverged" and stopped["stop_reason"] == "focus_changed at step 1"
    assert moved.browser.evaluate("window.__deleted") == 0 and stopped["history"] == []


def test_a_press_that_an_optimistic_page_takes_back_is_not_done_under_replay(lab):
    goals = [Step("Upvote it", expect=expect.element(role="button", name="Upvote", state={"pressed": True}))]

    def chooser(page, goal, history, **extra):
        chooser.calls.append(goal)
        if chooser.forbid:
            raise AssertionError("the model was asked during a replay")
        choice = next(a["id"] for a in page["actions"] if a["kind"] == "click" and a["label"] == "Upvote")
        return {
            "choice": choice, "operation": "CLICK", "target": "1", "confidence": 1.0,
            "probabilities": {choice: 1.0}, "latency_ms": 1, "usage": {},
        }

    chooser.forbid = False
    agent, state, _model, _wall = lab.run(url="steps_toggle.html", goals=goals, chooser=chooser)
    assert state["status"] == "done" and state["verified"]
    agent.close()

    chooser.forbid = True
    again, stopped, _model, _wall = lab.run(
        url="steps_flaky.html?revert=300", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False,
        action_settle_timeout_ms=0, expect_timeout_ms=400,
    )
    assert stopped["status"] == "diverged" and stopped["verified"] is False
    assert stopped["stop_reason"] in {"checks_unstable at step 1", "checks_unmet at step 1"}
    assert again.browser.evaluate("document.querySelector('button').getAttribute('aria-pressed')") == "false"


# --- fix round 2 ------------------------------------------------------------------------------------------------


def pick_chooser(select):
    """A model stub that answers with the action `select(agent_page_actions, browser)` names; forbid=True fails it."""

    def chooser(page, goal, history, **extra):
        chooser.calls.append(goal)
        if chooser.forbid:
            raise AssertionError("the model was asked during a replay")
        choice = select(page, chooser)
        return {
            "choice": choice, "operation": chooser.operation, "target": "1", "confidence": 1.0,
            "probabilities": {choice: 1.0}, "latency_ms": 1, "usage": {},
        }

    chooser.forbid, chooser.operation, chooser.agent = False, "CLICK", None
    return chooser


ROW_DELETE = "window.__jevFast.ids.get(document.querySelector('[data-row=b] button'))"


def test_a_reordered_list_of_look_alikes_on_a_long_page_heals(lab):
    goals = [Step("Delete row B", expect=expect.text("Deleted b"))]
    chooser = pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a.get("node") == c.agent.browser.evaluate(ROW_DELETE))
    )
    agent, state, _model, _wall = lab.run(url="replay_longlist.html", goals=goals, chooser=chooser)
    assert state["status"] == "done" and lab.clicked(agent) == ["b"]
    agent.close()
    chooser.forbid = False
    again, healed, _model, _wall = lab.run(
        url="replay_longlist.html?variant=swapped", goals=goals, chooser=chooser, replay=lab.trace(agent)
    )
    assert healed["status"] == "done" and healed["verified"] is True, healed["stop_reason"]
    assert lab.clicked(again) == ["b"]  # Not "a", which now sits where "b" was.
    again.close()
    heal = next(e for e in load_trace(lab.trace(again)) if e["type"] == "heal")
    assert heal["cause"] == "digest_changed" and heal["step"] == 0


def test_a_clock_outside_the_list_does_not_make_the_step_heal(lab):
    goals = [Step("Delete row B", expect=expect.text("Deleted b"))]
    chooser = pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a.get("node") == c.agent.browser.evaluate(ROW_DELETE))
    )
    agent, state, _model, _wall = lab.run(url="replay_longlist.html?variant=clock", goals=goals, chooser=chooser)
    assert state["status"] == "done"
    agent.close()
    chooser.forbid = True  # A different time on this load: still replayed, no model call.
    again, replayed, _model, _wall = lab.run(
        url="replay_longlist.html?variant=clock", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert replayed["status"] == "done" and sources(replayed) == ["replay"], replayed["stop_reason"]
    assert lab.clicked(again) == ["b"]


def test_volatile_text_inside_the_rows_heals(lab):
    goals = [Step("Delete row B", expect=expect.text("Deleted b"))]
    chooser = pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a.get("node") == c.agent.browser.evaluate(ROW_DELETE))
    )
    agent, state, _model, _wall = lab.run(url="replay_longlist.html?variant=stamp", goals=goals, chooser=chooser)
    assert state["status"] == "done"
    agent.close()
    again, healed, _model, _wall = lab.run(
        url="replay_longlist.html?variant=stamp", goals=goals, chooser=chooser, replay=lab.trace(agent)
    )
    assert healed["status"] == "done" and lab.clicked(again) == ["b"]
    again.close()
    heal = next(e for e in load_trace(lab.trace(again)) if e["type"] == "heal")
    assert heal["cause"] == "digest_changed"  # Safe direction: the model looks, the wrong row is never clicked.


LINK_GOALS = [Step("Follow the first Read more", expect=expect.text("Opened /a/1"))]


def enter_chooser():
    chooser = pick_chooser(lambda page, c: next(a["id"] for a in page["actions"] if a["id"] == "key:Enter"))
    chooser.operation = "PRESS_KEY"
    return chooser


def test_enter_on_another_look_alike_link_is_not_pressed(lab):
    chooser = enter_chooser()
    agent, state, _model, _wall = lab.run(url="replay_links.html", goals=LINK_GOALS, chooser=chooser)
    assert state["status"] == "done" and agent.browser.evaluate("window.__opened") == ["/a/1"]
    agent.close()
    chooser.forbid = True
    # Link 2 has focus: same role, label, tag and type, another href. Enter would open /a/2.
    other, stopped, _model, _wall = lab.run(
        url="replay_links.html?variant=second", goals=LINK_GOALS, chooser=chooser, replay=lab.trace(agent),
        heal=False,
    )
    assert stopped["status"] == "diverged" and stopped["stop_reason"] == "focus_changed at step 1"
    assert other.browser.evaluate("window.__opened") == [] and stopped["history"] == []


def test_a_link_that_keeps_its_text_but_points_elsewhere_is_not_clicked(lab):
    goals = [Step("Download the report", expect=expect.text("Opened /files/report.pdf"))]
    chooser = pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a["kind"] == "click" and a["label"] == "Download")
    )
    agent, state, _model, _wall = lab.run(url="replay_links.html", goals=goals, chooser=chooser)
    assert state["status"] == "done" and agent.browser.evaluate("window.__opened") == ["/files/report.pdf"]
    agent.close()
    chooser.forbid = True
    moved, stopped, _model, _wall = lab.run(
        url="replay_links.html?variant=moved", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert stopped["status"] == "diverged" and stopped["history"] == []
    assert moved.browser.evaluate("window.__opened") == []


def test_a_reload_runs_only_on_the_page_it_was_recorded_on(lab):
    goals = [Step("Reload the page", expect=expect.text("Page loaded"))]
    chooser = pick_chooser(lambda page, c: "reload")
    chooser.operation = "RELOAD"
    agent, state, _model, _wall = lab.run(url="replay_page.html?page=1", goals=goals, chooser=chooser)
    assert state["status"] == "done"
    agent.close()
    chooser.forbid = True
    same, replayed, _model, _wall = lab.run(
        url="replay_page.html?page=2", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert replayed["status"] == "done" and sources(replayed) == ["replay"]
    same.close()
    other, stopped, _model, _wall = lab.run(
        url="replay_page.html?section=2", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert stopped["status"] == "diverged" and stopped["stop_reason"] == "page_changed at step 1"
    assert stopped["history"] == []


def test_a_rollback_from_a_setinterval_is_caught_by_the_full_window_only(lab):
    goals = [Step("Upvote it", expect=expect.element(role="button", name="Upvote", state={"pressed": True}))]
    chooser = pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a["kind"] == "click" and a["label"] == "Upvote")
    )
    agent, state, _model, _wall = lab.run(url="steps_toggle.html", goals=goals, chooser=chooser)
    assert state["status"] == "done" and state["verified"]
    agent.close()
    chooser.forbid = True
    # replay_quiet_ms=None keeps the whole 500 ms window, which sees the interval take the press back at 420 ms.
    again, stopped, _model, _wall = lab.run(
        url="steps_interval.html?revert=420", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False,
        replay_quiet_ms=None,
    )
    assert stopped["status"] == "diverged" and stopped["verified"] is False
    assert stopped["stop_reason"] in {"checks_unstable at step 1", "checks_unmet at step 1"}
