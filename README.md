<img src="docs/banner.svg" alt="Jev Ultrafast · Browser Use × TypeSafe" width="100%" />

# Jev Ultrafast ⚡

> [!IMPORTANT]
> **BU Ultrafast, Browser Use Cloud's own hosted agent, is live on every plan.** Set `model: "bu-ultrafast"` to use it.
> **[Learn more](https://browser-use.com/ultrafast?utm_source=github&utm_medium=readme&utm_campaign=jev-ultrafast)** · **[Open Browser Use Cloud](https://cloud.browser-use.com?utm_source=github&utm_medium=readme&utm_campaign=jev-ultrafast)**

**A browser agent with a dynamic, indexed action space.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element. A small LLM writes text only when the operation is `TYPE_TEXT`.

**Zürich → London on Google Flights in 7.1 seconds.** One natural-language goal, actual text generation, and loading waits included.

<a href="docs/demo.mp4"><img src="docs/demo.gif" alt="A real Google Flights search at 1× speed, with generated city names and dynamic operation/target decisions" width="100%" /></a>

[Watch the MP4](docs/demo.mp4) · [Measurements](docs/performance.md) · [Read the loop](jev_ultrafast/agent.py)

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

The operations are `CLICK`, `HOVER`, `TYPE_TEXT`, `SELECT`, `PRESS_KEY`, `GOTO`, `BACK`, `RELOAD`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, and `BLOCKED`. Only supported operations and targets are offered. `HOVER` moves the pointer without pressing, for menus and tooltips that open on hover; it is offered only for elements that hint at a hover effect (`revealed_by`, `aria-haspopup`, `aria-expanded`, a non-empty `title`, or `aria-describedby`) and is left out entirely when none does. Controls that are hidden only by `opacity` until their row is hovered or focused are listed with `revealed_by: "hover"`; `CLICK` moves the pointer onto them, waits up to `reveal_timeout_ms` (default 500) for them to appear, and never presses one that stays transparent. Every click is preceded by a pointer move, and after the move the target is located, hit-tested and compared with its observed guard again (identity, role, name, value and state, `href`; not the surrounding text, which hover legitimately changes), so a page that relabels the control on hover, or covers or moves it, is refused instead of pressed. A refused press is recorded in `state["refused"]` (not in the history), shown to the model as a refused recent action, hidden from the next choice while the page is unchanged, and counts toward `stuck_after`. A control is a hover reveal only when one transparent layer (the control or a close ancestor) sits directly under a visible parent that is not `body`, and neither that layer nor any element between it and the control is `position: fixed`, a dialog (`dialog`, `role="dialog"` or `"alertdialog"`, `aria-modal="true"`, `[popover]`), and the layer itself covers at most a quarter of the viewport. A closed `opacity: 0` dialog, or a large opacity-0 mobile nav panel, is therefore not listed even inside app wrappers, while a toolbar in a tall message is. The remaining limit: a small, non-fixed, non-dialog transparent popover is still listed; a refused press on it is bounded by `stuck_after`.

```text
                      one TypeSafe request
                     ┌───────────────────────────┐
page → element table → operation                 │
                     │ click_target              │
                     │ type_text_target          │
                     │ select_target, if present │
                     └─────────────┬─────────────┘
                         use the matching target
                                   │
                    CLICK [7] ─────┤──→ browser
                TYPE_TEXT [3] ─────┘
                          ↓
                   small LLM → text → browser
```

Target questions are speculative. If the operation is `CLICK`, only `click_target` can execute. Two decisions, **one network round trip**. Each target head contains only compatible elements. Native dropdown choices carry an observed element/option index.

There are no site-specific action scripts or prepared field strings in the policy. The Flights example supplies a goal and independently verifies the outcome. The screenshot renderer adds labels afterward; it does not drive the browser.

## Try it

```bash
git clone https://github.com/browser-use/jev-ultrafast.git
cd jev-ultrafast
uv sync
cp .env.example .env
# Add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY.
uv run jev
```

Open **http://127.0.0.1:8766** and click **Start demo → Run automatically**. The inspector shows numbered elements, operation probabilities, target probabilities, and executed actions. **Choose next** pauses before execution.

Chrome connects through [Browser Harness](https://github.com/browser-use/browser-harness), installed by `uv sync`. Run `uv run browser-harness --doctor` if it needs connecting. Allow remote debugging in Chrome when prompted.

`TEXT_MODEL_API_KEY` is an OpenRouter key in the example configuration. The current demo uses `inception/mercury-2.5` with reasoning disabled. Gemini, GLM, and DeepSeek can also use the OpenAI-compatible text helper; configure the appropriate model, endpoint, and reasoning setting.

## Use the library

```python
from jev_ultrafast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run with `uv run --env-file .env python your_script.py`. The same policy can run a different task:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Find and open the Wikipedia article about Gödel’s incompleteness theorems.'
```

`uv run --env-file .env python examples/flights.py --keep-open` performs the flight search, checks the actual route/date/results, and saves its trace. It does not select or book a flight.

### Keys and navigation

`PRESS_KEY` sends one key to the element that has focus (marked `focused` in the element table), or to the page when nothing does. The model chooses among `Enter`, `Escape`, `Tab`, `Shift+Tab`, the arrow keys, `Space`, `Backspace`, `PageUp`, `PageDown`, `Home`, `End`, and any shortcut chords you declare. It never writes a key or a URL. `BACK` is offered only while the tab has a previous history entry (read from the browser's navigation history, not `history.length`, which also counts forward entries), `RELOAD` always, and `GOTO` only for pages you declare.

```python
with Agent(
    url,
    "Open the command palette and search for billing",
    keys=("Control+K", "Meta+K"),
    urls={"Settings": "https://app.example.com/settings"},
    navigation_timeout_ms=15000,
) as agent:
    for state in agent.run():
        pass
```

Declared chords are `(Control|Alt|Shift|Meta)+...+Key`, with at least one modifier and a key from the fixed set, a single letter or digit, or `F1` to `F12`; anything else raises `ValueError` at construction. URLs must be absolute `http(s)` URLs without credentials. After `BACK`, `RELOAD` or `GOTO` the agent waits for a new document (at most `navigation_timeout_ms`) and then observes even if the page is still loading. A `BACK` that finds no previous page sends nothing and is recorded like a refused press (`state["refused"]`, hidden from the next choice, counted by `stuck_after`). All of these are browser mutations: they are logged before the observation, never retried, count toward `max_steps` and `stuck_after`, and pass through `before_action` with `kind` `key`, `back`, `reload` or `goto`. A key action carries `key` and `focused` (`role`, `label`, `href`, `tag` and `type` of the focused element, or `None`), so a policy can refuse, for example, Enter on a focused Delete button; `goto` carries `url`. A policy that refuses `kind == "click"` on a control must also handle `kind == "key"` on the focused element, because Enter or Space on a focused Delete button activates it just like a click. The value of a focused password, file or hidden input is never read: its identity guard holds only its type and accessible name, and Enter on it still works. Keyboard focus is part of what the model sees, but moving focus alone is not progress: `stuck_after` compares the page without focus, so a model that keeps pressing `Tab` over the same inputs ends `blocked`. A key press is refused as stale unless the focused element (its node and guard) is still the one observed. The key head costs about 2.9 KB flat per request (the head's rule text plus 14 key choices); it does not shrink noticeably with fewer keys. Limits: keys go through the browser's input path, so a page that ignores synthetic key events or traps focus in a frame or shadow root is out of reach, and a key cannot type text (use `TYPE_TEXT`). Builds on the idea in [browser-use/jev-ultrafast#211](https://github.com/browser-use/jev-ultrafast/pull/211).

### Waits

`WAIT` no longer sleeps 0.1 s. It waits in code, without a model call, until the page is settled: the document is loaded (also after a script navigates meanwhile), no `fetch` or `XMLHttpRequest` is in flight or finished within `wait_quiet_ms` (default 500), no short timer that was pending when the wait began is still pending, and the DOM has not mutated for `wait_quiet_ms`; it gives up after `wait_timeout_ms` (default 30000). One `WAIT` therefore covers a whole streamed answer or a results page that loads after a request. A `WAIT` goes through `before_action` like every other action, and skips only the freshness check (a page that is still streaming is never fresh; a wait changes nothing, and every later press re-checks). It is recorded in the history with `waited_ms` and `met`. For `stuck_after` a `WAIT` that was met is progress, a `WAIT` that timed out while the content changed (a stream longer than the timeout) is progress, and a `WAIT` that timed out on an unchanged page is an unchanged step, so a model that keeps waiting on a page that never settles ends the run as `blocked` after `stuck_after` of them instead of burning `max_steps` times `wait_timeout_ms`.

After every other action, the existing settle (50 ms, 200 ms for comboboxes) stays. If at its end a counted request is in flight or started since the action began, or a short timer was armed since then (the baseline is read right before the input is sent, one small browser call), the same wait runs, bounded by `action_settle_timeout_ms` (default 5000, `0` turns it off), so "click, fetch, render" and "click, timer, navigate" are already done when the next observation is read. When nothing is pending it adds no browser call: the settle script returns the tracker counters it already reads. Timers and requests the page started before the action (a polling chain, a long poll) do not trigger it, however long the model took to choose: a polling page that arms a timer every 400 ms costs a click nothing. Requests with a non-`http(s)` URL (`data:`, `blob:`) are never counted, and relative URLs are resolved against the document base URL (`<base href>`), as the browser does.

Callers (test runners, assertions) can wait directly. Each call is bounded by `timeout_ms` (default 30000), costs no model call, never raises on timeout, and returns a `WaitResult(met, waited_ms, reason)`; the last 100 are kept in `state["waits"]`:

```python
agent.wait_for("settled", quiet_ms=500)
agent.wait_for("dom_quiet", quiet_ms=300, ignore_attributes=("style",))
agent.wait_for("network_idle", idle_ms=300, ignore_urls=["/poll", re.compile(r"/long/\d+$")])
agent.wait_for("element", role="button", name="Copy")                       # present (visible anywhere in the document)
agent.wait_for("element", role="button", name="Stop generating", present=False)
agent.wait_for("element", role="checkbox", name=re.compile("^Email"), state={"checked": True})
agent.wait_for("text", "Report ready")                                       # or a compiled regex; present=False waits for it to go
agent.wait_for("text_stable", quiet_ms=500, role="textbox", name="Answer")   # the text of that element (or the whole page without role/name) is unchanged
```

`role` is one of the roles the snapshot understands (`button`, `link`, `checkbox`, `textbox`, ...); `name` is the accessible name, exact or a compiled regex; `state` is a name, a list of names, or `{name: bool}` from `checked`, `expanded`, `selected`, `pressed`, `disabled`. The same role and name code serves the snapshot and the waits (`jev_ultrafast/a11y.js`). A regex is evaluated by the page's JavaScript `RegExp`: only the `re.IGNORECASE`, `re.DOTALL` and `re.MULTILINE` flags are carried over, and Python-only syntax (`(?P<name>...)`, `\A`, `\Z`, inline flags such as `(?i)`, possessive quantifiers, atomic groups, conditionals, comments) raises `ValueError` up front. A bug in a wait inside the page raises `ValueError` with the page's message instead of spinning until the timeout. `state` reads ARIA attributes (and the native `checked`/`disabled` state), so a native `<details>`/`<summary>` is never `expanded`; give it `aria-expanded`. Python `\d`, `\w`, `\s`, `\b` match Unicode and `$` also matches before a trailing newline, while the page's JavaScript `RegExp` is ASCII-only there and strict: write the pattern for JavaScript. `text_stable` on an element asks for some text by default (`require_text=True`), so an element that has not started to fill is not "stable"; the whole-page form does not (`require_text=False`).

Knobs on `Agent` (and `Browser`):

| Argument | Default | Meaning |
| --- | --- | --- |
| `ignore_urls` | `()` | Substrings or compiled regexes, matched against the absolute URL, for long-lived `fetch`/`XMLHttpRequest` requests (long polls, beacons) that no wait and no post-action trigger counts. Callers can add more per call. |
| `wait_timer_ms` | `2000` | A one-shot `setTimeout` of at most this long that is pending when a wait begins holds it until it fires. Timers armed later (a polling chain re-arming itself) never do. `0` turns timer counting off. |
| `body_cap_ms` | `30000` | The longest a `fetch` keeps counting as in flight after its response headers arrived. |
| `wait_ignore_attributes` | `()` | Attribute names (for example `("style",)`) whose changes are not DOM activity, for pages that rewrite them every frame. |
| `wait_interactive_grace_ms` | `2000` | How long a document may stay `interactive` (a hung image, script or beacon keeps it from `complete`) with nothing counted in flight before it counts as loaded. |
| `new_window` | `None` | See "Keep the owned tab rendering". |

How activity is counted. browser-harness delivers CDP events only through one shared, destructive buffer of 500 events for all sessions (a drain by anyone else empties it), so Jev does not use `Network.*` or `Page.*` events. Instead a small tracker (`waits.js`) is installed with `Page.addScriptToEvaluateOnNewDocument` before the page's own scripts run. It never changes what the page sees: `fetch`, `XMLHttpRequest.open/send`, `setTimeout`, `clearTimeout` and `clearInterval` are `Proxy` objects that forward every call, argument, `this` and return value untouched (`fetch.name` is still `fetch`, `fetch(...)` returns a promise derived from the browser's own, which settles one microtask later with the same response or the same error and stays unhandled exactly when the page leaves it unhandled, so `unhandledrejection` still fires once; the response itself is never cloned, teed or wrapped, so cancellation reaches the server and a slow reader still applies backpressure).

- A `fetch` counts from the call until its promise rejects, the page aborts it through its `AbortSignal`, or the browser reports its `PerformanceObserver` resource entry (paired by absolute URL, oldest first). Measured on Chrome: that entry arrives when the body is read to the end or cancelled, and not at all for a body nobody reads. So a response whose body has not been requested 500 ms after its headers (`response.bodyUsed` is still false) stops counting, and `body_cap_ms` after the headers is the hard cap for a body that is read slowly or never finished. A body the page starts reading late can therefore be missed by `network_idle`; `dom_quiet` and `text_stable` cover what it renders.
- An asynchronous `XMLHttpRequest` counts until `loadend`, until the page `open()`s the same object again (which abandons the request without any event), or `body_cap_ms` after `send`. Synchronous XHR, WebSockets and `EventSource` are never counted.
- Timers: see `wait_timer_ms`. `setInterval` is not counted. The threshold is a boundary, not a judgement: a toast that auto-hides in 1.5 s holds a wait for up to 1.5 s, and a page that redirects after 3 s is not waited for; change `wait_timer_ms` for such pages.
- DOM mutations come from one `MutationObserver` (child list, character data and attributes by name).

Limits: only the main frame's own requests are seen (not iframes, workers or service workers, nor images, scripts and styles, which `document.readyState` covers); a page that polls with `fetch` needs that URL in `ignore_urls`; a page that animates through `requestAnimationFrame` and rewrites DOM attributes every frame never gets a quiet DOM unless those attributes are listed in `wait_ignore_attributes` (measured on a `style` animation in CI: 700 ms of `dom_quiet` timeout versus met at once with `style` ignored; the default stays empty because a script that shows or hides content through `style` alone is a real change); and `element`, `text` and `text_stable` scan the document, so they poll at four times what one check costs (50 to 250 ms) rather than every 15 ms.

### Budgets and policy

Bound a run and veto actions without touching the agent's internals:

```python
from jev_ultrafast import ActionDenied, Agent

def no_deletes(action, page, text):
    if "delete" in action["label"].lower():
        raise ActionDenied("Delete controls are off limits")

with Agent(url, goal, max_steps=20, max_model_calls=40, before_action=no_deletes) as agent:
    for state in agent.run():
        pass
print(state["status"], state["stop_reason"], state["denied"])
```

`max_steps` bounds executed actions. `max_model_calls` bounds TypeSafe decisions plus text-helper calls (default `2 * max_steps`) and is checked before each call. `max_steps` is checked when an action would execute, so the decision that discovers the cap still counts as a model call. Reaching either ends the run with status `budget` and `state["stop_reason"]` (`max_steps` or `max_model_calls`) instead of an exception. `settle_ms` (default 50) and `autocomplete_settle_ms` (default 200, both may be 0) cap the wait after an action, and `stuck_after` (default 3) unchanged steps end the run as `blocked` (see Waits for which waits count). `before_action(action, page, text)` runs before each execution attempt, with the final typed text and copies of `action` and `page`. It can run more than once for one intended action if the page goes stale first, so keep it free of side effects or make it idempotent. A veto always prevents execution: raising `ActionDenied` executes nothing, records no history entry and ends the run as `denied`. Any other exception from the hook propagates.

### Steps and checks

A goal can be a list of steps, and a step can carry checks that the code evaluates, so DONE is not just the model's word:

```python
from jev_ultrafast import Agent, Step, Value, expect

steps = [
    Step("Search for the term", expect=[expect.text("Results for cats")]),
    Step("Open the first result", expect=[expect.url("/cats"), expect.text("Cat facts")]),
    Step(
        "Mark it as a favourite",
        expect=[expect.element(role="button", name="Favourite", state={"pressed": True})],
    ),
]
with Agent(url, steps, values={"term": "cats"}) as agent:
    for state in agent.run():
        pass
print(state["status"], state["verified"], [s["status"] for s in state["steps"]])
```

- **Steps.** `Agent(url, goals)` takes a string, or a list of strings and `Step(goal, expect=(), values=None)`; each item is one step. The model sees only the current step's goal, the goals already completed and the descriptions of the current step's checks, never the later steps. `state["steps"]` records, per step, `goal`, `status` (`pending`, `active`, `done`, `failed` or `blocked`), `checks` (description, `met`, `reason`, `waited_ms`), `started_ms`, `ended_ms`, `model_calls`, `actions` and `rejections`; `state["plan_index"]` is the current step. A plain string is one step with no checks and behaves as before; a list of strings is now one step per string (it used to be joined into one goal; to get the old behaviour pass `"\n".join(goals)` as one string - the measurements in `docs/` were taken that way, and `examples/flights.py` and `examples/run.py --goal` now run one step per goal).
- **Checks** are `expect.text(contains)` / `expect.no_text(contains)` (a string or a JavaScript-compatible compiled regex), `expect.element(role, name, state)` / `expect.no_element(...)` (the `element` wait), `expect.url(pattern)` (a substring, or a compiled regex that is searched), and `expect.check(fn, description, auto=False)` for a callable `fn(page) -> bool | (bool, reason)`, where `page` is a read-only facade (`evaluate`, `url`, `probe`, `element`, `text`; no `act`, `press`, `goto` or `close`). Each has a description that the model sees and the state records. Each takes `timeout_ms=` to override `expect_timeout_ms` (default 5000), the longest it waits for the page to catch up, so "the upvote is pressed" can wait the few hundred milliseconds the UI needs. A check never raises on timeout, and a callable that raises is recorded as unmet with its (redacted) message. Checks are page-global: a step whose action navigates away is checked against the page that is open afterwards, so pair it with `expect.url`. A negative check (`no_text`, `no_element`) holds on a blank page and on a dialog that never opened, and a step whose checks already hold when it starts completes after its first action; give such a step a positive check for the state you want. **A check callable must be read-only, idempotent and fast.** It is never interrupted, it is polled every 50 ms until it holds or its timeout passes (a DONE can run it dozens of times), `evaluate` still runs whatever JavaScript you give it, and the worst case of one DONE is `len(checks) * timeout` plus the settle wait (`expect_timeout_ms`) plus one more read, up to `max_rejections` times per step. A callable is not read after every action unless you pass `auto=True`, and a step with a callable that did not opt in never completes without a DONE decision. A coroutine function is refused (`async def`).
- **DONE is verified.** On a step with checks, DONE is accepted only when all hold, and again once the page has settled (`Browser.settled`, at most `expect_timeout_ms`): every check is read a second time, so a press that an optimistic UI applies and then takes back does not count. If either read fails, DONE is rejected: the step does not change, the history gets an entry with `kind: "done_rejected"` and the failed descriptions, and the model sees it as a recent action (`DONE rejected: element button "Favourite" pressed=true not met`). A rejection costs a model call, is not an action (`max_steps`, `stuck_after`), and `max_rejections` (default 3) rejections on one step end the run as `failed` with `stop_reason: "expectations"`. A step without checks accepts DONE as before and is recorded with `verified: false`.
- **Auto-complete.** After every executed action, the current step's checks are read once without waiting; if they all hold, the page is settled and every check is read again, and only then does the step complete with no DONE decision, so the run does not depend on the model noticing it is finished. If the second read fails, that counts as a rejected DONE (recorded, shown to the model, toward `max_rejections`). An unmet built-in check costs one browser round trip (about a millisecond, measured with `expect.element`); a callable costs whatever it does, which is why it only takes part with `auto=True`. `auto_complete=False` turns it off.
- **Verified runs.** `state["verified"]` is `True` only when every step had checks and they held twice, across a settle, at completion. `require_expectations=True` makes a step without checks a `ValueError` at construction, for test runners that never want an unverified pass. `failed` is a terminal status next to `done`, `blocked`, `denied` and `budget`.
- **Values.** `values={name: str | Value(text, secret=False)}` on the `Agent` (every step) or a `Step` (that step, overriding) are strings the caller already knows: a search term, a code. `TYPE_TEXT` then has a value choice among the declared names, plus `GENERATE` when a text helper is configured. Choosing a name types that exact string with no text-model call; the model chooses by name and sees the text only where the page or the history shows it (the typed text appears in the field and in the recent actions), unless the value is secret. With values and no text helper, `TYPE_TEXT` also offers `NONE` ("no declared value fits"): choosing it types nothing, records a refusal for that field (hidden from the next choice while the page is unchanged, counted toward `stuck_after`), and `NONE` and `GENERATE` are reserved names. The history records `text_source` (`value:<name>` or `generated`) and `value_name`. A `Value(..., secret=True)` (at least 4 characters; a short one replaces every occurrence, so "1234" also changes "$1234.00" in what the model reads) is shown as `<secret:name>` in the history, in `state`, in the page the model reads (the copy of the observation that the agent keeps replaces the text wherever the page echoes it, for example in the field's own value or in a confirmation message), and as the `text` argument of `before_action`, which also gets the name as `action["value_name"]`; only the browser receives the real text. The observation that the agent keeps is scrubbed before anything else sees it (also the first one), the exposed fingerprint is computed from the scrubbed page, check reasons, wait results and text-call records are scrubbed, generated text that contains a `<secret:` marker is refused, and a goal or check description that contains a declared secret is a `ValueError` at construction. The scrub covers the text as typed and its percent-encoded, plus-encoded, JSON-escaped and HTML-escaped forms. Limits: it is exact and case-sensitive, so a page that upper-cases, re-spaces or shortens the secret (the page text is cut at 6000 characters, so a secret straddling the cut leaks its prefix) is not caught; password inputs are not offered as targets at all (the snapshot skips them), so a secret goes to an ordinary field; screenshots (`screenshots=True`) are images of the page and are not redacted; a check callable and `expect.text` see the real page.

## Why it moves

- **One request per decision cycle.** Operation and target heads share the same observed state.
- **No screenshots in the default agent loop.** Jev consumes structured state. The inspector opts into screenshots; the video uses a separate continuous screencast.
- **One browser call per snapshot.** Read visible controls, their names, values, and text atomically. Keep references to the actual DOM nodes.
- **Validate the selected target.** Clicks check the document, form values, target, and nearby context. Animation alone does not force another prediction. Resolve current geometry and reject covered controls before input.
- **Wait for useful state.** After typing into a combobox, wait for visible suggestions, capped at 200 ms. Other interactions get at most two animation frames or 50 ms. These reads happen after execution is logged.
- **Keep the owned tab rendering.** The agent drives a tab opened in the background, with focus emulation. In headless Chrome a background tab inside the existing window is throttled to about one frame per second after its first clicks, and every pointer move then waits about 1 s for the next frame (measured in CI: a click step went from 1033 ms to 50 ms median). So `new_window=None` (the default) opens the tab in a window of its own when its user agent says `HeadlessChrome` (its product string says plain `Chrome`), and in a tab of the current window otherwise; `True` and `False` force either. Headed Chrome was not measured (a new window there is a visible one, and CDP does not say whether it takes focus), and no CI job covers it.
- **Send visible text.** Offscreen article bodies and footers do not fill the model context.
- **Reuse an interrupted text request.** A generated value survives a stale-page retry only if the entire text-helper input is unchanged.

Every executed target is resolved from an observed node. The executor rechecks page freshness and click occlusion. Model output never becomes selectors, coordinates, shell commands, or executable JavaScript. Text-helper output must parse as a small JSON object before typing.

## Small enough to read

| File | Job |
| --- | --- |
| [agent.py](jev_ultrafast/agent.py) | The complete loop and text-helper handoff |
| [snapshot.js](jev_ultrafast/snapshot.js) | Atomic DOM snapshot, indexed controls, freshness guards |
| [browser.py](jev_ultrafast/browser.py) | Browser connection, current geometry, execution |
| [model.py](jev_ultrafast/model.py) | Dynamic operation/target heads and text generation |
| [questions.py](jev_ultrafast/questions.py) | Model instructions |
| [demo.py](jev_ultrafast/demo.py) | Local inspector |

## Evidence and limits

The current video is a **7,073 ms** Google Flights run. Timing starts after initial page observation and includes model calls, generated text, browser work, stale decisions, and loading waits. A fresh independent check verifies the one-way setting, Zürich, London, September 20, 2026, and visible flight options. The video plays at 1×, with no opening hold and a 0.5-second final hold.

In six alternating runs with identical models and settings, both versions passed **3/3**. Median task time went from **9.450 s → 7.092 s**, a **25% reduction**; median browser protocol calls went from **1,092 → 101**. This is three repeats of one task on one browser profile, not a general reliability benchmark.

The same policy opened the requested Wikipedia article in **2.798 s** and passed a local hotel search/filter task in **1.896 s**. Runs, failures, source hashes, and measurement boundaries are in [performance.md](docs/performance.md).

A `DONE` choice still requires independent outcome verification. The DOM reader handles common HTML and ARIA controls, not the full accessible-name specification. Controls hidden by `display`, `visibility`, `aria-hidden`, or `inert` stay unlisted until something else (for example `HOVER` on a parent) shows them. A hover menu whose parent carries none of the hover signals above (for example a bare `<div>` with a CSS `:hover` rule) is not offered `HOVER`, so give such a parent `aria-haspopup`. Shadow roots, frames, canvas, uploads, pop-up tabs, and nested scrolling remain outside this MVP; keyboard support is the fixed key set and the shortcuts you declare. Owned tabs share the existing Chrome profile.

## Development

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
node --check jev_ultrafast/a11y.js
node --check jev_ultrafast/waits.js
uv build
```

Tests are offline. `BU_CDP_URL=http://127.0.0.1:9222 uv run pytest -m browser` runs the real-browser fixture tests (`tests/browser/`, pages in `tests/fixtures/`) against a Chrome started with `--remote-debugging-port=9222`; CI runs them in headless Chrome, and without `BU_CDP_URL` they skip. `uv run python scripts/check_guards.py` is the standalone version of the guard checks. None of them make model calls. Live examples and recording scripts make paid API calls. `scripts/record_flights.py <new-folder>` captures original browser timestamps; `scripts/render_demo.py <recording-folder>` renders that verified run at 1× and crops out the Google account strip. Credentials and raw traces stay ignored.

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness) · [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)
