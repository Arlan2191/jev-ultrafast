"""Record a run once, then replay it on real pages without the model, and heal where the page changed. The model is a
stub that knows the answers while recording and fails the test when it is asked during a replay: no paid call."""

import json
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
    agent, state, _model, _wall = lab.run(url="steps_flaky.html?revert=100000", goals=goals, chooser=chooser)
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
    assert replayed["status"] == "done" and set(sources(replayed)) == {"replay"}, replayed["stop_reason"]
    assert len(replayed["history"]) == len(state["history"]) and set(lab.clicked(again)) == {"b"}


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
    agent, state, _model, _wall = lab.run(url="steps_interval.html?revert=100000", goals=goals, chooser=chooser)
    assert state["status"] == "done" and state["verified"]
    agent.close()
    chooser.forbid = True
    # replay_quiet_ms=None keeps the whole 500 ms window, which sees the interval take the press back at 320 ms
    # (170 ms after the default window ends, 180 ms before this one does).
    again, stopped, _model, _wall = lab.run(
        url="steps_interval.html?revert=320", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False,
        replay_quiet_ms=None,
    )
    assert stopped["status"] == "diverged" and stopped["verified"] is False
    assert stopped["stop_reason"] in {"checks_unstable at step 1", "checks_unmet at step 1"}


# --- fix round 3 ------------------------------------------------------------------------------------------------


def download_chooser():
    return pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a["kind"] == "click" and a["label"] == "Download")
    )


DOWNLOAD = [Step("Download the report", expect=expect.text("Opened"))]


def test_a_link_keeps_its_origin_in_the_locator_and_another_host_is_not_clicked(lab, fixture_base_url):
    chooser = download_chooser()
    agent, state, _model, _wall = lab.run(url="replay_links.html", goals=DOWNLOAD, chooser=chooser)
    assert state["status"] == "done"
    agent.close()
    click = next(e for e in load_trace(lab.trace(agent)) if e["type"] == "action" and e["kind"] == "click")
    assert click["locator"]["href"] == "/files/report.pdf"
    assert click["locator"]["href_origin"] == fixture_base_url  # A relative link carries the page's own origin.
    chooser.forbid = True
    # Same text, same path, another host: not the link that was recorded.
    other, stopped, _model, _wall = lab.run(
        url="replay_links.html?variant=otherhost", goals=DOWNLOAD, chooser=chooser, replay=lab.trace(agent),
        heal=False,
    )
    assert stopped["status"] == "diverged" and stopped["history"] == []
    assert other.browser.evaluate("window.__opened") == []


def test_the_same_link_on_the_same_origin_is_still_replayed(lab):
    chooser = download_chooser()
    agent, state, _model, _wall = lab.run(url="replay_links.html", goals=DOWNLOAD, chooser=chooser)
    assert state["status"] == "done"
    agent.close()
    chooser.forbid = True
    again, replayed, _model, _wall = lab.run(
        url="replay_links.html", goals=DOWNLOAD, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert replayed["status"] == "done" and sources(replayed) == ["replay"]
    assert again.browser.evaluate("window.__opened") == ["/files/report.pdf"]


LAST_ROW = "window.__jevFast.ids.get(document.querySelector('[data-row=r12] button'))"


def test_a_tail_swap_in_a_list_over_4000_characters_heals_and_never_clicks_by_position(lab):
    goals = [Step("Delete the last row", expect=expect.text("Deleted r12"))]
    chooser = pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a.get("node") == c.agent.browser.evaluate(LAST_ROW))
    )
    agent, state, _model, _wall = lab.run(url="replay_longlist.html?variant=long", goals=goals, chooser=chooser)
    assert state["status"] == "done" and lab.clicked(agent) == ["r12"]
    assert agent.browser.evaluate("document.getElementById('list').textContent.length") > 4000
    agent.close()
    again, healed, _model, _wall = lab.run(
        url="replay_longlist.html?variant=tailswap", goals=goals, chooser=chooser, replay=lab.trace(agent)
    )
    assert healed["status"] == "done" and healed["verified"] is True, healed["stop_reason"]
    assert lab.clicked(again) == ["r12"]  # Not "r11", which now sits in the recorded (last) position.
    again.close()
    heal = next(e for e in load_trace(lab.trace(again)) if e["type"] == "heal")
    assert heal["cause"] == "digest_changed" and heal["step"] == 0


def test_a_list_over_the_node_cap_has_no_digest_and_is_ambiguous(lab):
    goals = [Step("Delete row B", expect=expect.text("Deleted b"))]
    chooser = pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a.get("node") == c.agent.browser.evaluate(ROW_DELETE))
    )
    agent, state, _model, _wall = lab.run(url="replay_longlist.html?variant=nodes", goals=goals, chooser=chooser)
    assert state["status"] == "done" and lab.clicked(agent) == ["b"]
    click = next(e for e in load_trace(lab.trace(agent)) if e["type"] == "action" and e["kind"] == "click")
    assert click["locator"]["digest"] is None
    agent.close()
    chooser.forbid = True
    again, stopped, _model, _wall = lab.run(
        url="replay_longlist.html?variant=nodes", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert stopped["status"] == "diverged" and stopped["stop_reason"] == "ambiguous at step 1"
    assert lab.clicked(again) == []


def test_a_click_is_not_replayed_on_another_route_than_the_recorded_one(lab):
    goals = [
        Step("Open the settings page", expect=expect.text("Row A")),
        Step("Delete row B", expect=expect.text("Deleted b")),
    ]

    def select(page, c):
        if page["title"] == "Route":
            return next(a["id"] for a in page["actions"] if a["kind"] == "click" and a["label"] == "Settings")
        return next(a["id"] for a in page["actions"] if a.get("node") == c.agent.browser.evaluate(ROW_DELETE))

    chooser = pick_chooser(select)
    agent, state, _model, _wall = lab.run(url="replay_route.html", goals=goals, chooser=chooser)
    assert state["status"] == "done", state["stop_reason"]
    agent.close()
    chooser.forbid = True
    # From the start page the recorded route holds at every click: replayed, no model call.
    again, replayed, _model, _wall = lab.run(
        url="replay_route.html", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert replayed["status"] == "done" and set(sources(replayed)) == {"replay"}, replayed["stop_reason"]
    again.close()
    # Started on the list page, the recorded "Settings" click belongs to the other route: nothing is clicked.
    other, stopped, _model, _wall = lab.run(
        url="replay_longlist.html", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert stopped["status"] == "diverged" and stopped["stop_reason"] == "page_changed at step 1"
    assert other.browser.evaluate("window.__clicked") == [] and stopped["history"] == []


def test_a_setinterval_rollback_is_not_seen_by_the_default_150_ms_window(lab):
    goals = [Step("Upvote it", expect=expect.element(role="button", name="Upvote", state={"pressed": True}))]
    chooser = pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a["kind"] == "click" and a["label"] == "Upvote")
    )
    agent, state, _model, _wall = lab.run(url="steps_interval.html?revert=100000", goals=goals, chooser=chooser)
    assert state["status"] == "done" and state["verified"]
    agent.close()
    chooser.forbid = True
    # Documented: the default quiet window (150 ms) ends long before the interval takes the press back at 1500 ms.
    again, replayed, _model, _wall = lab.run(
        url="steps_interval.html?revert=1500", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert replayed["status"] == "done" and sources(replayed) == ["replay"]


# --- id-tolerant routes -------------------------------------------------------------------------------------------

ID_A, ID_B = "3f2b8c1e-5d4a-4e7b-9c6d-1a2b3c4d5e6f", "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
ID_GOALS = [Step("Open the settings", expect=expect.text("Settings opened"))]


def settings_chooser():
    return pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a["kind"] == "click" and a["label"] == "Open settings")
    )


def test_a_replay_runs_on_another_resource_id_with_no_model_call(lab):
    chooser = settings_chooser()
    agent, state, _model, _wall = lab.run(url=f"items/{ID_A}/replay_id.html", goals=ID_GOALS, chooser=chooser)
    assert state["status"] == "done" and state["verified"]
    agent.close()
    chooser.forbid = True
    again, replayed, _model, _wall = lab.run(
        url=f"items/{ID_B}/replay_id.html", goals=ID_GOALS, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert replayed["status"] == "done" and sources(replayed) == ["replay"]
    assert replayed["replay"]["model_calls"] == 0 and replayed["replay"]["steps_healed"] == 0
    assert chooser.calls == []


def test_a_replay_on_a_path_that_is_not_an_id_is_still_another_page(lab):
    chooser = settings_chooser()
    agent, state, _model, _wall = lab.run(url=f"items/{ID_A}/replay_id.html", goals=ID_GOALS, chooser=chooser)
    assert state["status"] == "done"
    agent.close()
    chooser.forbid = True
    again, stopped, _model, _wall = lab.run(
        url="items/new/replay_id.html", goals=ID_GOALS, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert stopped["status"] == "diverged" and stopped["stop_reason"] == "page_changed at step 1"


def test_a_declared_route_pattern_lets_a_replay_run_on_another_path(lab):
    chooser = settings_chooser()
    agent, state, _model, _wall = lab.run(url="items/alpha/replay_id.html", goals=ID_GOALS, chooser=chooser)
    assert state["status"] == "done"
    agent.close()
    chooser.forbid = True
    again, replayed, _model, _wall = lab.run(
        url="items/beta/replay_id.html", goals=ID_GOALS, chooser=chooser, replay=lab.trace(agent), heal=False,
        replay_routes=[r"^/items/[^/]+/replay_id\.html$"],
    )
    assert replayed["status"] == "done" and sources(replayed) == ["replay"]


TESTID_GOALS = [Step("Add the first one", expect=expect.text("Added main"))]


MAIN = "window.__jevFast.ids.get(document.querySelector('[data-m=main]'))"


def testid_chooser():
    return pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a.get("node") == c.agent.browser.evaluate(MAIN))
    )


def test_a_unique_test_id_resolves_when_the_look_alikes_container_text_changed(lab):
    chooser = testid_chooser()
    agent, state, _model, _wall = lab.run(url="replay_testid.html?variant=live", goals=TESTID_GOALS, chooser=chooser)
    assert state["status"] == "done" and lab.clicked(agent) == ["main"]
    locator = next(e for e in load_trace(lab.trace(agent)) if e["type"] == "action")["locator"]
    assert locator["test_id"] == "add-main" and locator["test_id_unique"] is True
    agent.close()
    chooser.forbid = True  # Another price on this load: the digest differs, the test id still names the control.
    again, replayed, _model, _wall = lab.run(
        url="replay_testid.html?variant=live", goals=TESTID_GOALS, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert replayed["status"] == "done", replayed["stop_reason"]
    assert set(sources(replayed)) == {"replay"} and chooser.calls == [] and lab.clicked(again) == ["main"]


def test_a_test_id_moved_to_another_control_is_not_resolved_and_the_step_heals(lab):
    chooser = testid_chooser()
    agent, state, _model, _wall = lab.run(url="replay_testid.html?variant=live", goals=TESTID_GOALS, chooser=chooser)
    assert state["status"] == "done"
    agent.close()
    again, healed, _model, _wall = lab.run(
        url="replay_testid.html?variant=moved", goals=TESTID_GOALS, chooser=chooser, replay=lab.trace(agent)
    )
    assert healed["status"] == "done" and lab.clicked(again) == ["main"]  # Never the Remove button that has the id now.
    again.close()
    heal = next(e for e in load_trace(lab.trace(again)) if e["type"] == "heal")
    assert heal["cause"] == "digest_changed"


def test_a_test_id_that_is_duplicated_at_replay_is_not_resolved_by_it(lab):
    chooser = testid_chooser()
    agent, state, _model, _wall = lab.run(url="replay_testid.html?variant=live", goals=TESTID_GOALS, chooser=chooser)
    assert state["status"] == "done"
    agent.close()
    again, healed, _model, _wall = lab.run(
        url="replay_testid.html?variant=dup", goals=TESTID_GOALS, chooser=chooser, replay=lab.trace(agent)
    )
    assert healed["status"] == "done" and lab.clicked(again) == ["main"]
    again.close()
    heal = next(e for e in load_trace(lab.trace(again)) if e["type"] == "heal")
    assert heal["cause"] == "digest_changed"


def test_a_positional_test_id_never_clicks_the_other_named_row(lab):
    goals = [Step("Delete Alice", expect=expect.text("Deleted"))]
    alice = "window.__jevFast.ids.get(document.querySelector('[data-m=alice]'))"
    chooser = pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a.get("node") == c.agent.browser.evaluate(alice))
    )
    agent, state, _model, _wall = lab.run(url="replay_testid_rows.html", goals=goals, chooser=chooser)
    assert state["status"] == "done" and lab.clicked(agent) == ["alice"]
    locator = next(e for e in load_trace(lab.trace(agent)) if e["type"] == "action")["locator"]
    assert locator["test_id"] == "del-1" and locator["test_id_unique"] is True and locator["scope"]["name"] == "Alice"
    agent.close()
    chooser.forbid = True  # Bob's row carries del-1 now: it is outside Alice's scope, so the primary rule decides.
    again, replayed, _model, _wall = lab.run(
        url="replay_testid_rows.html?variant=reorder", goals=goals, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert replayed["status"] == "done", replayed["stop_reason"]
    assert lab.clicked(again) == ["alice"] and chooser.calls == []


SCOPE_GOALS = [Step("Search for it", expect=expect.text("Typed s1"))]
SHELL_INPUT = "window.__jevFast.ids.get(document.querySelector('[data-m=s1]'))"


def scope_chooser():
    def chooser(page, goal, history, **extra):
        chooser.calls.append(goal)
        if chooser.forbid:
            raise AssertionError("the model was asked during a replay")
        node = chooser.agent.browser.evaluate(SHELL_INPUT)
        choice = next(a["id"] for a in page["actions"] if a.get("node") == node)
        return {
            "choice": choice, "operation": "TYPE_TEXT", "target": "1", "value": "query", "confidence": 1.0,
            "probabilities": {choice: 1.0}, "latency_ms": 1, "usage": {},
        }

    chooser.forbid, chooser.agent = False, None
    return chooser


def record_in_shells(lab):
    chooser = scope_chooser()
    agent, state, _model, _wall = lab.run(url="replay_testid_scope.html", goals=SCOPE_GOALS, chooser=chooser)
    assert state["status"] == "done" and agent.browser.evaluate("window.__typed")[-1] == "s1"
    locator = next(e for e in load_trace(lab.trace(agent)) if e["type"] == "action")["locator"]
    assert locator["test_id"] == "search" and locator["test_id_unique"] is True
    assert locator["scope"]["count"] == 2 and locator["scope"]["ordinal"] == 0
    agent.close()
    return chooser, agent


def test_a_unique_test_id_in_a_repeated_same_named_scope_replays_without_the_model(lab):
    chooser, agent = record_in_shells(lab)
    chooser.forbid = True  # Another total on this load: the digest differs, the test id still names it.
    chooser.calls.clear()
    again, replayed, _model, _wall = lab.run(
        url="replay_testid_scope.html", goals=SCOPE_GOALS, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert replayed["status"] == "done", replayed["stop_reason"]
    assert set(sources(replayed)) == {"replay"} and chooser.calls == []
    assert again.browser.evaluate("window.__typed")[-1] == "s1"
    assert replayed["replay"]["model_calls"] == 0 and replayed["replay"]["steps_healed"] == 0


def test_a_third_same_named_scope_is_not_resolved_by_the_test_id_and_heals(lab):
    chooser, agent = record_in_shells(lab)
    again, healed, _model, _wall = lab.run(
        url="replay_testid_scope.html?variant=three", goals=SCOPE_GOALS, chooser=chooser, replay=lab.trace(agent)
    )
    assert healed["status"] == "done" and set(again.browser.evaluate("window.__typed")) == {"s1"}
    again.close()
    assert any(e["type"] == "heal" for e in load_trace(lab.trace(again)))


def test_a_test_id_moved_into_the_other_same_named_scope_is_not_followed(lab):
    chooser, agent = record_in_shells(lab)
    again, healed, _model, _wall = lab.run(
        url="replay_testid_scope.html?variant=moved", goals=SCOPE_GOALS, chooser=chooser, replay=lab.trace(agent)
    )
    # The test id is in the second landmark now: test_id_first never types there; the step heals onto the first.
    assert healed["status"] == "done" and set(again.browser.evaluate("window.__typed")) == {"s1"}
    again.close()
    assert any(e["type"] == "heal" for e in load_trace(lab.trace(again)))


def test_known_residual_a_test_id_moved_onto_a_look_alike_while_the_container_text_changed(lab):
    chooser = testid_chooser()
    agent, state, _model, _wall = lab.run(url="replay_testid.html?variant=live", goals=TESTID_GOALS, chooser=chooser)
    assert state["status"] == "done" and lab.clicked(agent) == ["main"]
    agent.close()
    chooser.forbid = True
    # Known residual (README): the digest differs, so the primary rule refuses and the moved test id stands. Pinned so a
    # change in either direction is visible; if this starts healing, update the README residual too.
    again, replayed, _model, _wall = lab.run(
        url="replay_testid.html?variant=residual",
        goals=TESTID_GOALS,
        chooser=chooser,
        replay=lab.trace(agent),
        heal=False,
    )
    # The goal's own check is what notices the wrong click here, not the resolver.
    assert replayed["status"] == "diverged" and lab.clicked(again) == ["beta"] and chooser.calls == []


def test_a_test_id_moved_onto_a_same_name_look_alike_is_a_refusal_that_heals(lab):
    chooser = testid_chooser()
    agent, state, _model, _wall = lab.run(url="replay_testid.html", goals=TESTID_GOALS, chooser=chooser)
    assert state["status"] == "done" and lab.clicked(agent) == ["main"]
    agent.close()
    # The primary rule still places the first Add (static page, equal digest): the two disagree, so it is a refusal.
    again, healed, _model, _wall = lab.run(
        url="replay_testid.html?variant=swap", goals=TESTID_GOALS, chooser=chooser, replay=lab.trace(agent)
    )
    assert healed["status"] == "done" and lab.clicked(again) == ["main"]  # Never the second Add.
    again.close()
    heal = next(e for e in load_trace(lab.trace(again)) if e["type"] == "heal")
    assert heal["cause"] == "ambiguous"


# --- step goal parameters ---------------------------------------------------------------------------------------

TARGET_OPEN = "window.__jevFast.ids.get(document.querySelector('[data-row=target] button'))"
GOAL = "Open the order titled {title}"


def target_chooser():
    return pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a.get("node") == c.agent.browser.evaluate(TARGET_OPEN))
    )


def title_steps(title):
    return [Step(GOAL, expect=expect.text("Opened {title}"), params={"title": title})]


def record_order_a(lab, chooser, url):
    agent, state, _model, _wall = lab.run(url=f"{url}name=Order%20A", goals=title_steps("Order A"), chooser=chooser)
    assert state["status"] == "done" and state["verified"] and lab.clicked(agent) == ["target"]
    assert chooser.calls == ["Open the order titled Order A"]
    agent.close()
    chooser.forbid = True
    return agent


def test_a_replay_with_another_param_value_resolves_inside_the_row_it_names(lab):
    chooser = target_chooser()
    agent = record_order_a(lab, chooser, "replay_params.html?")
    locators = [e["locator"] for e in load_trace(lab.trace(agent)) if e["type"] == "action"]
    assert locators[0]["scope"]["param"] == "title" and locators[0]["scope"]["name"] == "{title}"
    assert "Order A" not in json.dumps(locators)  # the param's name is recorded, never its value

    again, replayed, _model, _wall = lab.run(
        url="replay_params.html?name=Order%20B", goals=title_steps("Order B"), chooser=chooser, replay=lab.trace(agent)
    )
    assert replayed["status"] == "done" and replayed["verified"] is True, replayed["stop_reason"]
    assert chooser.calls == [] and replayed["decisions"] == [] and replayed["text_calls"] == []
    assert sources(replayed) == ["replay"] and lab.clicked(again) == ["target"]
    assert replayed["replay"]["model_calls"] == 0 and replayed["replay"]["steps_healed"] == 0
    again.close()
    assert load_trace(lab.trace(again))[-1]["totals"]["jev_calls"] == 0


def test_a_test_id_on_a_row_named_by_neither_value_is_not_acted_on(lab):
    chooser = target_chooser()
    agent = record_order_a(lab, chooser, "replay_params.html?")
    # The page has no row titled "Order B"; the recorded test id now sits on the "Other ..." row.
    again, replayed, _model, _wall = lab.run(
        url="replay_params.html?name=Order%20C&moved=1", goals=title_steps("Order B"), chooser=chooser,
        replay=lab.trace(agent), heal=False,
    )
    assert replayed["status"] == "diverged" and replayed["verified"] is not True
    assert lab.clicked(again) == [] and chooser.calls == [] and sources(replayed) == []


def test_a_stable_id_on_a_row_named_by_neither_value_is_not_acted_on(lab):
    chooser = target_chooser()
    agent = record_order_a(lab, chooser, "replay_params.html?byid=1&")
    # No row is titled "Order B"; row A still holds the recorded id. The id fallback must not click it.
    again, replayed, _model, _wall = lab.run(
        url="replay_params.html?byid=1&name=Order%20A", goals=title_steps("Order B"), chooser=chooser,
        replay=lab.trace(agent), heal=False,
    )
    assert replayed["status"] == "diverged" and replayed["verified"] is not True
    assert lab.clicked(again) == [] and chooser.calls == [] and sources(replayed) == []


def test_a_test_id_on_a_row_that_does_not_carry_the_param_fails_the_param_check(lab):
    chooser = target_chooser()
    agent, state, _model, _wall = lab.run(
        url="replay_params.html?plain=1&name=Order%20A", goals=title_steps("Order A"), chooser=chooser
    )
    assert state["status"] == "done" and state["verified"]
    agent.close()
    chooser.forbid = True
    # No scope (plain rows): the test id resolves to the decoy row, and "Opened Order B" never shows.
    again, replayed, _model, _wall = lab.run(
        url="replay_params.html?plain=1&name=Order%20C&moved=1", goals=title_steps("Order B"), chooser=chooser,
        replay=lab.trace(agent), heal=False, expect_timeout_ms=500,
    )
    assert replayed["status"] == "diverged" and replayed["verified"] is not True
    assert replayed["steps"][0]["status"] != "done" and chooser.calls == []


def test_a_changed_goal_template_is_a_replay_mismatch_even_with_the_same_param_value(lab):
    chooser = target_chooser()
    agent = record_order_a(lab, chooser, "replay_params.html?")
    changed = [Step("Open the order called {title}", expect=expect.text("Opened {title}"), params={"title": "Order A"})]
    with pytest.raises(ValueError, match="replay_mismatch.*step 1 goal"):
        lab.run(url="replay_params.html?name=Order%20A", goals=changed, chooser=chooser, replay=lab.trace(agent))
    assert chooser.calls == []


UNIQUE_GOALS = [Step("Pick it", expect=expect.text("Picked first"))]
FIRST = "window.__jevFast.ids.get(document.querySelector('[data-m=first]'))"


def unique_chooser():
    return pick_chooser(
        lambda page, c: next(a["id"] for a in page["actions"] if a.get("node") == c.agent.browser.evaluate(FIRST))
    )


def record_unique(lab):
    chooser = unique_chooser()
    agent, state, _model, _wall = lab.run(url="replay_unique.html", goals=UNIQUE_GOALS, chooser=chooser)
    assert state["status"] == "done" and lab.clicked(agent) == ["first"]
    locator = next(e for e in load_trace(lab.trace(agent)) if e["type"] == "action")["locator"]
    assert locator["test_id"] is None and locator["scope"]["count"] == 2
    assert locator["ordinal"]["document_count"] == 1
    agent.close()
    return chooser, agent


def test_a_control_unique_in_the_page_replays_without_the_digest_of_a_repeated_scope(lab):
    chooser, agent = record_unique(lab)
    chooser.forbid = True  # Another total on this load: the scope digest differs, the control is still the only one.
    chooser.calls.clear()
    again, replayed, _model, _wall = lab.run(
        url="replay_unique.html", goals=UNIQUE_GOALS, chooser=chooser, replay=lab.trace(agent), heal=False
    )
    assert replayed["status"] == "done", replayed["stop_reason"]
    assert set(sources(replayed)) == {"replay"} and chooser.calls == [] and lab.clicked(again) == ["first"]
    assert replayed["replay"]["model_calls"] == 0 and replayed["replay"]["steps_healed"] == 0


def test_a_duplicate_of_the_unique_control_is_not_resolved_by_position_and_heals(lab):
    chooser, agent = record_unique(lab)
    again, healed, _model, _wall = lab.run(
        url="replay_unique.html?variant=dup", goals=UNIQUE_GOALS, chooser=chooser, replay=lab.trace(agent)
    )
    assert healed["status"] == "done" and lab.clicked(again) == ["first"]
    again.close()
    heal = next(e for e in load_trace(lab.trace(again)) if e["type"] == "heal")
    assert heal["cause"] == "digest_changed"


def test_a_unique_control_moved_into_a_differently_named_scope_is_not_clicked_by_the_replay(lab):
    chooser, agent = record_unique(lab)
    again, healed, _model, _wall = lab.run(
        url="replay_unique.html?variant=moved", goals=UNIQUE_GOALS, chooser=chooser, replay=lab.trace(agent)
    )
    again.close()
    events = load_trace(lab.trace(again))
    assert any(e["type"] == "heal" for e in events)
    assert set(sources(healed)) == {"model"}  # The replay itself never pressed it.


def test_a_unique_control_in_another_record_of_a_same_named_list_is_not_clicked_by_the_replay(lab):
    chooser, agent = record_unique(lab)
    again, healed, _model, _wall = lab.run(
        url="replay_unique.html?variant=second", goals=UNIQUE_GOALS, chooser=chooser, replay=lab.trace(agent)
    )
    again.close()
    events = load_trace(lab.trace(again))
    assert any(e["type"] == "heal" for e in events)
    assert set(sources(healed)) == {"model"}  # The replay itself never pressed it.


def test_a_same_named_scope_added_to_the_page_heals_a_unique_control(lab):
    chooser, agent = record_unique(lab)
    again, healed, _model, _wall = lab.run(
        url="replay_unique.html?variant=three", goals=UNIQUE_GOALS, chooser=chooser, replay=lab.trace(agent)
    )
    assert healed["status"] == "done" and lab.clicked(again) == ["first"]
    again.close()
    events = load_trace(lab.trace(again))
    assert any(e["type"] == "heal" for e in events)
    assert set(sources(healed)) == {"model"}
