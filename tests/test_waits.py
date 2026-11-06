"""Offline contracts for condition waits. No browser, no paid APIs."""

import re
import time
from unittest.mock import Mock

import pytest
from test_agent import decision, page

from jev_ultrafast import Agent
from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module
from jev_ultrafast.browser import Browser
from jev_ultrafast.waits import (
    WaitResult,
    element_state,
    ignore_patterns,
    needs_settle,
    pattern,
)


def bare():
    b = Browser.__new__(Browser)
    b.session = "s"
    b.settle_ms, b.autocomplete_settle_ms = 50, 200
    return b


def page_result(value):
    return {"result": {"value": value}}


# Argument validation.


@pytest.mark.parametrize(
    "kwargs",
    [
        {"wait_quiet_ms": -1},
        {"wait_quiet_ms": 1.5},
        {"wait_quiet_ms": True},
        {"wait_timeout_ms": 0},
        {"wait_timeout_ms": "30000"},
        {"action_settle_timeout_ms": -1},
        {"action_settle_timeout_ms": None},
    ],
)
def test_wait_arguments_are_validated_before_a_browser_opens(kwargs, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(loop, "Browser", opened)
    with pytest.raises(ValueError, match=next(iter(kwargs))):
        Agent("https://example.test/", "goal", **kwargs)
    opened.assert_not_called()


def test_zero_quiet_and_zero_action_settle_are_accepted(monkeypatch):
    opened = Mock(return_value=Mock(observe=Mock(return_value=page())))
    monkeypatch.setattr(loop, "Browser", opened)
    Agent("https://example.test/", "goal", wait_quiet_ms=0, action_settle_timeout_ms=0)
    assert opened.call_args.kwargs["wait_quiet_ms"] == 0
    assert opened.call_args.kwargs["action_settle_timeout_ms"] == 0
    assert opened.call_args.kwargs["wait_timeout_ms"] == 30000


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda b: b.dom_quiet(quiet_ms=-1), "quiet_ms"),
        (lambda b: b.dom_quiet(timeout_ms=0), "timeout_ms"),
        (lambda b: b.network_idle(idle_ms=True), "idle_ms"),
        (lambda b: b.network_idle(ignore_urls=[3]), "ignore_urls"),
        (lambda b: b.settled(timeout_ms=1.5), "timeout_ms"),
        (lambda b: b.element(role=3), "role"),
        (lambda b: b.element(name=3), "name"),
        (lambda b: b.element(state="open"), "state"),
        (lambda b: b.element(present="yes"), "present"),
        (lambda b: b.text(None), "contains"),
        (lambda b: b.text("x", present=1), "present"),
        (lambda b: b.text_stable(role=1), "role"),
    ],
)
def test_primitives_reject_bad_arguments_without_touching_the_page(call, message):
    b = bare()
    b._wait = Mock()
    with pytest.raises(ValueError, match=message):
        call(b)
    b._wait.assert_not_called()


def test_patterns_ignore_lists_and_states_are_normalised():
    assert pattern("x", "Copy") == {"s": "Copy"}
    assert pattern("x", re.compile("^co", re.I | re.S)) == {"r": "^co", "f": "is"}
    assert pattern("x", None, optional=True) is None
    # A bare string is one substring, never a list of letters.
    assert ignore_patterns("/poll") == [{"s": "/poll"}]
    assert ignore_patterns(["/a", re.compile("b$")]) == [{"s": "/a"}, {"r": "b$", "f": ""}]
    assert ignore_patterns(None) == []
    assert element_state(None) == {}
    assert element_state("checked") == {"checked": True}
    assert element_state(["expanded", "disabled"]) == {"expanded": True, "disabled": True}
    assert element_state({"pressed": False}) == {"pressed": False}
    with pytest.raises(ValueError, match="state"):
        element_state({"checked": "yes"})


# Result objects and the timeout path.


def test_a_met_wait_returns_a_result_object():
    b = bare()
    b.call = Mock(return_value=page_result({"met": True, "reason": "settled", "document": [1, "a"]}))
    result = b.settled(100, 5000)
    assert isinstance(result, WaitResult) and result.met is True and result.reason == "settled"
    assert result.as_dict() == {"met": True, "waited_ms": result.waited_ms, "reason": "settled"}
    request = b.call.call_args.kwargs
    assert request["awaitPromise"] is True and '"kind": "settled"' in request["expression"]
    assert '"quiet_ms": 100' in request["expression"]


def test_a_timeout_never_raises_and_reports_why():
    b = bare()
    b.call = Mock(return_value=page_result({"met": False, "reason": "3 request(s) in flight", "document": [1, "a"]}))
    started = time.monotonic()
    result = b.network_idle(idle_ms=100, timeout_ms=60)
    assert result.met is False and result.reason == "timeout: 3 request(s) in flight"
    assert 60 <= result.waited_ms < 1000 and time.monotonic() - started < 2


def test_slices_stay_inside_the_ipc_timeout(monkeypatch):
    clock = [0.0]

    def call(*_args, **_kwargs):
        clock[0] += 2.0  # A slice that ran its full length.
        return page_result({"met": False, "reason": "x", "document": [1, "a"]})

    monkeypatch.setattr(browser_module.time, "monotonic", lambda: clock[0])
    b = bare()
    b.call = Mock(side_effect=call)
    b.settled(100, 4500)
    sent = [re.search(r'"slice_ms": ([\d.]+)', c.kwargs["expression"])[1] for c in b.call.call_args_list]
    assert [float(value) for value in sent] == [2000.0, 2000.0, 500.0]


def test_a_navigation_during_a_wait_waits_for_the_new_document_then_continues():
    b = bare()
    b.call = Mock(
        side_effect=[
            page_result({"met": False, "reason": "the DOM is still changing", "document": [1, "a"]}),
            RuntimeError("Execution context was destroyed."),
            page_result({"met": True, "reason": "settled", "document": [2, "b"]}),
        ]
    )
    b.wait_for_document = Mock()
    result = b.settled(100, 5000)
    assert result.met is True
    assert b.wait_for_document.call_args.args[0] == [1, "a"]  # Waits for any document other than the last seen.


def test_a_page_that_stays_in_transition_times_out_with_a_reason():
    b = bare()
    b.call = Mock(side_effect=RuntimeError("Execution context was destroyed."))
    b.wait_for_document = Mock()
    result = b.settled(100, 80)
    assert result.met is False and result.reason == "timeout: the page is navigating"


# WAIT, the post-action settle and history.


def test_wait_runs_settled_with_the_agent_arguments_and_skips_the_freshness_check():
    b = bare()
    b.wait_quiet_ms, b.wait_timeout_ms = 321, 4567
    b.settled = Mock(return_value=WaitResult(True, 1234, "settled"))
    b.mark_activity = Mock()
    b.fresh = Mock(return_value=False)  # A streaming page is never "fresh"; WAIT must not care.
    outcome = b.act({"id": "wait", "kind": "wait", "label": "Wait"}, page())
    b.settled.assert_called_once_with(321, 4567)
    assert outcome == {"executed": "wait", "wait": {"met": True, "waited_ms": 1234, "reason": "settled"}}
    b.fresh.assert_not_called()
    assert b.after_input is None


def test_a_timed_out_wait_is_recorded_with_waited_ms_and_met_false(runner):
    runner.state["browser"].act.return_value = {
        "executed": "wait",
        "wait": {"met": False, "waited_ms": 30001, "reason": "timeout: the DOM is still changing"},
    }
    runner.state["decision"] = {**decision("wait"), "operation": "WAIT"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    entry = runner.state["history"][0]
    assert entry["kind"] == "wait" and entry["met"] is False and entry["waited_ms"] == 30001
    assert runner.state["status"] == "ready"


def test_other_actions_carry_no_wait_fields(runner):
    runner.state["browser"].act.return_value = {"executed": "e3"}
    runner.state["decision"] = decision("e3")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert "waited_ms" not in runner.state["history"][0]


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


def settle_browser(timeout=5000, seen=None):
    b = bare()
    b.action_settle_timeout_ms, b.action_settle_quiet_ms = timeout, 150
    b.activity_seen = seen or {"epoch": "e1", "started": 3}
    b.settled = Mock(return_value=WaitResult(True, 7, "settled"))
    b.activity = Mock(return_value={"epoch": "e1", "started": 4, "inflight": 0, "timers": 0})
    return b


@pytest.mark.parametrize(
    "activity, expected",
    [
        ({"epoch": "e1", "started": 3, "inflight": 0, "timers": 0}, False),  # Nothing happened.
        ({"epoch": "e1", "started": 3, "inflight": 1, "timers": 0}, True),  # A request is in flight.
        ({"epoch": "e1", "started": 3, "inflight": 0, "timers": 2}, True),  # A short timer is pending.
        ({"epoch": "e1", "started": 4, "inflight": 0, "timers": 0}, True),  # A request started and already ended.
        ({"epoch": "e2", "started": 1, "inflight": 0, "timers": 0}, False),  # A new document is awaited elsewhere.
        ({"epoch": "e2", "started": 1, "inflight": 1, "timers": 0}, True),
        (None, False),
    ],
)
def test_the_post_action_settle_triggers_only_when_something_is_pending(activity, expected):
    b = settle_browser()
    b.settle_after_action(activity)
    assert b.settled.called is expected
    if expected:
        b.settled.assert_called_once_with(150, 5000)
        assert b.last_action_settle.met is True
    else:
        assert b.last_action_settle is None


def test_an_idle_action_adds_no_cdp_round_trip_and_updates_the_baseline():
    b = settle_browser()
    b.call = Mock()
    b.settle_after_action({"epoch": "e1", "started": 3, "inflight": 0, "timers": 0})
    b.call.assert_not_called()
    b.activity.assert_not_called()
    assert b.activity_seen["started"] == 3


def test_action_settle_can_be_turned_off():
    b = settle_browser(timeout=0)
    b.settle_after_action({"epoch": "e1", "started": 9, "inflight": 2, "timers": 1})
    b.settled.assert_not_called()


def test_needs_settle_is_false_without_a_baseline_unless_something_is_pending():
    assert needs_settle({"epoch": "e", "started": 5, "inflight": 0, "timers": 0}, {}) is False
    assert needs_settle({"epoch": "e", "started": 5, "inflight": 1, "timers": 0}, {}) is True


def test_observe_hands_the_settle_scripts_activity_to_the_post_action_settle(monkeypatch):
    b = bare()
    b.after_input = {"kind": "click", "node": 1}
    seen = {"epoch": "e1", "started": 1, "inflight": 1, "timers": 0}
    b.call = Mock(return_value=page_result(seen))
    b.settle_after_action = Mock()
    monkeypatch.setattr(browser_module, "browser_operation", lambda _request: {"ok": True})
    b.observe(screenshot=False)
    b.settle_after_action.assert_called_once_with(seen)


def test_the_settle_script_cancels_its_animation_frame_and_uses_the_trackers_timer():
    b = bare()
    b.after_input = {"kind": "click", "node": 1}
    b.call = Mock(return_value=page_result(None))
    b.settle_after_action = Mock()
    browser_module_operation = Mock(return_value={"ok": True})
    import jev_ultrafast.browser as module

    module.browser_operation, original = browser_module_operation, module.browser_operation
    try:
        b.observe(screenshot=False)
    finally:
        module.browser_operation = original
    expression = b.call.call_args.kwargs["expression"]
    # A frame left pending in a throttled tab makes the next pointer event wait for it.
    assert "cancelAnimationFrame(raf)" in expression and "__jevWait?.timeout" in expression


# Agent.wait_for.


def test_wait_for_records_results_and_rejects_unknown_kinds(runner):
    browser = runner.state["browser"]
    browser.text.return_value = WaitResult(True, 12, "text")
    runner.browser = browser
    result = runner.wait_for("text", "Done", timeout_ms=100)
    browser.text.assert_called_once_with("Done", timeout_ms=100)
    assert result.met is True
    assert runner.state["waits"] == [{"kind": "text", "met": True, "waited_ms": 12, "reason": "text"}]
    browser.mark_activity.assert_called_once()
    with pytest.raises(ValueError, match="Unknown wait"):
        runner.wait_for("sleep")


def test_the_wait_log_keeps_the_last_hundred(runner):
    runner.browser = runner.state["browser"]
    runner.browser.dom_quiet.return_value = WaitResult(False, 1, "timeout: x")
    for _ in range(130):
        runner.wait_for("dom_quiet")
    assert len(runner.state["waits"]) == 100


def test_the_wait_instruction_says_wait_settles_the_page():
    from jev_ultrafast.questions import NEXT_ACTION

    assert "streaming" in NEXT_ACTION and "until the page is settled" in NEXT_ACTION
