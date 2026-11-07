(() => {
// Accessible role, name and visibility. Shared by snapshot.js and waits.js (see browser.py).
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  // Hidden only by opacity (CSS visibility, display, aria-hidden and inert still count as hidden).
  const shown = e => !e.closest('[aria-hidden="true"],[inert]') && e.checkVisibility({checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  // A locator for one element: enough to find the same control again on a fresh load of the same page (see
  // trace.jsonl "action" events, and `resolve` below, which does the finding). Nothing here reads a field value.
  // An id counts as generated when it has a delimited all-digit segment (mat-input-3, radix-0, rc_select_0,
  // input-1), a long digit or hex run, or a framework's own marker (:r1:, ember12). `id` is a hint, not the key.
  const unstableId = id => !id || /:[a-z0-9]+:|[«»]|^_r_/i.test(id) || /^ember\d+$/i.test(id) || /^\d+$/.test(id) ||
    /(^|[-_:.])\d+([-_:.]|$)/.test(id) || /[0-9a-f]{8,}/i.test(id) || /\d{5,}/.test(id) || /\s/.test(id);
  const stableId = id => !unstableId(id);
  // A usable anchor: stable-looking and the only element in the document with that id.
  const anchorId = id => stableId(id) && document.querySelectorAll('#'+CSS.escape(id)).length===1;
  const clip = text => (text||'').replace(/\s+/g,' ').trim().slice(0,80);
  const implicit = {DIALOG:'dialog',FORM:'form',ARTICLE:'article',LI:'listitem',TR:'row',NAV:'navigation',
    MAIN:'main',HEADER:'banner',SECTION:'region'};
  const containers = 'dialog,[role="dialog"],[role="alertdialog"],form,article,li,tr,[role="row"],section,' +
    '[role="region"],nav,[role="navigation"],main,[role="main"],header,[role="banner"]';
  // A header is a banner only outside article, section, main and nav.
  const crole = c => c.tagName==='HEADER' && !c.getAttribute('role') && c.parentElement?.closest('article,section,main,nav,aside')
    ? null : (c.getAttribute('role') || implicit[c.tagName]);
  const firstText = c => {
    for (const n of c.childNodes) {
      if (n.nodeType===3 && n.textContent.trim()) return n.textContent;
      if (n.nodeType===1 && !n.matches('script,style,template') && shown(n)) {
        const t=clip(n.innerText); if (t) return t;
      }
    }
    return '';
  };
  // The scope's name: its own label, else for rows and articles their first text (not the whole row: prices,
  // counts and timestamps change), else its first heading.
  const scopeName = c => {
    const given=name(c.hasAttribute('aria-labelledby') || c.hasAttribute('aria-label') ? c : null);
    if (given) return clip(given);
    if (['listitem','row','article'].includes(crole(c))) {
      const h=c.querySelector('h1,h2,h3,h4,h5,h6,[role="heading"]');
      return clip(h?.innerText) || clip(firstText(c));
    }
    return clip(c.querySelector('h1,h2,h3,h4,h5,h6,legend,[role="heading"]')?.innerText);
  };
  // Containers that can be a scope: shown, with a role of their own and a name (unnamed ones are skipped).
  const scopeOf = c => {
    const role=crole(c), nm=role ? scopeName(c) : '';
    return role && nm ? {role, name:nm} : null;
  };
  const sameScope = (c, scope) => { const s=scopeOf(c); return s && s.role===scope.role && s.name===scope.name; };
  const scopesLike = scope => [...document.querySelectorAll(containers)].filter(c => shown(c) && sameScope(c, scope));
  // Elements of a role and (clipped) name below `root`, as the snapshot would offer them: shown ones only.
  // An element without a role (a password or date input) is matched by tag and type instead.
  const mine = (root, rname, ename, tag, type) => [...root.querySelectorAll(selector)]
    .filter(c => shown(c) && role(c)===rname && clip(name(c))===ename &&
      (rname!==null || (c.tagName.toLowerCase()===tag && (c.type ?? null)===type)));
  const textOf = e => e.isContentEditable || e.tagName==='SELECT' || e.tagName==='TEXTAREA' ? '' :
    clip(e.innerText || e.textContent);
  const cssPath = e => {
    const path = [];
    for (let n=e; n && n!==document.body && n!==document.documentElement; n=n.parentElement) {
      if (n!==e && anchorId(n.id)) { path.unshift(n.tagName.toLowerCase()+'#'+CSS.escape(n.id)); break; }
      const same=[...(n.parentElement?.children||[])].filter(c => c.tagName===n.tagName);
      path.unshift(n.tagName.toLowerCase()+':nth-of-type('+(same.indexOf(n)+1)+')');
    }
    if (!path[0]?.includes('#')) path.unshift('body');
    return path.join(' > ');
  };
  // Origin-free URL for a link: path + query, each query value, each ;path parameter value and the fragment
  // replaced by <redacted> unless `full` (keys are what a debugger needs; values are where tokens live). A link
  // that is not http(s) (javascript:, data:, mailto:, tel:) is its scheme only: the rest is a payload or an address.
  // Clipped to 2048 characters like every URL the run writes.
  const linkOf = (e, full) => {
    if (e.tagName!=='A' || e.getAttribute('href')===null) return null;
    try {
      const u=new URL(e.href, location.href);
      if (!['http:','https:'].includes(u.protocol)) return full ? u.href.slice(0,2048) : u.protocol;
      if (full) return (u.pathname+u.search).slice(0,2048);
      const path=u.pathname.replace(/;([^=;/]*)=[^;/]*/g, ';$1=<redacted>');
      const query=u.search.slice(1).split('&').filter(Boolean)
        .map(p => p.includes('=') ? p.split('=')[0]+'=<redacted>' : '<redacted>').join('&');
      return (path+(query ? '?'+query : '')+(u.hash.length>1 ? '#<redacted>' : '')).slice(0,2048);
    } catch { return null; }
  };
  const locator = (e, full=false) => {
    // Not in this document (a shadow root or a frame): it could never be found again by `resolve`.
    if (!e || !e.isConnected || !document.contains(e)) return null;
    let scope = null, container = null;
    for (let c=e.parentElement?.closest(containers); c; c=c.parentElement?.closest(containers)) {
      const found=scopeOf(c);
      if (found) { container=c; scope=found; break; }
    }
    const rname=role(e), ename=clip(name(e));
    const tag=e.tagName.toLowerCase(), type=e.type ?? null;
    const inDocument = mine(document, rname, ename, tag, type);
    const inScope = container ? mine(container, rname, ename, tag, type) : inDocument;
    if (container) {
      const like=scopesLike(scope);
      scope={...scope, ordinal:like.indexOf(container), count:like.length, css_path:cssPath(container)};
    }
    const label = e.labels?.length ? clip(name(e.labels[0])) : null;
    const attr = ['data-testid','data-test','data-qa','data-cy'].map(a => e.getAttribute(a)).find(Boolean) ?? null;
    const r=e.getBoundingClientRect();
    return {
      role: rname, name: ename, tag, type,
      href: linkOf(e, full), label_for: label, placeholder: e.getAttribute('placeholder'), test_id: attr,
      id: anchorId(e.id) ? e.id : null, scope,
      ordinal: {scope: inScope.indexOf(e), scope_count: inScope.length,
        document: inDocument.indexOf(e), document_count: inDocument.length},
      text: textOf(e), css_path: cssPath(e),
      rect: {x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height)},
    };
  };
  // Does the element still look like the one that was recorded: same tag, same type, same role (when recorded) and,
  // for anything that has text, the same clipped text.
  const verifies = (e, loc) => !!e && e.tagName.toLowerCase()===loc.tag && (e.type ?? null)===(loc.type ?? null) &&
    (!loc.role || role(e)===loc.role) && (typeof loc.text!=='string' || textOf(e)===loc.text);
  // The element a recorded locator points at on the page as it is now, or null; never another element.
  // {element, rule, unique, count_match, verified}. The primary rule (role_name_scope) takes the role + name matches
  // inside the scope (itself found by role + name + ordinal, with its count checked) and accepts one only when it is
  // the only match, or when the recorded counts (scope and in-scope) still hold; otherwise it refuses with rule
  // "ambiguous". Then test_id, a unique stable id and css_path, each accepted only if `verifies`.
  const resolve = loc => {
    const none = (rule=null) => ({element:null, rule, unique:false, count_match:false, verified:false});
    if (!loc || !loc.tag) return none();
    if (loc.name !== undefined && loc.ordinal) {
      let root = document, ordinal = loc.ordinal.document, recorded = loc.ordinal.document_count, scopeOk = true;
      if (loc.scope) {
        const like = scopesLike(loc.scope);
        root = like[loc.scope.ordinal ?? 0] ?? null;
        scopeOk = like.length===1 || like.length===loc.scope.count;
        ordinal = loc.ordinal.scope;
        recorded = loc.ordinal.scope_count;
      }
      const sameScopeCount = !loc.scope || (scopesLike(loc.scope).length===loc.scope.count);
      const found = root ? mine(root, loc.role ?? null, loc.name, loc.tag, loc.type ?? null) : [];
      if (found.length) {
        const unique = found.length===1, count_match = sameScopeCount && found.length===recorded;
        const element = found[ordinal];
        if (!scopeOk || !Number.isInteger(ordinal) || !element || !(unique || count_match)) return none('ambiguous');
        if (verifies(element, loc)) return {element, rule:'role_name_scope', unique, count_match, verified:true};
      }
    }
    const fallback = (element, rule, unique) => verifies(element, loc) ?
      {element, rule, unique, count_match:null, verified:true} : null;
    if (loc.test_id) {
      const hits=[...document.querySelectorAll('[data-testid],[data-test],[data-qa],[data-cy]')].filter(
        c => ['data-testid','data-test','data-qa','data-cy'].some(a => c.getAttribute(a)===loc.test_id));
      const hit = hits.length===1 ? fallback(hits[0], 'test_id', true) : null;
      if (hit) return hit;
    }
    if (loc.id && anchorId(loc.id)) {
      const hit = fallback(document.getElementById(loc.id), 'id', true);
      if (hit) return hit;
    }
    if (loc.css_path) {
      try { const hit=fallback(document.querySelector(loc.css_path), 'css_path', null); if (hit) return hit; } catch {}
    }
    return none();
  };
  return {visible, shown, name, roles, selector, role, stableId, locator, resolve};
})()
