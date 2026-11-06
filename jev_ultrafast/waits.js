(() => {
  // In-page activity tracker and condition checks. Installed on every new document (and evaluated once on the current
  // one), so it sees the page's own requests and timers from the start. It never changes what the page receives:
  // fetch, XMLHttpRequest and the timer functions are Proxies that forward every call unchanged and only note it.
  if (window.__jevWait) return window.__jevWait;
  if (window.parent !== window) return null; // The main frame only.
  const {visible, name, selector, role} = __A11Y__;
  const BODY_CAP_MS = __BODY_CAP_MS__, UNREAD_BODY_MS = 500, MAX_ENDS = 500, MAX_STARTS = 500;
  const nativeSet = window.setTimeout.bind(window), nativeClear = window.clearTimeout.bind(window);
  const now = () => performance.now();
  const w = {epoch: Math.random().toString(36).slice(2), inflight: new Map(), ends: [], starts: [], timers: new Map(),
    holds: new Map(), stable: new Map(), lastOther: now(), attrLast: new Map(), interactiveAt: now(),
    stats: {entry: 0, cap: 0, unread: 0, rejected: 0, abort: 0, loadend: 0}};
  let seq = 0;

  // A request is in flight from the call until (1) its promise rejects or the page aborts it, (2) the browser reports
  // its resource-timing entry (which Chrome does when the body was read to the end or cancelled), (3) XHR fires
  // loadend, (4) the response arrived but the page has not started to read the body UNREAD_BODY_MS later (Chrome
  // reports no entry for a body nobody reads), or (5) body_cap_ms after the response headers arrived.
  // Only http(s) requests are counted: data: and blob: URLs touch no network and Chrome reports no entry for them.
  const begin = (url, kind) => {
    if (!/^https?:/i.test(url)) return 0;
    const id = ++seq;
    w.inflight.set(id, {url, kind, timers: []});
    w.starts.push([id, url]);
    if (w.starts.length > MAX_STARTS) w.starts.shift();
    return id;
  };
  const end = (id, how) => {
    const entry = w.inflight.get(id);
    if (!entry) return;
    entry.timers.forEach(nativeClear);
    entry.cleanup?.();
    w.stats[how] = (w.stats[how] || 0) + 1;
    w.ends.push([entry.url, now()]);
    if (w.ends.length > MAX_ENDS) w.ends.shift();
    w.inflight.delete(id);
  };
  const absolute = value => {
    // Relative URLs resolve against the document base URL (<base href> included), as the browser does.
    try { const url = new URL(String(value), document.baseURI); url.hash = ''; return url.href; } catch { return 'invalid:'; }
  };
  const wrap = (target, apply) => new Proxy(target, {apply});

  // DOM mutations (child list, character data, attributes) anywhere in the document; attributes by name, so a caller
  // can ignore the ones a script rewrites every frame.
  new MutationObserver(records => {
    const at = now();
    for (const record of records) {
      if (record.type === 'attributes') w.attrLast.set(record.attributeName, at);
      else w.lastOther = at;
    }
  }).observe(document, {subtree: true, childList: true, characterData: true, attributes: true});
  document.addEventListener('readystatechange', () => {
    if (document.readyState === 'interactive') w.interactiveAt = now();
  });

  // The page's own fetch result is returned untouched: no clone, no tee, no wrapped body. The counter only notes the
  // call, and the browser's resource-timing entries say when a request finished (see the PerformanceObserver below).
  window.fetch = wrap(window.fetch, (target, self, args) => {
    const [input, init] = args;
    const id = begin(absolute(input instanceof Request ? input.url : input), 'fetch');
    let promise;
    try { promise = Reflect.apply(target, self, args); } catch (error) { end(id, 'rejected'); throw error; }
    let signal = null;
    try { signal = init?.signal ?? (input instanceof Request ? input.signal : null); } catch { /* the browser reports it */ }
    const entry = w.inflight.get(id);
    if (entry && typeof signal?.addEventListener === 'function') {
      const onAbort = () => end(id, 'abort');
      signal.addEventListener('abort', onAbort, {once: true});
      entry.cleanup = () => signal.removeEventListener('abort', onAbort);
    }
    // A derived promise: it stays unhandled exactly when the page leaves its own unhandled (the page never sees the
    // native promise, so attaching a handler to it here must not mark anything handled). It settles one microtask
    // after the native one.
    return promise.then(response => {
      const open = w.inflight.get(id);
      if (open) {
        open.timers.push(nativeSet(() => end(id, 'cap'), BODY_CAP_MS));
        // bodyUsed is a plain getter: it says whether a read was requested, without touching the stream.
        open.timers.push(nativeSet(() => { if (!response.bodyUsed) end(id, 'unread'); }, Math.min(BODY_CAP_MS, UNREAD_BODY_MS)));
      }
      return response;
    }, error => { end(id, 'rejected'); throw error; });
  });

  // XMLHttpRequest (asynchronous): in flight until loadend.
  const meta = new WeakMap(), xhr = XMLHttpRequest.prototype;
  xhr.open = wrap(xhr.open, (target, self, args) => {
    // open() abandons a request still in flight on this object without any event: end it here.
    const previous = meta.get(self);
    if (previous?.id) end(previous.id, 'abort');
    meta.set(self, {url: absolute(args[1]), async: args[2] !== false, id: 0});
    return Reflect.apply(target, self, args);
  });
  xhr.send = wrap(xhr.send, (target, self, args) => {
    const info = meta.get(self);
    if (!info?.async) return Reflect.apply(target, self, args);
    const id = begin(info.url, 'xmlhttprequest');
    info.id = id;
    const open = w.inflight.get(id);
    if (open) open.timers.push(nativeSet(() => end(id, 'cap'), BODY_CAP_MS));
    self.addEventListener('loadend', () => end(id, 'loadend'), {once: true});
    try { return Reflect.apply(target, self, args); } catch (error) { end(id, 'rejected'); throw error; }
  });

  // Resource timing: an entry for a fetch or XHR means the browser finished that request (body included, whether or
  // not the page read it). Paired with the oldest started request of the same kind and absolute URL.
  try {
    new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        if (entry.initiatorType !== 'fetch' && entry.initiatorType !== 'xmlhttprequest') continue;
        const url = absolute(entry.name);
        for (const [id, open] of w.inflight) {
          if (open.kind === entry.initiatorType && open.url === url) { end(id, 'entry'); break; }
        }
      }
    }).observe({type: 'resource', buffered: false});
  } catch { /* the hard cap releases a body that is never reported */ }

  // Timers: every setTimeout with a callback is noted (delay and order), nothing else changes. A wait only holds for
  // short timers that were already pending when it began (see `hold`), so a polling chain cannot extend it.
  window.setTimeout = wrap(window.setTimeout, (target, self, args) => {
    const [callback, delay, ...rest] = args;
    if (typeof callback !== 'function') return Reflect.apply(target, self, args);
    const wait = Number(delay) || 0;
    let id;
    const wrapped = function () { w.timers.delete(id); return callback.apply(this, arguments); };
    id = Reflect.apply(target, self, [wrapped, wait, ...rest]);
    w.timers.set(id, {delay: Math.max(0, Number(delay) >> 0), seq: ++seq}); // 32-bit, like the browser
    return id;
  });
  for (const method of ['clearTimeout', 'clearInterval']) {
    window[method] = wrap(window[method], (target, self, args) => { w.timers.delete(args[0]); return Reflect.apply(target, self, args); });
  }

  const matcher = pattern => {
    if (pattern == null) return () => true;
    if (pattern.r !== undefined) { const re = new RegExp(pattern.r, pattern.f || ''); return text => re.test(text); }
    return text => text === pattern.s;
  };
  const contains = pattern => {
    if (pattern.r !== undefined) { const re = new RegExp(pattern.r, pattern.f || ''); return text => re.test(text); }
    return text => text.includes(pattern.s);
  };
  const ignored = spec => {
    const tests = (spec?.ignore || []).map(p => p.r !== undefined ? (re => u => re.test(u))(new RegExp(p.r, p.f || ''))
      : u => u.includes(p.s));
    return url => tests.some(test => test(url));
  };
  const stateOf = (e, key) => {
    if (key === 'disabled') return e.matches(':disabled') || e.closest('[aria-disabled="true"]') !== null;
    if (key === 'checked' && ['checkbox', 'radio'].includes(e.type)) return e.checked;
    return e.getAttribute('aria-' + key) === 'true';
  };
  const find = spec => {
    const nameOk = matcher(spec.name);
    for (const e of document.querySelectorAll(selector)) {
      const r = role(e);
      if (!r || (spec.role && r !== spec.role) || !visible(e)) continue;
      if (!nameOk(name(e).replace(/\s+/g, ' ').trim())) continue;
      if (Object.entries(spec.state || {}).every(([key, want]) => stateOf(e, key) === want)) return e;
    }
    return null;
  };
  const pageText = () => document.body?.innerText ?? '';

  const counted = spec => { const skip = ignored(spec); return [...w.inflight.values()].filter(open => !skip(open.url)); };
  // Pending timers of at most timer_ms (0 turns timer counting off) armed after `since`, in the epoch it belongs to.
  const shortTimers = (spec, since) => spec.timer_ms > 0
    ? [...w.timers].filter(([, t]) => t.delay <= spec.timer_ms && t.seq > since).map(([id]) => id) : [];
  const lastDom = ignore => {
    let last = w.lastOther;
    for (const [attribute, at] of w.attrLast) if (!(ignore || []).includes(attribute) && at > last) last = at;
    return last;
  };
  const loaded = spec => document.readyState === 'complete' ||
    (document.readyState === 'interactive' && now() - w.interactiveAt >= spec.interactive_grace_ms && !counted(spec).length);

  const checks = {
    dom_quiet: spec => now() - lastDom(spec.ignore_attributes) >= spec.quiet_ms ? null : 'the DOM is still changing',
    network_idle(spec) {
      const open = counted(spec).length;
      if (open) return open + ' request(s) in flight';
      const skip = ignored(spec);
      const last = Math.max(-Infinity, ...w.ends.filter(([url]) => !skip(url)).map(([, at]) => at));
      return now() - last >= spec.idle_ms ? null : 'a request finished less than ' + spec.idle_ms + ' ms ago';
    },
    settled(spec) {
      if (!loaded(spec)) return 'the document is still loading';
      const held = (w.holds.get(spec.key) || []).filter(id => w.timers.has(id)).length;
      if (held) return held + ' short timer(s) pending';
      return checks.network_idle({...spec, idle_ms: spec.quiet_ms}) || checks.dom_quiet(spec);
    },
    element(spec) {
      const found = find(spec) !== null;
      return found === spec.present ? null : spec.present ? 'no matching element' : 'a matching element is still there';
    },
    text(spec) {
      const found = contains(spec.contains)(pageText());
      return found === spec.present ? null : spec.present ? 'the text is not on the page' : 'the text is still on the page';
    },
    text_stable(spec) {
      let text;
      if (spec.role || spec.name) { const e = find(spec); text = e ? e.innerText ?? e.textContent : null; }
      else text = pageText();
      if (text === null) return 'no matching element';
      if (spec.require_text && !text.trim()) return 'the text has not started';
      const seen = w.stable.get(spec.key);
      if (!seen || seen.text !== text) {
        w.stable.set(spec.key, {text, since: now()});
        if (w.stable.size > 20) w.stable.delete(w.stable.keys().next().value);
        return 'the text is still changing';
      }
      return now() - seen.since >= spec.quiet_ms ? null : 'the text is still changing';
    },
  };
  // element, text and text_stable scan the document: poll them at four times what one check costs (50 to 250 ms), so
  // waiting does not compete with the page being waited on. The others are O(1) and polled every 15 ms.
  const HEAVY = new Set(['element', 'text', 'text_stable']);

  w.timeout = nativeSet;
  // Counters for the post-action trigger: counted requests in flight, requests started after `since` (same epoch),
  // and short timers armed after it. The page-side filter keeps the ignore list in one place.
  w.activity = spec => {
    const skip = ignored(spec);
    const since = spec.epoch === w.epoch ? spec.since : seq;
    return {epoch: w.epoch, seq, inflight: counted(spec).length,
      new_started: w.starts.filter(([id, url]) => id > since && !skip(url)).length,
      timers: shortTimers(spec, since).length};
  };
  // Where the tracker stands right now: the post-action trigger counts only what starts after this point.
  w.mark = () => ({epoch: w.epoch, seq});
  w.document = () => [performance.timeOrigin, location.href];
  // One slice of a wait: resolves when the condition holds, or after slice_ms with the reason it does not. The
  // first slice of a wait fixes which timers can hold it.
  w.run = spec => new Promise((resolve, reject) => {
    try {
      if (spec.kind === 'settled' && !w.holds.has(spec.key)) {
        w.holds.set(spec.key, shortTimers(spec, spec.epoch === w.epoch ? spec.timers_since ?? 0 : 0));
        if (w.holds.size > 20) w.holds.delete(w.holds.keys().next().value);
      }
      const deadline = now() + spec.slice_ms;
      const tick = () => {
        try {
          const began = now();
          const unmet = checks[spec.kind](spec);
          if (unmet === null) {
            w.stable.delete(spec.key); w.holds.delete(spec.key);
            resolve({met: true, reason: spec.kind, document: w.document()});
          } else if (now() >= deadline) resolve({met: false, reason: unmet, document: w.document()});
          else nativeSet(tick, HEAVY.has(spec.kind) ? Math.min(250, Math.max(50, 4 * (now() - began))) : 15);
        } catch (error) { reject(error); }
      };
      tick();
    } catch (error) { reject(error); }
  });
  return window.__jevWait = w;
})()
