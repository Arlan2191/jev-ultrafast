"""Driving an existing tab: Browser(target_id=...) attaches, never creates or closes a target (offline)."""

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module
from jev_ultrafast.agent import Agent
from jev_ultrafast.browser import Browser

TABS = {"targetInfos": [{"targetId": "mine", "type": "page"}, {"targetId": "other", "type": "page"}]}


def install(monkeypatch, infos=TABS):
    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        return {
            "Target.createTarget": {"targetId": "created"},
            "Target.attachToTarget": {"sessionId": "s"},
            "Target.getTargets": infos,
        }.get(method, {})

    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_module, "cdp", cdp)
    monkeypatch.setattr(Browser, "evaluate", lambda self, expression: "complete")
    monkeypatch.setattr(Browser, "activity", lambda self: None)
    return calls


def methods(calls):
    return [m for m, _ in calls]


def test_a_target_id_attaches_without_creating_or_navigating(monkeypatch):
    calls = install(monkeypatch)
    browser = Browser(target_id="mine", new_window=False)
    assert "Target.createTarget" not in methods(calls) and "Page.navigate" not in methods(calls)
    attach = next(p for m, p in calls if m == "Target.attachToTarget")
    assert attach["targetId"] == "mine" and attach["flatten"] is True
    assert browser.target == "mine"


def test_the_callers_viewport_and_focus_are_left_alone(monkeypatch):
    calls = install(monkeypatch)
    Browser(target_id="mine")
    assert "Emulation.setDeviceMetricsOverride" not in methods(calls)
    assert "Emulation.setFocusEmulationEnabled" not in methods(calls)


def test_an_explicit_viewport_is_applied_to_the_attached_tab(monkeypatch):
    calls = install(monkeypatch)
    browser = Browser(target_id="mine", viewport=(800, 600))
    sent = next(p for m, p in calls if m == "Emulation.setDeviceMetricsOverride")
    assert (sent["width"], sent["height"]) == (800, 600) and browser.viewport == (800, 600)


def test_the_tracker_is_installed_on_the_existing_document_too(monkeypatch):
    calls = install(monkeypatch)
    Browser(target_id="mine")
    assert "Page.addScriptToEvaluateOnNewDocument" in methods(calls)
    evaluated = [p["expression"] for m, p in calls if m == "Runtime.evaluate"]
    assert any("__jevWait" in e for e in evaluated)


def test_url_with_a_target_id_navigates_the_attached_tab(monkeypatch):
    calls = install(monkeypatch)
    Browser("https://example.test/", target_id="mine")
    assert "Target.createTarget" not in methods(calls)
    assert next(p for m, p in calls if m == "Page.navigate")["url"] == "https://example.test/"


def test_close_detaches_and_never_closes_the_callers_tab(monkeypatch):
    calls = install(monkeypatch)
    browser = Browser(target_id="mine")
    browser.close()
    browser.close()
    assert "Target.closeTarget" not in methods(calls)
    detach = [p for m, p in calls if m == "Target.detachFromTarget"]
    assert len(detach) == 1 and detach[0]["sessionId"] == "s"


def test_a_created_tab_is_still_closed(monkeypatch):
    calls = install(monkeypatch)
    Browser("https://example.test/", new_window=False).close()
    assert "Target.closeTarget" in methods(calls) and "Target.detachFromTarget" not in methods(calls)


def test_neither_url_nor_target_id_is_an_error(monkeypatch):
    calls = install(monkeypatch)
    with pytest.raises(ValueError, match="url or target_id"):
        Browser()
    assert calls == []


def test_an_unknown_target_id_raises_before_attaching(monkeypatch):
    calls = install(monkeypatch)
    with pytest.raises(ValueError, match="gone"):
        Browser(target_id="gone")
    assert "Target.attachToTarget" not in methods(calls) and "Target.closeTarget" not in methods(calls)


def test_a_failed_setup_does_not_close_the_callers_tab(monkeypatch):
    calls = install(monkeypatch)
    monkeypatch.setattr(Browser, "evaluate", lambda self, expression: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        Browser(target_id="mine")
    assert "Target.closeTarget" not in methods(calls) and "Target.detachFromTarget" in methods(calls)


def test_the_agent_passes_target_id_through(monkeypatch):
    seen = {}

    def fake(*args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(loop, "Browser", fake)
    with pytest.raises(RuntimeError):
        Agent(goals="goal", target_id="mine")
    assert seen["kwargs"]["target_id"] == "mine"


def test_the_agent_requires_a_url_or_a_target_id(monkeypatch):
    opened = []
    monkeypatch.setattr(loop, "Browser", lambda *a, **k: opened.append(1))
    with pytest.raises(ValueError, match="url or target_id"):
        Agent(goals="goal")
    assert opened == []
