"""Offline contracts for replaying a recorded run without model calls, and healing on divergence. No browser, no paid
APIs: the model is a scripted stub that fails the test when it is asked and was not supposed to be."""

import json
from unittest.mock import Mock

import pytest

from jev_ultrafast import ActionDenied, Agent, Step, Value, expect
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import MutationTimeout, ReadTimeout, Refused, StalePage, fingerprint
from jev_ultrafast.trace import load_trace
from jev_ultrafast.waits import WaitResult

NODES = {"Search": 10, "Go": 20, "Row B": 40, "Pin": 50}
SECRET = "hunter2-correct-horse"


FOCUS = {"role": "textbox", "label": "Search", "href": None, "tag": "input", "type": "text", "node": 10}


def page(focus=None, goto_url="https://example.test/", delta=560, url="https://example.test/"):
    def click(node, label):
        return {"id": f"c{node}", "kind": "click", "label": label, "role": "button", "value": "", "node": node}

    state = {
        "url": url,
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "f10", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            click(20, "Go"),
            click(40, "Row B"),
            click(50, "Pin"),
            {"id": "wait", "kind": "wait", "label": "Wait"},
            {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": delta},
            {"id": "key:Enter", "kind": "key", "key": "Enter", "name": "Enter", "label": "Press Enter"},
            {"id": "goto:home", "kind": "goto", "name": "home", "url": goto_url, "label": "Open home"},
            {"id": "back", "kind": "back", "label": "Go back to the previous page"},
            {"id": "reload", "kind": "reload", "label": "Reload the current page"},
        ],
        "focus": focus,
        "marker": ["m"],
        "page_key": ["k"],
        "guards": {str(n): ["g"] for n in NODES.values()},
    }
    state["fingerprint"] = fingerprint(state)
    return state


def found(name, **over):
    return {
        "rule": "role_name_scope", "unique": True, "count_match": True, "verified": True, "tag": "button",
        "text": name, "hint": None, "node": NODES[name], **over,
    }


class Journey:
    """Three steps on a fake page: search (a typed value, then Go), open Row B, pin."""

    def __init__(self, monkeypatch, tmp_path):
        self.monkeypatch, self.tmp_path = monkeypatch, tmp_path
        self.flags = {"searched": False, "opened": False, "pinned": False}
        self.executed = []
        self.asked = []
        self.script = []
        self.resolves = {}
        self.act_error = None
        self.page_args = {}
        self.stale_after = None  # A node whose execution is followed by one stale observation.
        self.browser = Mock()
        self.browser.observe.side_effect = self.observe
        self.browser.fresh.return_value = True
        self.browser.evaluate.return_value = "https://example.test/"
        self.browser.screenshot.return_value = ""
        self.browser.version = {"product": "HeadlessChrome/130", "userAgent": "UA"}
        self.browser.probe.side_effect = lambda kind, timeout_ms=0, **_a: WaitResult(True, 0, kind)
        by_node = {n: name for name, n in NODES.items()}
        self.focus_resolves = None  # What the focus locator resolves to now: a node, or None for "nothing".
        self.browser.locator.side_effect = lambda node, full_urls=False: {
            "role": "button", "name": by_node[node], "tag": "button", "type": None,
        }
        self.browser.resolve.side_effect = self.resolve
        self.browser.act.side_effect = self.act
        monkeypatch.setattr(loop, "Browser", Mock(return_value=self.browser))
        monkeypatch.setattr(loop, "choose", self.choose)
        monkeypatch.setattr(loop, "field_text", self.field_text)

    def observe(self, **_kw):
        if self.stale_after is not None and self.stale_after in [e[0] for e in self.executed]:
            self.stale_after = None
            raise StalePage("Page did not settle")
        return page(**self.page_args)

    def resolve(self, locator, node=False):
        name = locator["name"]
        return self.resolves[name] if name in self.resolves else found(name)

    def act(self, action, page_, text=None):
        if self.act_error:
            error = self.act_error.pop(0) if self.act_error else None
            if error:
                raise error
        self.executed.append((action.get("node"), action["kind"], text))
        flag = {20: "searched", 40: "opened", 50: "pinned"}.get(action.get("node"))
        if flag:
            self.flags[flag] = True
        return {"executed": action["id"]}

    def choose(self, state, goal, history, **extra):
        self.asked.append(goal)
        if not self.script:
            raise AssertionError(f"the model was asked for {goal!r} and no answer was scripted")
        choice, operation, *value = self.script.pop(0)
        return {
            "choice": choice, "operation": operation, "target": "1", "confidence": 1.0,
            "probabilities": {choice: 1.0}, "latency_ms": 1, "usage": {"input_tokens": 10}, "model": "stub",
            **({"value": value[0]} if value else {}),
        }

    def field_text(self, context):
        self.asked.append("text helper")
        return "generated text", {"model": "text-stub", "latency_ms": 1}

    def steps(self):
        return [
            Step("Search for dune", expect=expect.check(lambda _p: self.flags["searched"], "searched", auto=True)),
            Step("Open the second result", expect=expect.check(lambda _p: self.flags["opened"], "opened", auto=True)),
            Step("Pin it", expect=expect.check(lambda _p: self.flags["pinned"], "pinned", auto=True)),
        ]

    def agent(self, name, **kwargs):
        kwargs.setdefault("values", {"query": "dune"})
        kwargs.setdefault("urls", {"home": "https://example.test/"})
        kwargs.setdefault("evidence_dir", self.tmp_path / name)
        kwargs.setdefault("evidence_capture", False)
        agent = Agent("https://example.test/", kwargs.pop("goals", None) or self.steps(), **kwargs)
        self.agents = [*getattr(self, "agents", []), agent]
        return agent

    def reset(self):
        self.flags.update(searched=False, opened=False, pinned=False)
        self.executed.clear()
        self.asked.clear()

    def run(self, agent):
        for _ in agent.run():
            pass
        agent.close()
        return agent.snapshot()

    def record(self, name="recorded"):
        self.script = [("f10", "TYPE_TEXT", "query"), ("c20", "CLICK"), ("c40", "CLICK"), ("c50", "CLICK")]
        agent = self.agent(name)
        final = self.run(agent)
        assert final["status"] == "done" and final["verified"], final["stop_reason"]
        self.reset()
        return self.tmp_path / name / "trace.jsonl"

    def record_controls(self, *controls, name="recorded"):
        """Record step 1 as `controls` (choice, operation pairs) then fill + Go; the other steps as usual."""
        self.script = [*controls, ("f10", "TYPE_TEXT", "query"), ("c20", "CLICK"), ("c40", "CLICK"), ("c50", "CLICK")]
        agent = self.agent(name)
        final = self.run(agent)
        assert final["status"] == "done" and final["verified"], final["stop_reason"]
        self.reset()
        return self.tmp_path / name / "trace.jsonl"

    def replay(self, trace, name="replayed", **kwargs):
        agent = self.agent(name, replay=trace, **kwargs)
        return agent, self.run(agent)


@pytest.fixture
def journey(monkeypatch, tmp_path):
    return Journey(monkeypatch, tmp_path)


def sources(final):
    return [h["source"] for h in final["history"]]


# --- construction: a trace is replayed only for the task it was recorded for ----------------------------------


def test_replay_runs_the_whole_journey_without_asking_the_model(journey):
    trace = journey.record()
    agent, final = journey.replay(trace)
    assert final["status"] == "done" and final["verified"] is True
    assert journey.asked == []
    assert sources(final) == ["replay"] * 4
    assert journey.executed == [(10, "fill", "dune"), (20, "click", None), (40, "click", None), (50, "click", None)]
    assert final["replay"] == {"steps_replayed": 3, "steps_healed": 0, "actions_replayed": 4, "model_calls": 0}
    assert final["decisions"] == [] and final["text_calls"] == []


def test_replay_accepts_loaded_events(journey):
    events = load_trace(journey.record())
    _agent, final = journey.replay(events)
    assert final["status"] == "done" and journey.asked == []


def test_a_mismatched_goal_check_or_value_name_is_refused(journey):
    trace = journey.record()
    reworded = journey.steps()
    reworded[1] = Step("Open the third result", expect=reworded[1].expect)
    with pytest.raises(ValueError, match="replay_mismatch.*step 2 goal"):
        journey.agent("a", goals=reworded, replay=trace)
    rechecked = journey.steps()
    rechecked[2] = Step(rechecked[2].goal, expect=expect.check(lambda _p: True, "something else", auto=True))
    with pytest.raises(ValueError, match="replay_mismatch.*step 3 checks"):
        journey.agent("b", goals=rechecked, replay=trace)
    with pytest.raises(ValueError, match="replay_mismatch.*value names"):
        journey.agent("c", replay=trace, values={"other": "dune"})
    with pytest.raises(ValueError, match="replay_mismatch.*the run has 2"):
        journey.agent("d", goals=journey.steps()[:2], replay=trace)


def test_goal_whitespace_is_normalised(journey):
    trace = journey.record()
    spaced = journey.steps()
    spaced[0] = Step("  Search   for\ndune ", expect=spaced[0].expect)
    agent, final = journey.replay(trace, goals=spaced)
    assert final["status"] == "done"


@pytest.mark.parametrize("status, verified", [("blocked", False), ("done", False), ("failed", False)])
def test_only_a_done_and_verified_trace_is_replayable(journey, status, verified):
    events = load_trace(journey.record())
    events[-1] = {**events[-1], "status": status, "verified": verified}
    with pytest.raises(ValueError, match="replay_requires_verified"):
        journey.agent("x", replay=events)


def test_a_trace_without_run_end_is_not_replayable(journey):
    events = load_trace(journey.record())[:-1]
    with pytest.raises(ValueError, match="replay_requires_verified"):
        journey.agent("x", replay=events)


@pytest.mark.parametrize("bad", [3, [], [{"type": "nope"}], "missing/trace.jsonl"])
def test_an_unreadable_replay_source_is_refused(journey, bad):
    with pytest.raises((ValueError, OSError)):
        journey.agent("x", replay=bad)


def test_heal_must_be_a_bool(journey):
    with pytest.raises(ValueError, match="heal"):
        journey.agent("x", heal="yes")


def test_a_refused_replay_leaves_no_browser_open(journey):
    events = load_trace(journey.record())
    events[-1] = {**events[-1], "status": "blocked"}
    loop.Browser.reset_mock()
    with pytest.raises(ValueError):
        journey.agent("x", replay=events)
    loop.Browser.assert_not_called()


# --- the plan: executed actions per step ----------------------------------------------------------------------


def test_the_plan_groups_executed_actions_by_step(journey):
    from jev_ultrafast import replay

    steps = replay.plan(load_trace(journey.record()), 3)
    assert [[p.kind for p in step] for step in steps] == [["fill", "click"], ["click"], ["click"]]
    assert steps[0][0].value_name == "query" and steps[0][0].text_source == "value:query"
    assert [p.locator["name"] for p in steps[0]] == ["Search", "Go"]


def test_the_plan_keeps_select_option_scroll_key_and_goto_ids():
    from jev_ultrafast import replay

    def action(kind, choice, **extra):
        return {
            "type": "action", "t_ms": 1, "index": 1, "kind": kind, "label": "x", "choice": choice, "locator": None,
            "text_source": None, "text": None, "result": None, **extra,
        }

    events = [
        {"type": "step", "t_ms": 0, "index": 0, "status": "active"},
        action("select", "s1", option="blue"),
        action("scroll", "scroll_down", delta=560),
        action("key", "key:Enter"),
        action("goto", "goto:home"),
        action("wait", "wait"),
        {"type": "refused", "t_ms": 2, "label": "x", "kind": "click", "reason": "r", "skipped": False},
    ]
    planned = replay.plan(events, 1)[0]
    assert [p.kind for p in planned] == ["select", "scroll", "key", "goto", "wait"]
    assert planned[0].option == "blue" and planned[0].operation == "SELECT"
    assert planned[2].operation == "PRESS_KEY" and planned[3].operation == "GOTO"
    assert planned[1].operation == "SCROLL_DOWN"


# --- mapping a resolved locator to an action the page offers --------------------------------------------------


def test_resolve_results_map_to_the_matching_action_or_a_cause():
    from jev_ultrafast import replay

    assert replay.unresolved(None) == "not_found"
    assert replay.unresolved({"rule": "ambiguous"}) == "ambiguous"
    assert replay.unresolved({"rule": "hint"}) == "hint_only"
    assert replay.unresolved({"rule": "role_name_scope", "verified": False, "node": 1}) == "unverified"
    assert replay.unresolved({"rule": "role_name_scope", "verified": True, "node": None}) == "not_observed"
    assert replay.unresolved({"rule": "id", "verified": True, "node": 3}) is None
    actions = [
        {"id": "a", "kind": "click", "node": 5, "label": "A"},
        {"id": "b", "kind": "fill", "node": 6, "label": "B"},
        {"id": "o1", "kind": "select", "node": 7, "value": "red", "label": "Red"},
        {"id": "o2", "kind": "select", "node": 7, "value": "blue", "label": "Blue"},
        {"id": "m", "kind": "click", "node": 8, "label": "Menu", "expanded": False},
    ]
    P = replay.Planned
    assert replay.element_action(P("click", "x"), actions, 5)[0]["id"] == "a"
    assert replay.element_action(P("click", "x"), actions, 6) == (None, "kind_not_offered")
    assert replay.element_action(P("fill", "x"), actions, 6)[0]["id"] == "b"
    assert replay.element_action(P("select", "x", option="blue"), actions, 7)[0]["id"] == "o2"
    assert replay.element_action(P("select", "x", option="green"), actions, 7) == (None, "kind_not_offered")
    assert replay.element_action(P("hover", "x"), actions, 8)[0]["id"] == "m"
    assert replay.element_action(P("hover", "x"), actions, 5) == (None, "kind_not_offered")
    twice = actions + [{"id": "a2", "kind": "click", "node": 5, "label": "A again"}]
    assert replay.element_action(P("click", "x"), twice, 5) == (None, "ambiguous")
    key = P("key", "key:Enter", focus_recorded=True, page_url="https://example.test/")
    here = "https://example.test/"
    assert replay.control_action(key, [{"id": "key:Enter", "kind": "key"}], page_url=here)[0]["id"] == "key:Enter"
    assert replay.control_action(P("key", "key:Tab", focus_recorded=True), [{"id": "key:Enter", "kind": "key"}]) == (
        None,
        "control_not_offered",
    )
    assert replay.control_action(P("goto", "goto:home"), [{"id": "goto:home", "kind": "wait"}])[1]


def test_value_sources_for_a_replayed_fill():
    from jev_ultrafast import replay

    P = replay.Planned
    values = {"query": Value("dune")}
    assert replay.fill_text(P("fill", "x", text_source="value:query", value_name="query"), values) == (
        {"value": "query"},
        None,
    )
    assert replay.fill_text(P("fill", "x", text_source="value:gone", value_name="gone"), values) == (
        None,
        "value_missing",
    )
    assert replay.fill_text(P("fill", "x", text_source="generated", text="hello"), values) == ({"text": "hello"}, None)
    assert replay.fill_text(P("fill", "x", text_source="generated", text="a <secret:pw> b"), values)[1] == (
        "generated_secret"
    )
    assert replay.fill_text(P("fill", "x", text_source="generated", text="cut", truncated=("text",)), values)[1] == (
        "text_unavailable"
    )
    assert replay.fill_text(P("fill", "x", text_source="generated", text=None), values)[1] == "text_unavailable"


# --- values --------------------------------------------------------------------------------------------------


def test_a_secret_is_typed_from_the_caller_and_is_not_in_the_trace(journey):
    journey.script = [("f10", "TYPE_TEXT", "query"), ("c20", "CLICK"), ("c40", "CLICK"), ("c50", "CLICK")]
    secret = {"query": Value(SECRET, secret=True)}
    agent = journey.agent("rec", values=secret)
    assert journey.run(agent)["status"] == "done"
    trace = journey.tmp_path / "rec" / "trace.jsonl"
    assert SECRET not in trace.read_text()
    assert journey.executed[0] == (10, "fill", SECRET)
    journey.reset()
    _agent, final = journey.replay(trace, values=secret)
    assert final["status"] == "done" and journey.asked == []
    assert journey.executed[0] == (10, "fill", SECRET)
    assert SECRET not in json.dumps(final, default=str)
    assert SECRET not in (journey.tmp_path / "replayed" / "trace.jsonl").read_text()


def test_the_caller_value_of_the_replay_is_typed_not_the_recorded_one(journey):
    trace = journey.record()
    _agent, final = journey.replay(trace, values={"query": "emma"})
    assert journey.executed[0] == (10, "fill", "emma")
    assert final["history"][0]["text_source"] == "value:query"


def test_a_generated_non_secret_text_is_replayed_as_recorded(journey):
    journey.script = [("f10", "TYPE_TEXT"), ("c20", "CLICK"), ("c40", "CLICK"), ("c50", "CLICK")]
    agent = journey.agent("rec", values={})
    assert journey.run(agent)["status"] == "done"
    assert journey.executed[0] == (10, "fill", "generated text")
    journey.reset()
    _agent, final = journey.replay(journey.tmp_path / "rec" / "trace.jsonl", values={})
    assert journey.executed[0] == (10, "fill", "generated text")
    assert journey.asked == [] and final["status"] == "done" and final["text_calls"] == []
    assert final["history"][0]["text_source"] == "generated" and final["history"][0]["source"] == "replay"


def test_a_generated_text_recorded_as_a_secret_marker_heals(journey):
    journey.script = [("f10", "TYPE_TEXT"), ("c20", "CLICK"), ("c40", "CLICK"), ("c50", "CLICK")]
    agent = journey.agent("rec", values={"pw": Value(SECRET, secret=True)})
    journey.field_text = lambda context: (SECRET, {"model": "text-stub", "latency_ms": 1})
    journey.monkeypatch.setattr(loop, "field_text", journey.field_text)
    assert journey.run(agent)["status"] == "done"
    trace = journey.tmp_path / "rec" / "trace.jsonl"
    assert "<secret:pw>" in trace.read_text() and SECRET not in trace.read_text()
    journey.reset()
    journey.script = [("f10", "TYPE_TEXT"), ("c20", "CLICK")]
    _agent, final = journey.replay(trace, values={"pw": Value(SECRET, secret=True)})
    assert final["replay"]["steps_healed"] == 1
    assert sources(final) == ["model", "model", "replay", "replay"]


# --- divergence: heal --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cause, found_",
    [
        ("not_found", None),
        ("ambiguous", {"rule": "ambiguous", "unique": False, "count_match": False, "verified": False}),
        ("hint_only", {"rule": "hint", "verified": False, "hint": {"tag": "button", "text": "Row B"}}),
        ("not_observed", found("Row B", node=None)),
        ("kind_not_offered", found("Row B", node=999)),
    ],
)
def test_a_step_that_no_longer_resolves_heals_and_replay_resumes(journey, cause, found_):
    trace = journey.record()
    journey.resolves["Row B"] = found_
    journey.script = [("c40", "CLICK")]
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and final["verified"] is True
    assert journey.asked == ["Open the second result"]  # Only step 2 asked the model.
    assert sources(final) == ["replay", "replay", "model", "replay"]
    assert final["replay"] == {"steps_replayed": 2, "steps_healed": 1, "actions_replayed": 3, "model_calls": 1}
    events = load_trace(journey.tmp_path / "replayed" / "trace.jsonl")
    heal = next(e for e in events if e["type"] == "heal")
    assert (heal["step"], heal["cause"], heal["healed"], heal["model_calls"]) == (1, cause, True, 1)
    end = events[-1]
    assert end["totals"]["replay_actions"] == 3 and end["totals"]["healed_steps"] == 1
    assert end["totals"]["jev_calls"] == 1


def test_a_missing_locator_always_heals(journey):
    events = load_trace(journey.record())
    for event in events:
        if event["type"] == "action" and event["locator"] and event["locator"]["name"] == "Pin":
            event["locator"], event["locator_error"] = None, "ElementGone"
    journey.script = [("c50", "CLICK")]
    _agent, final = journey.replay(events)
    assert final["status"] == "done" and final["replay"]["steps_healed"] == 1
    assert journey.asked == ["Pin it"]


def test_a_control_that_is_no_longer_offered_diverges(journey):
    events = load_trace(journey.record())
    for event in events:
        if event["type"] == "action" and event["locator"]["name"] == "Pin":
            event.update(kind="key", choice="key:Escape")
    journey.script = [("c50", "CLICK")]
    _agent, final = journey.replay(events)
    assert final["replay"]["steps_healed"] == 1 and final["status"] == "done"


def test_recorded_actions_that_are_spent_without_the_checks_holding_heal(journey):
    trace = journey.record()
    original, dead = journey.act, []

    def act(action, page_, text=None):
        if action.get("node") == 40 and not dead:
            dead.append(1)  # Executed, but the page did not change this time.
            journey.executed.append((40, "click", None))
            return {"executed": action["id"]}
        return original(action, page_, text)

    journey.browser.act.side_effect = act
    journey.script = [("c40", "CLICK")]
    agent, final = journey.replay(trace)  # The fake page never changes: the stuck rule must not end a replay.
    assert agent.replay.causes == {1: "checks_unmet"}
    assert final["status"] == "done" and journey.asked == ["Open the second result"]
    assert sources(final) == ["replay", "replay", "replay", "model", "replay"]


@pytest.mark.parametrize("error, cause", [(StalePage("stale"), "stale_twice"), (Refused("no"), "refused_twice")])
def test_a_stale_or_refused_act_twice_in_a_row_diverges(journey, error, cause):
    trace = journey.record()
    journey.act_error = [None, None, error, type(error)(str(error))]  # Steps 1: fill, Go; step 2: two failures.
    journey.script = [("c40", "CLICK")]
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and journey.asked == ["Open the second result"]
    heal = next(e for e in load_trace(journey.tmp_path / "replayed" / "trace.jsonl") if e["type"] == "heal")
    assert heal["cause"] == cause


def test_one_stale_act_is_looked_at_again_and_replay_goes_on(journey):
    trace = journey.record()
    journey.act_error = [None, None, StalePage("stale")]
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and journey.asked == [] and final["replay"]["steps_healed"] == 0
    assert journey.executed.count((40, "click", None)) == 1  # Never repeated once executed.


def test_a_healed_runs_own_trace_replays_without_the_model(journey):
    trace = journey.record()
    journey.resolves["Row B"] = None
    journey.script = [("c40", "CLICK")]
    journey.replay(trace)
    journey.reset()
    journey.resolves.clear()
    _agent, final = journey.replay(journey.tmp_path / "replayed" / "trace.jsonl", name="again")
    assert final["status"] == "done" and final["verified"] and journey.asked == []
    assert final["replay"]["actions_replayed"] == 4


# --- divergence: no heal -------------------------------------------------------------------------------------------


def test_without_heal_a_divergence_ends_diverged_and_executes_nothing_more(journey):
    trace = journey.record()
    journey.resolves["Row B"] = None
    _agent, final = journey.replay(trace, heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "not_found at step 2"
    assert final["verified"] is False
    assert journey.executed == [(10, "fill", "dune"), (20, "click", None)]
    assert journey.asked == []
    events = load_trace(journey.tmp_path / "replayed" / "trace.jsonl")
    heal = next(e for e in events if e["type"] == "heal")
    assert (heal["step"], heal["healed"], heal["model_calls"]) == (1, False, 0)
    assert events[-1]["status"] == "diverged"
    with pytest.raises(ValueError, match="stopped"):
        _agent.command("tick")


# --- policy ----------------------------------------------------------------------------------------------------


def test_a_deny_hook_ends_a_replay_denied_like_a_model_action(journey):
    trace = journey.record()
    seen = []

    def deny(action, page_, text):
        seen.append((action["kind"], action["label"]))
        if action["label"] == "Pin":
            raise ActionDenied("no pinning")

    _agent, final = journey.replay(trace, before_action=deny)
    assert final["status"] == "denied" and final["denied"]["reason"] == "no pinning"
    assert (50, "click", None) not in journey.executed
    assert seen[-1] == ("click", "Pin") and journey.asked == []
    assert [h["action"] for h in final["history"]] == ["Search", "Go", "Row B"]


# --- history and trace fields -----------------------------------------------------------------------------------------


def test_history_and_trace_mark_replayed_and_model_actions(journey):
    trace = journey.record()
    recorded = load_trace(trace)
    assert {e["source"] for e in recorded if e["type"] == "action"} == {"model"}
    journey.resolves["Row B"] = None
    journey.script = [("c40", "CLICK")]
    _agent, final = journey.replay(trace)
    actions = [e for e in load_trace(journey.tmp_path / "replayed" / "trace.jsonl") if e["type"] == "action"]
    assert [e["source"] for e in actions] == sources(final) == ["replay", "replay", "model", "replay"]
    assert [e["step"] for e in actions] == [0, 0, 1, 2]


def test_run_start_says_it_is_a_replay(journey):
    trace = journey.record()
    _agent, _final = journey.replay(trace)
    start = load_trace(journey.tmp_path / "replayed" / "trace.jsonl")[0]
    assert start["replay"]["heal"] is True and start["replay"]["actions"] == [2, 1, 1]
    assert load_trace(trace)[0]["replay"] is None


def test_totals_of_an_unreplayed_run_have_zero_replay_counts(journey):
    events = load_trace(journey.record())
    assert events[-1]["totals"]["replay_actions"] == 0 and events[-1]["totals"]["healed_steps"] == 0


def test_the_replay_state_exists_only_on_a_replaying_agent(journey):
    journey.script = [("f10", "TYPE_TEXT", "query"), ("c20", "CLICK"), ("c40", "CLICK"), ("c50", "CLICK")]
    agent = journey.agent("plain")
    assert "replay" not in journey.run(agent)


def test_replay_waits_run_in_code(journey):
    events = load_trace(journey.record())
    index = next(i for i, e in enumerate(events) if e["type"] == "action" and e["locator"]["name"] == "Row B")
    wait = {**events[index], "kind": "wait", "choice": "wait", "locator": None}
    events.insert(index, wait)
    _agent, final = journey.replay(events)
    assert final["status"] == "done" and journey.asked == []
    assert [h["kind"] for h in final["history"]].count("wait") == 1


def test_a_page_that_stops_reacting_diverges_instead_of_ending_blocked(journey):
    trace = journey.record()
    original = journey.act

    def act(action, page_, text=None):
        if action.get("node") == 40:
            journey.executed.append((40, "click", None))  # Executed, but nothing changes: the checks stay unmet.
            return {"executed": action["id"]}
        return original(action, page_, text)

    journey.browser.act.side_effect = act
    _agent, final = journey.replay(trace, heal=False)
    # fill, Go (unchanged page) and the dead click are three unchanged actions in a row: the model loop would call it
    # blocked, a replay calls it what it is.
    assert final["status"] == "diverged" and final["stop_reason"] == "checks_unmet at step 2"
    assert [h["kind"] for h in final["history"]] == ["fill", "click", "click"]


def optimistic(journey):
    """Step 2 holds once and not again, like a press an optimistic UI applied and a server took back."""
    calls = []

    def flaky(_page):
        calls.append(1)
        return journey.flags["opened"] and len(calls) != 2

    goals = journey.steps()
    goals[1] = Step(goals[1].goal, expect=expect.check(flaky, "opened", auto=True))
    return goals


def test_checks_that_do_not_stay_held_heal_a_replay_instead_of_rejecting_it(journey):
    trace = journey.record()
    journey.script = [("c40", "CLICK")]
    agent, final = journey.replay(trace, goals=optimistic(journey))
    assert final["status"] == "done" and final["verified"] and journey.asked == ["Open the second result"]
    assert agent.replay.causes == {1: "checks_unstable"}
    assert "done_rejected" not in [h["kind"] for h in final["history"]]
    events = load_trace(journey.tmp_path / "replayed" / "trace.jsonl")
    assert not [e for e in events if e["type"] == "done_rejected"]


def test_checks_that_do_not_stay_held_end_a_replay_diverged_without_heal(journey):
    trace = journey.record()
    _agent, final = journey.replay(trace, goals=optimistic(journey), heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "checks_unstable at step 2"
    assert "done_rejected" not in [h["kind"] for h in final["history"]] and journey.asked == []


# --- trace schema 2 reads schema 1 -----------------------------------------------------------------------------


def as_schema_1(events):
    """What a schema 1 writer produced: no heal events, and none of the action fields schema 2 added."""
    old = []
    for event in events:
        if event["type"] == "heal":
            continue
        event = dict(event)
        if event["type"] == "run_start":
            event["trace_schema"] = 1
        if event["type"] == "action":
            for name in ("step", "source", "option", "delta", "focus", "focus_locator", "page_url"):
                event.pop(name, None)
        old.append(event)
    return old


def test_a_schema_1_trace_still_loads_and_replays(journey, tmp_path):
    from jev_ultrafast import trace as traces

    assert traces.SCHEMA == 2 and traces.READABLE == (1, 2)
    old = as_schema_1(load_trace(journey.record()))
    path = tmp_path / "old.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in old))
    assert load_trace(path)[0]["trace_schema"] == 1
    _agent, final = journey.replay(path)
    assert final["status"] == "done" and final["verified"] and journey.asked == []
    assert sources(final) == ["replay"] * 4


def test_a_heal_event_in_a_schema_1_trace_is_an_error(journey, tmp_path):
    events = as_schema_1(load_trace(journey.record()))
    heal = {"type": "heal", "t_ms": 1, "step": 0, "cause": "x", "healed": False, "model_calls": 0}
    events.insert(-1, heal)
    path = tmp_path / "bad.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(ValueError, match="heal is a trace_schema 2 event"):
        load_trace(path)
    with pytest.raises(ValueError, match="heal is a trace_schema 2 event"):
        journey.agent("x", replay=events)
    events[0]["trace_schema"] = 3
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(ValueError, match="readable: 1, 2"):
        load_trace(path)
    with pytest.raises(ValueError, match="trace_schema 1 or 2"):
        journey.agent("y", replay=events)


def test_the_plan_carries_what_schema_2_added():
    from jev_ultrafast import replay

    base = {"type": "action", "t_ms": 1, "index": 1, "label": "x", "locator": None, "text_source": None, "text": None,
            "result": None}
    events = [
        {"type": "step", "t_ms": 0, "index": 0, "status": "active"},
        {**base, "kind": "key", "choice": "key:Enter", "focus": None},
        {**base, "kind": "key", "choice": "key:Tab"},
        {**base, "kind": "goto", "choice": "goto:home", "url": "https://example.test/"},
        {**base, "kind": "scroll", "choice": "scroll_down", "delta": 560},
    ]
    keys, tab, goto, scroll = replay.plan(events, 1)[0]
    assert keys.focus_recorded and keys.focus is None and not tab.focus_recorded
    assert goto.url == "https://example.test/" and scroll.delta == 560


# --- a replayed action is never executed twice ----------------------------------------------------------------


def test_a_stale_observation_after_an_executed_action_never_executes_it_again(journey):
    trace = journey.record()
    journey.stale_after = 20  # The page does not settle right after Go ran.
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and final["verified"] and journey.asked == []
    assert journey.executed.count((20, "click", None)) == 1
    assert journey.executed == [(10, "fill", "dune"), (20, "click", None), (40, "click", None), (50, "click", None)]


def test_a_refusal_before_input_is_looked_at_again_and_executes_once(journey):
    trace = journey.record()
    journey.act_error = [None, Refused("covered")]  # The fill runs; Go is refused once before any input.
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and journey.asked == []
    assert journey.executed.count((20, "click", None)) == 1
    refusals = [e for e in load_trace(journey.tmp_path / "replayed" / "trace.jsonl") if e["type"] == "refused"]
    assert len(refusals) == 1


# --- a key goes where it went ---------------------------------------------------------------------------------


MOVED = {"role": "button", "label": "Delete", "href": None, "tag": "button", "type": None, "node": 20}


def test_a_recorded_key_is_pressed_only_on_the_element_that_had_focus(journey):
    journey.page_args = {"focus": FOCUS}
    trace = journey.record_controls(("key:Enter", "PRESS_KEY"))
    key = next(e for e in load_trace(trace) if e["type"] == "action" and e["kind"] == "key")
    assert key["focus"] == {k: v for k, v in FOCUS.items() if k != "node"}

    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and journey.asked == [] and (None, "key", None) in journey.executed

    journey.reset()
    journey.page_args = {"focus": MOVED}
    journey.script = [("f10", "TYPE_TEXT", "query"), ("c20", "CLICK")]
    agent, final = journey.replay(trace, name="moved")
    assert agent.replay.causes == {0: "focus_changed"} and final["status"] == "done"
    assert not [e for e in journey.executed if e[1] == "key"]
    assert journey.asked == ["Search for dune"] * 2

    journey.reset()
    _agent, final = journey.replay(trace, name="stopped", heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "focus_changed at step 1"
    assert journey.executed == [] and journey.asked == []


def test_a_key_without_a_recorded_focus_always_heals(journey):
    journey.page_args = {"focus": FOCUS}
    old = as_schema_1(load_trace(journey.record_controls(("key:Enter", "PRESS_KEY"))))
    _agent, final = journey.replay(old, heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "focus_changed at step 1"
    assert journey.executed == []

    journey.reset()
    journey.page_args = {}  # Even where nothing has focus now: a schema 1 trace never said where the key went.
    _agent, final = journey.replay(old, name="unfocused", heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "focus_changed at step 1"


def test_no_focus_then_and_now_is_the_same_focus(journey):
    trace = journey.record_controls(("key:Enter", "PRESS_KEY"))  # Nothing had focus.
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and journey.asked == []


# --- goto and scroll -------------------------------------------------------------------------------------------


def test_goto_opens_only_the_url_it_opened(journey):
    trace = journey.record_controls(("goto:home", "GOTO"))
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and journey.asked == []

    journey.reset()
    journey.page_args = {"goto_url": "https://staging.example.test/"}
    _agent, final = journey.replay(trace, name="elsewhere", heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "goto_url_changed at step 1"
    assert journey.executed == []

    journey.reset()
    journey.script = [("f10", "TYPE_TEXT", "query"), ("c20", "CLICK")]
    agent, final = journey.replay(trace, name="healed")
    assert agent.replay.causes == {0: "goto_url_changed"} and final["status"] == "done"


def test_goto_compares_query_keys_not_values():
    from jev_ultrafast import replay

    page_ = [{"id": "goto:home", "kind": "goto", "url": "https://example.test/a?t=2&u=1"}]
    same = replay.Planned("goto", "goto:home", url="https://example.test/a?t=<redacted>&u=<redacted>")
    other = replay.Planned("goto", "goto:home", url="https://example.test/a?t=<redacted>&v=<redacted>")
    assert replay.control_action(same, page_)[1] is None
    assert replay.control_action(other, page_) == (None, "goto_url_changed")


def test_a_scroll_replays_the_distance_it_scrolled(journey):
    trace = journey.record_controls(("scroll_down", "SCROLL_DOWN"))
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and journey.asked == []

    journey.reset()
    journey.page_args = {"delta": 400}
    _agent, final = journey.replay(trace, name="shorter", heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "scroll_changed at step 1"
    assert journey.executed == []


# --- matching guards -------------------------------------------------------------------------------------------


def test_a_select_without_a_recorded_option_heals():
    from jev_ultrafast import replay

    actions = [{"id": "o", "kind": "select", "node": 7, "value": None, "label": "x"}]
    planned = replay.Planned("select", "o", option=None)
    assert replay.element_action(planned, actions, 7) == (None, "option_unavailable")


def test_a_value_whose_secrecy_changed_is_not_replayed(journey):
    trace = journey.record()
    with pytest.raises(ValueError, match="replay_mismatch: value secrecy differs"):
        journey.agent("a", replay=trace, values={"query": Value("dune-secret", secret=True)})
    secret = journey.agent("b", values={"query": Value("dune-secret", secret=True)})
    secret.close()


# --- replay-only shorter quiet window --------------------------------------------------------------------------


QUIET = {"inflight": 0, "new_started": 0, "timers": 0}


def quiet_windows(journey, activity, **kwargs):
    trace = journey.record()
    journey.browser.action_settle_quiet_ms = 150
    journey.browser.activity.return_value = activity
    journey.browser.settled.reset_mock()
    _agent, final = journey.replay(trace, **kwargs)
    assert final["status"] == "done"
    return [call.kwargs.get("quiet_ms") for call in journey.browser.settled.call_args_list]


def test_a_replay_waits_the_short_window_when_nothing_could_change_the_page(journey):
    assert quiet_windows(journey, QUIET) == [150, 150, 150]


@pytest.mark.parametrize("busy", [{"inflight": 1}, {"new_started": 2}, {"timers": 1}])
def test_a_replay_keeps_the_full_window_when_a_request_or_timer_is_pending(journey, busy):
    assert quiet_windows(journey, {**QUIET, **busy}) == [None, None, None]


def test_a_replay_keeps_the_full_window_for_a_check_that_is_not_cheap(journey):
    plain = [Step(s.goal, expect=expect.check(lambda _p: True, "x")) for s in journey.steps()]
    assert Step is not None and plain
    journey.browser.activity.return_value = QUIET
    journey.browser.action_settle_quiet_ms = 150
    agent = journey.agent("manual", goals=journey.steps())
    agent.state["page"] = agent.observe()
    agent.state["plan_index"] = 0
    agent.replay = None
    assert agent.quiet_replay() is False  # Not a replay at all: a model run keeps its verification contract.


def test_a_replay_keeps_the_full_window_when_the_post_action_settle_is_off(journey):
    journey.browser.action_settle_timeout_ms = 0
    assert quiet_windows(journey, QUIET) == [None, None, None]


def test_a_model_run_never_gets_the_short_window(journey):
    journey.script = [("f10", "TYPE_TEXT", "query"), ("c20", "CLICK"), ("c40", "CLICK"), ("c50", "CLICK")]
    journey.browser.activity.return_value = QUIET
    journey.browser.action_settle_quiet_ms = 150
    final = journey.run(journey.agent("model"))
    assert final["status"] == "done"
    assert all("quiet_ms" not in call.kwargs for call in journey.browser.settled.call_args_list)


# --- fix round 2 ------------------------------------------------------------------------------------------------


def test_a_changed_digest_is_its_own_cause():
    from jev_ultrafast import replay

    assert replay.unresolved({"rule": "ambiguous", "digest_changed": True}) == "digest_changed"
    assert replay.unresolved({"rule": "ambiguous", "digest_changed": False}) == "ambiguous"
    assert replay.unresolved({"rule": "ambiguous"}) == "ambiguous"


LINK = {"role": "link", "label": "Read more", "href": "/a/1", "tag": "a", "type": None, "node": 10}


def test_two_look_alike_focused_links_with_different_hrefs_are_not_the_same_focus():
    from jev_ultrafast import replay

    HERE = "https://example.test/"

    key = replay.Planned("key", "key:Enter", focus=LINK, focus_recorded=True, focus_locator={"name": "Read more"},
                         page_url=HERE)
    actions = [{"id": "key:Enter", "kind": "key"}]
    assert replay.control_action(key, actions, LINK, focus_node=10, page_url=HERE)[1] is None
    other = {**LINK, "href": "/a/2"}
    assert replay.control_action(key, actions, other, focus_node=10, page_url=HERE) == (None, "focus_changed")
    # The query VALUES are redacted in the trace and never compared; the keys are.
    recorded = replay.Planned(
        "key", "key:Enter", focus={**LINK, "href": "/a?id=<redacted>"}, focus_recorded=True, focus_locator={"x": 1},
        page_url=HERE,
    )
    same, other_keys = {**LINK, "href": "/a?id=7"}, {**LINK, "href": "/a?other=7"}
    assert replay.control_action(recorded, actions, same, focus_node=10, page_url=HERE)[1] is None
    assert replay.control_action(recorded, actions, other_keys, focus_node=10, page_url=HERE)[1] == "focus_changed"


def test_a_focused_element_must_resolve_like_a_click_to_the_focused_node(journey):
    journey.page_args = {"focus": FOCUS}
    trace = journey.record_controls(("key:Enter", "PRESS_KEY"))
    key = next(e for e in load_trace(trace) if e["type"] == "action" and e["kind"] == "key")
    assert key["focus_locator"]["name"] == "Search"

    journey.reset()
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and journey.asked == [] and (None, "key", None) in journey.executed

    # Same role, label, tag and type, but the focused element is another node than the one the locator finds.
    journey.reset()
    journey.page_args = {"focus": {**FOCUS, "node": 99}}
    _agent, final = journey.replay(trace, name="lookalike", heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "focus_changed at step 1"
    assert journey.executed == []

    # The locator no longer resolves at all (ambiguous or gone): the key is not pressed.
    journey.reset()
    journey.page_args = {"focus": FOCUS}
    journey.resolves["Search"] = {"rule": "ambiguous", "unique": False, "count_match": True, "verified": False}
    _agent, final = journey.replay(trace, name="unresolved", heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "focus_changed at step 1"
    assert journey.executed == []


def test_a_key_recorded_without_a_focus_locator_heals_when_something_has_focus(journey):
    journey.page_args = {"focus": FOCUS}
    events = load_trace(journey.record_controls(("key:Enter", "PRESS_KEY")))
    for event in events:
        if event["type"] == "action" and event["kind"] == "key":
            event.pop("focus_locator", None)
    _agent, final = journey.replay(events, heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "focus_changed at step 1"


def test_unknown_activity_keeps_the_full_window(journey):
    assert quiet_windows(journey, None) == [None, None, None]


def test_the_replay_window_is_a_knob(journey):
    assert quiet_windows(journey, QUIET, replay_quiet_ms=300) == [300, 300, 300]


def test_no_replay_window_means_the_full_window_always(journey):
    assert quiet_windows(journey, QUIET, replay_quiet_ms=None) == [None, None, None]


@pytest.mark.parametrize("bad", [-1, 1.5, "150", True])
def test_the_replay_window_must_be_a_non_negative_integer_or_none(journey, bad):
    with pytest.raises(ValueError, match="replay_quiet_ms"):
        journey.agent("x", replay_quiet_ms=bad)


PAGE_URL = "https://example.test/list?page=<redacted>"


@pytest.mark.parametrize(
    "kind, choice, operation",
    [("scroll", "scroll_down", "SCROLL_DOWN"), ("back", "back", "BACK"), ("reload", "reload", "RELOAD"),
     ("wait", "wait", "WAIT")],
)
def test_back_reload_scroll_and_wait_run_only_on_the_page_they_were_recorded_on(journey, kind, choice, operation):
    journey.page_args = {"url": "https://example.test/list?page=1"}
    trace = journey.record_controls((choice, operation))
    action = next(e for e in load_trace(trace) if e["type"] == "action" and e["kind"] == kind)
    assert action["page_url"] == PAGE_URL

    journey.reset()
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and journey.asked == []
    assert any(e[1] == kind for e in journey.executed)

    journey.reset()
    journey.page_args = {"url": "https://example.test/elsewhere"}
    _agent, final = journey.replay(trace, name="elsewhere", heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "page_changed at step 1"
    assert not [e for e in journey.executed if e[1] == kind]

    journey.reset()  # A different query KEY is another page; a different query value is the same page.
    journey.page_args = {"url": "https://example.test/list?page=2"}
    _agent, final = journey.replay(trace, name="same-keys", heal=False)
    assert final["status"] == "done"
    journey.reset()
    journey.page_args = {"url": "https://example.test/list?sort=2"}
    _agent, final = journey.replay(trace, name="other-keys", heal=False)
    assert final["stop_reason"] == "page_changed at step 1"


def test_a_control_recorded_without_a_page_url_heals(journey):
    journey.page_args = {"url": "https://example.test/list?page=1"}
    events = load_trace(journey.record_controls(("scroll_down", "SCROLL_DOWN")))
    for event in events:
        if event["type"] == "action":
            event.pop("page_url", None)
    _agent, final = journey.replay(events, heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "page_changed at step 1"


def test_the_plan_carries_the_page_url_and_the_focus_locator():
    from jev_ultrafast import replay

    base = {"type": "action", "t_ms": 1, "index": 1, "label": "x", "locator": None, "text_source": None, "text": None,
            "result": None}
    events = [
        {"type": "step", "t_ms": 0, "index": 0, "status": "active"},
        {**base, "kind": "key", "choice": "key:Enter", "focus": {"role": "link"}, "focus_locator": {"name": "x"},
         "page_url": "https://example.test/a"},
    ]
    (key,) = replay.plan(events, 1)[0]
    assert key.focus_locator == {"name": "x"} and key.page_url == "https://example.test/a"


# --- fix round 3: the page binding of every kind ------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["click", "fill", "select", "hover", "key"])
def test_a_mutating_kind_needs_the_recorded_host_and_path(kind):
    from jev_ultrafast import replay

    def planned(**over):
        return replay.Planned(kind, "x", page_url="https://example.test/list?page=<redacted>", **over)

    here = "https://example.test/list?page=2"
    assert replay.page_cause(planned(), here) is None
    assert replay.page_cause(planned(), "https://example.test/list?other=1#top") is None  # Query and fragment vary.
    for moved in ("https://example.test/elsewhere", "https://example.test/list/2", "https://other.test/list?page=2",
                  "http://example.test/list?page=2", None):
        assert replay.page_cause(planned(), moved) == "page_changed", moved
    assert replay.page_cause(replay.Planned(kind, "x"), here) == "page_changed"  # Schema 2 always says where.
    old = replay.Planned(kind, "x", page_required=False)
    assert replay.page_cause(old, here) is None  # Schema 1 never did.


def test_goto_is_not_bound_to_a_page():
    from jev_ultrafast import replay

    goto = replay.Planned("goto", "goto:home", url="https://example.test/")
    assert replay.page_cause(goto, "https://x.test/") is None


def test_a_click_runs_only_on_the_route_it_was_recorded_on(journey):
    trace = journey.record()
    journey.page_args = {"url": "https://example.test/?page=2"}  # Same host and path, other query: still replayed.
    _agent, final = journey.replay(trace, heal=False)
    assert final["status"] == "done" and journey.asked == []

    journey.reset()
    journey.page_args = {"url": "https://example.test/settings"}
    _agent, final = journey.replay(trace, name="other-path", heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "page_changed at step 1"
    assert journey.executed == []

    journey.reset()
    journey.page_args = {"url": "https://other.test/"}
    _agent, final = journey.replay(trace, name="other-host", heal=False)
    assert final["stop_reason"] == "page_changed at step 1" and journey.executed == []


def test_a_heal_that_leaves_the_route_does_not_replay_the_next_recorded_click(journey):
    trace = journey.record()
    journey.reset()
    plain_act = journey.act

    def act(action, page_, text=None):
        result = plain_act(action, page_, text)
        if action.get("node") == 20:  # The healed step's Go opens another page.
            journey.page_args = {"url": "https://example.test/elsewhere"}
        return result

    journey.browser.act.side_effect = act
    journey.resolves["Search"] = found("Search", rule="ambiguous", verified=False)
    journey.script = [("f10", "TYPE_TEXT", "query"), ("c20", "CLICK"), ("c40", "CLICK"), ("c50", "CLICK")]
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and sources(final) == ["model"] * 4
    causes = [(e["step"], e["cause"]) for e in load_trace(journey.tmp_path / "replayed" / "trace.jsonl")
              if e["type"] == "heal"]
    assert causes == [(0, "ambiguous"), (1, "page_changed"), (2, "page_changed")]
    assert len(journey.asked) == 4  # Step 1 fill and Go, then Row B and Pin.


def test_a_schema_1_click_is_not_bound_to_a_page(journey, tmp_path):
    old = as_schema_1(load_trace(journey.record()))
    path = tmp_path / "old-pages.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in old))
    journey.page_args = {"url": "https://example.test/elsewhere"}
    _agent, final = journey.replay(path, heal=False)
    assert final["status"] == "done"


def test_replay_uses_the_callers_viewport_not_the_recorded_one(journey):
    trace = journey.record()
    recorded = load_trace(trace)[0]["viewport"]
    agent, final = journey.replay(trace, viewport=(1470, 920))
    assert final["status"] == "done" and agent.viewport == (1470, 920)
    start = load_trace(journey.tmp_path / "replayed" / "trace.jsonl")[0]
    assert recorded == {"width": 1120, "height": 780}
    assert start["viewport"] == {"width": 1470, "height": 920}


# --- id-tolerant routes -------------------------------------------------------------------------------------------

UUID_A, UUID_B = "3f2b8c1e-5d4a-4e7b-9c6d-1a2b3c4d5e6f", "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


@pytest.mark.parametrize(
    "a,b,same",
    [
        (f"/chat/{UUID_A}", f"/chat/{UUID_B}", True),
        (f"/chat/{UUID_A.upper()}", f"/chat/{UUID_B}", True),
        ("/chat/new", f"/chat/{UUID_A}", False),
        ("/a/123", "/a/124", True),
        ("/a/123", "/a/settings", False),
        ("/a/settings", "/a/admin", False),
        ("/o/deadbeef01", "/o/0badcafe99", True),  # Hex run of 8+ with a digit.
        ("/o/deadbeef", "/o/facecafe", False),  # No digit: a word, not an id.
        ("/o/abc123", "/o/def456", False),  # Hex but shorter than 8.
        ("/s/Zm9vYmFyMTIzNDU2Nzg5", "/s/QmF6cXV4OTg3NjU0MzIx", True),  # base64url-ish, letters and digits.
        ("/s/averyverylongsegmentname", "/s/anotherverylongsegmentx", False),  # 16+ but no digit.
        (f"/chat/{UUID_A}/messages", f"/chat/{UUID_B}/messages", True),
        (f"/chat/{UUID_A}/messages", f"/chat/{UUID_B}/files", False),
        (f"/chat/{UUID_A}", f"/room/{UUID_B}", False),
    ],
)
def test_id_like_path_segments_are_compared_by_shape(a, b, same):
    from jev_ultrafast import replay

    host = "https://example.test"
    assert (replay.route(host + a) == replay.route(host + b)) is same
    planned = replay.Planned("click", "x", page_url=host + a)
    assert (replay.page_cause(planned, host + b) is None) is same
    wait = replay.Planned("wait", "wait", page_url=host + a + "?q=<redacted>")
    assert (replay.page_cause(wait, host + b + "?q=1") is None) is same


def test_a_page_bound_kind_still_needs_the_same_query_keys_and_host():
    from jev_ultrafast import replay

    wait = replay.Planned("reload", "reload", page_url=f"https://example.test/chat/{UUID_A}?tab=<redacted>")
    assert replay.page_cause(wait, f"https://example.test/chat/{UUID_B}?tab=2") is None
    assert replay.page_cause(wait, f"https://example.test/chat/{UUID_B}?other=2") == "page_changed"
    assert replay.page_cause(wait, f"https://other.test/chat/{UUID_B}?tab=2") == "page_changed"


def test_a_goto_url_check_stays_exact():
    from jev_ultrafast import replay

    goto = replay.Planned("goto", "goto:chat", url=f"https://example.test/chat/{UUID_A}")
    declared = {"id": "goto:chat", "kind": "goto", "url": f"https://example.test/chat/{UUID_B}"}
    assert replay.control_action(goto, [declared]) == (None, "goto_url_changed")


def test_a_declared_route_pattern_is_compared_by_pattern():
    from jev_ultrafast import replay

    routes = replay.route_patterns([r"^/orders/[^/]+$"])
    host = "https://example.test"
    assert replay.route(host + "/orders/pending", routes) == replay.route(host + "/orders/archived", routes)
    assert replay.route(host + "/orders/pending") != replay.route(host + "/orders/archived")
    assert replay.route(host + "/orders/pending", routes) != replay.route(host + "/invoices/pending", routes)
    assert replay.route(host + "/orders/a/b", routes) != replay.route(host + "/orders/a/c", routes)
    planned = replay.Planned("fill", "x", page_url=host + "/orders/pending")
    assert replay.page_cause(planned, host + "/orders/archived") == "page_changed"
    assert replay.page_cause(planned, host + "/orders/archived", routes) is None


@pytest.mark.parametrize("bad", ["^/a$", (1,), [1], ["("], [b"x"], {"a": 1}])
def test_replay_routes_must_be_a_list_of_regular_expressions(bad):
    from jev_ultrafast import replay

    with pytest.raises(ValueError, match="replay_routes"):
        replay.route_patterns(bad)


def test_replay_routes_accepts_compiled_patterns_and_none():
    import re

    from jev_ultrafast import replay

    assert replay.route_patterns(None) == ()
    assert replay.route_patterns([re.compile("^/x$")])[0].pattern == "^/x$"


def test_an_agent_with_a_bad_replay_route_opens_no_browser(monkeypatch):
    opened = Mock()
    monkeypatch.setattr(loop, "Browser", opened)
    with pytest.raises(ValueError, match="replay_routes"):
        Agent("https://example.test/", "goal", replay_routes=["("])
    opened.assert_not_called()


# --- fullmatch routes, the id-segment switch ------------------------------------------------------------------


def test_a_declared_route_pattern_must_match_the_whole_path():
    from jev_ultrafast import replay

    host = "https://example.test"
    unanchored = replay.route_patterns([r"/orders/"])
    assert replay.route(host + "/orders/1/cancel", unanchored) != replay.route(host + "/orders/2/refund", unanchored)
    assert replay.route(host + "/orders/1/cancel", unanchored) == replay.route(host + "/orders/2/cancel", ())  # ids
    prefix = replay.route_patterns([r"^/orders/[^/]+"])
    assert replay.route(host + "/orders/pending", prefix) == replay.route(host + "/orders/archived", prefix)
    assert replay.route(host + "/orders/pending", prefix) != replay.route(host + "/orders/pending/delete", prefix)
    planned = replay.Planned("click", "x", page_url=host + "/orders/1/cancel")
    assert replay.page_cause(planned, host + "/orders/2/refund", unanchored) == "page_changed"
    whole = replay.route_patterns([r"/orders/.*"])  # Matching everything below is still possible, on purpose.
    assert replay.page_cause(planned, host + "/orders/2/refund", whole) is None


def test_id_segments_off_compares_paths_exactly_and_declared_routes_still_apply():
    from jev_ultrafast import replay

    host = "https://example.test"
    planned = replay.Planned("click", "x", page_url=f"{host}/users/123/delete")
    assert replay.page_cause(planned, f"{host}/users/124/delete") is None
    assert replay.page_cause(planned, f"{host}/users/124/delete", id_segments=False) == "page_changed"
    assert replay.page_cause(planned, f"{host}/users/123/delete", id_segments=False) is None
    wait = replay.Planned("wait", "wait", page_url=f"{host}/chat/{UUID_A}?q=<redacted>")
    assert replay.page_cause(wait, f"{host}/chat/{UUID_B}?q=1", id_segments=False) == "page_changed"
    routes = replay.route_patterns([r"/users/[^/]+/delete"])
    assert replay.page_cause(planned, f"{host}/users/124/delete", routes, id_segments=False) is None


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_replay_id_segments_must_be_a_bool_and_opens_no_browser(bad, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(loop, "Browser", opened)
    with pytest.raises(ValueError, match="replay_id_segments"):
        Agent("https://example.test/", "goal", replay_id_segments=bad)
    opened.assert_not_called()


def test_a_replay_on_another_id_runs_by_default_and_diverges_with_id_segments_off(journey):
    journey.page_args = {"url": "https://example.test/users/123/delete"}
    trace = journey.record()
    journey.page_args = {"url": "https://example.test/users/124/delete"}
    _agent, final = journey.replay(trace)
    assert final["status"] == "done" and journey.asked == []
    journey.reset()
    _agent, final = journey.replay(trace, name="exact", replay_id_segments=False, heal=False)
    assert final["status"] == "diverged" and final["stop_reason"] == "page_changed at step 1"
    assert journey.executed == []


# --- a timeout in the middle of a replay or a run -------------------------------------------------------------


def test_a_mutation_timeout_ends_the_run_failed_and_the_action_is_never_sent_again(journey):
    journey.act_error = [MutationTimeout("Input.dispatchMouseEvent timed out after 15s", "Input.dispatchMouseEvent")]
    journey.script = [("c20", "CLICK")]
    agent = journey.agent("lost")
    final = journey.run(agent)
    assert final["status"] == "failed" and final["stop_reason"] == "mutation_timeout: Input.dispatchMouseEvent"
    assert [(h["kind"], h["executed"]) for h in final["history"]] == [("click", "unknown")]
    assert journey.browser.act.call_count == 1
    with pytest.raises(ValueError, match="stopped"):  # A later tick never reaches act again.
        agent.command("tick")
    assert journey.browser.act.call_count == 1
    events = load_trace(journey.tmp_path / "lost" / "trace.jsonl")
    timed_out = next(e for e in events if e["type"] == "mutation_timeout")
    assert timed_out["method"] == "Input.dispatchMouseEvent" and timed_out["kind"] == "click"
    action = next(e for e in events if e["type"] == "action")
    assert action["executed"] == "unknown" and action["index"] == timed_out["index"] == 1
    assert events[-1]["type"] == "run_end" and events[-1]["stop_reason"] == "mutation_timeout: Input.dispatchMouseEvent"
    assert events[-1]["status"] == "failed"


def test_a_mutation_timeout_in_a_replay_ends_the_run_the_same_way(journey):
    trace = journey.record()
    journey.act_error = [None, MutationTimeout("Input.insertText timed out", "Input.insertText")]
    _agent, final = journey.replay(trace)
    assert final["status"] == "failed" and final["stop_reason"] == "mutation_timeout: Input.insertText"
    assert [h.get("executed") for h in final["history"]] == [None, "unknown"]
    assert len(journey.executed) == 1 and journey.asked == []  # The fill ran; the one that timed out never did again.


def test_a_read_timeout_when_resolving_in_a_replay_looks_again_instead_of_diverging(journey):
    trace = journey.record()
    answers = [ReadTimeout("Runtime.evaluate timed out")]
    original = journey.resolve

    def resolve(locator, node=False):
        if answers:
            raise answers.pop()
        return original(locator, node)

    journey.browser.resolve.side_effect = resolve
    _agent, final = journey.replay(trace, heal=False)
    assert final["status"] == "done" and journey.asked == [] and final["replay"]["steps_healed"] == 0


def test_a_read_timeout_when_resolving_a_key_focus_is_not_a_missing_focus():
    agent = Agent.__new__(Agent)
    agent.browser = Mock(resolve=Mock(side_effect=ReadTimeout("Runtime.evaluate timed out")))
    planned = Mock(kind="key", focus_locator={"role": "textbox"})
    with pytest.raises(ReadTimeout):
        agent.focus_node(planned)
    agent.browser.resolve.side_effect = RuntimeError("gone")
    assert agent.focus_node(planned) is None


def test_two_read_timeouts_in_a_row_end_a_replay_failed_instead_of_raising(journey):
    trace = journey.record()
    calls = []

    def observe(**_kw):
        calls.append(1)
        if len(calls) > 1:
            raise ReadTimeout("Runtime.evaluate timed out")
        return page()

    journey.browser.observe.side_effect = observe
    journey.browser.fresh.return_value = False  # Every tick wants a fresh look.
    _agent, final = journey.replay(trace)
    assert final["status"] == "failed" and final["stop_reason"] == "read_timeout"
    assert journey.executed == []


def test_two_read_timeouts_in_a_row_end_a_model_run_failed_and_the_tick_re_observe_is_protected(journey):
    calls = []

    def observe(**_kw):
        calls.append(1)
        if len(calls) > 1:
            raise ReadTimeout("Runtime.evaluate timed out")
        return page()

    journey.browser.observe.side_effect = observe
    journey.browser.fresh.side_effect = ReadTimeout("Runtime.evaluate timed out")
    journey.script = [("c20", "CLICK")]
    agent = journey.agent("slow")
    final = journey.run(agent)
    assert final["status"] == "failed" and final["stop_reason"] == "read_timeout"
    journey.browser.act.assert_not_called()


def test_a_locator_read_is_tried_once_more_after_a_read_timeout(journey):
    answers = [ReadTimeout("Runtime.evaluate timed out")]
    original = journey.browser.locator.side_effect

    def locator(node, full_urls=False):
        if answers:
            raise answers.pop()
        return original(node, full_urls)

    journey.browser.locator.side_effect = locator
    trace = journey.record()
    actions = [e for e in load_trace(trace) if e["type"] == "action"]
    assert all(e["locator"] and not e.get("locator_error") for e in actions)
    journey.browser.locator.side_effect = Mock(side_effect=ReadTimeout("slow"))
    agent = journey.agent("twice")
    journey.script = [("f10", "TYPE_TEXT", "query"), ("c20", "CLICK"), ("c40", "CLICK"), ("c50", "CLICK")]
    journey.run(agent)
    again = [e for e in load_trace(journey.tmp_path / "twice" / "trace.jsonl") if e["type"] == "action"]
    assert {e["locator_error"] for e in again} == {"ReadTimeout"}
    assert journey.browser.locator.side_effect.call_count == 8  # Each of the 4 actions read twice, then gave up.
