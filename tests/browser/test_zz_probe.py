import json


def test_probe(open_page):
    b = open_page("stream.html")
    r = b.call("Runtime.evaluate", expression="start(); 1", returnByValue=True)
    print("PROBE", json.dumps(r)[:1500])
    r = b.call("Runtime.evaluate", expression="[typeof start, typeof window.__jevWait, String(window.fetch).slice(0,60)]", returnByValue=True)
    print("PROBE", json.dumps(r)[:600])
    assert False
