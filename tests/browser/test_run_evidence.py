"""Run evidence against a real Chrome: console, uncaught errors, network, screenshots, locators. Model is stubbed."""

import json
import os
import re
import statistics
import threading
import time
from pathlib import Path

from jev_ultrafast import Agent, trace
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import A11Y
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


def without_images(path):
    """The file's text without embedded screenshots (base64 can contain any three letters by chance)."""
    return re.sub(r"data:image/jpeg;base64,[A-Za-z0-9+/=]*", "", Path(path).read_text())


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
    assert by_url["1?token=<redacted>"]["status"] == 200
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


# (label, kind, nth among the shown controls with that label and kind) and the data-m marker of the element it hits.
LOCATOR_PLAN = [
    (("Delete", "click", 1), "dialog-delete"),  # The one inside the dialog.
    (("Open", "click", 1), "beta-open"),  # The link in the Beta row.
    (("Email address", "fill", 0), "email"),
    (("Save", "click", 0), "save"),
    (("Generated", "click", 0), "generated"),
    (("Delete", "click", 0), "outside-delete"),  # The one outside it.
    (("Delete", "click", 2), "panel1-delete"),  # Two sections named Panel; a hidden third one comes first.
    (("Delete", "click", 3), "panel2-delete"),
    (("Open", "click", 2), "same1-open"),  # Two rows named Same.
    (("Open", "click", 3), "same2-open"),
    (("Verify account", "click", 0), "verify"),
    (("Twin", "click", 1), "twin2"),  # The id is not unique, so it is not an anchor.
]


def marker_of(browser, locator):
    """{"rule", "marker"}: what resolve finds for a locator and the data-m marker of that element (test-only)."""
    return browser.evaluate(
        "(() => { const {resolve}=" + A11Y + f"; const r=resolve({json.dumps(locator)});"
        " return {rule:r.rule, marker:r.element ? r.element.getAttribute('data-m') : null}; })()"
    )


def test_locators_find_the_same_element_again(open_page, fixture_base_url, monkeypatch, tmp_path):
    open_page("blank.html")
    stub(monkeypatch, [step for step, _marker in LOCATOR_PLAN])
    run = tmp_path / "run"
    agent = Agent(
        f"{fixture_base_url}/locators.html", "Exercise it", evidence_dir=run, stuck_after=99, max_steps=30,
        values={"mail": "a@example.test"},
    )
    try:
        for _ in agent.run():
            pass
        history = agent.state["history"]
        actions = [e for e in trace.load_trace(run / "trace.jsonl") if e["type"] == "action"]
        assert [a["label"] for a in actions] == [
            "Delete", "Open", "Email address", "Save", "Generated", "Delete", "Delete", "Delete", "Open", "Open",
            "Verify account", "Twin",
        ]
        locators = [a["locator"] for a in actions]
        assert [h["locator"] for h in history] == locators and all(not a.get("locator_error") for a in actions)

        # The contract with replay: on a fresh load, each recorded locator resolves to the element it was read from.
        agent.browser.navigate({"kind": "reload"})
        found = [agent.browser.resolve(loc) for loc in locators]
        assert [marker_of(agent.browser, loc)["marker"] for loc in locators] == [m for _step, m in LOCATOR_PLAN]
        # A unique test id (Save) is read first; every other control is found by role, name and scope.
        assert {f["rule"] for f in found} == {"role_name_scope", "test_id_first"}
        assert found[3]["rule"] == "test_id_first" and found[0]["rule"] == "role_name_scope"
        assert all(f["verified"] and f["count_match"] for f in found)
        # And, when the name no longer matches, through the next rule down.
        field, save, generated = locators[2], locators[3], locators[4]
        renamed = {"name": "something else"}
        # Without the position data the primary rule is skipped and the fallbacks, which check the name, find them.
        unplaced = {"ordinal": None}
        assert agent.browser.resolve({**save, **unplaced})["rule"] == "test_id_first"
        assert agent.browser.resolve({**field, **unplaced})["rule"] == "id"
        # A renamed control is never returned by a fallback; css_path is only a hint, never an element.
        for loc in (save, field):
            assert agent.browser.resolve({**loc, **renamed})["rule"] == "hint"
        assert agent.browser.resolve({**generated, **renamed}) == {
            "rule": "hint", "unique": False, "count_match": False, "verified": False, "tag": None, "text": None,
            "hint": {"tag": "button", "text": "Generated"},
        }
        assert marker_of(agent.browser, {**generated, **renamed})["marker"] is None
        assert agent.browser.resolve({"role": "button", "name": "Nope", "ordinal": {"document": 0}}) is None
    finally:
        agent.close()
    dialog, link, field, save, generated, outside, panel1, panel2, same1, same2, verify, twin = locators

    assert dialog["role"] == "button" and dialog["name"] == "Delete" and dialog["tag"] == "button"
    assert {k: dialog["scope"][k] for k in ("role", "name", "ordinal", "count")} == {
        "role": "dialog", "name": "Confirm delete", "ordinal": 0, "count": 1,
    }
    assert dialog["scope"]["css_path"].startswith("body > main") and dialog["ordinal"]["scope"] == 0
    # The hidden Delete buttons are not counted: outside, dialog, two panels.
    assert dialog["ordinal"]["document"] == 1 and dialog["ordinal"]["document_count"] == 4
    assert outside["ordinal"]["document"] == 0 and (outside["scope"]["role"], outside["scope"]["name"]) == (
        "main", "Locators")

    assert link["role"] == "link" and link["scope"]["role"] == "listitem" and "Beta" in link["scope"]["name"]
    assert link["href"] == "/items/2?tab=<redacted>"
    assert link["ordinal"] == {"scope": 0, "scope_count": 1, "document": 1, "document_count": 4}
    assert (same1["scope"]["name"], same1["scope"]["ordinal"], same1["scope"]["count"]) == ("Same", 0, 2)
    assert (same2["scope"]["name"], same2["scope"]["ordinal"], same2["scope"]["count"]) == ("Same", 1, 2)
    assert (panel1["scope"]["role"], panel1["scope"]["name"], panel1["scope"]["ordinal"]) == ("region", "Panel", 0)
    assert (panel2["scope"]["ordinal"], panel2["scope"]["count"]) == (1, 2)

    assert field["role"] == "textbox" and field["label_for"] == "Email address" and field["id"] == "email"
    assert field["placeholder"] == "you@example.test"
    assert (field["scope"]["role"], field["scope"]["name"]) == ("form", "Contact")
    assert "a@example.test" not in json.dumps(field)

    assert save["test_id"] == "save-button" and save["text"] == "Save"
    assert generated["id"] is None and generated["name"] == "Generated"
    assert twin["id"] is None and twin["name"] == "Twin"
    assert verify["href"] == "/verify?token=<redacted>#<redacted>"
    assert all(loc["css_path"].startswith(("body", "main")) or "#" in loc["css_path"] for loc in locators)
    assert all(loc["rect"]["w"] > 0 and loc["rect"]["h"] > 0 for loc in locators)
    # An undeclared token in a clicked link, a request URL or a console line is in no written file.
    for path in run.rglob("*"):
        if path.is_file() and path.suffix != ".jpg":
            assert "zzz" not in without_images(path), path.name


# variant -> (markers that must still resolve to themselves, markers that must be refused: no element at all).
DRIFT = {
    "identical": (None, []),
    "rowabove": (["beta-open", "save", "email", "dialog-delete"], ["same1-open", "same2-open"]),
    "hide": (["dialog-delete", "panel1-delete", "save"], ["row1-remove", "row2-remove", "row3-remove"]),
    "icons": (["save", "pay", "dialog-delete"], ["icon1", "icon2", "icon3"]),
    "pay": (["save", "edit", "dialog-delete"], ["pay"]),
    "testid": (["pay", "save", "dialog-delete"], ["edit"]),
    "idwrap": (["pay", "save", "dialog-delete"], ["submit"]),
    # The reviewer's round 3 probes: counts differ, or a fallback would hit another control; always the same or none.
    "A": (["save", "dialog-delete"], ["a1", "a2", "a3"]),
    "E": (["save", "dialog-delete"], ["e1", "e2", "e3"]),
    "G": (["save", "dialog-delete"], ["g1", "g2", "g3"]),
    "H": (["save", "dialog-delete"], ["h1", "h2"]),
    "O": (["save", "dialog-delete"], ["o"]),
    "P": (["save", "dialog-delete"], ["p"]),
    "M": (["ma", "save", "dialog-delete"], ["mb"]),
    "N": (["mb", "save", "dialog-delete"], ["ma"]),
    "Q": (["q2", "save", "dialog-delete"], ["q1"]),
    # Round 4: a fallback never leaves the recorded scope. T, T3, U: the target is gone, a sibling has the same key.
    "T": (["t2", "t2s", "save", "dialog-delete"], ["t1"]),
    "T2": (["t2s", "save", "dialog-delete"], []),
    "T3": (["t3b", "save", "dialog-delete"], ["t3a"]),
    "U": (["save", "dialog-delete"], ["ua", "ub"]),  # ub: one Deal left of the two recorded, whichever it was
}


def test_a_drifted_page_never_resolves_to_another_element(open_page, fixture_base_url):
    """The replay contract: on a changed page a locator finds the SAME element (data-m marker) or nothing."""
    browser = open_page("locators.html")
    recorded = browser.evaluate(
        "(() => { const {locator, shown}=" + A11Y + "; return Object.fromEntries("
        "[...document.querySelectorAll('[data-m]')].filter(e => shown(e)).map(e => [e.dataset.m, locator(e)])"
        ".filter(pair => pair[1])); })()"
    )
    assert {"secret", "icon2", "row2-remove", "pay", "edit", "submit", "twin2"} <= set(recorded)
    assert {"a1", "e1", "g1", "h1", "o", "p", "ma", "mb", "q1", "q2"} <= set(recorded)
    assert {"t1", "t2", "t2s", "t3a", "t3b", "ua", "ub", "note"} <= set(recorded)
    assert recorded["t1"]["test_id_unique"] is False and recorded["t2s"]["test_id_unique"] is True
    assert recorded["ua"]["id_unique"] is True and recorded["ub"]["id_unique"] is None
    assert recorded["secret"]["role"] is None and recorded["secret"]["type"] == "password"
    assert "hidden-delete" not in recorded
    for variant, (kept, refused) in DRIFT.items():
        browser.navigate({"kind": "goto", "url": f"{fixture_base_url}/locators.html?variant={variant}"})
        found = {marker: marker_of(browser, loc) for marker, loc in recorded.items()}
        # Never another element, under any rule.
        wrong = {m: f for m, f in found.items() if f["marker"] not in (None, m)}
        assert not wrong, (variant, wrong)
        if kept is None:  # An identical page: all found, a unique test id first, the rest by the primary rule.
            by_test_id = {m for m, loc in recorded.items() if loc["test_id"] and loc["test_id_unique"]}
            assert by_test_id == {"save", "edit", "o", "t2s"}
            assert {m: f["rule"] for m, f in found.items()} == {
                m: "test_id_first" if m in by_test_id else "role_name_scope" for m in recorded
            }, variant
        for marker in kept or []:
            assert found[marker]["marker"] == marker, (variant, marker)
        for marker in refused:
            assert found[marker]["marker"] is None, (variant, marker, found[marker])
    # The refusal is a named rule, and the caller can tell: several matches, counts no longer as recorded.
    browser.navigate({"kind": "goto", "url": f"{fixture_base_url}/locators.html?variant=pay"})
    assert browser.resolve(recorded["pay"]) == {
        "rule": "ambiguous", "unique": False, "count_match": False, "verified": False, "tag": None, "text": None,
        "hint": None,
    }
    # T2 is the case the fallback is for: the scope was renamed, the test id is unique and the scope count was 1.
    browser.navigate({"kind": "goto", "url": f"{fixture_base_url}/locators.html?variant=T2"})
    assert browser.resolve(recorded["t2s"])["rule"] == "test_id_first"
    # The contents of an editable element are not returned, as they are not recorded.
    browser.navigate({"kind": "goto", "url": f"{fixture_base_url}/locators.html"})
    assert recorded["note"]["text"] == ""
    note = browser.resolve(recorded["note"])
    assert note["rule"] == "role_name_scope" and note["text"] == ""
    assert "secret-value-123" not in json.dumps(note)
    # A position is only a hint: M renamed the target, and css_path now points at another button, which is not returned.
    browser.navigate({"kind": "goto", "url": f"{fixture_base_url}/locators.html?variant=M"})
    assert browser.resolve(recorded["mb"]) == {
        "rule": "hint", "unique": False, "count_match": False, "verified": False, "tag": None, "text": None,
        "hint": {"tag": "button", "text": "X"},
    }


def test_urls_are_redacted_everywhere_the_run_wrote_them(open_page, fixture_base_url, monkeypatch, tmp_path):
    open_page("blank.html")
    stub(monkeypatch, [("Run", "click", 0)])
    run = tmp_path / "run"
    agent = Agent(
        f"{fixture_base_url}/evidence.html?session=zzz", "Press run", evidence_dir=run, stuck_after=99, max_steps=5
    )
    try:
        for _ in agent.run():
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            "api/1" in n["url"] for n in agent.evidence.stream.snapshot()[1]
        ):
            time.sleep(0.05)
    finally:
        agent.close()
    for path in run.rglob("*"):
        if path.is_file() and path.suffix != ".jpg":
            assert "zzz" not in without_images(path), path.name
    urls = [n["url"] for n in lines(run / "network.jsonl")]
    page = f"{fixture_base_url}/evidence.html?session=<redacted>"
    assert page in urls and f"{fixture_base_url}/api/1?token=<redacted>" in urls
    assert any(c["text"] == f"visiting {page}" for c in lines(run / "console.jsonl"))
    har = json.loads((run / "network.har").read_text())
    queries = [q for e in har["log"]["entries"] for q in e["request"]["queryString"]]
    assert {"name": "token", "value": "<redacted>"} in queries


def test_evidence_urls_full_keeps_what_the_page_used(open_page, fixture_base_url, monkeypatch, tmp_path):
    open_page("blank.html")
    stub(monkeypatch, [("Verify account", "click", 0)])
    run = tmp_path / "run"
    agent = Agent(f"{fixture_base_url}/locators.html", "Open it", evidence_dir=run, evidence_urls="full", max_steps=5)
    try:
        for _ in agent.run():
            pass
    finally:
        agent.close()
    action = next(e for e in trace.load_trace(run / "trace.jsonl") if e["type"] == "action")
    assert action["locator"]["href"] == "/verify?token=zzz"
    assert "token=zzz" in (run / "trace.jsonl").read_text()


def test_evidence_capture_false_runs_without_the_second_connection(open_page, fixture_base_url, monkeypatch, tmp_path):
    open_page("blank.html")
    stub(monkeypatch, [("Run", "click", 0)])
    run = tmp_path / "run"
    agent = Agent(
        f"{fixture_base_url}/evidence.html", "Press run", evidence_dir=run, evidence_capture=False, max_steps=5
    )
    try:
        assert threads() == []
        for _ in agent.run():
            pass
    finally:
        agent.close()
    assert not (run / "console.jsonl").exists() and not (run / "network.jsonl").exists()
    start = trace.load_trace(run / "trace.jsonl")[0]
    assert start["capture"]["reason"] == "evidence_capture=False"
    action = next(e for e in trace.load_trace(run / "trace.jsonl") if e["type"] == "action")
    assert action["locator"]["role"] == "button"
    assert len(list((run / "screenshots").glob("*.jpg"))) == 3


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
