"""Offline contracts for condition waits. No browser, no paid APIs."""

import re
import time
from unittest.mock import Mock

import pytest
from test_agent import decision, page

from jev_ultrafast import Agent
from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module
from jev_ultrafast.browser import Browser, fingerprint
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
            page_result([2, "b", "complete"]),  # The page answers again, with another document.
            page_result({"met": True, "reason": "settled", "document": [2, "b"]}),
        ]
    )
    result = b.settled(100, 5000)
    assert result.met is True
    assert b.call.call_count == 4


def test_a_page_that_stays_in_transition_times_out_with_a_reason():
    b = bare()
    b.call = Mock(side_effect=RuntimeError("Execution context was destroyed."))
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


def quiet(**fields):
    return {"epoch": "e1", "seq": 3, "inflight": 0, "new_started": 0, "timers": 0, **fields}


def settle_browser(timeout=5000, seen=None):
    b = bare()
    b.action_settle_timeout_ms, b.action_settle_quiet_ms = timeout, 150
    b.activity_seen = seen or quiet()
    b.settled = Mock(return_value=WaitResult(True, 7, "settled"))
    b.activity = Mock(return_value=quiet(seq=4))
    return b


@pytest.mark.parametrize(
    "activity, expected",
    [
        (quiet(), False),  # Nothing happened.
        (quiet(inflight=1), True),  # A counted request is in flight.
        (quiet(timers=2), True),  # A short timer was armed since the last read.
        (quiet(new_started=1), True),  # A counted request started and already ended.
        (None, False),
    ],
)
def test_the_post_action_settle_triggers_only_when_something_is_pending(activity, expected):
    b = settle_browser()
    b.settle_after_action(activity)
    assert b.settled.called is expected
    if expected:
        b.settled.assert_called_once_with(150, 5000, timers_since={"epoch": "e1", "since": 3})
        assert b.last_action_settle.met is True
    else:
        assert b.last_action_settle is None


def test_an_idle_action_adds_no_cdp_round_trip_and_updates_the_baseline():
    b = settle_browser()
    b.call = Mock()
    b.settle_after_action(quiet(seq=9))
    b.call.assert_not_called()
    b.activity.assert_not_called()
    assert b.activity_seen["seq"] == 9


def test_action_settle_can_be_turned_off():
    b = settle_browser(timeout=0)
    b.settle_after_action(quiet(inflight=2, timers=1))
    b.settled.assert_not_called()


def test_needs_settle_looks_only_at_the_counters_the_page_already_filtered():
    assert needs_settle(quiet()) is False
    assert needs_settle(quiet(inflight=1)) is True
    assert needs_settle(None) is False


def test_observe_hands_the_settle_scripts_activity_to_the_post_action_settle(monkeypatch):
    b = bare()
    b.after_input = {"kind": "click", "node": 1}
    seen = quiet(inflight=1)
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


# B1: a timed-out WAIT on an unchanged page is a stuck step.


def timed_out_wait(runner, observed=None):
    runner.state["browser"].act.return_value = {
        "executed": "wait",
        "wait": {"met": False, "waited_ms": 30000, "reason": "timeout: the DOM is still changing"},
    }
    if observed is not None:
        runner.state["browser"].observe.return_value = observed
    runner.state["decision"] = {**decision("wait"), "operation": "WAIT"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})


def test_three_timed_out_waits_on_an_unchanged_page_end_the_run_blocked(runner):
    for _ in range(3):
        timed_out_wait(runner)
    assert runner.state["status"] == "blocked"


def test_a_timed_out_wait_during_which_the_content_changed_does_not_count(runner):
    timed_out_wait(runner)
    timed_out_wait(runner)
    streaming = page()
    streaming["text"] = "A stream longer than the timeout"
    streaming["fingerprint"] = fingerprint(streaming)
    timed_out_wait(runner, streaming)
    assert runner.state["history"][-1]["content_changed"] is True
    assert runner.state["status"] == "ready"


def test_a_met_wait_on_an_unchanged_page_does_not_count(runner):
    runner.state["browser"].act.return_value = {
        "executed": "wait",
        "wait": {"met": True, "waited_ms": 600, "reason": "settled"},
    }
    for _ in range(4):
        runner.state["decision"] = {**decision("wait"), "operation": "WAIT"}
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "ready"


# S1 ignore_urls, S2 timers, S9 attributes: they reach the page-side spec.


def sent_specs(b):
    import json

    pattern_ = r"\.run\((.*)\) \?\? null\)$"
    return [json.loads(re.search(pattern_, c.kwargs["expression"])[1]) for c in b.call.call_args_list]


def test_browser_wide_knobs_are_part_of_every_wait_spec():
    b = bare()
    b.ignore = ignore_patterns(["/poll"])
    b.wait_timer_ms, b.wait_interactive_grace_ms, b.wait_ignore_attributes = 750, 1200, ("style",)
    b.call = Mock(return_value=page_result({"met": True, "reason": "settled", "document": [1, "a"]}))
    b.settled(100, 1000, ignore_urls=["/extra"])
    spec = sent_specs(b)[0]
    assert spec["ignore"] == [{"s": "/poll"}, {"s": "/extra"}]
    assert (spec["timer_ms"], spec["interactive_grace_ms"], spec["ignore_attributes"]) == (750, 1200, ["style"])


def test_a_call_can_override_the_ignored_attributes_and_set_the_timer_baseline():
    b = bare()
    b.call = Mock(return_value=page_result({"met": True, "reason": "dom_quiet", "document": [1, "a"]}))
    b.dom_quiet(100, 1000, ignore_attributes=("class", "style"))
    assert sent_specs(b)[0]["ignore_attributes"] == ["class", "style"]
    b.settled(100, 1000, timers_since={"epoch": "e1", "since": 7})
    assert sent_specs(b)[1]["epoch"] == "e1" and sent_specs(b)[1]["timers_since"] == 7


def test_text_stable_requires_text_for_an_element_but_not_for_the_whole_page():
    b = bare()
    b.call = Mock(return_value=page_result({"met": True, "reason": "text_stable", "document": [1, "a"]}))
    b.text_stable(100, role="textbox", name="Answer")
    b.text_stable(100)
    b.text_stable(100, role="textbox", require_text=False)
    assert [spec["require_text"] for spec in sent_specs(b)] == [True, False, False]
    with pytest.raises(ValueError, match="require_text"):
        b.text_stable(100, require_text="yes")


def test_the_activity_baseline_and_filters_go_to_the_page():
    import json

    b = bare()
    b.ignore, b.wait_timer_ms = ignore_patterns(["/poll"]), 900
    b.activity_seen = {"epoch": "e1", "seq": 5}
    b.evaluate = Mock(return_value={"epoch": "e1", "seq": 6})
    assert b.activity() == {"epoch": "e1", "seq": 6}
    sent = b.evaluate.call_args.args[0]
    assert sent.startswith("(window.__jevWait?.activity(") and "(function" not in sent
    assert json.loads(re.search(r"activity\((.*)\) \?\? null", sent)[1]) == {
        "epoch": "e1", "since": 5, "ignore": [{"s": "/poll"}], "timer_ms": 900,
    }


# The tracker is only sent when the page does not have it.


def test_the_tracker_text_is_sent_only_when_the_page_has_none():
    b = bare()
    b.call = Mock(
        side_effect=[page_result(None), page_result({"met": True, "reason": "settled", "document": [1, "a"]})]
    )
    assert b.settled(100, 1000).met is True
    first, second = (c.kwargs["expression"] for c in b.call.call_args_list)
    assert first.startswith("(window.__jevWait?.run(") and len(first) < 600
    assert second.startswith("(") and "__jevWait" in second and len(second) > 5000
    b.call = Mock(return_value=page_result({"met": True, "reason": "settled", "document": [1, "a"]}))
    b.settled(100, 1000)
    assert b.call.call_count == 1


# S3: a busy page never makes a wait raise.


def test_a_busy_page_is_an_unmet_slice_not_an_exception():
    b = bare()
    b.call = Mock(side_effect=TimeoutError("Runtime.evaluate timed out after 5s waiting for the daemon"))
    result = b.settled(100, 150)
    assert result.met is False and result.reason == "timeout: the page is navigating"
    assert b.activity() is None


def test_each_slice_waits_longer_for_the_daemon_than_the_slice_itself():
    b = bare()
    b.call = Mock(return_value=page_result({"met": True, "reason": "settled", "document": [1, "a"]}))
    b.settled(100, 5000)
    assert b.call.call_args.kwargs["_response_timeout"] == browser_module.WAIT_SLICE_MS / 1000 + 3


def test_a_transient_error_on_a_settled_document_returns_promptly():
    b = bare()
    b.call = Mock(
        side_effect=[
            RuntimeError("Cannot find context with specified id"),
            page_result([1, "a", "complete"]),  # Same document, nothing navigated.
            page_result({"met": True, "reason": "settled", "document": [1, "a"]}),
        ]
    )
    started = time.monotonic()
    assert b.settled(100, 30000).met is True
    assert time.monotonic() - started < 1


def test_the_wait_for_a_document_after_an_error_is_bounded_by_the_navigation_timeout():
    b = bare()
    b.navigation_timeout_ms = 120
    b.call = Mock(side_effect=RuntimeError("Execution context was destroyed."))
    started = time.monotonic()
    assert b._after_error([1, "a"], time.monotonic() + 30) == [1, "a"]
    assert time.monotonic() - started < 1  # Not the 30 s that remain of the wait.


# S4: an exception in the page is raised, a regex is checked up front.


def test_a_page_side_exception_is_raised_at_once_with_its_text():
    b = bare()
    b.call = Mock(
        return_value={
            "result": {"type": "object"},
            "exceptionDetails": {
                "text": "Uncaught",
                "exception": {"description": "SyntaxError: Invalid regular expression"},
            },
        }
    )
    with pytest.raises(ValueError, match="SyntaxError: Invalid regular expression"):
        b.text("x", timeout_ms=30000)
    assert b.call.call_count == 1


@pytest.mark.parametrize(
    "source",
    [r"(?P<x>a)", r"\Aa", r"a\Z", r"(?i)a", r"(?i:a)", r"a++", r"a*+", r"(?>a)", r"(a)?(?(1)b|c)", r"(?#note)a"],
)
def test_python_only_regex_syntax_is_rejected_before_the_page_sees_it(source):
    b = bare()
    b._wait = Mock()
    with pytest.raises(ValueError, match="JavaScript RegExp"):
        b.element(name=re.compile(source))
    b._wait.assert_not_called()


@pytest.mark.parametrize("flags", [re.VERBOSE, re.ASCII])
def test_unsupported_regex_flags_are_rejected(flags):
    b = bare()
    b._wait = Mock()
    with pytest.raises(ValueError, match="flags are supported"):
        b.text(re.compile("a", flags))


@pytest.mark.parametrize(
    "source", [r"^cop", r"(?<=a)b", r"[a\\A]", r"a+?", r"a\+\+", r"(?:a|b){2,3}", r"\bchunk \d+"]
)
def test_javascript_compatible_regexes_pass(source):
    assert pattern("x", re.compile(source))["r"] == source


# S5: the window default.


def make_browser(monkeypatch, product, **kwargs):
    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        return {
            "Browser.getVersion": {"product": product},
            "Target.createTarget": {"targetId": "t"},
            "Target.attachToTarget": {"sessionId": "s"},
        }.get(method, {})

    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_module, "cdp", cdp)
    monkeypatch.setattr(Browser, "evaluate", lambda self, expression: "complete")
    monkeypatch.setattr(Browser, "activity", lambda self: None)
    browser = Browser("https://example.test/", **kwargs)
    return browser, calls


@pytest.mark.parametrize(
    "product, option, expected",
    [
        ("HeadlessChrome/130.0.0.0", None, True),
        ("Chrome/130.0.0.0", None, False),
        ("Chrome/130.0.0.0", True, True),
        ("HeadlessChrome/130.0.0.0", False, False),
    ],
)
def test_the_owned_tab_gets_its_own_window_only_where_it_was_measured(monkeypatch, product, option, expected):
    browser, calls = make_browser(monkeypatch, product, new_window=option)
    created = next(params for method, params in calls if method == "Target.createTarget")
    assert created["newWindow"] is expected and created["background"] is True and browser.new_window is expected
    assert any(m == "Browser.getVersion" for m, _ in calls) is (option is None)


def test_page_enable_is_not_sent(monkeypatch):
    _, calls = make_browser(monkeypatch, "HeadlessChrome/130")
    assert "Page.enable" not in [m for m, _ in calls]
    assert "Page.addScriptToEvaluateOnNewDocument" in [m for m, _ in calls]


def test_the_body_cap_is_built_into_the_tracker_that_is_installed(monkeypatch):
    _, calls = make_browser(monkeypatch, "HeadlessChrome/130", body_cap_ms=1234)
    source = next(p["source"] for m, p in calls if m == "Page.addScriptToEvaluateOnNewDocument")
    assert "BODY_CAP_MS = 1234" in source and "__BODY_CAP_MS__" not in source and "__A11Y__" not in source


# Agent arguments of this round.


@pytest.mark.parametrize(
    "kwargs",
    [
        {"wait_timer_ms": -1},
        {"wait_timer_ms": 2.5},
        {"body_cap_ms": 0},
        {"wait_interactive_grace_ms": -1},
        {"new_window": "yes"},
        {"ignore_urls": [3]},
        {"ignore_urls": [re.compile("(?P<x>a)")]},
        {"wait_ignore_attributes": [3]},
    ],
)
def test_the_new_wait_arguments_are_validated_before_a_browser_opens(kwargs, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(loop, "Browser", opened)
    with pytest.raises(ValueError):
        Agent("https://example.test/", "goal", **kwargs)
    opened.assert_not_called()


def test_the_new_wait_arguments_reach_the_browser(monkeypatch):
    opened = Mock(return_value=Mock(observe=Mock(return_value=page())))
    monkeypatch.setattr(loop, "Browser", opened)
    Agent(
        "https://example.test/",
        "goal",
        ignore_urls=["/poll"],
        wait_timer_ms=0,
        body_cap_ms=5000,
        wait_ignore_attributes=("style",),
        wait_interactive_grace_ms=0,
        new_window=False,
    )
    kwargs = opened.call_args.kwargs
    assert kwargs["ignore_urls"] == ["/poll"] and kwargs["wait_timer_ms"] == 0 and kwargs["body_cap_ms"] == 5000
    assert kwargs["wait_ignore_attributes"] == ("style",) and kwargs["wait_interactive_grace_ms"] == 0
    assert kwargs["new_window"] is False


# S6: a WAIT is an action like any other for the policy hook.


def test_a_wait_reaches_before_action_like_any_action(runner):
    seen = []
    runner.before_action = lambda action, page, text: seen.append(action["kind"])
    runner.state["browser"].act.return_value = {
        "executed": "wait",
        "wait": {"met": True, "waited_ms": 5, "reason": "settled"},
    }
    runner.state["decision"] = {**decision("wait"), "operation": "WAIT"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert seen == ["wait"]


def test_a_vetoed_wait_is_not_executed(runner):
    def deny(action, page, text):
        raise loop.ActionDenied("no waiting")

    runner.before_action = deny
    runner.state["decision"] = {**decision("wait"), "operation": "WAIT"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["status"] == "denied"
