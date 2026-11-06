"""Where one non-navigating CLICK tick spends its time (stubbed model, so no paid call).

Prints a per-phase breakdown (visible with -rA, and appended to $GITHUB_STEP_SUMMARY in CI) and asserts only that
the tick stays under a generous bound, so the numbers are a record, not a flaky gate.
"""

import contextlib
import os
import statistics
import time

import pytest

from jev_ultrafast import Agent
from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module


class Recorder:
    def __init__(self):
        self.phases = []
        self.stack = []

    @contextlib.contextmanager
    def phase(self, name):
        entry = {"name": ".".join(self.stack + [name]), "ms": 0.0, "calls": []}
        self.stack.append(name)
        start = time.perf_counter()
        try:
            yield
        finally:
            entry["ms"] = (time.perf_counter() - start) * 1000
            self.stack.pop()
            self.phases.append(entry)

    def cdp(self, real):
        def timed(method, session_id=None, **params):
            start = time.perf_counter()
            try:
                return real(method, session_id=session_id, **params)
            finally:
                ms = (time.perf_counter() - start) * 1000
                expression = str(params.get("expression", ""))[:48].replace("\n", " ")
                self.calls.append((".".join(self.stack), method, expression, ms))

        return timed

    calls = None


def stub_click(monkeypatch):
    def choose(page, goal, history, refused=()):
        action = next(a for a in page["actions"] if a["label"] == "Press me")
        return {
            "choice": action["id"],
            "operation": "CLICK",
            "target": "1",
            "confidence": 1.0,
            "probabilities": {action["id"]: 1.0},
            "latency_ms": 1,
            "usage": {},
        }

    monkeypatch.setattr(loop, "choose", choose)


@pytest.mark.parametrize("new_window", [False, True, None])
def test_one_click_tick_breakdown(open_page, fixture_base_url, monkeypatch, new_window):
    open_page("tick_button.html")  # Skips (or fails in CI) when no browser is configured.
    stub_click(monkeypatch)
    recorder = Recorder()
    recorder.calls = []
    monkeypatch.setattr(browser_module, "cdp", recorder.cdp(browser_module.cdp))

    for name in ("fresh", "act", "observe"):
        real = getattr(browser_module.Browser, name)

        def wrap(real=real, name=name):
            def inner(self, *args, **kwargs):
                with recorder.phase(name):
                    return real(self, *args, **kwargs)

            return inner

        monkeypatch.setattr(browser_module.Browser, name, wrap())
    real_operation = browser_module.browser_operation

    def timed_operation(request):
        with recorder.phase(request["operation"]):
            return real_operation(request)

    monkeypatch.setattr(browser_module, "browser_operation", timed_operation)

    agent = Agent(
        f"{fixture_base_url}/tick_button.html", "Press the button", stuck_after=99, max_steps=20, new_window=new_window
    )
    try:
        # Floor: one trivial Runtime.evaluate round trip through browser-harness (new IPC connection per call).
        floor = []
        for _ in range(20):
            start = time.perf_counter()
            agent.browser.evaluate("1")
            floor.append((time.perf_counter() - start) * 1000)
        used = agent.browser.new_window
        version = browser_module.cdp("Browser.getVersion")
        print("Browser.getVersion:", version.get("product"), "|", version.get("userAgent"))
        if new_window is None:
            assert used is True  # Auto picks a window of its own in headless Chrome.
        recorder.calls.clear()
        recorder.phases.clear()
        ticks = []
        for _ in range(12):
            start = time.perf_counter()
            agent.command("tick")
            ticks.append((time.perf_counter() - start) * 1000)
        last_calls = [c for c in recorder.calls]
    finally:
        agent.close()

    lines = [
        f"### Click tick breakdown (stubbed model, headless Chrome, new_window={new_window!r}, "
        f"window used: {used})",
        f"- Runtime.evaluate('1') round trip: median {statistics.median(floor):.1f} ms, max {max(floor):.1f} ms",
        f"- tick ms (12 ticks): {', '.join(f'{t:.0f}' for t in ticks)}; median {statistics.median(ticks):.0f}",
        "",
        "Per phase, summed over 12 ticks (ms):",
    ]
    totals = {}
    for entry in recorder.phases:
        totals[entry["name"]] = totals.get(entry["name"], 0) + entry["ms"]
    lines += [f"- {name}: {ms / 12:.1f} per tick" for name, ms in sorted(totals.items(), key=lambda x: -x[1])]
    lines += ["", "CDP calls of the last tick (phase | method | expression | ms):"]
    per_tick = len(last_calls) // 12
    lines += [f"- {p} | {m} | {e} | {ms:.1f}" for p, m, e, ms in last_calls[-per_tick:]]
    report = "\n".join(lines)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(report + "\n\n")
    assert statistics.median(ticks) < 5000
    if new_window is not False:
        # Back to 1 s ticks (the throttled background tab) would show here; CI measures about 50 ms.
        assert statistics.median(ticks) < 300, ticks
