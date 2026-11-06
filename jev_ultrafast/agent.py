"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import copy
import time
from pathlib import Path

from .browser import Browser, Refused, StalePage, content_fingerprint
from .keys import validate_keys, validate_urls
from .model import action_space, choose, field_context, field_text
from .questions import MAX_STEPS

TERMINAL = {"done", "blocked", "denied", "budget"}


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
    stuck_after unchanged non-wait steps in a row
    end the run as "blocked"; so do stuck_after refused presses in a row (stop_reason "refused"). A refused press is
    recorded in state["refused"] (never in history), shown to the model as a recent action with refused=true, and its
    element is left out of the next choice while the page is unchanged.
    before_action(action, page, text) runs before each execution attempt, with the final typed text, and receives
    copies, so changing them cannot change what executes. It can run more than once for one intended action if the
    page goes stale first, so keep it free of side effects or make it idempotent. Raise ActionDenied(reason) to
    veto: a veto always prevents execution and the run ends as "denied".
    keys: extra shortcut chords the caller declares, e.g. ("Control+K",); the fixed keys (Enter, Escape, Tab, ...) are
    always offered. urls: {name: absolute http(s) URL} pages the model may GOTO; it never writes a URL.
    navigation_timeout_ms bounds the wait for a new document after BACK, RELOAD or GOTO (the page is observed anyway).
    navigation_commit_ms is how long, after a key or click, to watch for the document committing a navigation (a form
    submitted with Enter, a link); if it does, the new document is awaited like after a GOTO. 0 turns this off. A press
    that starts no navigation does not wait for the window.
    Key presses and navigation are browser mutations: they are logged, never retried, and go through before_action
    with kind "key" (action["key"], action["focused"]), "back", "reload" or "goto" (action["url"]).
    """

    def __init__(
        self,
        url,
        goals,
        *,
        record_dir=None,
        screenshots=False,
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
        navigation_commit_ms=1000,
    ):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        self.max_steps = positive_int("max_steps", max_steps)
        self.max_model_calls = positive_int("max_model_calls", max_model_calls, optional=True) or self.max_steps * 2
        self.stuck_after = positive_int("stuck_after", stuck_after)
        positive_int("settle_ms", settle_ms, minimum=0)
        positive_int("autocomplete_settle_ms", autocomplete_settle_ms, minimum=0)
        positive_int("reveal_timeout_ms", reveal_timeout_ms, minimum=0)
        positive_int("navigation_timeout_ms", navigation_timeout_ms)
        positive_int("navigation_commit_ms", navigation_commit_ms, minimum=0)
        keys = validate_keys(keys)
        urls = validate_urls(urls)
        if before_action is not None and not callable(before_action):
            raise ValueError("before_action must be callable")
        self.before_action = before_action
        plan = [task]
        self.pending_text = None
        self.browser = Browser(
            url,
            settle_ms=settle_ms,
            autocomplete_settle_ms=autocomplete_settle_ms,
            reveal_timeout_ms=reveal_timeout_ms,
            keys=keys,
            urls=urls,
            navigation_timeout_ms=navigation_timeout_ms,
            navigation_commit_ms=navigation_commit_ms,
        )
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            stop_reason=None,
            denied=None,
            refused=[],
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                if state["status"] in TERMINAL:
                    return self.snapshot()
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
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
            state["decision"] = choose(
                page, state["goal"], state["history"], **({"refused": refused[-10:]} if refused else {})
            )
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if decision["operation"] == "HOVER":
                action = {**action, "kind": "hover"}
            if action["kind"] == "key":
                # Lets a policy refuse a key by what has focus, e.g. Enter on a focused Delete button.
                focus = page.get("focus") or {}
                keys = ("role", "label", "href", "tag", "type")
                action = {**action, "focused": {k: focus.get(k) for k in keys} if focus else None}
            if len(state["history"]) >= self.max_steps:
                return self.stop("budget", "max_steps")
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    if self.model_calls() >= self.max_model_calls:
                        return self.stop("budget", "max_model_calls")
                    text, helper = field_text(context)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            if self.before_action:
                try:
                    self.before_action(copy.deepcopy(action), copy.deepcopy(page), text)
                except ActionDenied as veto:
                    self.pending_text = None
                    state["denied"] = {
                        "reason": veto.reason,
                        "action": action["label"],
                        "kind": action["kind"],
                        "choice": selected,
                    }
                    return self.stop("denied")
            # Browser.act checks freshness immediately before input, including after text generation.
            try:
                state["browser"].act(action, page, text=text)
            except Refused as refusal:
                # Nothing was pressed (or sent): not history, but the model and stuck_after must see it.
                refused = state.setdefault("refused", [])
                del refused[:-49]
                refused.append(
                    {
                        "label": action["label"],
                        "kind": action["kind"],
                        "reason": str(refusal),
                        "fingerprint": page["fingerprint"],
                        "node": action.get("node"),
                        "choice": selected,
                        "after": len(state["history"]),
                        "pointer_moved": refusal.pointer_moved,
                    }
                )
                if sum(r["after"] == len(state["history"]) for r in refused) >= self.stuck_after:
                    return self.stop("blocked", "refused")
                raise
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
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
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
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
            state["status"] = (
                "blocked"
                if len(repeated) == self.stuck_after
                and all(h["content_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def model_calls(self):
        return len(self.state["decisions"]) + len(self.state["text_calls"])

    def stop(self, status, reason=None):
        state = self.state
        state["status"] = status
        state["stop_reason"] = reason
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        return self.snapshot()

    def run(self):
        while self.state["status"] not in TERMINAL:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
