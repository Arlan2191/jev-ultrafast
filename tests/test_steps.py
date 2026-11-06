"""Offline contracts for steps, checked postconditions and caller-provided values. No paid APIs, no browser."""

import html
import json
import re
import time
import urllib.parse
from unittest.mock import Mock

import pytest

from jev_ultrafast import Agent, Step, Value, expect, model, questions
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import fingerprint
from jev_ultrafast.waits import WaitResult

SECRET = "hunter2-correct-horse"


def page(extra_text="", value=""):
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search " + extra_text,
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": value, "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
        "marker": ["m", extra_text, value],
        "page_key": ["k"],
        "guards": {"10": ["g", value], "20": ["g", ""]},
    }
    state["fingerprint"] = fingerprint(state)
    return state


def decision(action="e1", operation="TYPE_TEXT", value=None):
    return {
        "choice": action,
        "operation": operation,
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
        **({"value": value} if value else {}),
    }


class Fake:
    """A Mock browser whose condition probes answer from `met`."""

    def __init__(self, observed=None):
        self.met = True
        self.observed = observed or page()
        self.browser = Mock()
        self.browser.observe.side_effect = lambda **_kw: self.observed
        self.browser.fresh.return_value = True
        self.browser.act.return_value = {"executed": "x"}
        self.browser.evaluate.return_value = "https://example.test/"
        self.browser.probe.side_effect = lambda kind, timeout_ms=0, **_a: WaitResult(self.met, 0, f"{kind} reason")


def make(monkeypatch, goals="Find a book", observed=None, **kwargs):
    fake = Fake(observed)
    monkeypatch.setattr(loop, "Browser", Mock(return_value=fake.browser))
    agent = Agent("https://example.test/", goals, **kwargs)
    agent.state["started_at"] = time.perf_counter()
    agent.fake = fake
    return agent


def do(agent, action="DONE", operation="DONE", value=None):
    agent.state["decision"] = decision(action, operation, value)
    return agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})


def button():
    return expect.element(role="button", name="Go")


# --- constructors ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "built, description",
    [
        (expect.text("Saved"), 'text contains "Saved"'),
        (expect.no_text("Error"), 'text does not contain "Error"'),
        (expect.text(re.compile("Sav(ed|ing)")), "text matches /Sav(ed|ing)/"),
        (expect.element(role="button", name="Upvote", state={"pressed": True}), 'element button "Upvote" pressed=true'),
        (expect.element(role="checkbox", state=["checked", "disabled"]), "element checkbox checked=true disabled=true"),
        (expect.no_element(role="dialog"), "no element dialog"),
        (expect.url("/results"), 'url contains "/results"'),
        (expect.check(lambda b: True, "the cart has two rows"), "the cart has two rows"),
    ],
)
def test_expectations_have_human_descriptions(built, description):
    assert built.description == description


@pytest.mark.parametrize(
    "build",
    [
        lambda: expect.text(""),
        lambda: expect.text(3),
        lambda: expect.text(re.compile(r"(?P<x>a)")),
        lambda: expect.element(),
        lambda: expect.element(role="button", state="glowing"),
        lambda: expect.url(None),
        lambda: expect.check("not callable", "d"),
        lambda: expect.check(lambda b: True, ""),
        lambda: expect.text("x", timeout_ms=-1),
        lambda: expect.text("x", timeout_ms=1.5),
    ],
)
def test_invalid_expectations_are_rejected(build):
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"goals": lambda: [Step("")]},
        {"goals": lambda: [Step("a", expect=["text"])]},
        {"goals": lambda: [Step("a", values={"": "x"})]},
        {"goals": lambda: [Step("a", values={"q": 3})]},
        {"goals": lambda: [Step("a", values={"GENERATE": "x"})]},
        {"goals": lambda: [Step("a", values={"NONE": "x"})]},
        {"goals": lambda: [Step("a", values={"pw": Value("abc", secret=True)})]},
        {"goals": [3]},
        {"goals": [[]]},
        {"goals": []},
        {"goals": ["a", "  "]},
        {"goals": "a", "expect_timeout_ms": -1},
        {"goals": "a", "max_rejections": 0},
        {"goals": "a", "auto_complete": "yes"},
        {"goals": "a", "require_expectations": 1},
        {"goals": "a", "values": ["x"]},
    ],
)
def test_construction_is_validated_before_a_browser_opens(kwargs, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(loop, "Browser", opened)
    with pytest.raises(ValueError):
        Agent("https://example.test/", **{k: v() if callable(v) else v for k, v in kwargs.items()})
    opened.assert_not_called()


def test_require_expectations_rejects_a_step_without_checks(monkeypatch):
    monkeypatch.setattr(loop, "Browser", Mock())
    with pytest.raises(ValueError, match="expect"):
        Agent("https://example.test/", ["plain", Step("checked", expect=[button()])], require_expectations=True)
    fake = Fake()
    monkeypatch.setattr(loop, "Browser", Mock(return_value=fake.browser))
    Agent("https://example.test/", [Step("checked", expect=[button()])], require_expectations=True)


# --- steps and what the model sees ----------------------------------------------------------------------------


def test_a_plain_string_is_one_step_with_the_same_goal_and_no_extra_request_fields(monkeypatch):
    agent = make(monkeypatch, "Find a book")
    seen = {}

    def choose(page_, goal, history, **extra):
        seen.update(goal=goal, extra=extra)
        return decision()

    monkeypatch.setattr(loop, "choose", choose)
    agent.command("predict")
    assert seen == {"goal": "Find a book", "extra": {}}
    assert agent.state["goal"] == "Find a book" and agent.state["plan"] == ["Find a book"]
    assert agent.state["plan_index"] == 0
    assert [s["status"] for s in agent.state["steps"]] == ["active"]


def test_the_model_sees_only_the_current_goal_the_completed_goals_and_the_check_descriptions(monkeypatch):
    steps = [
        "Search for cats",
        Step("Open the first result", expect=[expect.url("/cats"), expect.text("Tabby")]),
        "Share",
    ]
    agent = make(monkeypatch, steps)
    seen = []
    monkeypatch.setattr(loop, "choose", lambda p, goal, history, **extra: seen.append((goal, extra)) or decision())
    agent.command("predict")
    do(agent)  # step 1 has no checks: DONE accepted
    agent.command("predict")
    assert seen[0] == ("Search for cats", {})
    goal, extra = seen[1]
    assert goal == "Open the first result"
    assert extra["completed"] == ["Search for cats"]
    assert extra["checks"] == ['url contains "/cats"', 'text contains "Tabby"']
    assert "Share" not in json.dumps(seen[1])


def test_choose_puts_steps_and_checks_in_the_instructions(monkeypatch):
    sent = {}
    monkeypatch.setenv("TYPESAFE_API_KEY", "t")
    monkeypatch.setattr(
        model,
        "post_json",
        lambda _u, _k, body: sent.update(body) or {"model": "m", "answers": {"operation": _choice(body)}},
    )
    model.choose(page(), "Step two", [], completed=["Step one"], checks=['text contains "A"'])
    instructions = sent["questions"]["operation"]["instructions"]
    assert instructions["goal"] == "Step two"
    assert instructions["completed_steps"] == ["Step one"]
    assert instructions["checks"] == ['text contains "A"']


def _choice(body, selected="DONE"):
    ids = list(body["questions"]["operation"]["criteria"])
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def test_done_without_checks_is_accepted_unverified_and_the_last_step_finishes_the_run(monkeypatch):
    agent = make(monkeypatch, ["one", "two"])
    snapshot = do(agent)
    assert snapshot["status"] == "ready" and snapshot["plan_index"] == 1
    assert [s["status"] for s in snapshot["steps"]] == ["done", "active"]
    snapshot = do(agent)
    assert snapshot["status"] == "done" and snapshot["plan_index"] == 2
    assert [s["status"] for s in snapshot["steps"]] == ["done", "done"]
    assert [s["verified"] for s in snapshot["steps"]] == [False, False]
    assert snapshot["verified"] is False


def test_step_records_goal_checks_timing_and_counts(monkeypatch):
    agent = make(monkeypatch, [Step("press it", expect=[button()])])
    monkeypatch.setattr(loop, "choose", lambda *a, **k: decision("e3", "CLICK"))
    agent.fake.met = False
    agent.command("predict")
    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    step = agent.state["steps"][0]
    assert step["goal"] == "press it" and step["status"] == "active"
    assert step["checks"] == [
        {"description": 'element button "Go"', "met": False, "reason": "element reason", "waited_ms": 0}
    ]
    assert step["model_calls"] == 1 and step["actions"] == 1
    assert step["started_ms"] is not None and step["ended_ms"] is None


# --- DONE with checks -----------------------------------------------------------------------------------------


def test_done_is_accepted_when_every_check_is_met_and_the_run_is_verified(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button(), expect.text("ok")])], expect_timeout_ms=1234)
    snapshot = do(agent)
    assert snapshot["status"] == "done" and snapshot["verified"] is True
    assert snapshot["steps"][0]["verified"] is True
    assert [c["met"] for c in snapshot["steps"][0]["checks"]] == [True, True]
    timeouts = [
        c.args[1] if len(c.args) > 1 else c.kwargs["timeout_ms"] for c in agent.fake.browser.probe.call_args_list
    ]
    assert timeouts == [1234, 1234]


def test_a_per_expectation_timeout_overrides_the_agent_default(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[expect.text("ok", timeout_ms=77)])], expect_timeout_ms=1234)
    do(agent)
    call = agent.fake.browser.probe.call_args
    assert (call.args[1] if len(call.args) > 1 else call.kwargs["timeout_ms"]) == 77


def test_a_rejected_done_changes_nothing_and_tells_the_model(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button()]), "next"])
    agent.fake.met = False
    snapshot = do(agent)
    assert snapshot["status"] == "ready" and snapshot["plan_index"] == 0
    assert snapshot["steps"][0]["status"] == "active" and snapshot["steps"][0]["rejections"] == 1
    entry = agent.state["history"][-1]
    assert entry["kind"] == "done_rejected"
    assert entry["failed"] == [{"description": 'element button "Go"', "reason": "element reason"}]
    assert entry["action"] == 'DONE rejected: element button "Go" not met'
    recent = model.recent_actions(agent.state["history"])
    assert recent[-1]["action"] == 'DONE rejected: element button "Go" not met'
    assert recent[-1]["kind"] == "done_rejected"
    agent.fake.met = True
    assert do(agent)["plan_index"] == 1


def test_rejections_do_not_count_as_actions_or_as_a_stuck_page(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button()])], max_steps=1, max_rejections=5, stuck_after=2)
    agent.fake.met = False
    for _ in range(4):
        assert do(agent)["status"] == "ready"
    assert len([h for h in agent.state["history"] if h["kind"] != "done_rejected"]) == 0


def test_rejections_count_toward_the_model_call_budget(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button()])], max_model_calls=2, max_rejections=9)
    agent.fake.met = False
    monkeypatch.setattr(loop, "choose", lambda *a, **k: decision("DONE", "DONE"))
    agent.command("predict")
    do(agent)
    agent.command("predict")
    do(agent)
    assert agent.command("predict")["stop_reason"] == "max_model_calls"


def test_max_rejections_ends_the_run_failed(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button()])], max_rejections=2)
    agent.fake.met = False
    assert do(agent)["status"] == "ready"
    snapshot = do(agent)
    assert snapshot["status"] == "failed" and snapshot["stop_reason"] == "expectations"
    assert snapshot["steps"][0]["status"] == "failed" and snapshot["verified"] is False
    with pytest.raises(ValueError, match="stopped"):
        agent.command("predict")


def test_blocked_marks_the_step_blocked(monkeypatch):
    agent = make(monkeypatch, ["one", "two"])
    snapshot = do(agent, "BLOCKED", "BLOCKED")
    assert snapshot["status"] == "blocked"
    assert [s["status"] for s in snapshot["steps"]] == ["blocked", "pending"]


def test_a_raising_check_is_recorded_as_failed_and_not_propagated(monkeypatch):
    def broken(browser):
        raise RuntimeError("cart api is down")

    agent = make(monkeypatch, [Step("x", expect=[expect.check(broken, "cart has rows", timeout_ms=1)])])
    snapshot = do(agent)
    assert snapshot["status"] == "ready"
    check = snapshot["steps"][0]["checks"][0]
    assert check["met"] is False and "cart api is down" in check["reason"]
    assert "cart api is down" in snapshot["history"][-1]["failed"][0]["reason"]


@pytest.mark.parametrize(
    "result, met",
    [(True, True), (False, False), ((True, "ok"), True), ((False, "two rows"), False), (None, False), (1, False)],
)
def test_check_callables_return_a_bool_or_a_bool_with_a_reason(monkeypatch, result, met):
    seen = []
    agent = make(
        monkeypatch,
        [Step("x", expect=[expect.check(lambda b: seen.append(b) or result, "d", timeout_ms=1)])],
        max_rejections=9,
    )
    do(agent)
    assert seen and isinstance(seen[0], expect.Facade) and seen[0].unwrap() is agent.browser
    assert agent.state["steps"][0]["checks"][0]["met"] is met
    if isinstance(result, tuple):
        assert agent.state["steps"][0]["checks"][0]["reason"] == result[1]


def test_url_expectation_reads_the_location_and_waits_for_it(monkeypatch):
    agent = make(monkeypatch, [Step("x", expect=[expect.url("/done")])], expect_timeout_ms=1)
    agent.fake.browser.evaluate.return_value = "https://example.test/start"
    assert do(agent)["steps"][0]["checks"][0]["met"] is False
    agent.fake.browser.evaluate.return_value = "https://example.test/done?x=1"
    assert do(agent)["status"] == "done"
    regex = make(monkeypatch, [Step("x", expect=[expect.url(re.compile(r"/done\?x=\d$"))])])
    regex.fake.browser.evaluate.return_value = "https://example.test/done?x=1"
    assert do(regex)["status"] == "done"


# --- auto-complete --------------------------------------------------------------------------------------------


def test_auto_complete_finishes_a_step_after_an_action_without_a_done_decision(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button()]), "then"])
    monkeypatch.setattr(loop, "choose", lambda *a, **k: decision("e3", "CLICK"))
    agent.command("predict")
    do(agent, "e3", "CLICK")
    assert agent.state["plan_index"] == 1 and agent.state["steps"][0]["status"] == "done"
    assert agent.state["steps"][0]["verified"] is True
    assert agent.state["status"] == "ready"
    timeouts = {
        c.args[1] if len(c.args) > 1 else c.kwargs["timeout_ms"] for c in agent.fake.browser.probe.call_args_list
    }
    assert timeouts == {0}


def test_auto_complete_on_the_last_step_ends_the_run_done_and_verified(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button()])])
    snapshot = do(agent, "e3", "CLICK")
    assert snapshot["status"] == "done" and snapshot["verified"] is True
    assert sum(1 for d in snapshot["decisions"] if d["choice"] == "DONE") == 0


def test_unmet_checks_after_an_action_change_nothing(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button()])])
    agent.fake.met = False
    snapshot = do(agent, "e3", "CLICK")
    assert snapshot["status"] == "ready" and snapshot["steps"][0]["status"] == "active"
    assert snapshot["steps"][0]["rejections"] == 0
    assert [h["kind"] for h in snapshot["history"]] == ["click"]


def test_auto_complete_can_be_turned_off(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button()])], auto_complete=False)
    do(agent, "e3", "CLICK")
    agent.fake.browser.probe.assert_not_called()
    assert agent.state["steps"][0]["status"] == "active"


def test_auto_complete_ignores_steps_without_checks(monkeypatch):
    agent = make(monkeypatch, ["plain"])
    do(agent, "e3", "CLICK")
    agent.fake.browser.probe.assert_not_called()


def test_a_met_check_beats_the_stuck_rule(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button()])], stuck_after=1)
    snapshot = do(agent, "e3", "CLICK")  # nothing on the page changed, but the check passed
    assert snapshot["status"] == "done"


# --- values ---------------------------------------------------------------------------------------------------


def space(values, helper, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k") if helper else monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    sent = {}
    monkeypatch.setenv("TYPESAFE_API_KEY", "t")

    def post(_u, _k, body):
        sent.update(body)
        answers = {"operation": _choice(body, "TYPE_TEXT"), "type_text_target": _choice_of(["1"], "1")}
        if "type_text_value" in body["questions"]:
            ids = list(body["questions"]["type_text_value"]["criteria"])
            answers["type_text_value"] = _choice_of(ids, ids[0])
        return {"model": "m", "answers": answers}

    monkeypatch.setattr(model, "post_json", post)
    result = model.choose(page(), "goal", [], values=values)
    return sent, result


def _choice_of(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


@pytest.mark.parametrize(
    "values, helper, offered",
    [
        ([], False, None),
        ([], True, None),
        (["term"], False, ["term", "NONE"]),
        (["term"], True, ["term", "GENERATE"]),
        (["term", "code"], False, ["term", "code", "NONE"]),
        (["term", "code"], True, ["term", "code", "GENERATE"]),
    ],
)
def test_the_value_head_matrix(values, helper, offered, monkeypatch):
    sent, result = space(values, helper, monkeypatch)
    if offered is None:
        assert "type_text_value" not in sent["questions"] and result["value"] is None
    else:
        assert list(sent["questions"]["type_text_value"]["criteria"]) == offered
        assert result["value"] == offered[0]


def test_the_value_head_is_not_consulted_for_other_operations(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "t")

    def post(_u, _k, body):
        return {
            "model": "m",
            "answers": {
                "operation": _choice(body, "CLICK"),
                "click_target": _choice_of(["2"], "2"),
                "type_text_target": _choice_of(["1"], "1"),
                "type_text_value": {"choice": "invented"},
            },
        }

    monkeypatch.setattr(model, "post_json", post)
    assert model.choose(page(), "goal", [], values=["term"])["value"] is None


def test_an_invalid_value_choice_is_rejected(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "t")
    monkeypatch.setattr(
        model,
        "post_json",
        lambda _u, _k, b: {
            "model": "m",
            "answers": {
                "operation": _choice(b, "TYPE_TEXT"),
                "type_text_target": _choice_of(["1"], "1"),
                "type_text_value": {"choice": "invented"},
            },
        },
    )
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "goal", [], values=["term"])


def test_value_contents_never_reach_the_request(monkeypatch):
    agent = make(
        monkeypatch,
        [Step("log in", values={"password": Value(SECRET, secret=True)})],
        values={"term": "needle in a haystack"},
    )
    seen = {}
    monkeypatch.setattr(loop, "choose", lambda p, g, h, **extra: seen.update(extra=extra, page=p) or decision())
    agent.command("predict")
    assert sorted(seen["extra"]["values"]) == ["password", "term"]
    blob = json.dumps(seen["extra"])
    assert SECRET not in blob and "haystack" not in blob


def test_choosing_a_value_name_types_the_exact_string_with_zero_text_calls(monkeypatch):
    agent = make(monkeypatch, "search", values={"term": "  exact, odd  string "})
    monkeypatch.setattr(loop, "field_text", lambda _c: pytest.fail("the text helper must not be called"))
    do(agent, "e1", "TYPE_TEXT", value="term")
    assert agent.fake.browser.act.call_args.kwargs["text"] == "  exact, odd  string "
    assert agent.state["text_calls"] == []
    entry = agent.state["history"][-1]
    assert entry["text"] == "  exact, odd  string " and entry["text_source"] == "value:term"
    assert entry["value_name"] == "term"


def test_generate_uses_the_text_helper_and_is_recorded_as_generated(monkeypatch):
    agent = make(monkeypatch, "search", values={"term": "x"})
    monkeypatch.setattr(loop, "field_text", lambda _c: ("made up", {"model": "m", "latency_ms": 1, "usage": {}}))
    do(agent, "e1", "TYPE_TEXT", value="GENERATE")
    assert agent.fake.browser.act.call_args.kwargs["text"] == "made up"
    assert len(agent.state["text_calls"]) == 1
    assert agent.state["history"][-1]["text_source"] == "generated"


def test_without_values_typing_is_generated_as_before(monkeypatch):
    agent = make(monkeypatch, "search")
    monkeypatch.setattr(loop, "field_text", lambda _c: ("made up", {"model": "m", "latency_ms": 1, "usage": {}}))
    do(agent, "e1", "TYPE_TEXT")
    assert agent.state["history"][-1]["text_source"] == "generated"
    assert "value_name" not in agent.state["history"][-1] or agent.state["history"][-1]["value_name"] is None


def test_a_step_value_overrides_an_agent_value_of_the_same_name(monkeypatch):
    agent = make(monkeypatch, [Step("a", values={"term": "step"})], values={"term": "agent"})
    do(agent, "e1", "TYPE_TEXT", value="term")
    assert agent.fake.browser.act.call_args.kwargs["text"] == "step"


def test_secrets_are_redacted_everywhere_but_the_browser_gets_the_real_text(monkeypatch):
    hooked = []
    agent = make(
        monkeypatch,
        [Step("log in", values={"password": Value(SECRET, secret=True)})],
        before_action=lambda action, page_, text: hooked.append((text, action.get("value_name"), json.dumps(page_))),
    )
    do(agent, "e1", "TYPE_TEXT", value="password")
    assert agent.fake.browser.act.call_args.kwargs["text"] == SECRET
    assert hooked == [("<secret:password>", "password", hooked[0][2])] and SECRET not in hooked[0][2]
    entry = agent.state["history"][-1]
    assert entry["text"] == "<secret:password>" and entry["text_source"] == "value:password"
    assert entry["value_name"] == "password"
    blob = json.dumps(agent.snapshot(), default=str)
    assert SECRET not in blob
    assert SECRET not in json.dumps({k: v for k, v in agent.state.items() if k != "browser"}, default=str)


def test_a_secret_that_the_page_echoes_is_scrubbed_from_what_the_model_and_state_see(monkeypatch):
    agent = make(monkeypatch, [Step("log in", values={"password": Value(SECRET, secret=True)})])
    echoed = page(extra_text=f"you typed {SECRET}", value=SECRET)
    agent.fake.observed = echoed
    do(agent, "e1", "TYPE_TEXT", value="password")
    exposed = agent.state["page"]
    assert SECRET not in json.dumps(exposed, default=str) and "<secret:password>" in exposed["text"]
    assert exposed["fingerprint"] == fingerprint(exposed) != echoed["fingerprint"]  # S3: no guess oracle
    seen = {}
    monkeypatch.setattr(loop, "choose", lambda p, g, h, **extra: seen.update(page=p) or decision("e3", "CLICK"))
    agent.command("predict")
    assert SECRET not in json.dumps(seen["page"], default=str)
    # The browser is still checked and driven with the raw observation.
    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert agent.fake.browser.fresh.call_args.args[0] is echoed
    assert agent.fake.browser.act.call_args.args[1] is echoed


def test_a_non_secret_value_is_visible_to_the_hook_with_its_name(monkeypatch):
    hooked = []
    agent = make(
        monkeypatch, "go", values={"term": "cats"}, before_action=lambda a, p, t: hooked.append((t, a["value_name"]))
    )
    do(agent, "e1", "TYPE_TEXT", value="term")
    assert hooked == [("cats", "term")]


def test_value_validation():
    assert Value("abcd", secret=True).secret is True
    assert Value("x").secret is False
    with pytest.raises(ValueError):
        Value("")
    with pytest.raises(ValueError):
        Value("x" * 2001)
    with pytest.raises(ValueError):
        Value("abc", secret=True)  # too short to redact safely
    with pytest.raises(ValueError):
        Value("abcd", secret="yes")


def test_text_is_not_asked_of_the_helper_when_values_need_no_helper(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    agent = make(monkeypatch, "go", values={"term": "cats"})
    do(agent, "e1", "TYPE_TEXT", value="term")
    assert agent.state["history"][-1]["text"] == "cats"


def test_a_press_that_raises_leaves_no_action_baseline_for_the_next_observation(monkeypatch):
    import jev_ultrafast.browser as browser
    from jev_ultrafast.browser import Browser, Refused

    b = Browser.__new__(Browser)
    b.session, b.keys, b.urls = "s", (), {}
    b.fresh = Mock(return_value=True)
    b.evaluate = Mock(return_value={"epoch": 1, "seq": 4})
    b.reveal_timeout_ms = 0
    monkeypatch.setattr(browser, "browser_operation", Mock(side_effect=Refused("covered")))
    with pytest.raises(Refused):
        b.act({"id": "e3", "kind": "click", "node": 20}, page())
    assert b.action_mark is None


def test_a_failing_settle_leaves_no_action_baseline(monkeypatch):
    from jev_ultrafast.browser import Browser

    b = Browser.__new__(Browser)
    b.session, b.keys, b.urls, b.settle_ms, b.autocomplete_settle_ms = "s", (), {}, 0, 0
    b.after_input = {"kind": "click", "node": 1}
    b.action_mark = {"epoch": 1, "seq": 4}
    b.call = Mock(return_value={"result": {"value": {"inflight": 1}}})
    b.activity_spec = Mock(return_value={})
    b.settle_after_action = Mock(side_effect=ValueError("the wait failed in the page"))
    with pytest.raises(ValueError):
        b.observe(screenshot=False)
    assert b.action_mark is None


# --- review round 1: redaction holes (B1-B3, S2-S4, S9) ---------------------------------------------------------

LOGIN = [Step("log in", values={"password": Value(SECRET, secret=True)})]


def hook_log(agent):
    return agent.before_action.log


def recording_hook():
    def hook(action, page_, text):
        hook.log.append(json.dumps({"action": action, "page": page_, "text": text}, default=str))

    hook.log = []
    return hook


def test_a_secret_on_the_first_page_never_reaches_the_state_the_model_or_the_hook(monkeypatch):
    seen = []
    observed = page(extra_text=f"hint: {SECRET}", value=SECRET)
    agent = make(monkeypatch, LOGIN, observed=observed, before_action=recording_hook())
    plain = {k: v for k, v in agent.state.items() if k != "browser"}
    assert SECRET not in json.dumps(agent.snapshot(), default=str)
    assert SECRET not in json.dumps(plain, default=str)  # also the marker and the guards
    monkeypatch.setattr(
        loop,
        "choose",
        lambda p, g, h, **extra: seen.append(json.dumps([p, g, h, extra], default=str)) or decision("e3", "CLICK"),
    )
    agent.command("predict")
    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert seen and all(SECRET not in blob for blob in seen)
    assert agent.before_action.log and all(SECRET not in blob for blob in agent.before_action.log)
    # The browser is still checked and driven with the raw first observation.
    assert agent.fake.browser.fresh.call_args.args[0] is observed
    assert agent.fake.browser.act.call_args.args[1] is observed


def test_check_reasons_are_scrubbed_from_the_state_and_the_history(monkeypatch):
    def leak(browser):
        raise RuntimeError(f"api said {SECRET}")

    steps = [
        Step(
            "log in",
            values={"password": Value(SECRET, secret=True)},
            expect=[expect.url("zzz"), expect.check(leak, "api agrees", timeout_ms=1), expect.text("zzz")],
        )
    ]
    agent = make(monkeypatch, steps, expect_timeout_ms=1)
    agent.fake.browser.evaluate.return_value = f"https://example.test/?q={SECRET}"
    agent.fake.browser.probe.side_effect = lambda kind, timeout_ms=0, **_a: WaitResult(False, 0, f"saw {SECRET}")
    snapshot = do(agent)
    assert SECRET not in json.dumps(snapshot, default=str)
    reasons = [c["reason"] for c in snapshot["steps"][0]["checks"]]
    assert all("<secret:password>" in r for r in reasons)
    assert [f["reason"] for f in snapshot["history"][-1]["failed"]] == reasons


def test_a_label_that_echoes_a_secret_is_scrubbed_from_the_text_call_record(monkeypatch):
    observed = page()
    observed["actions"][0]["label"] = f"Search {SECRET}"
    agent = make(monkeypatch, LOGIN, observed=observed)
    monkeypatch.setattr(loop, "field_text", lambda _c: ("made up", {"model": "m", "latency_ms": 1, "usage": {}}))
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
    snapshot = do(agent, "e1", "TYPE_TEXT", value="GENERATE")
    assert SECRET not in json.dumps(snapshot, default=str)
    assert snapshot["text_calls"][0]["field"] == "Search <secret:password>"


ODD = 'p@ss wo"rd&<1>/x'


@pytest.mark.parametrize(
    "form",
    [
        ODD,
        urllib.parse.quote(ODD),
        urllib.parse.quote(ODD, safe=""),
        urllib.parse.quote_plus(ODD),
        json.dumps(ODD)[1:-1],
        html.escape(ODD),
        html.escape(ODD, quote=False),
    ],
)
def test_every_encoded_form_of_a_secret_is_scrubbed(form, monkeypatch):
    observed = page(extra_text=f"echo {form} end")
    observed["url"] = f"https://example.test/?q={form}"
    agent = make(monkeypatch, [Step("go", values={"pw": Value(ODD, secret=True)})], observed=observed)
    blob = json.dumps(agent.snapshot(), default=str)
    assert form not in blob and ODD not in blob
    assert "<secret:pw>" in agent.state["page"]["url"] and "<secret:pw>" in agent.state["page"]["text"]


def test_the_exposed_fingerprint_is_computed_from_the_scrubbed_page(monkeypatch):
    observed = page(extra_text=f"pin {SECRET}")
    agent = make(monkeypatch, LOGIN, observed=observed)
    exposed = agent.state["page"]
    assert exposed["fingerprint"] == fingerprint(exposed) != observed["fingerprint"]
    guess = json.loads(json.dumps(exposed).replace("<secret:password>", SECRET))
    assert fingerprint(guess) != exposed["fingerprint"]  # a candidate secret cannot be tested against it
    plain = make(monkeypatch, "go", observed=observed)
    assert plain.state["page"]["fingerprint"] == observed["fingerprint"]  # no secrets: untouched


def test_the_text_helper_may_not_reply_with_a_redaction_marker(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
    reply = {"choices": [{"message": {"content": json.dumps({"text": "again <secret:password>"})}}]}
    monkeypatch.setattr(model, "post_json", lambda *_a: reply)
    with pytest.raises(ValueError, match="no valid field value"):
        model.field_text({"goal": "g"})
    assert "<secret:" in questions.TEXT_VALUE


@pytest.mark.parametrize("form", [ODD, urllib.parse.quote_plus(ODD), json.dumps(ODD)[1:-1], html.escape(ODD)])
@pytest.mark.parametrize("where", ["goal", "step", "later", "check"])
def test_a_goal_or_check_description_that_holds_a_secret_is_refused_at_construction(form, where, monkeypatch):
    secret, text = Value(ODD, secret=True), f"type {form}"
    steps = {
        "goal": text,
        "step": [Step(text, values={"pw": secret})],
        "later": [Step("fine", values={"pw": secret}), Step(text)],
        "check": [Step("fine", values={"pw": secret}, expect=[expect.check(lambda b: True, text)])],
    }[where]
    opened = Mock()
    monkeypatch.setattr(loop, "Browser", opened)
    with pytest.raises(ValueError, match="secret"):
        Agent("https://example.test/", steps, values={"pw": secret} if where == "goal" else None)
    opened.assert_not_called()


def test_goals_and_descriptions_are_scrubbed_wherever_they_are_sent_anyway(monkeypatch):
    agent = make(monkeypatch, [Step("fine", values={"pw": Value(SECRET, secret=True)}, expect=[button()])])
    object.__setattr__(agent.steps[0], "goal", f"sneaky {SECRET}")
    seen = []
    monkeypatch.setattr(loop, "choose", lambda p, g, h, **extra: seen.append(json.dumps([g, extra])) or decision())
    agent.command("predict")
    assert SECRET not in seen[0] and "<secret:password>" in seen[0]


# --- review round 1: stable completion (S1) ---------------------------------------------------------------------


def flaky(*results, log=None):
    """A check callable that answers from `results` in order (the last one repeats) and logs each read."""
    calls = iter(results)
    last = [True]

    def fn(browser):
        last[0] = next(calls, last[0])
        if log is not None:
            log.append("check")
        return last[0]

    return fn


def test_done_waits_for_the_page_to_settle_and_reads_every_check_again(monkeypatch):
    log = []
    steps = [Step("x", expect=[expect.check(flaky(True, True, log=log), "held", timeout_ms=1)])]
    agent = make(monkeypatch, steps, expect_timeout_ms=700)
    agent.fake.browser.settled.side_effect = lambda **kw: log.append(("settled", kw["timeout_ms"]))
    assert do(agent)["status"] == "done"
    assert log == ["check", ("settled", 700), "check"]


def test_a_check_that_flips_back_across_the_settle_is_a_rejected_done(monkeypatch):
    steps = [Step("x", expect=[expect.check(flaky(True, False), "held", timeout_ms=1)])]
    agent = make(monkeypatch, steps, expect_timeout_ms=1)
    snapshot = do(agent)
    assert snapshot["status"] == "ready" and snapshot["verified"] is False
    assert snapshot["steps"][0]["status"] == "active" and snapshot["steps"][0]["rejections"] == 1
    entry = snapshot["history"][-1]
    assert entry["kind"] == "done_rejected" and entry["failed"][0]["description"] == "held"
    assert "held not met" in entry["action"]


def test_the_second_read_covers_all_checks_not_only_the_last(monkeypatch):
    first = expect.check(flaky(True, False), "first", timeout_ms=1)
    second = expect.check(flaky(True, True), "second", timeout_ms=1)
    agent = make(monkeypatch, [Step("x", expect=[first, second])], expect_timeout_ms=1)
    snapshot = do(agent)
    assert snapshot["status"] == "ready"
    assert [f["description"] for f in snapshot["history"][-1]["failed"]] == ["first"]


def test_auto_complete_needs_a_stable_pass_and_a_flip_counts_as_a_rejected_done(monkeypatch):
    steps = [Step("x", expect=[expect.check(flaky(True, False, True, True), "held", auto=True, timeout_ms=1)])]
    agent = make(monkeypatch, steps, expect_timeout_ms=1)
    snapshot = do(agent, "e3", "CLICK")
    assert snapshot["status"] == "ready" and snapshot["steps"][0]["rejections"] == 1
    assert snapshot["history"][-1]["kind"] == "done_rejected" and snapshot["verified"] is False
    assert do(agent, "e3", "CLICK")["status"] == "done"  # held at both reads this time


def test_repeated_flips_end_the_run_failed(monkeypatch):
    steps = [Step("x", expect=[expect.check(flaky(True, False, True, False), "held", auto=True, timeout_ms=1)])]
    agent = make(monkeypatch, steps, expect_timeout_ms=1, max_rejections=2)
    do(agent, "e3", "CLICK")
    snapshot = do(agent, "e3", "CLICK")
    assert snapshot["status"] == "failed" and snapshot["stop_reason"] == "expectations"


# --- review round 1: check callables (S5) and small corrections ----------------------------------------------


def test_a_check_callable_gets_a_read_only_facade_not_the_browser(monkeypatch):
    got = []
    steps = [Step("x", expect=[expect.check(lambda b: got.append(b) or True, "d", timeout_ms=1)])]
    agent = make(monkeypatch, steps)
    agent.fake.browser.evaluate.return_value = "https://example.test/now"
    do(agent)
    facade = got[0]
    assert sorted(n for n in dir(facade) if not n.startswith("_") and n != "unwrap") == [
        "element", "evaluate", "probe", "text", "url"
    ]
    assert facade.url() == "https://example.test/now"
    assert facade.evaluate("1 + 1") == "https://example.test/now"  # forwarded to the browser
    for name in ("act", "press", "close", "goto", "observe"):
        assert not hasattr(facade, name)
    agent.fake.met = True
    assert facade.element(role="button", name="Go") is True and facade.text("Go") is True
    agent.fake.met = False
    assert facade.element(role="button") is False and facade.probe("text", contains="x").met is False


def test_callables_run_in_the_auto_pass_only_when_the_caller_opts_in(monkeypatch):
    ran = []
    quiet = expect.check(lambda b: ran.append("quiet") or True, "quiet", timeout_ms=1)
    agent = make(monkeypatch, [Step("x", expect=[quiet])], expect_timeout_ms=1)
    assert do(agent, "e3", "CLICK")["status"] == "ready" and ran == []  # not run, so the step cannot complete
    assert do(agent)["status"] == "done" and ran  # a DONE decision still checks it
    ran.clear()
    eager = expect.check(lambda b: ran.append("eager") or True, "eager", auto=True, timeout_ms=1)
    agent = make(monkeypatch, [Step("x", expect=[eager])], expect_timeout_ms=1)
    assert do(agent, "e3", "CLICK")["status"] == "done" and ran


def test_one_callable_without_auto_keeps_the_whole_step_from_completing_by_itself(monkeypatch):
    steps = [Step("x", expect=[button(), expect.check(lambda b: True, "api", timeout_ms=1)])]
    agent = make(monkeypatch, steps, expect_timeout_ms=1)
    assert do(agent, "e3", "CLICK")["status"] == "ready"


def test_check_arguments_are_validated():
    with pytest.raises(ValueError):
        expect.check(lambda b: True, "d", auto="yes")

    async def later(browser):
        return True

    with pytest.raises(ValueError, match="async"):
        expect.check(later, "d")
    assert expect.check(lambda b: True, "d", auto=True).auto is True


def test_checks_after_the_first_unmet_one_keep_no_stale_result(monkeypatch):
    agent = make(monkeypatch, [Step("x", expect=[button(), expect.url("/never")])], auto_complete=True)
    agent.fake.browser.evaluate.return_value = "https://example.test/start"
    do(agent, "e3", "CLICK")
    assert agent.state["steps"][0]["checks"][0]["met"] is True
    agent.fake.met = False
    do(agent, "e3", "CLICK")
    assert [c["met"] for c in agent.state["steps"][0]["checks"]] == [False, None]


@pytest.mark.parametrize("result, reason", [(1, "non-bool"), ("yes", "non-bool"), (None, "None"), (False, "falsy")])
def test_a_check_callable_that_returns_a_non_bool_says_so(monkeypatch, result, reason):
    agent = make(monkeypatch, [Step("x", expect=[expect.check(lambda b: result, "d", timeout_ms=1)])])
    do(agent)
    assert reason in agent.state["steps"][0]["checks"][0]["reason"]


def test_a_check_callable_that_returns_a_coroutine_is_unmet_without_a_warning(monkeypatch):
    async def later():
        return True

    agent = make(monkeypatch, [Step("x", expect=[expect.check(lambda b: later(), "d", timeout_ms=1)])])
    do(agent)
    assert "async" in agent.state["steps"][0]["checks"][0]["reason"]


# --- review round 1: values with no helper (nit) --------------------------------------------------------------


def test_with_values_and_no_helper_a_declared_none_choice_is_offered_and_worded(monkeypatch):
    sent, _ = space(["term"], False, monkeypatch)
    criteria = sent["questions"]["type_text_value"]["criteria"]
    assert "NONE" in criteria and "GENERATE" not in criteria
    assert "declared value" in sent["questions"]["operation"]["criteria"]["TYPE_TEXT"]
    assert "small LLM" not in sent["questions"]["operation"]["criteria"]["TYPE_TEXT"]
    assert "NONE" in questions.VALUES


def test_the_done_criterion_names_the_checks_when_there_are_some(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "t")
    sent = {}

    def post(_u, _k, body):
        sent.update(body)
        return {"model": "m", "answers": {"operation": _choice(body, "DONE")}}

    monkeypatch.setattr(model, "post_json", post)
    model.choose(page(), "goal", [])
    plain = sent["questions"]["operation"]["criteria"]["DONE"]
    model.choose(page(), "goal", [], checks=["a"], completed=["b"])
    assert plain == "Every requirement is visibly satisfied."
    assert sent["questions"]["operation"]["criteria"]["DONE"] != plain
    assert "checks" in sent["questions"]["operation"]["criteria"]["DONE"]


def test_choosing_none_types_nothing_and_hides_that_field_until_the_page_changes(monkeypatch):
    agent = make(monkeypatch, "search", values={"term": "cats"}, stuck_after=2)
    snapshot = do(agent, "e1", "TYPE_TEXT", value="NONE")
    agent.fake.browser.act.assert_not_called()
    assert snapshot["status"] == "ready" and snapshot["history"] == []
    refused = snapshot["refused"]
    assert len(refused) == 1 and refused[0]["node"] == 10 and "no declared value" in refused[0]["reason"]
    seen = {}
    monkeypatch.setattr(
        loop, "choose", lambda p, g, h, **x: seen.update(page=p, refused=x["refused"]) or decision("e3", "CLICK")
    )
    agent.command("predict")
    assert [a["id"] for a in seen["page"]["actions"]] == ["e3", "wait"] and seen["refused"][0]["kind"] == "fill"
    # A second refusal on the same page ends the run, as for a refused press.
    again = agent.state["decision"] = decision("e1", "TYPE_TEXT", "NONE")
    assert again
    final = agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert final["status"] == "blocked" and final["stop_reason"] == "refused"
