import os
import time


def test_probe(open_page):
    b = open_page("tick_button.html")

    def ms(fn):
        start = time.perf_counter()
        fn()
        return round((time.perf_counter() - start) * 1000)

    def move(i=0):
        return ms(lambda: b.call("Input.dispatchMouseEvent", type="mouseMoved", x=40 + i, y=20, button="none"))

    def click():
        return ms(lambda: [b.call("Input.dispatchMouseEvent", type=t, x=40, y=20, button="left", clickCount=1)
                           for t in ("mousePressed", "mouseReleased")])

    def raf():
        return ms(lambda: b.call("Runtime.evaluate", expression="new Promise(r=>requestAnimationFrame(r))",
                                 awaitPromise=True, returnByValue=True))

    def vis():
        return b.evaluate("[document.visibilityState, document.hasFocus()]")

    out = [("start vis", vis()), ("raf x3", [raf() for _ in range(3)]), ("move x3", [move(i) for i in range(3)])]
    out.append(("click", click()))
    out.append(("vis after click", vis()))
    out.append(("raf x3 after click", [raf() for _ in range(3)]))
    out.append(("move x3 after click", [move(i) for i in range(3)]))
    out.append(("click2", click()))
    out.append(("raf x3 after click2", [raf() for _ in range(3)]))
    out.append(("move x3 after click2", [move(i) for i in range(3)]))
    time.sleep(2)
    out.append(("move x3 after 2 s idle", [move(i) for i in range(3)]))
    b.call("Page.setWebLifecycleState", state="active")
    out.append(("raf/move after lifecycle active", [raf(), move()]))
    b.call("Emulation.setFocusEmulationEnabled", enabled=True)
    out.append(("raf/move after refocus emul", [raf(), move()]))
    out.append(("click3", click()))
    out.append(("raf/move after click3", [raf(), move(), move()]))
    out.append(("click+Page.bringToFront", ms(lambda: b.call("Page.bringToFront"))))
    out.append(("raf/move after bringToFront", [raf(), move(), move()]))
    out.append(("click4", click()))
    out.append(("raf/move after click4 (front)", [raf(), move(), move()]))
    lines = ["### probe3"] + [f"- {n}: {d}" for n, d in out]
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write("\n".join(lines) + "\n")
