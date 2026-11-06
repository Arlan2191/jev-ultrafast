(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  // Hidden only by opacity (CSS visibility, display, aria-hidden and inert still count as hidden).
  const shown = e => !e.closest('[aria-hidden="true"],[inert]') && e.checkVisibility({checkVisibilityCSS:true});
  // The host a pointer can be moved onto to reveal e. Only ONE transparent layer may sit between the control and a
  // visible host: the control itself or a close ancestor with opacity 0 whose parent passes the full check. A closed
  // opacity-0 dialog, nav or toast is not a hover reveal, so a candidate is dropped when the transparent layer (or
  // anything between it and the control) is fixed or a dialog or popover, or the layer alone covers over a quarter
  // of the viewport. The host itself may be any size: a message that hosts a toolbar is often tall.
  const overlay = n => getComputedStyle(n).position==='fixed' || n.tagName==='DIALOG' ||
    ['dialog','alertdialog'].includes(n.getAttribute('role')) || n.getAttribute('aria-modal')==='true' ||
    n.hasAttribute('popover');
  const revealHost = e => {
    let layer=e;
    while (layer && getComputedStyle(layer).opacity!=='0') layer=layer.parentElement;
    if (!layer) return null;
    for (let n=e; ; n=n.parentElement) {
      if (overlay(n)) return null;
      if (n===layer) break;
    }
    const lr=layer.getBoundingClientRect();
    if (lr.width*lr.height > 0.25*innerWidth*innerHeight) return null;
    const p=layer.parentElement;
    if (!p || p===document.body || p===document.documentElement || !visible(p)) return null;
    const r=p.getBoundingClientRect();
    return r.width>0 && r.height>0 ? p : null;
  };
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
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  // "scoped" adds the enclosing scope's text. That text depends on :hover (display:none and visibility:hidden text
  // is left out of innerText), so the press-time compare after the pointer move omits it.
  cache.guard=(e,scoped=true)=>{
    // Opacity is not part of the identity: a hover-revealed control must keep its guard while it fades in.
    if (!e?.isConnected || !shown(e)) return null;
    const guard=[identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('aria-pressed'),e.getAttribute('href')];
    if (!scoped) return guard;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [...guard,scope?.innerText?.slice(0,6000)||''];
  };
  // The element that has keyboard focus, if it is not the document itself. A key press is only valid while this
  // identity and guard still match what was observed. A password, file or hidden input is never read: its guard
  // is identity, type and accessible name only, so no value can enter the observation through focus.
  cache.focused=()=>{
    const e=document.activeElement;
    if (!e || e===document.body || e===document.documentElement) return null;
    const guard=safe(e) ? cache.guard(e) : (shown(e) ? [identity(e),e.type,name(e)] : null);
    return {node:identity(e),role:role(e),label:name(e)||e.tagName.toLowerCase(),href:e.getAttribute('href'),
      tag:e.tagName.toLowerCase(),type:e.type??null,guard};
  };
  // Controls that only appear while the pointer is over their row come after every visible control,
  // so they can never push a visible one past the cap.
  const actions=[], revealed=[];
  for (const e of document.querySelectorAll(selector)) {
    if (!safe(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const reveal=!visible(e);
    if (reveal && (e.tagName==='SELECT' || !shown(e) || !revealHost(e))) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    if (reveal) base.revealed_by='hover';
    if (e===document.activeElement) base.focused=true;
    // Only these elements are offered the HOVER operation (a revealed control always is).
    if (e.getAttribute('aria-haspopup') && e.getAttribute('aria-haspopup')!=='false' ||
        e.hasAttribute('aria-expanded') || e.getAttribute('title')?.trim() || e.hasAttribute('aria-describedby'))
      base.hover_signal=true;
    const out=reveal ? revealed : actions;
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        out.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      out.push({...base,kind:editable?'fill':'click',value});
      if (editable) out.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  actions.push(...revealed);
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions,
    focus:cache.focused(),history_length:history.length};
})()
