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


def await_page(browser, expression, timeout_s=10):
    """Poll a page expression until it is truthy, so a wait never races page work that has not started yet."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if browser.evaluate(f"!!({expression})"):
            return
        time.sleep(0.01)
    raise AssertionError(f"page never reached: {expression}")


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
    browser = agent.browser
    # Armed in the same evaluate that shows the text, so the page timer does not depend on how fast the wait starts.
    browser.evaluate(
        "window.__cleared = false; document.getElementById('result').textContent='Working'; "
        "setTimeout(() => { document.getElementById('result').textContent=''; window.__cleared = true; }, 1000); 1"
    )
    result = agent.wait_for("text", "Working", present=False, timeout_ms=5000)
    assert result.met is True, result
    # The wait returned because the text left, not before: the timer had fired and the text is gone.
    assert browser.evaluate("window.__cleared") is True
    assert browser.evaluate("document.getElementById('result').textContent") == ""


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
    browser = agent.browser
    browser.evaluate(
        "window.__ticks = 0; window.__done = false; const t = setInterval(() => { "
        "document.body.append(document.createElement('b')); window.__ticks++; "
        "if (window.__ticks === 20) { clearInterval(t); window.__done = true; } }, 25); 1"
    )
    await_page(browser, "window.__ticks >= 1")  # The page is mutating before the wait starts.
    ticks_before = browser.evaluate("window.__ticks")
    result = agent.wait_for("dom_quiet", quiet_ms=300, timeout_ms=5000)
    assert result.met is True, result
    # The wait outlived the mutations: the interval had finished and nodes were appended after the wait began.
    assert browser.evaluate("window.__done") is True
    assert browser.evaluate("window.__ticks") == 20 and ticks_before < 20


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


# Timers (S2): only timers pending when a wait begins hold it, and the threshold is a knob.


def test_a_polling_page_settles_because_later_timers_never_hold_the_wait(start):
    agent = start("poll.html", ignore_urls=["/api/50"])
    result = agent.wait_for("settled", quiet_ms=300, timeout_ms=6000)
    # The pending poll timer (400 ms) holds the wait once; the timer its continuation arms afterwards does not.
    assert result.met is True and result.waited_ms < 2500, result
    time.sleep(1.0)
    assert agent.browser.evaluate("window.polls") >= 1  # The page really keeps polling.


def test_the_poll_timer_is_not_waited_for_when_timer_counting_is_off(start):
    agent = start("poll.html", ignore_urls=["/api/50"], wait_timer_ms=0)
    result = agent.wait_for("settled", quiet_ms=100, timeout_ms=6000)
    assert result.met is True and result.waited_ms < 600, result


def timer_wait(start, delay, **kwargs):
    agent = start("blank.html", **kwargs)
    agent.browser.evaluate(f"window.fired = false; setTimeout(() => {{ window.fired = true; }}, {delay}); 1")
    result = agent.wait_for("settled", quiet_ms=0, timeout_ms=8000)
    return result, agent.browser.evaluate("window.fired")


def test_a_timer_of_exactly_the_threshold_is_waited_for_and_one_millisecond_more_is_not(start):
    held, fired = timer_wait(start, 2000)
    assert fired is True and held.waited_ms >= 1700, held
    not_held, fired = timer_wait(start, 2001)
    assert fired is False and not_held.waited_ms < 1000, not_held


def test_the_timer_threshold_is_a_knob(start):
    short, fired = timer_wait(start, 600, wait_timer_ms=500)
    assert fired is False and short.waited_ms < 400, short
    longer, fired = timer_wait(start, 3000, wait_timer_ms=3000)
    assert fired is True and longer.waited_ms >= 2700, longer


# Agent(ignore_urls): the escape hatch works for WAIT and for the post-action settle (S1).


def click_poll(start, monkeypatch, **kwargs):
    stub(monkeypatch, [("CLICK", "Poll")])
    agent = start("events.html", max_steps=1, **kwargs)
    began = time.monotonic()
    agent.command("tick")
    return agent, (time.monotonic() - began) * 1000


def test_an_ignored_long_request_started_by_a_click_does_not_slow_the_tick(start, monkeypatch):
    agent, took = click_poll(start, monkeypatch, ignore_urls=["/hang"], action_settle_timeout_ms=3000)
    assert agent.browser.last_action_settle is None and took < 1500, took


def test_an_unignored_long_request_started_by_a_click_costs_the_settle_timeout(start, monkeypatch):
    agent, took = click_poll(start, monkeypatch, action_settle_timeout_ms=1200)
    assert agent.browser.last_action_settle.met is False and took >= 1200, took


def test_an_ignored_long_request_does_not_block_wait(start, monkeypatch):
    stub(monkeypatch, [("CLICK", "Poll"), ("WAIT", "wait")])
    agent = start("events.html", max_steps=2, ignore_urls=[re.compile("/ha+ng$")], action_settle_timeout_ms=0)
    agent.command("tick")
    agent.command("tick")
    entry = agent.state["history"][1]
    assert entry["kind"] == "wait" and entry["met"] is True and entry["waited_ms"] < 2500, entry


def test_a_plain_click_on_a_polling_page_does_not_wait_for_the_pages_own_timers(start, monkeypatch):
    stub(monkeypatch, [("CLICK", "Press me")])
    agent = start("poll.html", ignore_urls=["/api/50"], max_steps=1)
    began = time.monotonic()
    agent.command("tick")
    # Timers the page armed before the click are not caused by it.
    assert agent.browser.last_action_settle is None and (time.monotonic() - began) < 1.5


def slow_model(monkeypatch, delay):
    """The model call between the read and the action: seconds with a real model."""
    real = loop.choose

    def choose(*args, **kwargs):
        time.sleep(delay)
        return real(*args, **kwargs)

    monkeypatch.setattr(loop, "choose", choose)


def test_a_slow_model_between_the_read_and_a_click_does_not_make_the_click_wait_for_the_pages_polling(
    start, monkeypatch
):
    stub(monkeypatch, [("CLICK", "Press me")])
    slow_model(monkeypatch, 1.5)  # poll.html arms a 400 ms timer and a request all the time.
    agent = start("poll.html", ignore_urls=["/api/50"], max_steps=1)
    began = time.monotonic()
    agent.command("tick")
    assert agent.browser.last_action_settle is None
    assert (time.monotonic() - began) < 3.0  # The 1.5 s model call and a plain click, no settle on top.


def test_a_timer_the_click_arms_still_triggers_the_settle(start, monkeypatch):
    stub(monkeypatch, [("CLICK", "Arm")])
    slow_model(monkeypatch, 0.5)
    agent = start("timer_click.html", max_steps=1)
    agent.command("tick")
    assert agent.browser.last_action_settle is not None and agent.browser.last_action_settle.met is True
    assert agent.browser.evaluate("document.title") == "done"


def test_the_start_of_a_new_document_is_not_counted_as_a_recent_request(open_page):
    browser = open_page("blank.html")
    # Nothing ran: the quiet period is already over, whatever idle_ms is.
    result = browser.network_idle(idle_ms=60000, timeout_ms=1000)
    assert result.met is True, result


# DOM attribute noise and a document that stays "interactive" (S9).


ANIMATE = (
    "const bar = document.createElement('div'); document.body.append(bar); let n = 0;"
    " const timer = setInterval(() => { bar.style.width = (++n) + 'px'; if (n >= 100) clearInterval(timer); }, 20); 1"
)


def test_attribute_noise_keeps_the_dom_busy_unless_the_attribute_is_ignored(start):
    agent = start("blank.html")
    agent.browser.evaluate(ANIMATE)
    await_page(agent.browser, "document.body.lastChild && document.body.lastChild.style.width")
    busy = agent.wait_for("dom_quiet", quiet_ms=300, timeout_ms=700)
    ignored = agent.wait_for("dom_quiet", quiet_ms=300, timeout_ms=700, ignore_attributes=("style",))
    print(
        f"style animation: default met={busy.met} in {busy.waited_ms} ms; "
        f"style ignored met={ignored.met} in {ignored.waited_ms} ms"
    )
    assert busy.met is False and ignored.met is True and ignored.waited_ms < 500


def test_the_agent_level_ignored_attributes_apply_to_settled(start):
    agent = start("blank.html", wait_ignore_attributes=("style",))
    agent.browser.evaluate(ANIMATE)
    await_page(agent.browser, "document.body.lastChild && document.body.lastChild.style.width")
    assert agent.wait_for("settled", quiet_ms=200, timeout_ms=700).met is True
    # A child list change is not an attribute: it still counts.
    agent.browser.evaluate(
        "window.__added = 0; setInterval(() => { document.body.append(document.createElement('i')); "
        "window.__added++; }, 20); 1"
    )
    await_page(agent.browser, "window.__added >= 1")
    assert agent.wait_for("settled", quiet_ms=200, timeout_ms=500).met is False


def test_a_document_stuck_interactive_counts_as_loaded_after_the_grace_period(start):
    agent = start("blank.html", wait_interactive_grace_ms=600)
    agent.browser.evaluate("location.href = '/interactive.html'; 1")
    result = agent.wait_for("settled", quiet_ms=100, timeout_ms=8000)
    assert result.met is True and result.waited_ms < 6000, result
    assert agent.browser.evaluate("document.readyState") == "interactive"


def test_a_document_still_loading_is_not_settled(start):
    agent = start("blank.html", wait_interactive_grace_ms=60000)
    agent.browser.evaluate("location.href = '/interactive.html'; 1")
    result = agent.wait_for("settled", quiet_ms=100, timeout_ms=1500)
    assert result.met is False and "still loading" in result.reason, result


# A page-side error is raised, not waited out (S4), and element text_stable needs text (N7).


def test_text_stable_of_an_element_that_has_no_text_yet_is_not_stable(start):
    agent = start("stream.html")
    early = agent.wait_for("text_stable", quiet_ms=100, role="textbox", name="Answer", timeout_ms=600)
    assert early.met is False and "not started" in early.reason
    anywhere = agent.wait_for(
        "text_stable", quiet_ms=100, role="textbox", name="Answer", timeout_ms=600, require_text=False
    )
    assert anywhere.met is True
