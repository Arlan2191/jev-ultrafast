"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import contextlib
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import _IPCResponseTimeout, cdp

from .keys import key_events, offered_keys
from .waits import (
    DEFAULT_VIEWPORT,
    WaitResult,
    attribute_names,
    element_state,
    ignore_patterns,
    needs_settle,
    non_negative,
    pattern,
    positive,
    viewport_size,
)

# Accessible role, name and visibility: one implementation for the snapshot and the condition waits.
A11Y = Path(__file__).with_name("a11y.js").read_text()


def script(name, **placeholders):
    text = Path(__file__).with_name(name).read_text().replace("__A11Y__", A11Y)
    for key, value in placeholders.items():
        text = text.replace(key, str(value))
    return text


# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = script("snapshot.js")
# Page-side request/timer/DOM tracker and condition checks. Idempotent: evaluating it again returns the installed one.
# The default is only a template; each Browser builds its own with its body_cap_ms.
TRACKER = script("waits.js", __BODY_CAP_MS__=30000)
# One page-side wait call is at most this long, so it stays inside browser-harness's IPC response timeout.
WAIT_SLICE_MS = 2000
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class ReadTimeout(StalePage, TimeoutError):
    """A read-only CDP call got no answer in time. The page is treated as stale: observe again."""


class MutationTimeout(RuntimeError):
    """A CDP call that changes the page (input, navigation, key) got no answer in time. It may or may not have been
    applied, so it is never retried. `method` is the CDP method that timed out. Browser raises it; the Agent ends the
    run as "failed" with stop_reason "mutation_timeout: <method>"."""

    def __init__(self, message, method=None):
        super().__init__(message)
        self.method = method


# CDP methods that change the page. Everything else the library sends is a read. Classified by method, not by
# params: mouseMoved and mouseWheel are kept as mutations although a move or a scroll is harmless to repeat, because
# a hover handler is a policy-gated action here and the whole Input.dispatchMouseEvent family is then never retried.
# Target.createTarget is one too: a timeout may have opened a tab we hold no id for, so it is not retried (the tab may
# be orphaned; Browser cannot close what it never got an id for).
MUTATING = {
    "Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText", "Page.navigate", "Page.reload",
    "Page.navigateToHistoryEntry", "Target.createTarget",
}
NAVIGATING = {"Page.navigate", "Page.reload", "Page.navigateToHistoryEntry"}
DEFAULT_CDP_TIMEOUT_MS = 15000
MAX_READ_TIMEOUTS = 2  # Consecutive ReadTimeouts one observe() tolerates.


def cdp_call(method, session, timeout_s, mutation=None, **params):
    """The one way the library sends a CDP call: with a response timeout of its own (browser-harness defaults to 5 s).
    A timeout raises MutationTimeout for a call that changes the page, ReadTimeout for a read. `mutation` overrides
    the method's own class (an evaluate that sets a value is a mutation)."""
    mutating = method in MUTATING if mutation is None else mutation
    try:
        return cdp(method, session_id=session, _response_timeout=timeout_s, **params)
    except _IPCResponseTimeout as error:
        if mutating:
            raise MutationTimeout(
                f"{method} timed out after {timeout_s:g}s waiting for the daemon; it may have been applied, so it "
                "was not retried",
                method,
            ) from error
        raise ReadTimeout(f"{method} timed out after {timeout_s:g}s waiting for the daemon") from error


class Refused(StalePage):
    """The pointer already moved onto the target, then the press was refused. Nothing was pressed."""

    pointer_moved = True


class Unavailable(Refused):
    """The action cannot run (BACK with no previous page). Nothing was sent and the pointer did not move."""

    pointer_moved = False


NAVIGATION = {"back", "reload", "goto"}


def control_actions(info, keys, urls):
    """Target-less and caller-declared controls. Keys and URLs come from the caller, never from the model."""
    extra = [{"id": f"key:{k}", "kind": "key", "key": k, "name": k, "label": f"Press {k}"} for k in keys]
    if info.get("can_go_back"):
        extra.append({"id": "back", "kind": "back", "label": "Go back to the previous page"})
    extra.append({"id": "reload", "kind": "reload", "label": "Reload the current page"})
    extra += [
        {"id": f"goto:{name}", "kind": "goto", "name": name, "url": url, "label": f"Open {name}"}
        for name, url in urls.items()
    ]
    return extra


class Browser:
    wait_quiet_ms = 500
    wait_timeout_ms = 30000
    action_settle_timeout_ms = 5000
    action_settle_quiet_ms = 150
    last_action_settle = None
    wait_timer_ms = 2000
    wait_interactive_grace_ms = 2000
    navigation_timeout_ms = 15000
    cdp_timeout_ms = DEFAULT_CDP_TIMEOUT_MS
    activity_seen = None
    action_mark = None
    ignore = []
    wait_ignore_attributes = ()
    tracker = TRACKER
    viewport = DEFAULT_VIEWPORT

    def __init__(
        self,
        url,
        *,
        settle_ms=50,
        autocomplete_settle_ms=200,
        reveal_timeout_ms=500,
        keys=(),
        urls=None,
        navigation_timeout_ms=15000,
        wait_quiet_ms=500,
        wait_timeout_ms=30000,
        action_settle_timeout_ms=5000,
        action_settle_quiet_ms=150,
        new_window=None,
        ignore_urls=(),
        wait_timer_ms=2000,
        body_cap_ms=30000,
        wait_ignore_attributes=(),
        wait_interactive_grace_ms=2000,
        on_target=None,
        viewport=DEFAULT_VIEWPORT,
        cdp_timeout_ms=DEFAULT_CDP_TIMEOUT_MS,
    ):
        self.viewport = viewport_size(viewport)
        self.cdp_timeout_ms = positive("cdp_timeout_ms", cdp_timeout_ms)
        self.wait_quiet_ms = non_negative("wait_quiet_ms", wait_quiet_ms)
        self.wait_timeout_ms = positive("wait_timeout_ms", wait_timeout_ms)
        self.action_settle_timeout_ms = non_negative("action_settle_timeout_ms", action_settle_timeout_ms)
        self.action_settle_quiet_ms = non_negative("action_settle_quiet_ms", action_settle_quiet_ms)
        self.wait_timer_ms = non_negative("wait_timer_ms", wait_timer_ms)
        self.wait_interactive_grace_ms = non_negative("wait_interactive_grace_ms", wait_interactive_grace_ms)
        self.ignore = ignore_patterns(ignore_urls)
        self.wait_ignore_attributes = attribute_names(wait_ignore_attributes)
        self.tracker = script("waits.js", __BODY_CAP_MS__=positive("body_cap_ms", body_cap_ms))
        self.last_action_settle = None
        self.activity_seen = None
        self.action_mark = None
        self.keys = offered_keys(keys)
        self.urls = dict(urls or {})
        self.navigation_timeout_ms = navigation_timeout_ms
        self.settle_ms = settle_ms
        self.reveal_timeout_ms = reveal_timeout_ms
        self.autocomplete_settle_ms = autocomplete_settle_ms
        ensure_daemon()
        # Read only when something needs it: the window choice, or run evidence (which records the browser).
        self.version = self.call("Browser.getVersion", session=None) if new_window is None or on_target else {}
        # In headless Chrome an owned tab in the existing window is a background tab: after a few clicks Chrome
        # throttles its frames to about 1 Hz and every pointer move then waits ~1 s for the next frame. A window of
        # its own stays at full rate. Headed Chrome was not measured and a new window is a visible one, so by default
        # (new_window=None) only a headless browser (HeadlessChrome in its product or user agent) gets it; True or
        # False force it.
        if new_window is None:
            new_window = "HeadlessChrome" in f"{self.version.get('product', '')} {self.version.get('userAgent', '')}"
        self.new_window = bool(new_window)
        self.target = None
        try:
            self.target = self.call(
                "Target.createTarget", session=None, url="about:blank", background=True, newWindow=self.new_window
            )["targetId"]
            self.session = self.call("Target.attachToTarget", session=None, targetId=self.target, flatten=True)[
                "sessionId"
            ]
            if on_target:
                on_target(self.target)  # Before the first navigation, so the first load is also seen.
            width, height = self.viewport
            self.call(
                "Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False
            )
            # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            # The tracker must exist before the page's own scripts run, so it sees their requests and timers.
            self.call("Page.addScriptToEvaluateOnNewDocument", source=self.tracker)
            self.call("Page.navigate", url=url)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.evaluate("document.readyState") == "complete":
                    break
                time.sleep(0.02)
            self.mark_activity()
        except BaseException:
            with contextlib.suppress(Exception):
                self.close()  # Do not leak the owned tab when setup fails.
            raise

    def call(self, method, session=False, **params):
        """One CDP call on this browser's session (session=None: the browser itself) with the per-call timeout."""
        timeout_s = params.pop("_response_timeout", None)
        if timeout_s is None:
            timeout_s = (self.navigation_timeout_ms if method in NAVIGATING else self.cdp_timeout_ms) / 1000
        return cdp_call(method, self.session if session is False else session, timeout_s, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def screenshot(self):
        return self.call("Page.captureScreenshot", format="jpeg", quality=72)["data"]

    def locator(self, node, full_urls=False):
        """Where the observed element is, described to be found again on a fresh load (see a11y.js locator). A link's
        href has its query values and fragment replaced by <redacted> unless full_urls. None when the node is gone;
        {"error": "NotInDocument"} for an element in a shadow root or a frame, which could never be found again."""
        return self.evaluate(
            "(() => { const {locator}=" + A11Y + f"; const e=window.__jevFast?.nodes.get({int(node)});"
            f" if (!e) return null; const l=locator(e, {'true' if full_urls else 'false'});"
            " return l ?? {error:'NotInDocument'}; })()"
        )

    def resolve(self, locator, node=False):
        """The element a recorded locator points at on the page as it is now, or None when nothing matches:
        {"rule", "unique", "count_match", "verified", "tag", "text", "hint"}. Three outcomes: resolved (rule
        role_name_scope, test_id_first or id, verified, tag and text set); "ambiguous" (several controls matched and the
        recorded counts no longer hold, tag and text None); "hint" (only the recorded css_path still points at
        something: hint is its {tag, text}, it is not the element, show it or ignore it). An element is returned only
        if its tag, type, role, name and text still match. node=True adds "node": the id the last observation gave
        the element (None if that observation did not list it), which is how a replay finds the action to run."""
        found = self.evaluate(
            "(() => { const {resolve, textOf}=" + A11Y + f"; const r=resolve({json.dumps(locator)});"
            " return {rule:r.rule, unique:r.unique, count_match:r.count_match, verified:r.verified,"
            " hint:r.hint ?? null, digest_changed:r.digest_changed===true,"
            " tag:r.element ? r.element.tagName.toLowerCase() : null,"
            " text:r.element ? textOf(r.element) : null,"
            " node:r.element ? (window.__jevFast?.ids.get(r.element) ?? null) : null}; })()"
        )
        if not found["rule"]:
            return None
        if not node:
            del found["node"]
        if not found["digest_changed"]:
            del found["digest_changed"]  # Only present when the digest was the reason for "ambiguous".
        return found

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            activity = None
            try:
                response = self.call(
                    "Runtime.evaluate",
                    expression="""((action, settleMs, autocompleteSettleMs, tracker) => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false, raf=0;
                      const finish=()=>{
                        stopped=true;cancelAnimationFrame(raf);resolve(window.__jevWait?.activity(tracker) ?? null);
                      };
                      // The tracker's own timer: this settle is not page activity.
                      (window.__jevWait?.timeout ?? setTimeout)(finish,autocomplete ? autocompleteSettleMs : settleMs);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else raf=requestAnimationFrame(ready);
                      };
                      raf=requestAnimationFrame(ready);
                    }))(""" + f"{json.dumps(action)},{int(self.settle_ms)},{int(self.autocomplete_settle_ms)},"
                    f"{json.dumps(self.activity_spec())})",
                    awaitPromise=True,
                    returnByValue=True,
                )
                activity = (response or {}).get("result", {}).get("value")
            except (RuntimeError, TimeoutError):
                pass
            try:
                self.settle_after_action(activity)
            finally:
                self.action_mark = None
        timeouts = 0
        for attempt in range(10):
            try:
                return browser_operation(
                    {
                        "operation": "observe",
                        "session": self.session,
                        "screenshot": screenshot,
                        "keys": getattr(self, "keys", ()),
                        "urls": getattr(self, "urls", {}),
                        "cdp_timeout_ms": self.cdp_timeout_ms,
                    }
                )
            except StalePage as error:
                # A page whose reads time out would cost 15 s a try: give up on the second one in a row.
                timeouts = timeouts + 1 if isinstance(error, ReadTimeout) else 0
                if attempt == 9 or timeouts >= MAX_READ_TIMEOUTS:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] == "key":
            # The key goes to whatever has focus: the same document, and the same focused element in the same state.
            focus = page.get("focus")
            current = self.evaluate(
                "(() => { const c=window.__jevFast; if (!c) return null; const f=c.focused();"
                " return [c.pageKey(), f?.node ?? null, f?.guard ?? null]; })()"
            )
            return current == [page["page_key"], focus["node"] if focus else None, focus["guard"] if focus else None]
        if action is not None and action["kind"] in {"click", "select", "hover"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if action["kind"] == "wait":
            # Waiting changes nothing, so a page that is still streaming is not "stale" for it.
            result = self.settled(self.wait_quiet_ms, self.wait_timeout_ms)
            self.mark_activity()
            self.after_input = None
            return {"executed": action["id"], "wait": result.as_dict()}
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        self.mark_action()
        try:
            return self.send(action, page, text)
        except BaseException:
            self.action_mark = None  # Nothing was sent that the next observation should settle after.
            raise

    def send(self, action, page, text):
        if action["kind"] in NAVIGATION:
            self.navigate(action)
            self.after_input = action
            return {"executed": action["id"]}
        if action["kind"] == "key":
            for event in key_events(action["key"]):
                self.call("Input.dispatchKeyEvent", **event)
            self.after_input = action
            return {"executed": action["id"]}
        if "node" in action:
            # The press-time check compares this guard again after the pointer has moved.
            action = {**action, "guard": page.get("guards", {}).get(str(action["node"]))}
        result = browser_operation(
            {
                "operation": "act",
                "session": self.session,
                "action": action,
                "text": text,
                "reveal_timeout_ms": self.reveal_timeout_ms,
                "viewport": self.viewport,
                "cdp_timeout_ms": self.cdp_timeout_ms,
            }
        )
        self.after_input = action
        return result

    def navigate(self, action):
        """back, reload or goto, then wait (bounded) for a new document. A slow page is observed anyway."""
        before = self.evaluate("[performance.timeOrigin, location.href]")
        kind = action["kind"]
        if kind == "back":
            history = self.call("Page.getNavigationHistory")
            index = history["currentIndex"] - 1
            if index < 0:
                raise Unavailable("There is no previous page. Not sent.")
            self.call("Page.navigateToHistoryEntry", entryId=history["entries"][index]["id"])
        elif kind == "reload":
            self.call("Page.reload")
        else:
            self.call("Page.navigate", url=action["url"])
        self.wait_for_document(before)

    def wait_for_document(self, before, timeout_ms=None):
        """Wait (bounded by navigation_timeout_ms) for a complete document other than the one in `before`."""
        deadline = time.monotonic() + (self.navigation_timeout_ms if timeout_ms is None else timeout_ms) / 1000
        while time.monotonic() < deadline:
            try:
                now = self.evaluate("[performance.timeOrigin, location.href, document.readyState]")
            except (StalePage, RuntimeError, TimeoutError):
                now = None  # The old context is going away, or the page is too busy to answer yet.
            if now and now[2] == "complete" and now[:2] != before:
                return
            time.sleep(0.02)

    def activity_spec(self):
        """What the page-side counters need: the baseline to count from, the ignore list and the timer threshold."""
        seen = self.action_mark or self.activity_seen or {}
        return {
            "epoch": seen.get("epoch"),
            "since": seen.get("seq", 0),
            "ignore": self.ignore,
            "timer_ms": self.wait_timer_ms,
        }

    def activity(self):
        """Counters of the page-side tracker ({epoch, seq, inflight, new_started, timers}, with ignore_urls and the
        timer threshold applied), or None while navigating or when the page is too busy to answer."""
        call = f"activity({json.dumps(self.activity_spec())})"
        try:
            return self.evaluate(f"(window.__jevWait?.{call} ?? null)") or self.evaluate(f"({self.tracker}).{call}")
        except (StalePage, RuntimeError, TimeoutError):
            return None

    def mark_action(self):
        """Baseline of the post-action trigger: the tracker's position right before the input is sent, so only what
        the action itself starts counts (not what a polling page did while the model was thinking). One tiny read."""
        self.action_mark = None
        try:
            self.action_mark = self.evaluate("(window.__jevWait?.mark() ?? null)")
        except (StalePage, RuntimeError, TimeoutError):
            pass

    def mark_activity(self):
        """Everything up to now is accounted for; the next action only reacts to what starts after this."""
        self.activity_seen = self.activity()

    def settle_after_action(self, activity):
        """After an action's fixed settle: if counted requests or short timers are pending (or a request started or a
        timer was armed since the action began), wait for the page to settle, bounded by action_settle_timeout_ms. Adds
        no round trip when nothing is pending."""
        self.last_action_settle = None
        if activity and self.action_settle_timeout_ms and needs_settle(activity):
            seen = self.action_mark or self.activity_seen or {}
            self.last_action_settle = self.settled(
                self.action_settle_quiet_ms,
                self.action_settle_timeout_ms,
                timers_since={"epoch": seen.get("epoch"), "since": seen.get("seq", 0)},
            )
            activity = self.activity()
        self.action_mark = None
        self.activity_seen = activity

    def _spec(self, kind, ignore_urls=(), **fields):
        """The page-side description of a wait, with the Browser-wide knobs filled in."""
        return {
            "kind": kind,
            "ignore": self.ignore + ignore_patterns(ignore_urls),
            "timer_ms": self.wait_timer_ms,
            "interactive_grace_ms": self.wait_interactive_grace_ms,
            "ignore_attributes": list(self.wait_ignore_attributes),
            **fields,
        }

    def _run_slice(self, request):
        """One page-side slice. The 9 KB tracker is only sent when the page does not have it yet."""
        call = f"run({json.dumps(request)})"
        extra = {"awaitPromise": True, "returnByValue": True, "_response_timeout": WAIT_SLICE_MS / 1000 + 3}
        response = self.call("Runtime.evaluate", expression=f"(window.__jevWait?.{call} ?? null)", **extra)
        if not response.get("exceptionDetails") and response.get("result", {}).get("value") is None:
            response = self.call("Runtime.evaluate", expression=f"({self.tracker}).{call}", **extra)
        return response

    def _after_error(self, deadline):
        """A slice failed (the page navigated, or is too busy to answer). Wait, bounded by navigation_timeout_ms and
        the deadline, until the page answers with a document that has started to load; then slice again. A transient
        error on the same document returns at once."""
        end = min(deadline, time.monotonic() + self.navigation_timeout_ms / 1000)
        while time.monotonic() < end:
            try:
                now = self.evaluate("[performance.timeOrigin, location.href, document.readyState]")
            except (StalePage, RuntimeError, TimeoutError):
                now = None
            if now and now[2] != "loading":
                return
            time.sleep(0.02)

    def _wait(self, spec, timeout_ms):
        """Run a page-side condition in slices until it holds or timeout_ms passes. Never raises on timeout, and a
        busy or navigating page is just an unmet slice. A page-side exception (a bug in the condition) is raised."""
        start = time.monotonic()
        deadline = start + timeout_ms / 1000
        spec = {**spec, "key": uuid.uuid4().hex}
        reason = "timeout"
        while True:
            remaining_ms = (deadline - time.monotonic()) * 1000
            request = {**spec, "slice_ms": max(0, min(WAIT_SLICE_MS, remaining_ms))}
            outcome = None
            try:
                response = self._run_slice(request)
                details = response.get("exceptionDetails")
                if details:
                    text = (details.get("exception") or {}).get("description") or details.get("text")
                    raise ValueError(f"The wait failed in the page: {text}")
                outcome = response.get("result", {}).get("value")
            except (RuntimeError, TimeoutError):
                pass  # The old context is going away, or the page is busy: this slice is unmet.
            if outcome and outcome["met"]:
                return WaitResult(True, round((time.monotonic() - start) * 1000), outcome["reason"])
            if outcome:
                reason = outcome["reason"]
            else:
                reason = "the page is navigating"
                self._after_error(deadline)
                time.sleep(0.02)
            if time.monotonic() >= deadline:
                return WaitResult(False, round((time.monotonic() - start) * 1000), f"timeout: {reason}")

    def dom_quiet(self, quiet_ms=500, timeout_ms=30000, ignore_attributes=None):
        """No DOM mutation (child list, character data, attributes) for quiet_ms. Attribute changes by name in
        ignore_attributes (default: the Browser's wait_ignore_attributes) do not count."""
        spec = self._spec("dom_quiet", quiet_ms=non_negative("quiet_ms", quiet_ms))
        if ignore_attributes is not None:
            spec["ignore_attributes"] = list(attribute_names(ignore_attributes))
        return self._wait(spec, positive("timeout_ms", timeout_ms))

    def network_idle(self, idle_ms=500, ignore_urls=(), timeout_ms=30000):
        """No in-flight fetch or XMLHttpRequest for idle_ms. WebSockets and EventSource are never counted;
        ignore_urls (substrings or compiled regexes, matched against the absolute URL, added to the Browser's own)
        skips other long-lived ones."""
        spec = self._spec("network_idle", ignore_urls, idle_ms=non_negative("idle_ms", idle_ms))
        return self._wait(spec, positive("timeout_ms", timeout_ms))

    def settled(self, quiet_ms=500, timeout_ms=30000, ignore_urls=(), ignore_attributes=None, timers_since=None):
        """The document is loaded (also after a navigation that commits meanwhile), no counted request is in flight or
        finished within quiet_ms, no short one-shot timer that was pending when the wait began is still pending, and
        the DOM was quiet for quiet_ms. timers_since ({epoch, since}) limits the held timers to those armed after it."""
        spec = self._spec("settled", ignore_urls, quiet_ms=non_negative("quiet_ms", quiet_ms))
        if ignore_attributes is not None:
            spec["ignore_attributes"] = list(attribute_names(ignore_attributes))
        if timers_since:
            spec["epoch"], spec["timers_since"] = timers_since["epoch"], timers_since["since"]
        return self._wait(spec, positive("timeout_ms", timeout_ms))

    def element_spec(self, role, name, state, present):
        if role is not None and not isinstance(role, str):
            raise ValueError("role must be a string")
        if type(present) is not bool:
            raise ValueError("present must be a boolean")
        return self._spec(
            "element", role=role, name=pattern("name", name, optional=True), state=element_state(state), present=present
        )

    def text_spec(self, contains, present):
        if type(present) is not bool:
            raise ValueError("present must be a boolean")
        return self._spec("text", contains=pattern("contains", contains), present=present)

    def element(self, role=None, name=None, state=None, present=True, timeout_ms=30000):
        """An element with this role, accessible name (exact string or compiled regex) and state is visible anywhere
        in the document (present=True), or no such element is (present=False)."""
        spec = self.element_spec(role, name, state, present)
        return self._wait(spec, positive("timeout_ms", timeout_ms))

    def text(self, contains, present=True, timeout_ms=30000):
        """The visible document text contains a string or regex (present=True), or no longer does (present=False)."""
        spec = self.text_spec(contains, present)
        return self._wait(spec, positive("timeout_ms", timeout_ms))

    def probe(self, kind, timeout_ms=0, **args):
        """An element or text condition for a step's checks: like element() / text(), but timeout_ms may be 0, which
        reads the page once and returns at once."""
        spec = (self.element_spec if kind == "element" else self.text_spec)(**args)
        return self._wait(spec, non_negative("timeout_ms", timeout_ms))

    def text_stable(self, quiet_ms=500, role=None, name=None, timeout_ms=30000, require_text=None):
        """The visible text of the whole page, or of the element matched by role and name, is unchanged for quiet_ms.
        require_text (default: True for an element, False for the whole page) also asks for some text, so an element
        that has not started to fill is not "stable"."""
        if role is not None and not isinstance(role, str):
            raise ValueError("role must be a string")
        if require_text is not None and type(require_text) is not bool:
            raise ValueError("require_text must be a boolean")
        element_form = role is not None or name is not None
        spec = self._spec(
            "text_stable",
            quiet_ms=non_negative("quiet_ms", quiet_ms),
            role=role,
            name=pattern("name", name, optional=True),
            require_text=element_form if require_text is None else require_text,
        )
        return self._wait(spec, positive("timeout_ms", timeout_ms))

    def close(self):
        if self.target:
            # Cleared before the call and never retried, by design: a close that timed out may hang again, and a
            # second close() must not send closeTarget twice.
            target, self.target = self.target, None
            self.call("Target.closeTarget", session=None, targetId=target)


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")} | {"focus": state.get("focus")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def content_fingerprint(state):
    """The page without keyboard focus. Moving focus is not progress, so the stuck rule compares this instead."""
    actions = [{k: v for k, v in a.items() if k != "focused"} for a in state["actions"]]
    content = {k: state[k] for k in ("url", "text", "scroll")} | {"actions": actions}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    timeout_s = request.get("cdp_timeout_ms", DEFAULT_CDP_TIMEOUT_MS) / 1000
    selecting = operation == "act" and request["action"]["kind"] == "select"

    def call(method, mutation=None, **params):
        return cdp_call(method, session, params.pop("_response_timeout", timeout_s), mutation=mutation, **params)

    def evaluate(expression, await_promise=False, wait_s=0):
        extra = {"awaitPromise": True} if await_promise else {}
        # A select sets the value inside its evaluate, so an unanswered one may have been applied: a mutation.
        result = call(
            "Runtime.evaluate", mutation=True if selecting else None, expression=expression, returnByValue=True,
            _response_timeout=timeout_s + wait_s, **extra,
        )
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            # The wheel turns over the middle of the viewport, which is on screen at any size.
            width, height = request.get("viewport") or DEFAULT_VIEWPORT
            call(
                "Input.dispatchMouseEvent", type="mouseWheel", x=width // 2, y=height // 2, deltaX=0,
                deltaY=action["delta"],
            )
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            def locate(phase, guarded=False):
                # "reveal" skips the opacity and hit-test checks: a control that appears on hover is transparent
                # (and often pointer-events:none) until the pointer is over its row. "guarded" also compares the
                # observed guard (minus scope text, which hover legitimately changes), so a control that became
                # something else under the moved pointer is never pressed; a cover or shift is caught by the hit-test.
                return evaluate("""((action, phase, guarded) => {
                  const e=window.__jevFast?.nodes.get(action.node);
                  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                      !e.checkVisibility({checkOpacity:phase!=='reveal',checkVisibilityCSS:true})) return null;
                  // Hover-tolerant: the observed guard without its trailing scope text (see snapshot.js).
                  if (guarded && JSON.stringify(window.__jevFast.guard(e,false))!==
                      JSON.stringify(action.guard?.slice(0,-1))) return null;
                  if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
                  const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
                  if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
                  if (phase!=='reveal' && !e.contains(document.elementFromPoint(x,y))) return null;
                  if (action.kind==='select') {
                    if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                        !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                    e.value=action.value;
                    e.dispatchEvent(new Event('input',{bubbles:true}));
                    e.dispatchEvent(new Event('change',{bubbles:true}));
                  }
                  return {x,y};
                })(""" + json.dumps(action) + "," + json.dumps(phase) + "," + json.dumps(guarded) + ")")

            def move(point):
                call("Input.dispatchMouseEvent", type="mouseMoved", x=point["x"], y=point["y"], button="none")

            reveal = bool(action.get("revealed_by")) and kind != "select"
            if kind == "select":
                target = locate("press")
                if target is None:
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
            else:
                point = locate("reveal" if reveal else "press")
                if point is None:
                    raise StalePage("Target changed or is covered. Observe again.")
                # Real pointer order: move first. Hover handlers may reflow the page or change the control, so
                # everything is located, hit-tested and guard-compared again AFTER the move, right before the press.
                move(point)
                if kind == "hover":
                    return {"executed": action["id"]}
                if reveal:
                    timeout = int(request.get("reveal_timeout_ms", 500))
                    # Resolves at the first frame whose opacity is above 0 (a fade-in counts as shown).
                    shown = evaluate(
                        """((action, timeout) => new Promise(resolve => {
                          const e=window.__jevFast?.nodes.get(action.node);
                          const end=performance.now()+timeout;
                          const check=()=>{
                            if (e?.isConnected && e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))
                              resolve(true);
                            else if (performance.now()>=end) resolve(false);
                            else requestAnimationFrame(check);
                          };
                          check();
                        }))("""
                        + json.dumps(action)
                        + f",{timeout})",
                        await_promise=True,
                        wait_s=timeout / 1000,
                    )
                    if not shown:
                        # A control that stays transparent is never pressed (this also blocks invisible honeypots).
                        raise Refused("Control stayed hidden after hovering. Not pressed. Observe again.")
                target = locate("press", guarded="guard" in action)
                if target is None:
                    raise Refused("Target moved, changed or is covered after the pointer moved. Not pressed.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["can_go_back"] = call("Page.getNavigationHistory")["currentIndex"] > 0
    info["actions"] += control_actions(info, request.get("keys", ()), request.get("urls", {}))
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
