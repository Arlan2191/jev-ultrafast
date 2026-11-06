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
- Screenshots are optional; the model does not consume them. Keep demonstration footage at its original speed.
- Keep credentials server-side and .env ignored. Tests must not call paid APIs.
- Verify actual final outcomes independently. A DONE choice is not proof of success.
- Keep examples, README claims, raw evidence, and model-call counts consistent.
- Do not commit or push unless the user requests it.

Checks: uv run ruff check ., uv run pytest, node --check jev_ultrafast/static/app.js, uv build. Real-browser lane: BU_CDP_URL=... uv run pytest -m browser (fixture pages in tests/fixtures/, runs in CI).
