"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Refused(StalePage):
    """The pointer already moved onto the target, then the press was refused. Nothing was pressed."""


class Browser:
    def __init__(self, url, *, settle_ms=50, autocomplete_settle_ms=200, reveal_timeout_ms=500):
        self.settle_ms = settle_ms
        self.reveal_timeout_ms = reveal_timeout_ms
        self.autocomplete_settle_ms = autocomplete_settle_ms
        ensure_daemon()
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""((action, settleMs, autocompleteSettleMs) => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? autocompleteSettleMs : settleMs);
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
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + f"{json.dumps(action)},{int(self.settle_ms)},{int(self.autocomplete_settle_ms)})",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
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
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
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
            }
        )
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression, await_promise=False):
        extra = {"awaitPromise": True} if await_promise else {}
        result = call("Runtime.evaluate", expression=expression, returnByValue=True, **extra)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
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
                  if (false && JSON.stringify(window.__jevFast.guard(e,false))!==
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
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
