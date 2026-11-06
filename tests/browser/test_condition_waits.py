"""Condition waits on real pages: streams, fetches, timers, long-lived connections. The model is stubbed."""

import re
import statistics
import time

import pytest

from jev_ultrafast import Agent
from jev_ultrafast import agent as loop


def stub(monkeypatch, plan):
    """plan: [(operation, label or id)], one entry per step; the last repeats."""

    def choose(page, goal, history, refused=()):
        operation, wanted = plan[min(len(history), len(plan) - 1)]
        action = next(a for a in page["actions"] if wanted in {a["id"], a["label"]})
        return {
            "choice": action["id"],
            "operation": operation,
            "target": "1",
            "confidence": 1.0,
            "probabilities": {action["id"]: 1.0},
            "latency_ms": 1,
            "usage": {},
        }

    monkeypatch.setattr(loop, "choose", choose)


@pytest.fixture
def start(open_page, fixture_base_url):
    open_page("smoke.html")  # Skips (or fails in CI) when no browser is configured.
    agents = []

    def start_(name, **kwargs):
        agent = Agent(f"{fixture_base_url}/{name}", "Use the page", **kwargs)
        agents.append(agent)
        return agent

    yield start_
    for agent in agents:
        agent.close()


def begin_stream(browser):
    started = time.monotonic()
    browser.evaluate("start(); 1")
    return started


def elapsed_ms(started):
    return (time.monotonic() - started) * 1000


def test_one_wait_covers_a_whole_stream_with_no_further_model_call(start, monkeypatch):
    stub(monkeypatch, [("WAIT", "wait")])
    agent = start("stream.html", max_steps=1, action_settle_timeout_ms=0)
    started = begin_stream(agent.browser)
    agent.command("tick")
    total = elapsed_ms(started)
    entry = agent.state["history"][0]
    assert entry["kind"] == "wait" and entry["met"] is True
    assert 3000 <= total <= 3000 + 500 + 1500, total
    assert entry["waited_ms"] <= total
    assert "chunk 6" in agent.state["page"]["text"]
    assert len(agent.state["decisions"]) == 1  # One model call covered the whole stream.


def test_text_stable_on_the_streaming_element(start):
    agent = start("stream.html")
    started = begin_stream(agent.browser)
    # An element that has not started to fill is stable too, so wait for the first text first.
    assert agent.wait_for("text", "chunk 1", timeout_ms=5000).met is True
    # Chunks arrive 500 ms apart, so only a quiet period longer than that waits for the last one (at 2.5 s).
    result = agent.wait_for("text_stable", quiet_ms=800, role="textbox", name="Answer")
    assert result.met is True and 2500 + 800 <= elapsed_ms(started) <= 2500 + 800 + 1500
    assert "chunk 6" in agent.browser.evaluate("document.getElementById('out').textContent")


def test_an_element_appears_after_the_stream_and_a_spinner_goes_away(start):
    agent = start("stream.html")
    absent = agent.wait_for("element", role="button", name="Copy", timeout_ms=300)
    assert absent.met is False and absent.reason.startswith("timeout") and absent.waited_ms >= 300
    started = begin_stream(agent.browser)
    gone = agent.wait_for("element", role="button", name="Stop generating", present=False)
    assert gone.met is True and 2800 <= elapsed_ms(started) <= 4500
    copy = agent.wait_for("element", role="button", name=re.compile("^cop", re.I), timeout_ms=2000)
    assert copy.met is True
    assert agent.wait_for("text", "chunk 6").met is True
    assert agent.wait_for("text", re.compile(r"chunk 7"), timeout_ms=200).met is False


def test_text_present_false_waits_for_text_to_leave(start):
    agent = start("fetch_render.html")
    agent.browser.evaluate("document.getElementById('result').textContent='Working'; 1")
    agent.browser.evaluate("setTimeout(() => { document.getElementById('result').textContent=''; }, 300); 1")
    result = agent.wait_for("text", "Working", present=False, timeout_ms=3000)
    assert result.met is True and result.waited_ms >= 250


def test_a_long_lived_event_source_does_not_block_network_idle(start):
    agent = start("events.html")
    result = agent.wait_for("network_idle", idle_ms=200, timeout_ms=3000)
    assert result.met is True and result.waited_ms < 1500, result


def test_an_ignored_long_poll_is_ignored_and_an_unignored_one_blocks(start):
    agent = start("events.html")
    agent.browser.evaluate("document.getElementById('poll').click(); 1")
    blocked = agent.wait_for("network_idle", idle_ms=100, timeout_ms=700)
    assert blocked.met is False and "in flight" in blocked.reason
    for ignore in (["/hang"], [re.compile(r"/ha+ng$")], "hang"):
        assert agent.wait_for("network_idle", idle_ms=100, ignore_urls=ignore, timeout_ms=3000).met is True


def test_dom_quiet_waits_for_mutations_to_stop(start):
    agent = start("fetch_render.html")
    agent.browser.evaluate(
        "let n = 0; const t = setInterval(() => { document.body.append(document.createElement('b')); "
        "if (++n === 10) clearInterval(t); }, 50); 1"
    )
    started = time.monotonic()
    result = agent.wait_for("dom_quiet", quiet_ms=200, timeout_ms=5000)
    assert result.met is True and elapsed_ms(started) >= 450


def test_a_click_that_fetches_is_observed_after_the_result_rendered_without_a_wait(start, monkeypatch):
    stub(monkeypatch, [("CLICK", "Load report")])
    agent = start("fetch_render.html", max_steps=1)
    agent.command("tick")
    assert "Report ready: ok" in agent.state["page"]["text"]
    assert [h["kind"] for h in agent.state["history"]] == ["click"]
    assert agent.browser.last_action_settle.met is True


def test_a_click_that_navigates_from_a_timer_observes_the_new_page(start, monkeypatch):
    stub(monkeypatch, [("CLICK", "Continue")])
    agent = start("timer_nav.html", max_steps=1)
    agent.command("tick")
    assert agent.state["page"]["url"].endswith("/nav_two.html")
    assert "Second page" in agent.state["page"]["text"]


def test_a_page_that_never_settles_times_out_without_raising_and_the_run_continues(start, monkeypatch):
    stub(monkeypatch, [("WAIT", "wait")])
    agent = start("mutating.html", wait_timeout_ms=400, wait_quiet_ms=200, max_steps=3)
    direct = agent.wait_for("settled", quiet_ms=200, timeout_ms=300)
    assert direct.met is False and direct.reason.startswith("timeout") and 300 <= direct.waited_ms < 1500
    agent.command("tick")
    first = agent.state["history"][0]
    assert first["met"] is False and first["kind"] == "wait" and 400 <= first["waited_ms"] < 2000
    assert agent.state["status"] == "ready"
    agent.command("tick")
    assert len(agent.state["history"]) == 2 and agent.state["status"] == "ready"
    assert [w["kind"] for w in agent.state["waits"]] == ["settled"]


def test_caller_waits_are_recorded_and_capped(start):
    agent = start("fetch_render.html")
    for _ in range(105):
        agent.wait_for("dom_quiet", quiet_ms=0, timeout_ms=100)
    assert len(agent.state["waits"]) == 100 and agent.state["waits"][-1]["kind"] == "dom_quiet"


def test_a_plain_click_adds_almost_no_latency_for_the_post_action_settle(start, monkeypatch):
    stub(monkeypatch, [("CLICK", "Press me")])
    medians = {}
    for timeout in (0, 5000):
        agent = start("tick_button.html", action_settle_timeout_ms=timeout, stuck_after=99, max_steps=30)
        agent.command("tick")  # Warm up.
        times = []
        for _ in range(5):
            began = time.perf_counter()
            agent.command("tick")
            times.append((time.perf_counter() - began) * 1000)
        medians[timeout] = statistics.median(times)
        assert agent.browser.last_action_settle is None  # Nothing was pending, so no settled() ran.
    print(f"plain click tick, median of 5: settle off {medians[0]:.1f} ms, on {medians[5000]:.1f} ms")
    assert medians[5000] - medians[0] < 30, medians
