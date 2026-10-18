// Shared, platform-neutral DOM helpers for read-only page probes.
// browser_client.load_probe wraps these helpers and a platform probe
// (cli/<platform>/probe.js) into one evaluation; the probe calls them as plain
// functions.
//
// locate(e) returns a CSS selector that the extension's actions resolve again
// right before acting (they require exactly one visible match). Selectors are
// built from what the site itself keeps stable — semantic ids, `name`,
// `aria-label`, scoped by a stable ancestor — never from generated ids such as
// `goog_1420216748`. A positional fallback is used only when nothing else is
// unique and is reported as `stable: false`.

const visible = e => {
  if (!e) return false;
  const r=e.getBoundingClientRect(),s=getComputedStyle(e);
  return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
};
const clean = s => String(s||'').replace(/\s+/g,' ').trim();
const label = e => clean(e?.getAttribute('aria-label')||e?.getAttribute('title')||e?.innerText);

// Generated ids: framework counters, long digit runs, hex/uuid blobs.
const stableId = id => !!id && /^[A-Za-z][\w-]*$/.test(id) && !/\d{3,}/.test(id) &&
  !/^(goog_|yt-uix-|ember|react-|radix-|mui-|headlessui-|:r)/i.test(id) && !/[0-9a-f]{8,}/i.test(id);
// Semantic class names (`ytcpAppHeaderCreateIcon`, `upload-button`), not
// framework scope markers or hashed CSS-module names (`css-1x2y3z`, `sc-aBcD`).
const stableClass = c => /^[A-Za-z][A-Za-z_-]{7,}$/.test(c) && !/^(style-scope|ng-|css-|sc-|jsx-|is-|has-)/i.test(c) &&
  !/(active|selected|focused|hover|open|visible|hidden|disabled|loading|first-?load)/i.test(c);
const quote = v => '"'+String(v).replace(/\\/g,'\\\\').replace(/"/g,'\\"')+'"';

// Exactly `e` among the elements the extension would consider (visible ones,
// unless `e` itself is hidden, e.g. a file input).
const uniqueIn = (selector, e, scope=document) => {
  let found;
  try { found=[...scope.querySelectorAll(selector)]; } catch { return false; }
  if (visible(e)) found=found.filter(visible);
  return found.length===1 && found[0]===e;
};

// Language-neutral selectors first (ids, `name`); `aria-label` follows the UI
// language, so it is tried only after stable-ancestor scoping.
const ownSelectors = e => {
  const tag=e.tagName.toLowerCase(), neutral=[], labelled=[];
  if (stableId(e.id)) neutral.push('#'+CSS.escape(e.id), tag+'#'+CSS.escape(e.id));
  const name=e.getAttribute('name');
  if (name && name.length<=80 && !/\d{5,}/.test(name)) neutral.push(`${tag}[name=${quote(name)}]`);
  const aria=e.getAttribute('aria-label');
  if (aria && aria.length<=120) labelled.push(`${tag}[aria-label=${quote(aria)}]`);
  return {neutral,labelled};
};

// Nearest stable ancestor selectors, innermost first.
const ancestorScopes = (e, scopes=[]) => {
  const out=[...scopes];
  for (let p=e.parentElement;p && p!==document.documentElement;p=p.parentElement) {
    if (stableId(p.id)) out.push('#'+CSS.escape(p.id));
    for (const c of [...p.classList].filter(stableClass).slice(0,2)) out.push('.'+CSS.escape(c));
  }
  return out;
};

const positional = e => {
  const parts=[];
  while (e && e.nodeType===1 && e!==document.documentElement) {
    if (stableId(e.id)) { parts.unshift('#'+CSS.escape(e.id)); break; }
    const tag=e.tagName.toLowerCase();
    const same=e.parentElement?[...e.parentElement.children].filter(x=>x.tagName===e.tagName):[e];
    parts.unshift(same.length>1?`${tag}:nth-of-type(${same.indexOf(e)+1})`:tag);
    e=e.parentElement;
  }
  return parts.join(' > ');
};

// scopes: extra stable container selectors to try first (e.g. a dialog tag, or
// a row selector keyed by a business id), outermost meaning broadest.
const locate = (e, scopes=[]) => {
  if (!e) return null;
  const {neutral,labelled}=ownSelectors(e), tag=e.tagName.toLowerCase(), anc=ancestorScopes(e,scopes);
  const tries=[...neutral,
    ...anc.flatMap(scope=>[...neutral.map(s=>scope+' '+s),scope+' '+tag]),
    ...labelled, ...anc.flatMap(scope=>labelled.map(s=>scope+' '+s))];
  for (const s of tries) if (uniqueIn(s,e)) return {selector:s,stable:true};
  return {selector:positional(e),stable:false};
};

const control = (e, scopes=[]) => {
  if (!e) return null;
  const at=locate(e,scopes);
  return {target:at.selector,stable:at.stable,name:label(e),
    enabled:!e.closest('[disabled],[aria-disabled="true"]')&&!e.matches(':disabled')};
};
const first = (root, selectors) => {
  for (const selector of selectors) { const found=[...root.querySelectorAll(selector)].find(visible); if (found) return found; }
  return null;
};
