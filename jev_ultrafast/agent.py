"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import copy
import functools
import re
import time
from pathlib import Path

from . import replay as recorded
from .browser import Browser, Refused, StalePage, content_fingerprint, fingerprint
from .evidence import DROPPED_PART, MODES, URL_MODES, Evidence, library_version, redact_url
from .keys import validate_keys, validate_urls
from .model import action_space, choose, field_context, field_text
from .questions import MAX_STEPS, NONE
from .steps import parse_goals, redaction, secret_forms, validate_values
from .trace import SCHEMA
from .waits import DEFAULT_VIEWPORT, MAX_WAITS, attribute_names, ignore_patterns, needs_settle, viewport_size

TERMINAL = {"done", "blocked", "denied", "budget", "failed", "diverged"}
LOCATED = {"click", "fill", "select", "hover"}  # Action kinds that target an element, so get a locator.
# Dropped from the copy of a page that the model and the state see when a secret value is declared: the marker and the
# element guards hold raw field values, and only the browser needs them (it keeps the raw page).
RAW_ONLY = {"marker", "guards"}
UNTOUCHED = {"screenshot"}  # The fingerprint is recomputed from the scrubbed page (see Agent.shown).


@functools.lru_cache(maxsize=32)
def secret_pattern(secrets):
    return re.compile("|".join(re.escape(text) for text, _name in secrets))


def scrub_text(text, secrets):
    """One pass, longest form first, so a marker that was just written is never scrubbed again."""
    names = dict(secrets)
    return secret_pattern(tuple(secrets)).sub(lambda found: redaction(names[found.group()]), text)


def scrub(value, secrets):
    """A copy of an observation with every secret text replaced by its marker."""
    if isinstance(value, str):
        return scrub_text(value, secrets) if secrets else value
    if isinstance(value, dict):
        return {
            k: v if k in UNTOUCHED else scrub(v, secrets) for k, v in value.items() if k not in RAW_ONLY
        }
    if isinstance(value, list | tuple):
        return [scrub(v, secrets) for v in value]
    return value


def history_entry_failed(unmet):
    return [{"description": d, "reason": r} for d, r in unmet]


class ActionDenied(Exception):
    """Raised by a before_action hook to veto an action. The action is never executed."""

    def __init__(self, reason="Action denied"):
        super().__init__(reason)
        self.reason = reason


def positive_int(name, value, *, optional=False, minimum=1):
    if optional and value is None:
        return value
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be {'a positive' if minimum else 'a non-negative'} integer")
    return value


class Agent:
    """Run a goal on a page.

    Budgets: max_steps bounds executed actions; max_model_calls bounds TypeSafe decisions plus text-helper calls
    (default 2 * max_steps). Reaching either ends the run with status "budget" and state["stop_reason"].
    settle_ms / autocomplete_settle_ms cap the post-action wait; reveal_timeout_ms bounds how long a click waits
    for a hover-revealed control to appear after the pointer moves onto it (it is never pressed if it does not);
    stuck_after unchanged steps in a row end the run as "blocked" (a WAIT that was met, or that timed out while the
    content changed, is not unchanged; a WAIT that timed out on an unchanged page is); so do stuck_after refused
    presses in a row (stop_reason "refused"). A refused press is recorded in state["refused"] (never in history),
    shown to the model as a recent action with refused=true, and its element is left out of the next choice while
    the page is unchanged.
    before_action(action, page, text) runs before each execution attempt, with the final typed text, and receives
    copies, so changing them cannot change what executes. It can run more than once for one intended action if the
    page goes stale first, so keep it free of side effects or make it idempotent. Raise ActionDenied(reason) to
    veto: a veto always prevents execution and the run ends as "denied".
    keys: extra shortcut chords the caller declares, e.g. ("Control+K",); the fixed keys (Enter, Escape, Tab, ...) are
    always offered. urls: {name: absolute http(s) URL} pages the model may GOTO; it never writes a URL.
    navigation_timeout_ms bounds the wait for a new document after BACK, RELOAD or GOTO (the page is observed anyway).
    Key presses and navigation are browser mutations: they are logged, never retried, and go through before_action
    with kind "key" (action["key"], action["focused"]), "back", "reload" or "goto" (action["url"]).
    WAIT waits (in code, without a model call) until the page is settled: the document is loaded, no fetch or
    XMLHttpRequest is in flight, no short timer is pending and the DOM has been quiet for wait_quiet_ms, at most
    wait_timeout_ms. It is recorded in history with waited_ms and met (false on timeout). After every other action,
    if requests or short timers are pending, the same wait runs bounded by action_settle_timeout_ms (0 turns it off).
    wait_for(kind, ...) runs one condition wait for the caller and returns its WaitResult; see README.
    ignore_urls: substrings or compiled regexes for long-lived fetch or XMLHttpRequest URLs (long polls, beacons)
    that no wait and no post-action trigger counts. wait_timer_ms (default 2000, 0 = off): a one-shot setTimeout of
    at most this long that is pending when a wait begins holds it until it fires; timers armed later never do.
    body_cap_ms: at most this long after a response's headers a fetch stops counting as in flight.
    wait_ignore_attributes: attribute names (for example ("style",)) whose changes are not DOM activity.
    wait_interactive_grace_ms: how long a document may stay "interactive" before it counts as loaded.
    A WAIT goes through before_action like any action.
    replay: the evidence_dir/trace.jsonl path (or the loaded events, jev_ultrafast.trace.load_trace) of an earlier run
    that ended done and verified, for the SAME goals, checks and value names (otherwise ValueError). The run then asks
    no model: for each step it re-resolves every recorded element by its locator, runs the matching action through
    the usual path (freshness, before_action, settle, history, evidence; source "replay") and moves on when the step's
    checks hold. When a step diverges (the locator no longer resolves to one element, the control is no longer
    offered, a press is refused or stale twice in a row, the recorded actions are spent while the checks do not
    hold or do not stay held, a goto URL, scroll distance, page or key focus differs from the recorded one) heal=True
    (default) hands the rest of that step to the normal model loop and resumes replay at the next step; heal=False
    ends the run as "diverged" with stop_reason "<cause> at step N". A before_action veto still ends the run as
    "denied". state["replay"] counts steps_replayed, steps_healed, actions_replayed and model_calls.
    replay_quiet_ms: replay only: the quiet window stable() waits before re-reading the checks when the page-side
    tracker shows nothing pending (default 150; None always keeps the full 500 ms, which also catches rollbacks the
    tracker cannot see: WebSocket, SSE, postMessage, setInterval, ignore_urls endpoints). A model run ignores it.
    goals: a string, or a list of strings and Step(goal, expect=(), values=None). Each item is one step; the model
    sees only the current step's goal, the goals already completed and the current step's check descriptions.
    Checks (jev_ultrafast.expect) are evaluated in code. A DONE choice on a step with checks is accepted only when
    all hold, each waiting at most expect_timeout_ms (default 5000); otherwise it is rejected (history kind
    "done_rejected", shown to the model as a recent action), and max_rejections rejections on one step (default 3)
    end the run as "failed" with stop_reason "expectations". After every executed action the checks run once without
    waiting, and a step whose checks all hold completes without a DONE decision (auto_complete=False turns that off).
    state["verified"] is True when every step had checks and all passed; require_expectations=True makes a step
    without checks a construction error. values: {name: str | Value(text, secret=False)} for typing, for all steps (a
    Step's own values override). The model chooses a value by name only and never sees its text; secret values are
    redacted as "<secret:name>" in history, state and the page the model reads, and before_action receives that marker
    as text, with the name as action["value_name"].
    evidence_dir: a new or empty directory; the run writes trace.jsonl (every event the agent saw, decided and did,
    with a replay-grade locator for each executed element: see jev_ultrafast.trace), screenshots/NNNN.jpg,
    console.jsonl, network.jsonl, network.har (no bodies) and report.html there. Everything but the screenshots
    passes through the secret scrub; screenshots are images of the page and are not redacted. screenshots: with
    evidence_dir, "actions" (default: the first page, the page after every executed action, and the last), "all"
    (also every other observation), "failure" (only the last page, when the run does not end done) or "none";
    without it, True keeps its old meaning (every observation carries a screenshot). locators: add the locator to
    every history entry (on by default with evidence_dir). Console and network need BU_CDP_URL or BU_CDP_WS (they
    are marked unavailable in run_start otherwise). evidence_urls: "redacted" (default) writes every URL (network,
    console, observe, locator href, report) as origin + path with each query value and the fragment replaced by
    <redacted>; "full" keeps them, for private use only. evidence_capture: False runs without the second websocket
    (no console or network files; trace, screenshots and locators remain), for CDP endpoints that allow one client.
    new_window: None (default) gives the owned tab a window of its own only in headless Chrome, where a background
    tab is throttled to about one frame per second after its first clicks; True or False force it. Headed Chrome
    has not been tested, and a new window there is a visible one.
    viewport: (width, height) of the owned tab in CSS pixels, default (1120, 780), at most 7680x4320. Responsive pages
    change layout with it, so set it to match the other drivers or the screens you care about; it is recorded in the
    run_start event of the evidence. A replay does not
    take the viewport from the recorded trace: the caller sets it (the trace records the one it ran with), so replay at
    the recorded size by passing the same value.
    """

    # What an Agent built without __init__ (a test double) means: one unchecked step, no values.
    steps, values, secrets, _raw = (), {}, (), None
    evidence, locators, evidence_urls = None, False, "redacted"
    expect_timeout_ms, max_rejections, auto_complete = 5000, 3, True
    viewport = DEFAULT_VIEWPORT
    replay = None

    def __init__(
        self,
        url,
        goals,
        *,
        record_dir=None,
        screenshots=None,
        evidence_dir=None,
        locators=None,
        evidence_urls="redacted",
        evidence_capture=True,
        max_steps=MAX_STEPS,
        max_model_calls=None,
        settle_ms=50,
        autocomplete_settle_ms=200,
        reveal_timeout_ms=500,
        stuck_after=3,
        before_action=None,
        keys=(),
        urls=None,
        navigation_timeout_ms=15000,
        wait_quiet_ms=500,
        wait_timeout_ms=30000,
        action_settle_timeout_ms=5000,
        new_window=None,
        ignore_urls=(),
        wait_timer_ms=2000,
        body_cap_ms=30000,
        wait_ignore_attributes=(),
        wait_interactive_grace_ms=2000,
        expect_timeout_ms=5000,
        max_rejections=3,
        auto_complete=True,
        require_expectations=False,
        values=None,
        viewport=DEFAULT_VIEWPORT,
        replay=None,
        heal=True,
        replay_quiet_ms=150,
    ):
        self.viewport = viewport = viewport_size(viewport)
        steps = parse_goals(goals)
        positive_int("expect_timeout_ms", expect_timeout_ms, minimum=0)
        positive_int("max_rejections", max_rejections)
        for name, flag in (("auto_complete", auto_complete), ("require_expectations", require_expectations)):
            if type(flag) is not bool:
                raise ValueError(f"{name} must be True or False")
        if require_expectations:
            for number, step in enumerate(steps, 1):
                if not step.expect:
                    raise ValueError(f"Step {number} has no expect checks (require_expectations=True)")
        values = validate_values(values)
        if type(heal) is not bool:
            raise ValueError("heal must be True or False")
        positive_int("replay_quiet_ms", replay_quiet_ms, optional=True, minimum=0)
        self.replay_quiet_ms = replay_quiet_ms
        self.replay = recorded.Replay(replay, steps, values, heal) if replay is not None else None
        secrets = {
            form: name
            for source in (values, *(s.values for s in steps))
            for name, v in source.items()
            if v.secret
            for form in secret_forms(v.text)
        }
        secrets = tuple(sorted(secrets.items(), key=lambda item: (-len(item[0]), item[0])))
        if secrets:
            texts = [step.goal for step in steps] + [e.description for step in steps for e in step.expect]
            if any(secret_pattern(secrets).search(text) for text in texts):
                raise ValueError("A goal or check description contains a declared secret value; describe it by name")
        self.max_steps = positive_int("max_steps", max_steps)
        self.max_model_calls = positive_int("max_model_calls", max_model_calls, optional=True) or self.max_steps * 2
        self.stuck_after = positive_int("stuck_after", stuck_after)
        positive_int("settle_ms", settle_ms, minimum=0)
        positive_int("autocomplete_settle_ms", autocomplete_settle_ms, minimum=0)
        positive_int("reveal_timeout_ms", reveal_timeout_ms, minimum=0)
        positive_int("navigation_timeout_ms", navigation_timeout_ms)
        positive_int("wait_quiet_ms", wait_quiet_ms, minimum=0)
        positive_int("wait_timeout_ms", wait_timeout_ms)
        positive_int("action_settle_timeout_ms", action_settle_timeout_ms, minimum=0)
        positive_int("wait_timer_ms", wait_timer_ms, minimum=0)
        positive_int("body_cap_ms", body_cap_ms)
        positive_int("wait_interactive_grace_ms", wait_interactive_grace_ms, minimum=0)
        if new_window is not None and type(new_window) is not bool:
            raise ValueError("new_window must be True, False or None")
        ignore_patterns(ignore_urls)
        attribute_names(wait_ignore_attributes)
        keys = validate_keys(keys)
        urls = validate_urls(urls)
        if before_action is not None and not callable(before_action):
            raise ValueError("before_action must be callable")
        self.before_action = before_action
        plan = [step.goal for step in steps]
        self.steps, self.values = steps, values
        self.expect_timeout_ms, self.max_rejections = expect_timeout_ms, max_rejections
        self.auto_complete = auto_complete
        self.secrets = secrets
        self.pending_text = None
        if isinstance(screenshots, str):
            if screenshots not in MODES or not evidence_dir:
                raise ValueError(f"screenshots={screenshots!r} needs evidence_dir and one of {', '.join(MODES)}")
            mode = screenshots
        elif screenshots is None or type(screenshots) is bool:
            mode = {None: "actions", True: "all", False: "none"}[screenshots]
        else:
            raise ValueError("screenshots must be True, False, None or one of " + ", ".join(MODES))
        if locators is not None and type(locators) is not bool:
            raise ValueError("locators must be True, False or None")
        self.locators = bool(evidence_dir) if locators is None else locators
        if evidence_urls not in URL_MODES:
            raise ValueError(f"evidence_urls must be one of {', '.join(URL_MODES)}")
        if type(evidence_capture) is not bool:
            raise ValueError("evidence_capture must be True or False")
        self.evidence_urls = evidence_urls
        self.evidence = (
            Evidence(evidence_dir, mode, clean=self.clean, urls=evidence_urls, capture=evidence_capture)
            if evidence_dir
            else None
        )
        screenshots = screenshots is True
        try:
            self.browser = Browser(
                url,
                settle_ms=settle_ms,
                autocomplete_settle_ms=autocomplete_settle_ms,
                reveal_timeout_ms=reveal_timeout_ms,
                keys=keys,
                urls=urls,
                navigation_timeout_ms=navigation_timeout_ms,
                wait_quiet_ms=wait_quiet_ms,
                wait_timeout_ms=wait_timeout_ms,
                action_settle_timeout_ms=action_settle_timeout_ms,
                new_window=new_window,
                ignore_urls=ignore_urls,
                wait_timer_ms=wait_timer_ms,
                body_cap_ms=body_cap_ms,
                wait_ignore_attributes=wait_ignore_attributes,
                wait_interactive_grace_ms=wait_interactive_grace_ms,
                viewport=viewport,
                **({"on_target": self.evidence.attach} if self.evidence else {}),
            )
        except Exception:
            if self.evidence:
                self.evidence.close()
            raise
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        self.config = dict(
            max_steps=max_steps, max_model_calls=self.max_model_calls, settle_ms=settle_ms,
            autocomplete_settle_ms=autocomplete_settle_ms, reveal_timeout_ms=reveal_timeout_ms,
            stuck_after=stuck_after, keys=list(keys), urls=dict(urls), navigation_timeout_ms=navigation_timeout_ms,
            wait_quiet_ms=wait_quiet_ms, wait_timeout_ms=wait_timeout_ms,
            action_settle_timeout_ms=action_settle_timeout_ms, new_window=new_window,
            ignore_urls=[getattr(u, "pattern", u) for u in ignore_urls], wait_timer_ms=wait_timer_ms,
            body_cap_ms=body_cap_ms, wait_ignore_attributes=list(wait_ignore_attributes),
            wait_interactive_grace_ms=wait_interactive_grace_ms, expect_timeout_ms=expect_timeout_ms,
            max_rejections=max_rejections, auto_complete=auto_complete, require_expectations=require_expectations,
            before_action=before_action is not None, locators=self.locators, evidence_urls=evidence_urls,
            evidence_capture=evidence_capture, viewport=list(viewport), replay=replay is not None, heal=heal,
            screenshots=self.evidence.mode if self.evidence else None,
        )
        try:
            self.trace_start(url)
            page = self.shown(self.browser.observe(screenshot=self.screenshots))
            self.trace_observe(page, important=True)
        except Exception:
            if self.evidence:
                self.evidence.close()
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal=self.clean("\n".join(plan)),
            steps=[
                dict(
                    goal=self.clean(step.goal),
                    status="active" if i == 0 else "pending",
                    checks=[
                        dict(description=self.clean(e.description), met=None, reason=None, waited_ms=None)
                        for e in step.expect
                    ],
                    started_ms=None,
                    ended_ms=None,
                    model_calls=0,
                    actions=0,
                    rejections=0,
                    verified=False,
                )
                for i, step in enumerate(steps)
            ],
            verified=False,
            page=page,
            decision=None,
            history=[],
            status="ready",
            stop_reason=None,
            denied=None,
            refused=[],
            waits=[],
            plan=[self.clean(goal) for goal in plan],
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
            **({"replay": self.replay.counts()} if self.replay else {}),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))
        self.trace("step", index=0, status="active")

    def snapshot(self):
        self.sync_step()
        if self.replay:
            self.state["replay"] = self.replay.counts(self.state, self.model_calls())
        if self.evidence and self.state["status"] in TERMINAL:
            self.end_evidence()
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    # Observation, steps and checks.

    def clean(self, text):
        """Text that is recorded or sent, with every declared secret replaced by its marker."""
        return scrub(text, self.secrets)

    def shown(self, raw):
        """The copy of an observation that the model and the state keep: every declared secret hidden, and the
        fingerprint computed from what is left, so it cannot be used to test a guess. The browser is always checked
        and driven with the raw observation (raw_page)."""
        if not self.secrets:
            return raw
        shown = scrub(raw, self.secrets)
        shown["fingerprint"] = fingerprint(shown)
        self._raw = (shown, raw)
        return shown

    def observe(self, after_action=False):
        page = self.shown(self.state["browser"].observe(screenshot=self.screenshots))
        self.trace_observe(page, important=after_action)
        return page

    # Evidence (see jev_ultrafast.evidence). Every method here is a no-op without evidence_dir.

    def trace(self, event_type, /, **fields):
        if self.evidence and not self.evidence.ended:
            self.evidence.event(event_type, **fields)

    def trace_start(self, url):
        if not self.evidence:
            return
        version = getattr(self.browser, "version", None)
        version = version if isinstance(version, dict) else {}
        names = {name: v.secret for name, v in self.values.items()}
        self.evidence.event(
            "run_start",
            trace_schema=SCHEMA,
            library_version=library_version(),
            url=url,
            browser={"product": version.get("product"), "user_agent": version.get("userAgent")},
            viewport=dict(zip(("width", "height"), self.viewport)),
            steps=[
                {"goal": step.goal, "checks": [e.description for e in step.expect], "values": {
                    name: v.secret for name, v in step.values.items()}}
                for step in self.steps
            ],
            values=names,
            config=self.config,
            replay=self.replay.describe() if self.replay else None,
            capture=self.evidence.capture,
        )

    def trace_observe(self, page, important=False):
        if not self.evidence:
            return
        focus = page.get("focus") or {}
        self.trace(
            "observe",
            url=page["url"],
            title=page.get("title"),
            fingerprint=page["fingerprint"],
            elements=len(page["actions"]),
            focus={k: focus.get(k) for k in ("role", "label", "tag", "type", "href")} if focus else None,
            screenshot=self.evidence.observed(self.browser, page, important),
        )

    def end_evidence(self):
        """Write run_end once, with the final screenshot and totals that match the state."""
        evidence, state = self.evidence, self.state
        if not evidence or evidence.ended:
            return
        usage = {"jev": {}, "text": {}}
        for kind, rows in (("jev", state["decisions"]), ("text", state["text_calls"])):
            for row in rows:
                for key, count in (row.get("usage") or {}).items():
                    if isinstance(count, int | float) and not isinstance(count, bool):
                        usage[kind][key] = usage[kind].get(key, 0) + count
        done = sum(r["status"] == "done" for r in state.get("steps", []))
        missing = sum(
            1 for h in state["history"] if self.locators and h["kind"] in LOCATED and h.get("locator") is None
        )
        evidence.event(
            "run_end",
            status=state["status"],
            stop_reason=state["stop_reason"] or ("closed" if state["status"] not in TERMINAL else None),
            verified=state["verified"],
            totals={
                "steps": len(state.get("steps", [])),
                "steps_done": done,
                "actions": self.executed(),
                "jev_calls": len(state["decisions"]),
                "text_calls": len(state["text_calls"]),
                "tokens": usage,
                "wall_ms": state["elapsed_ms"],
                "locator_missing": missing,
                "replay_actions": sum(h.get("source") == "replay" for h in state["history"]),
                "healed_steps": len(self.replay.causes) if self.replay else 0,
            },
            dropped=evidence.dropped(),
            dropped_part=DROPPED_PART,
            capture_lost=evidence.lost(),
            screenshot=evidence.final(self.browser, state["status"]),
        )
        evidence.ended = True
        evidence.write_streams()

    def locate(self, action):
        """(locator, error) for the element an action targets, read in the page just before it runs. The error is
        None, or why a replayable action has no locator (the trace says so instead of recording a bare null)."""
        if not self.locators or action["kind"] not in LOCATED:
            return None, None
        if type(action.get("node")) is not int:
            return None, "NoNode"
        try:
            found = self.browser.locator(action["node"], full_urls=self.evidence_urls == "full")
        except Exception as error:
            return None, type(error).__name__
        if found and found.get("error"):
            return None, found["error"]
        return (self.clean(found), None) if found else (None, "ElementGone")

    def locate_focus(self, page):
        """The locator of the element that has focus, for a key press (None when nothing has focus, locators are off
        or the element cannot be found again)."""
        node = (page.get("focus") or {}).get("node")
        if not self.locators or type(node) is not int:
            return None
        try:
            found = self.browser.locator(node, full_urls=self.evidence_urls == "full")
        except Exception:
            return None
        return self.clean(found) if found and not found.get("error") else None

    def raw_page(self):
        raw = self._raw
        return raw[1] if raw and raw[0] is self.state["page"] else self.state["page"]

    def now_ms(self):
        started = self.state.get("started_at")
        return round((time.perf_counter() - started) * 1000) if started is not None else 0

    def executed(self):
        """Executed actions: a rejected DONE is in the history (the model must see it) but is not an action."""
        return sum(h["kind"] != "done_rejected" for h in self.state["history"])

    def current_step(self):
        """(the Step, its state record), or (None, None) for an Agent without steps."""
        records = self.state.get("steps")
        index = self.state.get("plan_index", 0)
        if not records or index >= len(records):
            return None, None
        return self.steps[index], records[index]

    def goal(self):
        step, _record = self.current_step()
        return step.goal if step else self.state["goal"]

    def current_values(self):
        step, _record = self.current_step()
        return {**self.values, **(step.values if step else {})}

    def sync_step(self):
        """Counts of the active step: model calls and executed actions since it started."""
        _step, record = self.current_step()
        if record and record["status"] == "active":
            marks = self.__dict__.setdefault("_marks", {}).get(self.state["plan_index"], (0, 0))
            record["model_calls"] = self.model_calls() - marks[0]
            record["actions"] = self.executed() - marks[1]

    def activate(self, record):
        record.update(status="active", started_ms=self.now_ms() if self.state.get("started_at") else None)
        self.trace("step", index=self.state["steps"].index(record), status="active")
        marks = self.__dict__.setdefault("_marks", {})
        marks[self.state["steps"].index(record)] = (self.model_calls(), self.executed())

    def end_step(self, status, verified=False):
        _step, record = self.current_step()
        if record and record["status"] == "active":
            self.sync_step()
            record.update(status=status, ended_ms=self.now_ms(), verified=verified)
            index = self.state["steps"].index(record)
            if self.replay and index in self.replay.causes and self.replay.healing == index:
                self.trace(
                    "heal", step=index, cause=self.replay.causes[index], healed=True, outcome=status,
                    model_calls=record["model_calls"],
                )
            self.trace("step", index=index, status=status, verified=verified)

    def finish_run(self):
        state = self.state
        state["status"], state["stop_reason"] = "done", None
        state["plan_index"] = len(state["steps"]) if state.get("steps") else 1
        state["verified"] = bool(state.get("steps")) and all(r["verified"] for r in state["steps"])
        state["elapsed_ms"] = self.now_ms()

    def complete_step(self):
        """The current step holds: advance, or end the run when it was the last."""
        state = self.state
        verified = bool(self.current_step()[0].expect)
        self.end_step("done", verified)
        if state["plan_index"] + 1 >= len(state["steps"]):
            return self.finish_run()
        state["plan_index"] += 1
        self.activate(state["steps"][state["plan_index"]])
        state["status"] = "ready"
        state["elapsed_ms"] = self.now_ms()

    def run_checks(self, once, *, everything=False, again=False):
        """Evaluate the current step's checks in code and record each result. once=True is the cheap pass: no waiting,
        and it stops at the first unmet check (the later ones show as unread) unless everything=True. Otherwise every
        check runs, each waiting at most its timeout. again=True is the read after the settle: waited_ms adds up.
        Returns the unmet (description, reason) pairs."""
        step, record = self.current_step()
        unmet, stopped = [], False
        for expectation, entry in zip(step.expect, record["checks"], strict=True):
            if stopped:
                entry.update(met=None, reason=None, waited_ms=None)
                continue
            timeout = 0 if once else expectation.timeout_for(self.expect_timeout_ms)
            result = expectation.evaluate(self.state["browser"], timeout)
            reason = self.clean(result.reason)
            waited = result.waited_ms + ((entry["waited_ms"] or 0) if again else 0)
            entry.update(met=result.met, reason=reason, waited_ms=waited)
            self.trace(
                "check", step=self.state["plan_index"], description=entry["description"], met=result.met,
                reason=reason, waited_ms=waited,
            )
            if not result.met:
                unmet.append((self.clean(expectation.description), reason))
                stopped = once and not everything
        return unmet

    def stable(self):
        """The checks held once. A page that applies a press optimistically and takes it back (a server that refused)
        holds them for an instant, so wait for the page to settle and read every check again."""
        if self.expect_timeout_ms:
            browser = self.state["browser"]
            quiet = {"quiet_ms": self.replay_quiet_ms} if self.quiet_replay() else {}
            browser.settled(timeout_ms=self.expect_timeout_ms, **quiet)
        return self.run_checks(True, everything=True, again=True)

    def quiet_replay(self):
        """Replay only: the fixed quiet window of stable() exists to catch an optimistic UI that a server takes back,
        which needs a counted request or a short timer. With the post-action settle on (it waited for any that the
        action started), none pending or started since, and no check that could change the page by being read, the
        shorter post-action window is enough. The double read stays."""
        step, _record = self.current_step()
        browser = self.state["browser"]
        if self.replay_quiet_ms is None or not self.replaying() or not step or not all(e.cheap for e in step.expect):
            return False
        if not browser.action_settle_timeout_ms:
            return False  # The caller turned the post-action wait off: this window is the only guard left.
        try:
            activity = browser.activity()
        except Exception:
            return False
        # None: the page is navigating or too busy to answer. That is exactly when it cannot vouch for itself.
        return activity is not None and not needs_settle(activity)

    def reject(self, unmet):
        """Record a DONE that did not hold (also a step that completed by itself but did not stay complete). Returns
        True when the rejections ended the run."""
        state = self.state
        _step, record = self.current_step()
        record["rejections"] += 1
        state["history"].append(
            {
                "step": self.executed() + 1,
                "action": "DONE rejected: " + "; ".join(f"{description} not met" for description, _ in unmet),
                "kind": "done_rejected",
                "choice": "DONE",
                "text": None,
                "failed": [{"description": d, "reason": r} for d, r in unmet],
                "page_changed": False,
                "content_changed": None,
                "url": state["page"]["url"],
                "elapsed_ms": self.now_ms(),
            }
        )
        self.trace("done_rejected", step=self.state["plan_index"], failed=history_entry_failed(unmet))
        if record["rejections"] >= self.max_rejections:
            self.end_step("failed")
            self.stop("failed", "expectations")
            return True
        return False

    def decide_done(self):
        """A DONE choice. Without checks it is accepted as the model's word; with checks it is accepted only when they
        hold, and again after the page has settled, and otherwise rejected with the failed descriptions in the history
        for the model to read."""
        state = self.state
        step, _record = self.current_step()
        if step is None:
            state["status"], state["plan_index"] = "done", 1
            state["elapsed_ms"] = self.now_ms()
            return self.snapshot()
        unmet = self.run_checks(once=False) if step.expect else []
        if step.expect and not unmet:
            unmet = self.stable()
        if not unmet:
            self.complete_step()
            return self.snapshot()
        if not self.reject(unmet):
            state["status"] = "ready"
            state["elapsed_ms"] = self.now_ms()
        return self.snapshot()

    WAITS = ("dom_quiet", "network_idle", "settled", "element", "text", "text_stable")

    def wait_for(self, kind, *args, **kwargs):
        """Run one condition wait in code, with no model call: dom_quiet, network_idle, settled, element, text or
        text_stable (arguments as on Browser, each with timeout_ms). Returns a WaitResult(met, waited_ms, reason)
        and never raises on timeout. The result is also appended to state["waits"] (the last 100)."""
        if kind not in self.WAITS:
            raise ValueError(f"Unknown wait {kind!r}; use one of {', '.join(self.WAITS)}")
        result = getattr(self.browser, kind)(*args, **kwargs)
        self.browser.mark_activity()
        waits = self.state.setdefault("waits", [])
        entry = {"kind": kind, **result.as_dict()}
        waits.append({**entry, "reason": self.clean(entry.get("reason"))} if "reason" in entry else entry)
        self.trace(
            "wait", kind=kind, met=entry.get("met"), waited_ms=entry.get("waited_ms"), reason=entry.get("reason")
        )
        del waits[:-MAX_WAITS]
        return result

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            if self.replaying():
                try:
                    return self.replay_tick()
                except StalePage:
                    # The observation itself went stale (a navigation that is slow to settle): look again.
                    state["decision"], state["status"] = None, "ready"
                    state["page"] = self.observe()
                    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                    return self.snapshot()
            try:
                self.command("predict", {})
                if state["status"] in TERMINAL:
                    return self.snapshot()
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = self.observe()
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(self.raw_page()):
                state["page"] = self.observe()
            state["decision"] = None
            if state["status"] in TERMINAL:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if self.model_calls() >= self.max_model_calls:
                return self.stop("budget", "max_model_calls")
            page = state["page"]
            refused = state.get("refused", [])
            # Drops by node: one refusal hides both the fill and the "Open <label>" click of the same element.
            # An action without a node (BACK) is hidden by its id.
            here = [r for r in refused if r["fingerprint"] == page["fingerprint"]]
            skip = {r["node"] for r in here if r["node"] is not None}
            skip_ids = {r.get("choice") for r in here if r["node"] is None}
            if here:
                page = {
                    **page,
                    "actions": [
                        a for a in page["actions"] if a.get("node") not in skip and a["id"] not in skip_ids
                    ],
                }
            step, record = self.current_step()
            if record and record["started_ms"] is None:
                record["started_ms"] = self.now_ms()
            extra = {"refused": refused[-10:]} if refused else {}
            if step and record["status"] == "active":
                done = [self.clean(r["goal"]) for r in state["steps"] if r["status"] == "done"]
                extra |= {"completed": done} if done else {}
                extra |= {"checks": [self.clean(e.description) for e in step.expect]} if step.expect else {}
            values = self.current_values()
            extra |= {"values": list(values)} if values else {}
            state["decision"] = choose(page, self.clean(self.goal()), state["history"], **extra)
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
            if self.evidence:
                decided = state["decision"]
                labels = {a["id"]: a["label"] for a in page["actions"]}
                ranked = sorted(decided["probabilities"].items(), key=lambda item: -item[1])[:5]
                self.trace(
                    "decision",
                    operation=decided["operation"],
                    target=decided["target"],
                    choice=decided["choice"],
                    value=decided.get("value"),
                    top=[{"id": i, "label": labels.get(i), "p": round(p, 4)} for i, p in ranked],
                    confidence=decided["confidence"],
                    latency_ms=decided["latency_ms"],
                    usage=decided["usage"],
                    model=decided.get("model"),
                )
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            raw = self.raw_page()
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(raw):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                if selected == "DONE":
                    return self.decide_done()
                self.end_step("blocked")
                state["status"] = "blocked"
                state["plan_index"] = 0 if not state.get("steps") else state["plan_index"]
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()

            def shape(a):
                if decision["operation"] == "HOVER":
                    a = {**a, "kind": "hover"}
                if a["kind"] == "key":
                    # Lets a policy refuse a key by what has focus, e.g. Enter on a focused Delete button.
                    focus = page.get("focus") or {}
                    keys = ("role", "label", "href", "tag", "type")
                    a = {**a, "focused": {k: focus.get(k) for k in keys} if focus else None}
                return a

            shown = shape(next(a for a in page["actions"] if a["id"] == selected))
            # The executor gets the raw observation's action; everything recorded or shown gets the scrubbed one.
            action = shown if raw is page else shape(next(a for a in raw["actions"] if a["id"] == selected))
            if self.executed() >= self.max_steps:
                return self.stop("budget", "max_steps")
            text, helper, source, value_name, declared = None, None, None, None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(raw):
                    raise StalePage("Page changed before text generation. Choose again.")
                if decision.get("value") == NONE:
                    # No declared value fits and there is no text helper: type nothing, and leave this field out of
                    # the next choice (a skip: it does not count toward stuck_after).
                    return self.skip_field(shown, page, selected)
                declared = self.current_values().get(decision.get("value"))
                if decision.get("text") is not None:
                    # A replayed generated text: exactly what the recorded run typed, with no text-model call.
                    text, source = decision["text"], "generated"
                elif declared:
                    # A caller-provided value: no text-model call, and a secret stays out of everything recorded.
                    text, value_name = declared.text, decision["value"]
                    source = f"value:{value_name}"
                else:
                    source = "generated"
                    context = field_context(self.clean(self.goal()), shown, page, state["history"])
                    if self.pending_text and self.pending_text[0] == context:
                        _, text, helper = self.pending_text
                    else:
                        if self.model_calls() >= self.max_model_calls:
                            return self.stop("budget", "max_model_calls")
                        text, helper = field_text(context)
                        self.pending_text = (context, text, helper)
                        state["text_calls"].append({**helper, "field": shown["label"], "value": text})
            recorded = redaction(value_name) if declared and declared.secret else text
            hooked = {**shown, "value_name": value_name} if action["kind"] == "fill" else shown
            if self.before_action:
                try:
                    self.before_action(copy.deepcopy(hooked), copy.deepcopy(page), recorded)
                except ActionDenied as veto:
                    self.pending_text = None
                    state["denied"] = {
                        "reason": veto.reason,
                        "action": shown["label"],
                        "kind": shown["kind"],
                        "choice": selected,
                    }
                    self.trace("denied", **state["denied"])
                    self.end_step("blocked")
                    return self.stop("denied")
            locator, locator_error = self.locate(action)
            focus_locator = self.locate_focus(page) if shown["kind"] == "key" else None
            # Browser.act checks freshness immediately before input, including after text generation.
            try:
                outcome = state["browser"].act(action, raw, text=text)
            except Refused as refusal:
                # Nothing was pressed (or sent): not history, but the model and stuck_after must see it.
                if self.refuse(shown, page, selected, str(refusal), refusal.pointer_moved):
                    return self.stop("blocked", "refused")
                raise
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            waited = outcome.get("wait") if isinstance(outcome, dict) else None
            state["history"].append(
                {
                    "step": self.executed() + 1,
                    "action": shown["label"],
                    "kind": shown["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": recorded,
                    **({"text_source": source, "value_name": value_name} if source else {}),
                    "source": decision.get("source", "model"),
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "content_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                    **({"waited_ms": waited["waited_ms"], "met": waited["met"]} if waited else {}),
                    **({"locator": locator} if self.locators else {}),
                    **({"locator_error": locator_error} if locator_error else {}),
                }
            )
            if self.evidence:
                self.trace(
                    "action",
                    index=len(state["history"]),
                    step=state["plan_index"],
                    source=decision.get("source", "model"),
                    name=shown.get("name"),
                    option=shown.get("value") if shown["kind"] == "select" else None,
                    delta=shown.get("delta"),
                    kind=shown["kind"],
                    label=shown["label"],
                    choice=selected,
                    operation=decision["operation"],
                    locator=locator,
                    locator_error=locator_error,
                    text_source=source,
                    text=recorded,
                    value_name=value_name,
                    key=shown.get("key"),
                    focus=shown.get("focused") if shown["kind"] == "key" else None,
                    **({"focus_locator": focus_locator} if shown["kind"] == "key" else {}),
                    page_url=redact_url(page["url"]),
                    url=shown.get("url"),
                    result=outcome if isinstance(outcome, dict) else None,
                )
            state["page"] = self.observe(after_action=True)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                # Focus-free, for the stuck rule: pressing Tab over two inputs changes nothing on the page.
                content_changed=content_fingerprint(state["page"]) != content_fingerprint(page),
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-self.stuck_after :]
            # A replayed action is not a loop risk: the recorded run passed this rule, the plan is finite, and a page
            # that stopped reacting is a divergence for replay_tick to heal, not a reason to stop.
            state["status"] = (
                "blocked"
                if decision.get("source") != "replay"
                and len(repeated) == self.stuck_after
                and all(
                    h["content_changed"] is False and (h["kind"] != "wait" or h.get("met") is False)
                    for h in repeated
                )
                else "ready"
            )
            step, record = self.current_step()
            watching = (
                self.auto_complete
                and step
                and step.expect
                and record["status"] == "active"
                and all(e.cheap for e in step.expect)
            )
            if watching and not self.run_checks(True):
                # Every check holds: no DONE decision (and no model call) is needed, once it holds again when settled.
                unmet = self.stable()
                if not unmet:
                    self.complete_step()
                elif decision.get("source") == "replay":
                    # Held once and not twice (an optimistic rollback): for the model loop to look at, not a reject.
                    self.diverge(state["plan_index"], "checks_unstable")
                elif not self.reject(unmet) and state["status"] == "blocked":
                    self.end_step("blocked")
            elif state["status"] == "blocked":
                self.end_step("blocked")
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    # Replay (see jev_ultrafast.replay). A replayed action is an ordinary decision that costs no model call.

    def replaying(self):
        run = self.replay
        index = self.state.get("plan_index", 0)
        return bool(run) and index < len(self.steps) and run.healing != index

    def replay_decision(self, planned):
        """(decision, None) for the next recorded action on the page as it is now, or (None, why it cannot run)."""
        actions = self.state["page"]["actions"]
        if planned.kind in recorded.ELEMENT:
            if not planned.locator:
                return None, "locator_missing"
            if cause := recorded.page_cause(planned, self.state["page"]["url"]):
                return None, cause
            try:
                found = self.browser.resolve(planned.locator, node=True)
            except Exception:
                return None, "resolve_failed"
            cause = recorded.unresolved(found)
            action, cause = (None, cause) if cause else recorded.element_action(planned, actions, found["node"])
        else:
            page_ = self.state["page"]
            action, cause = recorded.control_action(
                planned, actions, page_.get("focus"), self.focus_node(planned), page_["url"]
            )
        if cause:
            return None, cause
        decision = {
            "choice": action["id"], "operation": planned.operation, "target": None, "confidence": 1.0,
            "probabilities": {action["id"]: 1.0}, "latency_ms": 0, "usage": {}, "source": "replay",
        }
        if planned.kind == "fill":
            extra, cause = recorded.fill_text(planned, self.current_values())
            if cause:
                return None, cause
            decision |= extra
        return decision, None

    def focus_node(self, planned):
        """The node the locator of a recorded key's focused element resolves to now, or None (like a click's locator:
        unique, verified and observed, else nothing)."""
        if planned.kind != "key" or not planned.focus_locator:
            return None
        try:
            found = self.browser.resolve(planned.focus_locator, node=True)
        except Exception:
            return None
        return None if recorded.unresolved(found) else found["node"]

    def replay_tick(self):
        state, run = self.state, self.replay
        if state["started_at"] is None:
            state["started_at"] = time.perf_counter()
        if state["status"] in TERMINAL:
            raise ValueError("This run has stopped. Start a fresh demo.")
        if not self.browser.fresh(self.raw_page()):
            state["page"] = self.observe()
        state["decision"] = None
        step, record = self.current_step()
        if record["started_ms"] is None:
            record["started_ms"] = self.now_ms()
        index = state["plan_index"]
        if run.cursor[0] != index:
            run.cursor, run.failures = [index, 0], 0
        queue = run.plan[index]
        if run.cursor[1] >= len(queue):
            unmet = self.run_checks(once=False) if step.expect else []
            if step.expect and not unmet:
                unmet = self.stable()
            if unmet:
                return self.diverge(index, "checks_unmet")
            self.complete_step()
            return self.snapshot()
        decision, cause = self.replay_decision(queue[run.cursor[1]])
        if cause:
            return self.diverge(index, cause)
        state["decision"], state["status"] = decision, "predicted"
        executed = len(state["history"])
        try:
            self.command("act", {"fingerprint": state["page"]["fingerprint"]})
        except StalePage as error:
            state["decision"], state["status"] = None, "ready"
            if len(state["history"]) > executed:
                # It ran, and only the observation after it went stale. Decide by what happened, never by the
                # exception: the action is done, the cursor moves on and the next tick looks at the page again.
                run.failures = 0
                run.cursor[1] += 1
                return self.snapshot()
            # Nothing was executed (a refusal or a stale page is raised before input): look again, once.
            run.failures += 1
            if run.failures >= 2:
                return self.diverge(index, ("refused" if isinstance(error, Refused) else "stale") + "_twice")
            state["page"] = self.observe()
            return self.snapshot()
        run.failures = 0
        if state["status"] not in TERMINAL:
            run.cursor[1] += 1
        return self.snapshot()

    def diverge(self, index, cause):
        """The recorded run no longer fits this page at step `index`: heal it with the model, or stop."""
        state, run = self.state, self.replay
        if self.replay.heal:
            run.healing, run.causes[index] = index, cause
            state["decision"], state["status"] = None, "ready"
            return self.snapshot()
        self.trace("heal", step=index, cause=cause, healed=False, outcome="diverged", model_calls=0)
        return self.stop("diverged", f"{cause} at step {index + 1}")

    def refuse(self, shown, page, selected, reason, pointer_moved=False, skipped=False):
        """Record an action that was not executed and tell the model about it. True when stuck_after refusals in a row
        (with nothing executed between) say the run should stop. A skipped field (NONE) is hidden like a refusal but
        does not count: it cannot repeat on the same page and max_model_calls bounds the loop."""
        state = self.state
        refused = state.setdefault("refused", [])
        del refused[:-49]
        refused.append(
            {
                "label": shown["label"],
                "kind": shown["kind"],
                "reason": reason,
                "fingerprint": page["fingerprint"],
                "node": shown.get("node"),
                "choice": selected,
                "after": len(state["history"]),
                "pointer_moved": pointer_moved,
                "skipped": skipped,
            }
        )
        self.trace(
            "refused", label=shown["label"], kind=shown["kind"], reason=reason, skipped=skipped,
            pointer_moved=pointer_moved,
        )
        return sum(r["after"] == len(state["history"]) and not r["skipped"] for r in refused) >= self.stuck_after

    def skip_field(self, shown, page, selected):
        self.refuse(shown, page, selected, "no declared value fits this field; nothing typed", skipped=True)
        self.state["status"] = "ready"
        self.state["elapsed_ms"] = self.now_ms()
        return self.snapshot()

    def model_calls(self):
        return len(self.state["decisions"]) + len(self.state["text_calls"])

    def stop(self, status, reason=None):
        state = self.state
        state["status"] = status
        state["stop_reason"] = reason
        if status != "failed":
            self.end_step("blocked")
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        return self.snapshot()

    def run(self):
        while self.state["status"] not in TERMINAL:
            yield self.command("tick")

    def close(self):
        try:
            if self.evidence:
                try:
                    self.end_evidence()
                finally:
                    self.evidence.close()
        finally:
            self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
