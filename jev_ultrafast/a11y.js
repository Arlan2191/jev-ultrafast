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
  // trace.jsonl "action" events). Nothing here reads a field value.
  const unstableId = id => !id || /:[a-z0-9]+:|[«»]|^_r_/i.test(id) || /^ember\d+$/i.test(id) || /^\d+$/.test(id) ||
    /[0-9a-f]{8,}/i.test(id) || /\d{5,}/.test(id) || /\s/.test(id);
  const stableId = id => !unstableId(id);
  const clip = text => (text||'').replace(/\s+/g,' ').trim().slice(0,80);
  const implicit = {DIALOG:'dialog',FORM:'form',ARTICLE:'article',LI:'listitem',TR:'row',NAV:'navigation',
    MAIN:'main',HEADER:'banner',SECTION:'region'};
  const containers = 'dialog,[role="dialog"],[role="alertdialog"],form,article,li,tr,[role="row"],section,' +
    '[role="region"],nav,[role="navigation"],main,[role="main"],header,[role="banner"]';
  const scopeName = c => {
    const given=name(c.hasAttribute('aria-labelledby') || c.hasAttribute('aria-label') ? c : null);
    if (given) return clip(given);
    if (['listitem','row','article'].includes(c.getAttribute('role') || implicit[c.tagName])) return clip(c.innerText);
    return clip(c.querySelector('h1,h2,h3,h4,h5,h6,legend,[role="heading"]')?.innerText);
  };
  const locator = e => {
    if (!e || !e.isConnected) return null;
    let scope = null, container = null;
    for (let c=e.parentElement?.closest(containers); c; c=c.parentElement?.closest(containers)) {
      const crole=c.getAttribute('role') || implicit[c.tagName], cname=scopeName(c);
      if (crole==='region' && !cname) continue;
      container=c; scope={role:crole,name:cname}; break;
    }
    const mine = (root, rname, ename) => [...root.querySelectorAll(selector)]
      .filter(c => role(c)===rname && name(c)===ename);
    const rname=role(e), ename=name(e);
    const inDocument = mine(document, rname, ename), inScope = container ? mine(container, rname, ename) : inDocument;
    const path = [];
    for (let n=e; n && n!==document.body && n!==document.documentElement; n=n.parentElement) {
      if (n!==e && stableId(n.id)) { path.unshift(n.tagName.toLowerCase()+'#'+CSS.escape(n.id)); break; }
      const same=[...(n.parentElement?.children||[])].filter(c => c.tagName===n.tagName);
      path.unshift(n.tagName.toLowerCase()+':nth-of-type('+(same.indexOf(n)+1)+')');
    }
    if (!path[0]?.includes('#')) path.unshift('body');
    const label = e.labels?.length ? clip(name(e.labels[0])) : null;
    let href = null;
    if (e.tagName==='A' && e.getAttribute('href')!==null) {
      try { const u=new URL(e.href, location.href); href=u.pathname+u.search; } catch { href=null; }
    }
    const attr = ['data-testid','data-test','data-qa','data-cy'].map(a => e.getAttribute(a)).find(Boolean) ?? null;
    const r=e.getBoundingClientRect();
    return {
      role: rname, name: clip(ename), tag: e.tagName.toLowerCase(), type: e.type ?? null,
      href, label_for: label, placeholder: e.getAttribute('placeholder'), test_id: attr,
      id: stableId(e.id) ? e.id : null, scope,
      ordinal: {scope: inScope.indexOf(e), scope_count: inScope.length,
        document: inDocument.indexOf(e), document_count: inDocument.length},
      text: clip(e.innerText || e.textContent), css_path: path.join(' > '),
      rect: {x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height)},
    };
  };
  return {visible, shown, name, roles, selector, role, stableId, locator};
})()
