"""Offline contracts for steps, checked postconditions and caller-provided values. No paid APIs, no browser."""

import json
import re
import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import Agent, Step, Value, expect, model
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
        {"goals": [Step("")]},
        {"goals": [Step("a", expect=["text"])]},
        {"goals": [Step("a", values={"": "x"})]},
        {"goals": [Step("a", values={"q": 3})]},
        {"goals": [Step("a", values={"GENERATE": "x"})]},
        {"goals": [Step("a", values={"pw": Value("abc", secret=True)})]},
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
        Agent("https://example.test/", **kwargs)
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
    steps = ["Search for cats", Step("Open the first result", expect=[expect.url("/cats"), expect.text("Tabby")]), "Share"]
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
    model.choose(page(), "Step two", [], completed=["Step one"], checks=["text contains \"A\""])
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
    assert step["checks"] == [{"description": 'element button "Go"', "met": False, "reason": "element reason", "waited_ms": 0}]
    assert step["model_calls"] == 1 and step["actions"] == 1
    assert step["started_ms"] is not None and step["ended_ms"] is None


# --- DONE with checks -----------------------------------------------------------------------------------------


def test_done_is_accepted_when_every_check_is_met_and_the_run_is_verified(monkeypatch):
    agent = make(monkeypatch, [Step("press", expect=[button(), expect.text("ok")])], expect_timeout_ms=1234)
    snapshot = do(agent)
    assert snapshot["status"] == "done" and snapshot["verified"] is True
    assert snapshot["steps"][0]["verified"] is True
    assert [c["met"] for c in snapshot["steps"][0]["checks"]] == [True, True]
    timeouts = [c.args[1] if len(c.args) > 1 else c.kwargs["timeout_ms"] for c in agent.fake.browser.probe.call_args_list]
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
    assert seen and seen[0] is agent.browser
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
    timeouts = {c.args[1] if len(c.args) > 1 else c.kwargs["timeout_ms"] for c in agent.fake.browser.probe.call_args_list}
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
        (["term"], False, ["term"]),
        (["term"], True, ["term", "GENERATE"]),
        (["term", "code"], True, ["term", "code", "GENERATE"]),
    ],
)
def test_the_value_head_matrix(values, helper, offered, monkeypatch):
    sent, result = space(values, helper, monkeypatch)
    if offered is None:
        assert "type_text_value" not in sent["questions"] and "value" not in result
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
                "click_target": _choice_of(["1"], "1"),
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
    assert exposed["fingerprint"] == echoed["fingerprint"]
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
    agent = make(monkeypatch, "go", values={"term": "cats"}, before_action=lambda a, p, t: hooked.append((t, a["value_name"])))
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
