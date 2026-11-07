"""Viewport option: validation and what reaches the browser (offline)."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module
from jev_ultrafast.agent import Agent
from jev_ultrafast.browser import Browser, browser_operation
from jev_ultrafast.waits import viewport_size


def fake_cdp(calls):
    def cdp(method, **params):
        calls.append((method, params))
        return {"Target.createTarget": {"targetId": "t"}, "Target.attachToTarget": {"sessionId": "s"}}.get(method, {})

    return cdp


def make_browser(monkeypatch, **kwargs):
    calls = []
    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_module, "cdp", fake_cdp(calls))
    monkeypatch.setattr(Browser, "evaluate", lambda self, expression: "complete")
    monkeypatch.setattr(Browser, "activity", lambda self: None)
    return Browser("https://example.test/", new_window=False, **kwargs), calls


@pytest.mark.parametrize("bad", [(0, 600), (800, -1), (800.0, 600), (True, 600), ("800", "600"), (800,), (1, 2, 3),
                                 800, None, (7681, 600), (800, 4321)])
def test_a_bad_viewport_is_rejected(bad):
    with pytest.raises(ValueError, match="viewport"):
        viewport_size(bad)


def test_limits_are_accepted_and_lists_become_tuples():
    assert viewport_size((1, 1)) == (1, 1)
    assert viewport_size([7680, 4320]) == (7680, 4320)


def test_the_default_viewport_is_unchanged(monkeypatch):
    browser, calls = make_browser(monkeypatch)
    sent = next(p for m, p in calls if m == "Emulation.setDeviceMetricsOverride")
    assert (sent["width"], sent["height"]) == (1120, 780) and browser.viewport == (1120, 780)


def test_the_device_metrics_call_carries_the_viewport(monkeypatch):
    _, calls = make_browser(monkeypatch, viewport=(1470, 920))
    sent = next(p for m, p in calls if m == "Emulation.setDeviceMetricsOverride")
    assert (sent["width"], sent["height"]) == (1470, 920)


def test_a_bad_viewport_creates_no_tab(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_module, "cdp", fake_cdp(calls))
    with pytest.raises(ValueError, match="viewport"):
        Browser("https://example.test/", viewport=(0, 0))
    assert not [m for m, _ in calls if m == "Target.createTarget"]


@pytest.mark.parametrize("size", [(800, 600), (1470, 920), None])
def test_the_wheel_turns_over_the_middle_of_the_viewport(monkeypatch, size):
    calls = []
    monkeypatch.setattr(browser_module, "cdp", fake_cdp(calls))
    request = {"operation": "act", "session": "s", "action": {"id": "scroll_down", "kind": "scroll", "delta": 560}}
    if size:
        request["viewport"] = size
    browser_operation(request)
    wheel = next(p for m, p in calls if m == "Input.dispatchMouseEvent")
    width, height = size or (1120, 780)
    assert (wheel["x"], wheel["y"], wheel["deltaY"]) == (width // 2, height // 2, 560)


def test_send_passes_the_viewport_to_the_operation(monkeypatch):
    browser, _ = make_browser(monkeypatch, viewport=(800, 600))
    seen = []
    monkeypatch.setattr(browser_module, "browser_operation", lambda request: seen.append(request) or {})
    browser.send({"id": "scroll_down", "kind": "scroll", "delta": 560}, {}, None)
    assert seen[0]["viewport"] == (800, 600)


def test_the_agent_validates_before_opening_anything(monkeypatch):
    opened = Mock()
    monkeypatch.setattr(loop, "Browser", opened)
    with pytest.raises(ValueError, match="viewport"):
        Agent("https://example.test/", "Find a book", viewport=(0, 600))
    opened.assert_not_called()


def scroll_step(height):
    """Evaluate the scroll step line of snapshot.js with a given innerHeight."""
    source = (Path(__file__).resolve().parent.parent / "jev_ultrafast" / "snapshot.js").read_text()
    line = next(row.strip() for row in source.splitlines() if row.strip().startswith("const step="))
    out = subprocess.run(
        ["node", "-e", f"const innerHeight={height}; {line} console.log(step)"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout)


@pytest.mark.parametrize("height, step", [(780, 560), (400, 287), (1, 1), (0, 1)])
def test_the_scroll_step_follows_the_viewport_height(height, step):
    assert scroll_step(height) == step
