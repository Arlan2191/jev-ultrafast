"""Run evidence against a real Chrome: console, uncaught errors, network, screenshots, locators. Model is stubbed."""

import json
import os
import statistics
import threading
import time
from pathlib import Path

from jev_ultrafast import Agent, trace
from jev_ultrafast import agent as loop
from jev_ultrafast.evidence import THREAD


def stub(monkeypatch, plan):
    """choose() answers from `plan`: (label, kind, nth) per call, then DONE. No paid call."""
    queue = list(plan)

    def choose(page, goal, history, **extra):
        if not queue:
            return decision("DONE", "DONE")
        label, kind, nth = queue.pop(0)
        matches = [a for a in page["actions"] if a.get("label") == label and a.get("kind") == kind]
        action = matches[nth]
        value = "mail" if kind == "fill" else None
        return decision(action["id"], "TYPE_TEXT" if kind == "fill" else "CLICK", value)

    monkeypatch.setattr(loop, "choose", choose)


def decision(choice, operation, value=None):
    return {
        "choice": choice, "operation": operation, "target": "1", "confidence": 1.0,
        "probabilities": {choice: 1.0}, "latency_ms": 1, "usage": {"input_tokens": 7}, "model": "stub",
        **({"value": value} if value else {}),
    }


def lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def threads():
    return [t.name for t in threading.enumerate() if t.name == THREAD]


def test_a_run_writes_console_network_screenshots_and_a_trace(open_page, fixture_base_url, monkeypatch, tmp_path):
    open_page("blank.html")  # Skips (or fails in CI) when no browser is configured.
    stub(monkeypatch, [("Run", "click", 0)])
    run = tmp_path / "run"
    agent = Agent(f"{fixture_base_url}/evidence.html", "Press run", evidence_dir=run, stuck_after=99, max_steps=5)
    try:
        for _ in agent.run():
            pass
        state = agent.snapshot()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            "unreachable" in n["url"] and n["failure"] for n in agent.evidence.stream.snapshot()[1]
        ):
            time.sleep(0.05)
        assert threads() == [THREAD]
    finally:
        agent.close()
    assert threads() == []
    assert state["status"] == "done"

    console = lines(run / "console.jsonl")
    texts = [(c["level"], c["text"]) for c in console]
    assert ("log", "page loaded 42") in texts and ("log", "hello Object") in texts
    assert ("warning", "careful now") in texts and ("error", "something broke") in texts
    exception = next(c for c in console if c["source"] == "exception")
    assert "uncaught boom" in exception["text"] and exception["stack_top"].split(":")[0]

    network = lines(run / "network.jsonl")
    by_url = {n["url"].rsplit("/", 1)[-1]: n for n in network}
    assert by_url["0"]["status"] == 200 and by_url["0"]["resource_type"] == "Fetch"
    assert by_url["missing-resource"]["status"] == 404
    assert by_url["unreachable"]["status"] is None and by_url["unreachable"]["failure"]
    for row in (by_url["0"], by_url["missing-resource"]):
        assert row["duration_ms"] is not None and row["duration_ms"] >= 0 and row["start_ms"] >= 0
        assert row["mime"] and row["method"] == "GET" and row["encoded_size"] is not None
    har = json.loads((run / "network.har").read_text())
    assert har["log"]["version"] == "1.2" and len(har["log"]["entries"]) == len(network)
    assert {e["response"]["status"] for e in har["log"]["entries"]} >= {200, 404, 0}

    events = trace.load_trace(run / "trace.jsonl")
    end = events[-1]
    assert end["type"] == "run_end" and end["totals"]["actions"] == len(state["history"]) == 1
    assert end["totals"]["jev_calls"] == len(state["decisions"]) == 2
    assert end["totals"]["tokens"]["jev"] == {"input_tokens": 14}
    screenshots = sorted(p.name for p in (run / "screenshots").glob("*.jpg"))
    assert len(screenshots) == 3 and all((run / "screenshots" / n).stat().st_size > 500 for n in screenshots)
    assert "something broke" in (run / "report.html").read_text()


def test_locators_find_the_same_element_again(open_page, fixture_base_url, monkeypatch, tmp_path):
    open_page("blank.html")
    plan = [
        ("Delete", "click", 1),  # The one inside the dialog.
        ("Open", "click", 1),  # The link in the Beta row.
        ("Email address", "fill", 0),
        ("Save", "click", 0),
        ("Generated", "click", 0),
        ("Delete", "click", 0),  # The one outside it.
    ]
    stub(monkeypatch, plan)
    run = tmp_path / "run"
    agent = Agent(
        f"{fixture_base_url}/locators.html", "Exercise it", evidence_dir=run, stuck_after=99, max_steps=10,
        values={"mail": "a@example.test"},
    )
    try:
        for _ in agent.run():
            pass
        history = agent.state["history"]
    finally:
        agent.close()
    actions = [e for e in trace.load_trace(run / "trace.jsonl") if e["type"] == "action"]
    assert [a["label"] for a in actions] == ["Delete", "Open", "Email address", "Save", "Generated", "Delete"]
    assert [h["locator"] for h in history] == [a["locator"] for a in actions]
    dialog, link, field, save, generated, outside = (a["locator"] for a in actions)

    assert dialog["role"] == "button" and dialog["name"] == "Delete" and dialog["tag"] == "button"
    assert dialog["scope"] == {"role": "dialog", "name": "Confirm delete"}
    assert dialog["ordinal"]["scope"] == 0 and dialog["ordinal"]["document"] == 1
    assert dialog["ordinal"]["document_count"] == 2
    assert outside["ordinal"]["document"] == 0 and outside["scope"] == {"role": "main", "name": "Locators"}

    assert link["role"] == "link" and link["href"] == "/items/2?tab=info" and link["scope"]["role"] == "listitem"
    assert "Beta" in link["scope"]["name"] and link["ordinal"] == {
        "scope": 0, "scope_count": 1, "document": 1, "document_count": 2,
    }

    assert field["role"] == "textbox" and field["label_for"] == "Email address" and field["id"] == "email"
    assert field["placeholder"] == "you@example.test" and field["scope"] == {"role": "form", "name": "Contact"}
    assert "a@example.test" not in json.dumps(field)

    assert save["test_id"] == "save-button" and save["text"] == "Save"
    assert generated["id"] is None and generated["name"] == "Generated"
    assert all(loc["css_path"].startswith(("body", "main")) or "#" in loc["css_path"] for loc in actions_of(actions))
    assert all(loc["rect"]["w"] > 0 and loc["rect"]["h"] > 0 for loc in actions_of(actions))


def actions_of(actions):
    return [a["locator"] for a in actions]


def test_evidence_cost_per_click_tick(open_page, fixture_base_url, monkeypatch, tmp_path):
    """Median of 5 non-navigating click ticks: evidence off, evidence on without screenshots, and one screenshot.

    A record, not a gate: the numbers go to the step summary, and the bound is generous so a slow runner stays green.
    """
    open_page("blank.html")

    def pick(page, goal, history, **extra):
        action = next(a for a in page["actions"] if a["label"] == "Press me")
        return decision(action["id"], "CLICK")

    monkeypatch.setattr(loop, "choose", pick)

    def median_tick(**options):
        agent = Agent(f"{fixture_base_url}/tick_button.html", "Press", stuck_after=99, max_steps=30, **options)
        try:
            agent.command("tick")  # Warm-up: the first tick installs the page-side helpers.
            times = []
            for _ in range(5):
                start = time.perf_counter()
                agent.command("tick")
                times.append((time.perf_counter() - start) * 1000)
            return statistics.median(times), agent
        finally:
            agent.close()

    off, _ = median_tick()
    none, _ = median_tick(evidence_dir=tmp_path / "none", screenshots="none")
    actions, _ = median_tick(evidence_dir=tmp_path / "actions", screenshots="actions")
    shot = actions - none
    report = (
        "### Evidence cost per non-navigating click tick (median of 5, stubbed model)\n"
        f"- evidence off: {off:.0f} ms\n"
        f"- evidence on, screenshots none (trace, locator, console/network capture): {none:.0f} ms\n"
        f"- evidence on, screenshots actions: {actions:.0f} ms (screenshot cost about {shot:.0f} ms)"
    )
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(report + "\n\n")
    assert none < 300 and actions < 600, report
