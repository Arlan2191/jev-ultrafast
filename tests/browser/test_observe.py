"""Smoke test: observe a fixture page and check the element table the model would see."""


def test_observe_element_table(open_page):
    browser = open_page("smoke.html")
    page = browser.observe(screenshot=False)

    table = [(a["role"], a["label"], a["kind"]) for a in page["actions"] if a["kind"] != "wait"]
    assert ("link", "Help center", "click") in table, table
    assert ("button", "Save changes", "click") in table, table
    assert ("textbox", "Display name", "fill") in table, table
    assert ("checkbox", "Email updates", "click") in table, table
    selects = [a for a in page["actions"] if a["kind"] == "select"]
    assert [a["label"] for a in selects] == ["Language → German"], table
    assert "Account settings" in page["text"]
    assert page["title"] == "Smoke page"
