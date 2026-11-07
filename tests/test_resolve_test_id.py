"""Offline pins for the test-id-first rule of a11y.js `resolve`, run under node + jsdom (skipped when either is missing:
jsdom is not a dependency). The browser fixtures in tests/browser/test_record_replay.py pin the same cases in a real
browser, in CI."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jev_ultrafast import agent as loop

A11Y = Path(loop.__file__).with_name("a11y.js")

SCRIPT = r"""
const {JSDOM} = require('jsdom');
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const load = html => {
  const dom = new JSDOM(html, {runScripts: 'outside-only'});
  dom.window.Element.prototype.checkVisibility = function () { return !this.closest('[hidden]'); };
  Object.defineProperty(dom.window.HTMLElement.prototype, 'innerText', {get() { return this.textContent; }});
  dom.window.CSS = {escape: s => s};
  dom.window.eval('globalThis.__a=(' + src + ')');
  return dom.window;
};
const {record, replay, marker} = JSON.parse(process.argv[2]);
const locator = load(record).eval(`__a.locator(document.querySelector('[data-m=${marker}]'))`);
const pick = 'r => ({rule: r.rule, m: r.element && r.element.dataset.m})';
const found = load(replay).eval(`(${pick})(__a.resolve(${JSON.stringify(locator)}))`);
console.log(JSON.stringify(found));
"""


def resolve(record, replay, marker):
    out = subprocess.run(
        ["node", "-e", SCRIPT, str(A11Y), json.dumps({"record": record, "replay": replay, "marker": marker})],
        capture_output=True, text=True,
    )
    if "Cannot find module 'jsdom'" in out.stderr:
        pytest.skip("jsdom is not installed")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def rows(names):
    items = "".join(
        f'<li><h3>{n}</h3><button data-testid="del-{i}" data-m="{n}">Delete</button></li>'
        for i, n in enumerate(names, 1)
    )
    return f"<ul>{items}</ul>"


def adds(tid_on, text="Price 1"):
    def button(m):
        tid = ' data-testid="add"' if m == tid_on else ""
        return f'<div><span>{m}</span> <button data-m="{m}"{tid}>Add</button></div>'

    return f'<div id="feed"><p>{text}</p>{button("a")}{button("b")}{button("c")}</div>'


def test_a_positional_test_id_in_another_named_row_is_not_followed():
    found = resolve(rows(["Alice", "Bob"]), rows(["Bob", "Alice"]), "Alice")
    assert found == {"rule": "role_name_scope", "m": "Alice"}  # Never Bob, whose row carries del-1 now.


def test_a_test_id_inside_its_recorded_scope_still_resolves_first():
    assert resolve(rows(["Alice", "Bob"]), rows(["Alice", "Bob"]), "Alice") == {"rule": "test_id_first", "m": "Alice"}


def test_a_test_id_moved_onto_a_same_name_look_alike_is_a_refusal():
    found = resolve(adds("a"), adds("b"), "a")
    assert found == {"rule": "ambiguous", "m": None}


def test_a_unique_test_id_wins_when_the_look_alikes_container_text_changed():
    found = resolve(adds("a"), adds("a", text="Price 2"), "a")
    assert found == {"rule": "test_id_first", "m": "a"}
