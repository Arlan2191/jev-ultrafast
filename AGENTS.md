# Jev Ultrafast

Read README.md before editing. Keep the loop small: page -> indexed elements -> operation + target -> execution.

- The input is one natural-language goal. Do not add site-specific plans or hardcoded field values.
- TypeSafe chooses an operation and operation-specific target heads in one request. Consume only the selected operation's target.
- Targets must map to observed elements and supported operations. Never let the model emit selectors or executable code.
- TYPE_TEXT invokes the text LLM. Cache a stale retry's value only while its entire helper input is identical.
- `before_action` runs before each execution attempt, after text generation, so it can run more than once for one intended action if the page goes stale first: keep it free of side effects or idempotent. A veto (`ActionDenied`) always prevents execution and leaves no history entry.
- HOVER is a browser mutation too (it can open menus). A control hidden only by opacity is pressed only after the pointer reveals it; one that stays transparent is never pressed. Every click is move, then re-locate, hit-test and guard-compare, then press; a refusal after the move is recorded in `state["refused"]` (never history), hidden from the next choice while the page is unchanged, and counts toward `stuck_after`.
- Keys and URLs are caller-declared choices (`Agent(keys=..., urls=...)`); the model picks among them and never writes either. Key presses, BACK, RELOAD and GOTO are browser mutations like clicks.
- Never retry a browser mutation. Log execution before observing its result.
- Waits cost no model call: WAIT and the post-action settle run `settled()` in code (tracker in `waits.js`, role/name in `a11y.js` shared with `snapshot.js`). A wait never raises on timeout and is never a browser mutation: it skips the freshness check but still goes through `before_action`. A WAIT that timed out on an unchanged page counts toward `stuck_after`. The tracker must never change what the page sees: no clone, tee or wrapped body, only Proxies that forward every call.
- Screenshots are optional; the model does not consume them. Keep demonstration footage at its original speed.
- Keep credentials server-side and .env ignored. Tests must not call paid APIs.
- Verify actual final outcomes independently. A DONE choice is not proof of success.
- Checks (`Step(expect=...)`, `jev_ultrafast.expect`) are evaluated in code, never by the model; DONE on a step with checks is accepted only when they hold twice, across a settle, and an action whose checks hold (twice) completes the step without a DONE call. A check callable is read-only, idempotent and fast, and runs after every action only with `auto=True`.
- The model picks a declared value by name; it sees the text only where the page or the history shows it, and never for a secret (`values=`). A secret value is redacted (also its encoded forms) from history, state, hook text, check reasons and the page the model reads, including the first observation, and only the browser receives the real text.
- Run evidence (`Agent(evidence_dir=...)`, `trace.py`, `evidence.py`, `report.py`) is off by default and read-only: console and network come from a websocket of its own (never browser-harness's shared event buffer, never wrapped page functions), every file but the screenshots passes through the secret scrub, and `Agent.close()` must leave no thread running. The element locator lives in `a11y.js` so a replay can reuse it.
- Replay (`Agent(replay=...)`, `replay.py`) executes only recorded actions that were re-resolved and re-verified on the current page, through the same `before_action` hook and the normal act path; the model is never asked unless a step diverges (then only for that step, or `diverged` with `heal=False`), a veto is `denied` and never healed, and a secret comes from the caller again, never from the trace.
- Keep examples, README claims, raw evidence, and model-call counts consistent.
- Do not commit or push unless the user requests it.

Checks: uv run ruff check ., uv run pytest, node --check jev_ultrafast/static/app.js, uv build. Real-browser lane: BU_CDP_URL=... uv run pytest -m browser (fixture pages in tests/fixtures/, runs in CI).
