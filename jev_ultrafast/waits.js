(() => {
  // In-page activity tracker and condition checks. Installed on every new document (and evaluated once on the current
  // one), so it sees the page's own requests and timers from the start. It never changes what the page receives.
  if (window.__jevWait) return window.__jevWait;
  const {visible, name, selector, role} = __A11Y__;
  const SHORT_TIMER_MS = 2000, MAX_ENDS = 500;
  const nativeSet = window.setTimeout.bind(window), nativeClear = window.clearTimeout.bind(window);
  const now = () => performance.now();
  const w = {epoch: Math.random().toString(36).slice(2), started: 0, inflight: new Map(), ends: [],
    timers: new Set(), lastMutation: now(), stable: new Map()};
  let seq = 0;

  const begin = url => { const id = ++seq; w.inflight.set(id, url); w.started++; return id; };
  const end = id => {
    if (!w.inflight.has(id)) return;
    w.ends.push([w.inflight.get(id), now()]);
    if (w.ends.length > MAX_ENDS) w.ends.shift();
    w.inflight.delete(id);
  };
  const absolute = value => { try { return new URL(String(value), location.href).href; } catch { return String(value); } };

  // DOM mutations (childList, characterData, attributes) anywhere in the document.
  new MutationObserver(() => { w.lastMutation = now(); })
    .observe(document, {subtree: true, childList: true, characterData: true, attributes: true});

  // fetch: in flight until the response body is fully read, or the request fails or aborts. The body is only
  // counted on a clone of the response, so the page receives the original response and data untouched.
  const nativeFetch = window.fetch;
  if (nativeFetch) {
    window.fetch = function (input) {
      const id = begin(absolute(input instanceof Request ? input.url : input));
      let promise;
      try { promise = nativeFetch.apply(this, arguments); } catch (error) { end(id); throw error; }
      return promise.then(response => {
        try {
          if (!response.body) end(id);
          else {
            const reader = response.clone().body.getReader();
            const pump = () => reader.read().then(chunk => chunk.done ? end(id) : pump(), () => end(id));
            pump();
          }
        } catch { end(id); }
        return response;
      }, error => { end(id); throw error; });
    };
  }

  // XMLHttpRequest (asynchronous): in flight until loadend.
  const meta = new WeakMap(), xhr = XMLHttpRequest.prototype;
  const nativeOpen = xhr.open, nativeSend = xhr.send;
  xhr.open = function (method, url, async) {
    meta.set(this, {url: absolute(url), async: async !== false});
    return nativeOpen.apply(this, arguments);
  };
  xhr.send = function () {
    const info = meta.get(this);
    if (!info?.async) return nativeSend.apply(this, arguments);
    const id = begin(info.url);
    this.addEventListener('loadend', () => end(id), {once: true});
    try { return nativeSend.apply(this, arguments); } catch (error) { end(id); throw error; }
  };

  // Timers: a one-shot timer of at most SHORT_TIMER_MS that the page starts outside another timer callback is
  // pending activity (a script that navigates or renders after a short delay). A timer started from inside a timer
  // callback is a polling or animation chain and is never counted, so such a page can still settle.
  let inTimer = 0;
  window.setTimeout = function (callback, delay, ...args) {
    if (typeof callback !== 'function') return nativeSet(callback, delay, ...args);
    const tracked = !inTimer && !(Number(delay) > SHORT_TIMER_MS);
    let id;
    id = nativeSet(function () {
      if (tracked) w.timers.delete(id);
      inTimer++;
      try { return callback.apply(this, arguments); } finally { inTimer--; }
    }, delay, ...args);
    if (tracked) w.timers.add(id);
    return id;
  };
  window.clearTimeout = id => { w.timers.delete(id); nativeClear(id); };

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
    const tests = (spec.ignore || []).map(p => p.r !== undefined ? (re => u => re.test(u))(new RegExp(p.r, p.f || ''))
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

  const checks = {
    dom_quiet: spec => now() - w.lastMutation >= spec.quiet_ms ? null : 'the DOM is still changing',
    network_idle(spec) {
      const skip = ignored(spec);
      const open = [...w.inflight.values()].filter(url => !skip(url)).length;
      if (open) return open + ' request(s) in flight';
      const last = Math.max(0, ...w.ends.filter(([url]) => !skip(url)).map(([, at]) => at));
      return now() - last >= spec.idle_ms ? null : 'a request finished less than ' + spec.idle_ms + ' ms ago';
    },
    settled(spec) {
      if (document.readyState !== 'complete') return 'the document is still loading';
      if (w.timers.size) return w.timers.size + ' short timer(s) pending';
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
      const seen = w.stable.get(spec.key);
      if (!seen || seen.text !== text) {
        w.stable.set(spec.key, {text, since: now()});
        if (w.stable.size > 20) w.stable.delete(w.stable.keys().next().value);
        return 'the text is still changing';
      }
      return now() - seen.since >= spec.quiet_ms ? null : 'the text is still changing';
    },
  };

  w.activity = () => ({epoch: w.epoch, started: w.started, inflight: w.inflight.size, timers: w.timers.size});
  w.document = () => [performance.timeOrigin, location.href];
  // One slice of a wait: resolves when the condition holds, or after slice_ms with the reason it does not.
  w.run = spec => new Promise(resolve => {
    const deadline = now() + spec.slice_ms;
    const tick = () => {
      const unmet = checks[spec.kind](spec);
      if (unmet === null) { w.stable.delete(spec.key); resolve({met: true, reason: spec.kind, document: w.document()}); }
      else if (now() >= deadline) resolve({met: false, reason: unmet, document: w.document()});
      else nativeSet(tick, 15);
    };
    tick();
  });
  return window.__jevWait = w;
})()
