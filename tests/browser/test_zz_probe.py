import os
import time


def timed(browser, **event):
    start = time.perf_counter()
    browser.call("Input.dispatchMouseEvent", **event)
    return round((time.perf_counter() - start) * 1000)


def test_probe(open_page):
    b = open_page("tick_button.html")
    out = []
    def move(x, y):
        return timed(b, type="mouseMoved", x=x, y=y, button="none")
    def click(x, y):
        return [timed(b, type="mousePressed", x=x, y=y, button="left", clickCount=1),
                timed(b, type="mouseReleased", x=x, y=y, button="left", clickCount=1)]
    out.append(("move same x8", [move(100, 100) for _ in range(8)]))
    out.append(("move alternating x8", [move(100 + 5 * (i % 2), 100) for i in range(8)]))
    out.append(("move distinct x8", [move(100 + 7 * i, 100) for i in range(8)]))
    out.append(("move+click same spot x6", [(move(40, 20), click(40, 20)) for _ in range(6)]))
    out.append(("move+click, dom change between x6",
                [(b.evaluate("document.body.append(document.createElement('i')); 1"), move(40, 20), click(40, 20)) for _ in range(6)]))
    out.append(("move+click, rAF between x6",
                [(b.call("Runtime.evaluate", expression="new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))", awaitPromise=True), move(40, 20), click(40, 20)) for _ in range(6)]))
    lines = ["### probe"] + [f"- {name}: {data}" for name, data in out]
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write("\n".join(lines) + "\n")
