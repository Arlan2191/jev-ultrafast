import os
import time


def test_probe(open_page):
    b = open_page("tick_button.html")

    def move(i=0):
        start = time.perf_counter()
        b.call("Input.dispatchMouseEvent", type="mouseMoved", x=100 + 3 * i, y=100, button="none")
        return round((time.perf_counter() - start) * 1000)

    def moves(n=4):
        return [move(i) for i in range(n)]

    def ev(expression, wait=False):
        return b.call("Runtime.evaluate", expression=expression, awaitPromise=wait, returnByValue=True)

    out = [("A fresh", moves())]
    ev("new Promise(r=>requestAnimationFrame(r))", True)
    out.append(("B after one awaited rAF", moves(6)))
    time.sleep(3)
    out.append(("D after 3 s idle", moves(3)))
    ev("new Promise(r=>setTimeout(r,60))", True)
    out.append(("E after setTimeout wait", moves(3)))
    ev("cancelAnimationFrame(requestAnimationFrame(()=>{}))")
    out.append(("F after rAF scheduled+cancelled", moves(3)))
    ev("window.__t=0; (function f(){window.__t++; requestAnimationFrame(f)})()")
    out.append(("G while a rAF loop runs", moves(3)))
    out.append(("G2 rAF loop counter", ev("window.__t")["result"]["value"]))
    ev("window.__stop=1; window.requestAnimationFrame=()=>0")
    lines = ["### probe2"] + [f"- {n}: {d}" for n, d in out]
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write("\n".join(lines) + "\n")
