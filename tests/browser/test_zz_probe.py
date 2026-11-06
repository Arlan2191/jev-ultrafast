import os
import time

import pytest
from browser_harness.helpers import cdp


def ms(fn):
    start = time.perf_counter()
    fn()
    return round((time.perf_counter() - start) * 1000)


VARIANTS = {
    "background": {},
    "background+newWindow": {"newWindow": True},
    "foreground": {"background": False},
    "background then activateTarget": {"after": "activate"},
    "background then bringToFront": {"after": "front"},
    "background, no focus emulation": {"focus": False},
}


@pytest.mark.parametrize("name", list(VARIANTS))
def test_probe(open_page, fixture_base_url, name):
    open_page("tick_button.html")
    v = VARIANTS[name]
    params = {"url": "about:blank", "background": v.get("background", True)}
    if v.get("newWindow"):
        params["newWindow"] = True
    target = cdp("Target.createTarget", **params)["targetId"]
    session = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]

    def call(method, **p):
        return cdp(method, session_id=session, **p)

    call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
    if v.get("focus", True):
        call("Emulation.setFocusEmulationEnabled", enabled=True)
    call("Page.navigate", url=f"{fixture_base_url}/tick_button.html")
    time.sleep(0.5)
    if v.get("after") == "activate":
        cdp("Target.activateTarget", targetId=target)
    if v.get("after") == "front":
        call("Page.bringToFront")

    def move(i=0):
        return ms(lambda: call("Input.dispatchMouseEvent", type="mouseMoved", x=40 + i, y=20, button="none"))

    def click():
        return ms(lambda: [call("Input.dispatchMouseEvent", type=t, x=40, y=20, button="left", clickCount=1)
                           for t in ("mousePressed", "mouseReleased")])

    def raf():
        return ms(lambda: call("Runtime.evaluate", expression="new Promise(r=>requestAnimationFrame(r))",
                               awaitPromise=True, returnByValue=True))

    out = [("raf", [raf() for _ in range(2)]), ("click", click())]
    for i in range(3):
        out.append((f"after click {i}: raf, move, move", [raf(), move(), move()]))
    out.append(("click again", click()))
    out.append(("after 2nd click: move x3", [move(i) for i in range(3)]))
    cdp("Target.closeTarget", targetId=target)
    lines = [f"### probe4 {name}"] + [f"- {n}: {d}" for n, d in out]
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write("\n".join(lines) + "\n")
