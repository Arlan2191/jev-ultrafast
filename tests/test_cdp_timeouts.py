"""Per-call CDP timeouts: every call carries a response timeout, a read that times out is a stale page, a mutation that
times out is never retried and names the call (offline)."""

from unittest.mock import Mock

import pytest
from browser_harness.helpers import _IPCResponseTimeout

from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module
from jev_ultrafast.agent import Agent
from jev_ultrafast.browser import Browser, StalePage, browser_operation


def fake_cdp(calls, slow=()):
    def cdp(method, session_id=None, _response_timeout=5, **params):
        calls.append((method, _response_timeout, params))
        if method in slow:
            raise _IPCResponseTimeout(f"{method} timed out after {_response_timeout:g}s waiting for the daemon")
        return {"Target.createTarget": {"targetId": "t"}, "Target.attachToTarget": {"sessionId": "s"}}.get(method, {})

    return cdp


def make_browser(monkeypatch, slow=(), **kwargs):
    calls = []
    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_module, "cdp", fake_cdp(calls, slow))
    monkeypatch.setattr(Browser, "evaluate", lambda self, expression: "complete")
    monkeypatch.setattr(Browser, "activity", lambda self: None)
    return Browser("https://example.test/", new_window=False, **kwargs), calls


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "15000", None])
def test_a_bad_cdp_timeout_is_rejected(bad, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(loop, "Browser", opened)
    with pytest.raises(ValueError, match="cdp_timeout_ms"):
        Agent("https://example.test/", "goal", cdp_timeout_ms=bad)
    opened.assert_not_called()
    with pytest.raises(ValueError, match="cdp_timeout_ms"):
        Browser.__new__(Browser).__init__("https://example.test/", cdp_timeout_ms=bad)


def test_every_call_of_the_library_carries_the_timeout(monkeypatch):
    browser, calls = make_browser(monkeypatch, cdp_timeout_ms=7000, navigation_timeout_ms=9000)
    browser.call("Page.captureScreenshot")
    browser.close()
    seen = {method: timeout for method, timeout, _params in calls}
    assert seen["Target.createTarget"] == seen["Target.attachToTarget"] == 7
    assert seen["Emulation.setDeviceMetricsOverride"] == seen["Page.captureScreenshot"] == 7
    assert seen["Target.closeTarget"] == 7
    assert seen["Page.navigate"] == 9  # navigation keeps navigation_timeout_ms


def test_the_default_is_15_seconds(monkeypatch):
    browser, calls = make_browser(monkeypatch)
    assert browser.cdp_timeout_ms == 15000
    assert {t for m, t, _p in calls if m != "Page.navigate"} == {15}


def test_the_operation_script_uses_the_browsers_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_module, "cdp", fake_cdp(calls))

    def evaluate_everything(method, session_id=None, _response_timeout=5, **params):
        calls.append((method, _response_timeout, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": {"actions": [], "url": "u", "text": "", "scroll": {}}}}
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 0}
        return {"data": ""}

    monkeypatch.setattr(browser_module, "cdp", evaluate_everything)
    browser_operation({"operation": "observe", "session": "s", "cdp_timeout_ms": 12000})
    assert {t for _m, t, _p in calls} == {12}


def test_a_timed_out_read_is_a_stale_page_and_a_timeout():
    assert issubclass(browser_module.ReadTimeout, StalePage) and issubclass(browser_module.ReadTimeout, TimeoutError)


def test_a_read_that_times_out_raises_read_timeout_naming_the_call(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_module, "cdp", fake_cdp(calls, slow={"Runtime.evaluate"}))
    with pytest.raises(browser_module.ReadTimeout, match="Runtime.evaluate timed out after 15s"):
        browser_module.send("Runtime.evaluate", "s", 15, expression="1")


@pytest.mark.parametrize(
    "method", ["Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText", "Page.navigate", "Page.reload"]
)
def test_a_mutation_that_times_out_raises_mutation_timeout_naming_the_call(method, monkeypatch):
    calls = []
    monkeypatch.setattr(browser_module, "cdp", fake_cdp(calls, slow={method}))
    with pytest.raises(browser_module.MutationTimeout, match=f"{method} timed out after 4s") as raised:
        browser_module.send(method, "s", 4)
    assert not isinstance(raised.value, StalePage | TimeoutError) and "not retried" in str(raised.value)
    assert len(calls) == 1


def test_observe_looks_again_after_a_read_timeout(monkeypatch):
    browser = Browser.__new__(Browser)
    browser.session = "s"
    slow = browser_module.ReadTimeout
    answers = [slow("Runtime.evaluate timed out"), slow("Page.captureScreenshot timed out"), {"ok": 1}]

    def operation(_request):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(browser_module, "browser_operation", operation)
    assert browser.observe() == {"ok": 1} and answers == []


def test_observe_is_bounded_when_every_read_times_out(monkeypatch):
    browser = Browser.__new__(Browser)
    browser.session = "s"
    operation = Mock(side_effect=browser_module.ReadTimeout("Runtime.evaluate timed out"))
    monkeypatch.setattr(browser_module, "browser_operation", operation)
    with pytest.raises(browser_module.ReadTimeout):
        browser.observe()
    assert operation.call_count == 10


def test_a_read_timeout_in_the_freshness_check_makes_the_agent_observe_again():
    from tests.test_agent import page  # noqa: PLC0415

    fresh_page = page()
    agent = loop.Agent.__new__(loop.Agent)
    agent.screenshots, agent.pending_text, agent.replay = False, None, None
    agent.max_steps, agent.max_model_calls, agent.stuck_after, agent.before_action = 60, 120, 3, None
    fresh = Mock(side_effect=browser_module.ReadTimeout("Runtime.evaluate timed out"))
    browser = Mock(fresh=fresh, observe=Mock(return_value=fresh_page))
    agent.browser = browser
    agent.state = {
        "browser": browser, "page": fresh_page, "decision": None, "goal": "g", "history": [], "decisions": [],
        "status": "ready", "started_at": 0.0, "record": False, "text_calls": [], "steps": [], "plan_index": 0,
    }
    agent.snapshot = Mock(return_value={"status": "ready"})
    agent.shown = lambda p: p
    agent.trace_observe = Mock()
    assert agent.command("tick") == {"status": "ready"}
    browser.observe.assert_called_once()


def input_calls(monkeypatch, slow):
    calls = []

    def cdp(method, session_id=None, _response_timeout=5, **params):
        calls.append(method)
        if method == slow:
            raise _IPCResponseTimeout(f"{method} timed out after {_response_timeout:g}s waiting for the daemon")
        if method == "Runtime.evaluate":
            return {"result": {"value": {"x": 5, "y": 5}}}
        return {}

    monkeypatch.setattr(browser_module, "cdp", cdp)
    return calls


def test_a_click_whose_press_times_out_is_never_retried(monkeypatch):
    calls = input_calls(monkeypatch, "Input.dispatchMouseEvent")
    request = {"operation": "act", "session": "s", "action": {"kind": "click", "id": "e1", "node": 1}}
    with pytest.raises(browser_module.MutationTimeout, match="Input.dispatchMouseEvent timed out"):
        browser_operation(request)
    assert calls.count("Input.dispatchMouseEvent") == 1  # The move; nothing after it was sent.


def test_a_key_that_times_out_is_not_retried_and_the_browser_does_not_observe(monkeypatch):
    calls = input_calls(monkeypatch, "Input.dispatchKeyEvent")
    browser = Browser.__new__(Browser)
    browser.session, browser.cdp_timeout_ms, browser.navigation_timeout_ms = "s", 15000, 15000
    browser.keys, browser.urls = (), {}
    action = {"kind": "key", "key": "Enter", "id": "key:Enter"}
    with pytest.raises(browser_module.MutationTimeout, match="Input.dispatchKeyEvent timed out"):
        browser.send(action, {}, None)
    assert calls.count("Input.dispatchKeyEvent") == 1
    assert getattr(browser, "after_input", None) is None


def test_a_navigation_that_times_out_is_a_mutation_error(monkeypatch):
    calls = input_calls(monkeypatch, "Page.navigate")
    browser = Browser.__new__(Browser)
    browser.session, browser.cdp_timeout_ms, browser.navigation_timeout_ms = "s", 15000, 15000
    monkeypatch.setattr(Browser, "evaluate", lambda self, expression: [0, "u"])
    with pytest.raises(browser_module.MutationTimeout, match="Page.navigate timed out"):
        browser.navigate({"kind": "goto", "url": "https://example.test/"})
    assert calls.count("Page.navigate") == 1


def test_a_select_whose_script_times_out_is_a_mutation(monkeypatch):
    calls = input_calls(monkeypatch, "Runtime.evaluate")
    request = {
        "operation": "act", "session": "s",
        "action": {"kind": "select", "id": "e1", "node": 1, "value": "a"},
    }
    with pytest.raises(browser_module.MutationTimeout, match="Runtime.evaluate timed out"):
        browser_operation(request)
    assert calls == ["Runtime.evaluate"]


def test_a_click_locate_that_times_out_is_a_stale_read_and_nothing_was_pressed(monkeypatch):
    calls = input_calls(monkeypatch, "Runtime.evaluate")
    request = {"operation": "act", "session": "s", "action": {"kind": "click", "id": "e1", "node": 1}}
    with pytest.raises(browser_module.ReadTimeout):
        browser_operation(request)
    assert "Input.dispatchMouseEvent" not in calls
