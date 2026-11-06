"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import ActionDenied, Agent, model
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import Refused, StalePage, browser_operation, fingerprint


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    a.max_steps, a.max_model_calls, a.stuck_after, a.before_action = 60, 120, 3, None
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from examples.flights import verify

    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-09-20",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sun, Sep 20"),
                ("Nonstop flight on Sunday, September 20. Select flight", ""),
            ]
        ],
    }
    assert verify(actual)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


def act(runner, name="e3"):
    runner.state["decision"] = decision(name)
    return runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})


def test_max_steps_ends_the_run_as_budget_without_a_third_action(runner):
    runner.max_steps = 2
    act(runner)
    runner.state["page"]["fingerprint"] = runner.state["browser"].observe.return_value["fingerprint"]
    act(runner)
    snapshot = act(runner)
    assert snapshot["status"] == "budget" and snapshot["stop_reason"] == "max_steps"
    assert runner.state["browser"].act.call_count == 2
    assert len(runner.state["history"]) == 2


def test_model_call_cap_counts_text_calls_and_never_exceeds_the_cap(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    chooser = Mock(return_value=decision("e3"))
    monkeypatch.setattr(loop, "choose", chooser)
    runner.max_model_calls = 2
    runner.state["decisions"] = [{}]  # one decision already spent
    snapshot = act(runner, "e1")  # TYPE_TEXT: the text call is the second and last allowed call
    assert helper.call_count == 1 and snapshot["status"] == "ready"
    runner.state["status"] = "ready"
    snapshot = runner.command("predict", {})
    assert snapshot["status"] == "budget" and snapshot["stop_reason"] == "max_model_calls"
    chooser.assert_not_called()


def test_text_call_that_would_exceed_the_cap_is_never_made(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.max_model_calls = 1
    runner.state["decisions"] = [{}]
    snapshot = act(runner, "e1")
    assert snapshot["status"] == "budget" and snapshot["stop_reason"] == "max_model_calls"
    helper.assert_not_called()
    runner.state["browser"].act.assert_not_called()


def test_tick_returns_budget_instead_of_raising(runner, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3")))
    runner.max_model_calls = 1
    runner.state["decisions"] = [{}]
    assert runner.command("tick")["status"] == "budget"
    assert list(runner.run()) == []


def test_before_action_veto_executes_nothing(runner):
    seen = []

    def hook(action, page, text):
        seen.append((action["label"], text))
        raise ActionDenied("no go")

    runner.before_action = hook
    snapshot = act(runner)
    assert snapshot["status"] == "denied"
    assert snapshot["denied"] == {"reason": "no go", "action": "Go", "kind": "click", "choice": "e3"}
    assert seen == [("Go", None)]
    runner.state["browser"].act.assert_not_called()
    assert runner.state["history"] == [] and runner.pending_text is None
    with pytest.raises(ValueError, match="stopped"):
        runner.command("predict", {})


def test_before_action_sees_generated_text_and_allow_executes(runner, monkeypatch):
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", {"model": "test", "latency_ms": 10})))
    hook = Mock()
    runner.before_action = hook
    act(runner, "e1")
    assert hook.call_args.args[2] == "book"
    runner.state["browser"].act.assert_called_once()
    assert len(runner.state["history"]) == 1


def test_before_action_not_called_for_done(runner):
    runner.before_action = Mock()
    runner.state["decision"] = {**decision("DONE"), "choice": "DONE"}
    assert runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})["status"] == "done"
    runner.before_action.assert_not_called()


def test_other_hook_exceptions_propagate(runner):
    runner.before_action = Mock(side_effect=KeyError("bug"))
    with pytest.raises(KeyError):
        act(runner)
    runner.state["browser"].act.assert_not_called()


def test_stuck_after_is_honoured(runner):
    runner.stuck_after = 2
    runner.state["page"] = runner.state["browser"].observe.return_value
    act(runner)
    assert runner.state["status"] == "ready"
    assert act(runner)["status"] == "blocked"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_steps": 0},
        {"max_steps": True},
        {"max_steps": "5"},
        {"max_model_calls": 0},
        {"stuck_after": 0},
        {"settle_ms": -1},
        {"autocomplete_settle_ms": -1},
        {"reveal_timeout_ms": -1},
        {"before_action": "nope"},
    ],
)
def test_construction_arguments_are_validated_before_a_browser_opens(kwargs, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(loop, "Browser", opened)
    with pytest.raises(ValueError):
        Agent("https://example.test/", "goal", **kwargs)
    opened.assert_not_called()


def test_hook_runs_per_attempt_on_stale_retry_with_same_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    hook = Mock()
    runner.before_action = hook
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    fingerprint_ = runner.state["page"]["fingerprint"]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": fingerprint_})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": fingerprint_})
    assert hook.call_count == 2
    assert [c.args[2] for c in hook.call_args_list] == ["book", "book"]
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2
    assert len(runner.state["history"]) == 1


def test_mutating_hook_cannot_change_what_executes(runner, monkeypatch):
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", {"model": "test", "latency_ms": 10})))

    def hook(action, page, text):
        action["label"] = "Tampered"
        action["node"] = 999
        page["actions"].clear()

    runner.before_action = hook
    act(runner, "e1")
    executed, page_arg = runner.state["browser"].act.call_args.args[:2]
    assert executed["label"] == "Search" and executed["node"] == 10
    assert page_arg["actions"] and runner.state["history"][0]["action"] == "Search"


@pytest.mark.parametrize("settle,auto", [(0, 0), (7, 123)])
def test_settle_values_reach_the_observe_script(settle, auto, monkeypatch):
    from jev_ultrafast import browser as browser_module

    b = browser_module.Browser.__new__(browser_module.Browser)
    b.settle_ms, b.autocomplete_settle_ms, b.session = settle, auto, "s"
    b.after_input = {"kind": "click", "node": 1}
    calls = []
    monkeypatch.setattr(b, "call", lambda method, **params: calls.append((method, params)) or {})
    monkeypatch.setattr(browser_module, "browser_operation", lambda _request: {"ok": True})
    b.observe(screenshot=False)
    expression = calls[0][1]["expression"]
    assert expression.endswith(f',{settle},{auto})')
    assert calls[0][1]["awaitPromise"] is True


def test_zero_settle_is_accepted(monkeypatch):
    browser = Mock()
    browser.observe.return_value = page()
    opened = Mock(return_value=browser)
    monkeypatch.setattr(loop, "Browser", opened)
    Agent("https://example.test/", "goal", settle_ms=0, autocomplete_settle_ms=0)
    assert opened.call_args.kwargs == {
        "settle_ms": 0,
        "autocomplete_settle_ms": 0,
        "reveal_timeout_ms": 500,
    }


def revealed_page():
    p = page()
    p["actions"].insert(
        2,
        {
            "id": "e3r", "kind": "click", "label": "Copy", "role": "button", "value": "", "node": 40,
            "revealed_by": "hover",
        },
    )
    p["fingerprint"] = fingerprint(p)
    return p


def test_action_space_carries_revealed_by_and_offers_hover_per_clickable_element():
    elements, targets, _controls = model.action_space(revealed_page()["actions"])
    copy_ = next(e for e in elements if e["label"] == "Copy")
    assert copy_["revealed_by"] == "hover"
    assert copy_["operations"] == ["CLICK", "HOVER"]
    assert set(targets["HOVER"]) == {copy_["index"]}  # Only the element with a hover signal gets the head.
    assert targets["HOVER"][copy_["index"]]["id"] == "e3r"
    assert next(e for e in elements if e["label"] == "Go")["operations"] == ["CLICK"]
    assert "revealed_by" not in next(e for e in elements if e["label"] == "Go")


def test_choose_offers_hover_with_its_own_target_head(monkeypatch):
    seen = {}

    def post(_url, _key, body):
        seen.update(body["questions"])
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "HOVER"),
                "hover_target": choice(body["questions"]["hover_target"]["criteria"], "2"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(revealed_page(), "Copy the answer", [])
    assert "HOVER" in seen["operation"]["criteria"] and "hover_target" in seen
    assert seen["hover_target"]["criteria"]["2"]["revealed_by"] == "hover"
    assert d["operation"] == "HOVER" and d["choice"] == "e3r"


def test_hover_executes_through_browser_act_hook_history_and_stuck_counter(runner):
    runner.stuck_after = 2
    runner.state["page"] = runner.state["browser"].observe.return_value
    hook = Mock()
    runner.before_action = hook
    hover = {**decision("e3"), "operation": "HOVER", "target": "2"}
    runner.state["decision"] = hover
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    executed = runner.state["browser"].act.call_args.args[0]
    assert executed["kind"] == "hover" and executed["id"] == "e3"
    assert hook.call_args.args[0]["kind"] == "hover"
    assert runner.state["history"][0]["kind"] == "hover" and runner.state["status"] == "ready"
    runner.state["decision"] = hover
    assert runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})["status"] == "blocked"


def test_hover_veto_executes_nothing(runner):
    runner.before_action = Mock(side_effect=ActionDenied("no hover"))
    runner.state["decision"] = {**decision("e3"), "operation": "HOVER"}
    snapshot = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert snapshot["status"] == "denied" and snapshot["denied"]["kind"] == "hover"
    runner.state["browser"].act.assert_not_called()


def test_reveal_timeout_is_validated_and_passed_to_the_browser(monkeypatch):
    opened = Mock()
    monkeypatch.setattr(loop, "Browser", opened)
    for bad in (-1, True, "5", 1.5):
        with pytest.raises(ValueError):
            Agent("https://example.test/", "goal", reveal_timeout_ms=bad)
    opened.assert_not_called()
    opened.return_value.observe.return_value = page()
    Agent("https://example.test/", "goal", reveal_timeout_ms=0)
    assert opened.call_args.kwargs["reveal_timeout_ms"] == 0


def test_hover_is_a_fresh_checked_kind_and_requests_carry_the_reveal_timeout(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.session, b.reveal_timeout_ms = "s", 123
    seen = []
    b.evaluate = lambda expression: seen.append(expression) or [None, None]
    # A hover is checked against the per-node guard, not the page marker.
    assert b.fresh({**page(), "page_key": 1, "guards": {}}, {"kind": "hover", "node": 20}) is False
    assert "guard(" in seen[0]
    b.fresh = Mock(return_value=True)
    operation = Mock(return_value={})
    monkeypatch.setattr(browser, "browser_operation", operation)
    b.act({"id": "e1", "kind": "hover", "node": 1}, page())
    assert operation.call_args.args[0]["reveal_timeout_ms"] == 123


def test_every_press_is_preceded_by_a_pointer_move(monkeypatch):
    import jev_ultrafast.browser as browser

    calls = []

    def cdp(method, **params):
        calls.append((method, params.get("type")))
        if method == "Runtime.evaluate":
            return {"result": {"value": {"x": 5, "y": 6}}}
        return {}

    monkeypatch.setattr(browser, "cdp", cdp)
    browser_operation({"operation": "act", "session": "s", "action": {"id": "e1", "kind": "click", "node": 1}})
    mouse = [t for m, t in calls if m == "Input.dispatchMouseEvent"]
    assert mouse == ["mouseMoved", "mousePressed", "mouseReleased"]


def test_hover_moves_the_pointer_and_never_presses(monkeypatch):
    import jev_ultrafast.browser as browser

    calls = []

    def cdp(method, **params):
        calls.append((method, params.get("type")))
        if method == "Runtime.evaluate":
            return {"result": {"value": {"x": 5, "y": 6}}}
        return {}

    monkeypatch.setattr(browser, "cdp", cdp)
    browser_operation({"operation": "act", "session": "s", "action": {"id": "e1", "kind": "hover", "node": 1}})
    assert [t for m, t in calls if m == "Input.dispatchMouseEvent"] == ["mouseMoved"]


def test_a_reveal_candidate_that_stays_hidden_is_never_pressed(monkeypatch):
    import jev_ultrafast.browser as browser

    calls = []

    def cdp(method, **params):
        calls.append((method, params.get("type")))
        if method == "Runtime.evaluate":
            # Geometry for the reveal step, then "never became visible".
            return {"result": {"value": {"x": 5, "y": 6} if "awaitPromise" not in params else False}}
        return {}

    monkeypatch.setattr(browser, "cdp", cdp)
    action = {"id": "e1", "kind": "click", "node": 1, "revealed_by": "hover"}
    with pytest.raises(StalePage, match="hidden"):
        browser_operation({"operation": "act", "session": "s", "action": action, "reveal_timeout_ms": 10})
    types = [t for m, t in calls if m == "Input.dispatchMouseEvent"]
    assert types == ["mouseMoved"]


@pytest.mark.parametrize(
    "signal", [{"revealed_by": "hover"}, {"hover_signal": True}, {"expanded": "false"}]
)
def test_hover_is_offered_only_for_elements_with_a_hover_signal(signal):
    p = page()
    elements, targets, _ = model.action_space(p["actions"])
    assert "HOVER" not in targets and all("HOVER" not in e["operations"] for e in elements)
    p["actions"][2].update(signal)
    elements, targets, _ = model.action_space(p["actions"])
    assert list(targets["HOVER"].values()) == [p["actions"][2]]
    assert next(e for e in elements if e["label"] == "Go")["operations"] == ["CLICK", "HOVER"]


def test_refused_presses_reach_recent_actions_in_order_and_history_stays_executed_only():
    history = [{"action": "Open", "kind": "click", "text": None, "page_changed": True}]
    refused = [{"label": "Remove", "kind": "click", "after": 1}]
    assert model.recent_actions(history, refused) == [
        {"action": "Open", "kind": "click", "text": None, "page_changed": True},
        {"action": "Remove", "kind": "click", "text": None, "page_changed": False, "refused": True},
    ]


def honeypot_run(runner, monkeypatch):
    seen = []

    def choose(p, goal, history, refused=()):
        seen.append((p, list(refused)))
        return {**decision("e3"), "operation": "CLICK", "target": "2"}

    monkeypatch.setattr(loop, "choose", choose)
    runner.state["browser"].act.side_effect = Refused("Control stayed hidden after hovering. Not pressed.")
    return seen


def test_a_refused_press_is_recorded_not_executed_and_hidden_from_the_next_choice(runner, monkeypatch):
    seen = honeypot_run(runner, monkeypatch)
    runner.command("tick")
    state = runner.state
    assert state["history"] == [] and state["status"] == "ready"
    (record,) = state["refused"]
    assert record["label"] == "Go" and record["kind"] == "click" and record["pointer_moved"] is True
    assert record["fingerprint"] == state["page"]["fingerprint"] and record["reason"]
    runner.command("tick")
    page_seen, refused_seen = seen[1]
    assert all(a.get("node") != 20 for a in page_seen["actions"]) and refused_seen == state["refused"][:1]


def test_a_model_that_always_picks_the_honeypot_ends_blocked_long_before_the_call_budget(runner, monkeypatch):
    seen = honeypot_run(runner, monkeypatch)
    snapshot = runner.command("tick")
    while snapshot["status"] not in loop.TERMINAL:
        snapshot = runner.command("tick")
    assert snapshot["status"] == "blocked" and snapshot["stop_reason"] == "refused"
    assert len(seen) == runner.stuck_after < runner.max_model_calls
    assert runner.state["history"] == []


def test_an_executed_action_resets_the_consecutive_refusal_count(runner, monkeypatch):
    honeypot_run(runner, monkeypatch)
    runner.stuck_after = 2
    runner.command("tick")
    runner.state["browser"].act.side_effect = None
    runner.command("tick")
    assert len(runner.state["history"]) == 1
    runner.state["browser"].act.side_effect = Refused("again")
    assert runner.command("tick")["status"] == "ready"  # One refusal since the last executed action.
