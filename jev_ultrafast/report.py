"""report.html: one static page for a human, built from the (already scrubbed) trace and stream records.

No scripts and no external assets. Screenshots are embedded as data URIs up to EMBED_LIMIT bytes in all; past that
they are linked relative to the run directory.
"""

import base64
import html
import json
from pathlib import Path

EMBED_LIMIT = 12 * 1024 * 1024
# The page has no script, link or external asset; a later edit that adds one must also loosen this.
CSP = "default-src 'none'; img-src data: 'self'; style-src 'unsafe-inline'"

STYLE = """
:root{--bg:#fff;--fg:#1c1c1e;--muted:#6b6b70;--line:#d8d8de;--card:#f6f6f9;--ok:#1a7f37;--bad:#c62828;--warn:#9a6700}
@media (prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#ececf0;--muted:#9a9aa3;--line:#33333b;--card:#1f1f25;
--ok:#4cc26a;--bad:#ff7b72;--warn:#d9a441}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0 auto;
max-width:960px;padding:16px}
h1{font-size:20px}h2{font-size:16px;margin-top:28px;border-bottom:1px solid var(--line);padding-bottom:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:8px 0}
.meta{color:var(--muted);font-size:12px}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
code{font:12px ui-monospace,monospace;word-break:break-all}
img{max-width:100%;border:1px solid var(--line);border-radius:4px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:3px 6px;text-align:left;
vertical-align:top;font-size:12px}
"""


def esc(value):
    return html.escape("" if value is None else str(value))


def image(path, run_dir, budget):
    if not path:
        return ""
    try:
        data = (run_dir / path).read_bytes()
    except OSError:
        return f'<div class="meta">{esc(path)} (missing)</div>'
    if budget["left"] >= len(data):
        budget["left"] -= len(data)
        source = "data:image/jpeg;base64," + base64.b64encode(data).decode()
    else:
        source = html.escape(path, quote=True)
        budget["linked"] = True
    return f'<div><img alt="{esc(path)}" src="{source}"></div>'


def locator_text(locator):
    if not locator:
        return ""
    scope = locator.get("scope") or {}
    parts = [f"{locator.get('role')} {locator.get('name')!r}"]
    if scope:
        parts.append(f"in {scope.get('role')} {scope.get('name')!r}")
    for key in ("test_id", "id", "href", "label_for"):
        if locator.get(key):
            parts.append(f"{key}={locator[key]}")
    return "; ".join(parts)


def event_card(event, run_dir, budget):
    kind = event["type"]
    stamp = f'<span class="meta">{event["t_ms"]} ms</span>'
    if kind == "action":
        extra = esc(event.get("text")) if event.get("text") is not None else ""
        lines = [f"<b>Action {esc(event['index'])}: {esc(event['label'])}</b> ({esc(event['kind'])}) {stamp}"]
        if extra:
            lines.append(f"<div>typed <code>{extra}</code> ({esc(event.get('text_source'))})</div>")
        if event.get("locator"):
            lines.append(f'<div class="meta">{esc(locator_text(event["locator"]))}</div>')
        elif event.get("locator_error"):
            lines.append(f'<div class="meta warn">no locator: {esc(event["locator_error"])}</div>')
        return '<div class="card">' + "".join(lines) + "</div>"
    if kind == "observe":
        title = f"Observed {esc(event['url'])} ({esc(event['elements'])} elements)"
        return f'<div class="card">{title} {stamp}{image(event.get("screenshot"), run_dir, budget)}</div>'
    if kind == "decision":
        top = ", ".join(f"{esc(t['label'])} {t['p']}" for t in event["top"][:3])
        return f'<div class="card">Decision {esc(event["operation"])}: {esc(top)} {stamp}</div>'
    if kind == "check":
        mark = '<span class="ok">met</span>' if event["met"] else '<span class="bad">not met</span>'
        return f'<div class="card">Check {esc(event["description"])}: {mark} {esc(event["reason"])} {stamp}</div>'
    if kind == "step":
        return f'<div class="card">Step {esc(event["index"] + 1)}: {esc(event["status"])} {stamp}</div>'
    if kind == "refused":
        label = "Skipped" if event["skipped"] else "Refused"
        return f'<div class="card warn">{label}: {esc(event["label"])} - {esc(event["reason"])} {stamp}</div>'
    if kind == "denied":
        return f'<div class="card bad">Denied by policy: {esc(event["action"])} - {esc(event["reason"])} {stamp}</div>'
    if kind == "done_rejected":
        failed = "; ".join(f"{esc(f['description'])} ({esc(f['reason'])})" for f in event["failed"])
        return f'<div class="card warn">DONE rejected: {failed} {stamp}</div>'
    if kind == "wait":
        wait = f"Wait {esc(event['kind'])}: met={esc(event['met'])} {esc(event['waited_ms'])} ms"
        return f'<div class="card">{wait}</div>'
    return ""


def capture_notes(start, end, capture):
    """What the page must not leave unsaid: capture that ended early, records not kept, actions without locators."""
    notes = []
    lost = (capture or {}).get("lost") or (end or {}).get("capture_lost")
    if lost:
        when = f"{(lost.get('t_ms') or 0) / 1000:.1f} s"
        notes.append(
            f"Console and network capture ended at {when}: {lost.get('reason')}. "
            "Nothing after that is in console.jsonl, network.jsonl or this page."
        )
    dropped = (end or {}).get("dropped") or {}
    if dropped.get("console") or dropped.get("network"):
        notes.append(
            f"Not kept (between the first and the last entries of each stream, errors are kept): "
            f"{dropped.get('console', 0)} console, {dropped.get('network', 0)} network."
        )
    if dropped.get("errors"):
        notes.append(f"{dropped['errors']} capture events could not be read and were skipped.")
    missing = ((end or {}).get("totals") or {}).get("locator_missing")
    if missing:
        notes.append(f"{missing} executed actions have no locator, so this trace cannot be replayed.")
    return notes


def render_report(events, console, network, run_dir, capture=None):
    run_dir, budget = Path(run_dir), {"left": EMBED_LIMIT}
    start = next((e for e in events if e["type"] == "run_start"), {})
    end = next((e for e in reversed(events) if e["type"] == "run_end"), None)
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">']
    out.append(f'<meta http-equiv="Content-Security-Policy" content="{CSP}">')
    out.append('<meta name="viewport" content="width=device-width,initial-scale=1"><title>Run report</title>')
    out.append(f"<style>{STYLE}</style></head><body><h1>Run report</h1>")
    if end:
        verdict = "ok" if end["status"] == "done" else "bad"
        totals = end["totals"]
        out.append(
            f'<div class="card"><b class="{verdict}">{esc(end["status"])}</b>'
            f" {esc(end['stop_reason'] or '')} - verified: {esc(end['verified'])}"
            f'<div class="meta">{esc(totals.get("actions"))} actions, {esc(totals.get("jev_calls"))} Jev calls, '
            f"{esc(totals.get('text_calls'))} text calls, {esc(totals.get('wall_ms'))} ms</div></div>"
        )
    else:
        out.append('<div class="card warn">The run has no end event (it was interrupted).</div>')
    for note in capture_notes(start, end, capture):
        out.append(f'<div class="card warn">{esc(note)}</div>')
    out.append("<h2>Steps</h2><ol>")
    for step in start.get("steps", []):
        checks = "".join(f"<li>{esc(c)}</li>" for c in step.get("checks", []))
        out.append(f"<li>{esc(step.get('goal'))}<ul>{checks}</ul></li>")
    out.append("</ol><h2>Timeline</h2>")
    out += [event_card(e, run_dir, budget) for e in events if e["type"] != "run_start"]
    if end and end.get("screenshot"):
        out.append("<h2>Final page</h2>" + image(end["screenshot"], run_dir, budget))
    errors = [c for c in console if c.get("level") in {"error", "warning", "assert"} or c.get("source") == "exception"]
    out.append(f"<h2>Console errors and warnings ({len(errors)})</h2>")
    if capture and not capture.get("console"):
        out.append(f'<div class="meta">Console was not captured: {esc(capture.get("reason"))}</div>')
    out.append("<table>")
    for c in errors:
        out.append(f"<tr><td>{esc(c.get('level'))}</td><td>{esc(c.get('text'))}</td><td>{esc(c.get('url'))}</td></tr>")
    failed = [n for n in network if n.get("failure") or (n.get("status") or 0) >= 400]
    out.append(f"</table><h2>Failed requests ({len(failed)} of {len(network)})</h2><table>")
    for n in failed:
        reason = n.get("failure") or n.get("status")
        out.append(f"<tr><td>{esc(n['method'])}</td><td>{esc(n['url'])}</td><td>{esc(reason)}</td></tr>")
    out.append("</table>")
    config = esc(json.dumps(start.get("config")))
    out.append(f'<p class="meta">Screenshots are not redacted. Config: <code>{config}</code></p>')
    if budget.get("linked"):
        out.append('<p class="meta">Some screenshots are linked from the run directory, not embedded.</p>')
    out.append("</body></html>")
    return "".join(out)
