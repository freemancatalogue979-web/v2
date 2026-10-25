// No dependencies: node tests/test_tab_identity.cjs
//
// "The client.html favicon and page title in streaming mode should match the
// server side accurately, even when it changes."
//
// A streamed visit IS the site, so the browser tab has to say so: the site's
// real title and the site's real icon — and follow both when they change
// ("Inbox" -> "Inbox (3)", an unread-badge icon, a login flow renaming the
// tab). Guessing an icon from the hostname through a favicon service is not
// that: it is a different site's idea of the logo.
//
// This runs the real tab-identity block from client.html in a `vm` against a
// fake outer document and a fake mirrored document, so the rules are pinned:
//
//   * title and icon come from the mirrored document (the capture's own bytes)
//   * a change in that document is followed, and a repeat is a no-op
//   * the server can push both between captures (['meta', title, icon])
//   * LPV / workflow branding always wins — it is never ticked over
//
// The other end — the capture page reporting the change — is checked here too.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const source = fs.readFileSync(path.join(__dirname, '..', 'client.html'), 'utf8');
const py = fs.readFileSync(path.join(__dirname, '..', 'dom_capture.py'), 'utf8');

// ---------------------------------------------------------------------------
// Extract the block
// ---------------------------------------------------------------------------
const startMark = '            // The tab is the page (streamed visits)';
const endMark = '            function updateFavicon(faviconUrl) {';
const start = source.indexOf(startMark);
const end = source.indexOf(endMark, start);
assert.ok(start > 0 && end > start, 'tab identity block not found in client.html');
const block = source.slice(start, end);
ok(/syncMirrorTabMeta/.test(block), 'the block owns the sync function');
ok(/applyMirrorTabFromPage/.test(block), 'the block owns the pushed-meta handler');

// ---------------------------------------------------------------------------
// Fake outer document (client.html itself)
// ---------------------------------------------------------------------------
function makeOuter() {
  const writes = [];
  const links = [];
  const state = { title: 'Loading...' };
  const pageTitleEl = { textContent: 'Loading...' };

  const linkEl = (attrs) => {
    const el = {
      tagName: 'LINK',
      dataset: {},
      _attrs: Object.assign({}, attrs),
      getAttribute(n) { return Object.prototype.hasOwnProperty.call(el._attrs, n) ? el._attrs[n] : null; },
      setAttribute(n, v) { writes.push(n); el._attrs[n] = String(v); },
      removeAttribute(n) { delete el._attrs[n]; },
    };
    Object.defineProperty(el, 'id', {
      get() { return el._attrs.id || ''; },
      set(v) { el._attrs.id = String(v); },
    });
    return el;
  };

  const head = {
    appendChild(child) { links.push(child); return child; },
  };

  // The document starts with the boot favicon, exactly like the real page.
  const boot = linkEl({ rel: 'icon', href: 'data:image/svg+xml,boot' });
  links.push(boot);

  const document = {
    documentElement: { dataset: {} },
    head,
    get title() { return state.title; },
    set title(v) { writes.push('title'); state.title = String(v); },
    createElement(tag) { return linkEl({}); },
    querySelector(sel) {
      if (/link\[rel\*=["']?icon/.test(sel)) return links[0] || null;
      return null;
    },
    getElementById(id) {
      return links.find((l) => l._attrs.id === id) || null;
    },
  };

  return { document, writes, links, state, pageTitleEl };
}

// ---------------------------------------------------------------------------
// Fake mirrored document (the captured site)
// ---------------------------------------------------------------------------
function makeMirror(opts = {}) {
  const links = (opts.links || []).map((l) => ({
    _attrs: l,
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(l, n) ? l[n] : null; },
  }));
  const observed = [];
  const doc = {
    title: opts.title === undefined ? 'Example Bank' : opts.title,
    baseURI: opts.baseURI || 'https://bank.example/account',
    head: {
      querySelectorAll(sel) {
        // "link[href]" — every link in the head that has an href.
        return links.filter((l) => l.getAttribute('href') != null);
      },
    },
    __links: links,
  };
  return { doc, links, observed };
}

function makeSandbox(outer, mirror, extras = {}) {
  const sandbox = {
    document: outer.document,
    window: {},
    URL,
    console,
    String,
    Math,
    parseFloat,
    Image: extras.Image || function () { this.src = ''; },
    MutationObserver: extras.MutationObserver || function (cb) {
      this._cb = cb;
      this.observe = (target) => { (target.__observed = target.__observed || []).push(cb); };
    },
    elements: { pageTitle: outer.pageTitleEl },
    lpvBootBranding: extras.lpvBootBranding || {},
    frame: mirror ? { contentDocument: mirror.doc } : null,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(block, sandbox);
  return sandbox;
}

// ---------------------------------------------------------------------------
// 1. The tab takes the site's title and the site's own icon
// ---------------------------------------------------------------------------
{
  const outer = makeOuter();
  const mirror = makeMirror({
    title: 'Example Bank — Accounts',
    links: [
      { rel: 'stylesheet', href: '/app.css' },
      { rel: 'apple-touch-icon', href: '/apple-180.png', sizes: '180x180' },
      { rel: 'icon', href: '/static/favicon-32.png', sizes: '32x32', type: 'image/png' },
      { rel: 'icon', href: '/static/icon.svg', sizes: 'any', type: 'image/svg+xml' },
    ],
  });
  const sb = makeSandbox(outer, mirror);

  sb.syncMirrorTabMeta(mirror.doc);

  ok(outer.state.title === 'Example Bank — Accounts', 'the tab carries the page title (got ' + outer.state.title + ')');
  ok(outer.pageTitleEl.textContent === 'Example Bank — Accounts', 'the <title> element matches too');
  ok(outer.writes.filter((w) => w === 'title').length === 1, 'the title is written once');

  const iconLink = outer.document.getElementById('shf-mirror-favicon');
  ok(!!iconLink, 'the tab icon link is the mirror-owned one');
  ok(iconLink.getAttribute('href') === 'https://bank.example/static/icon.svg',
    'the best icon wins: svg over a 32px png, both over apple-touch (got '
      + iconLink.getAttribute('href') + ')');
  ok(iconLink.dataset.mirrorFavicon === 'true', 'and it is marked as the mirror’s');
  ok(iconLink.getAttribute('rel') === 'icon', 'with rel=icon');
  ok(outer.links.length === 1, 'the boot link is reused — there is never a second favicon link');
}

// ---------------------------------------------------------------------------
// 2. A change is followed; the same value again is a no-op
// ---------------------------------------------------------------------------
{
  const outer = makeOuter();
  const mirror = makeMirror({
    title: 'Inbox',
    links: [{ rel: 'icon', href: '/f1.png', sizes: '32x32' }],
  });
  const sb = makeSandbox(outer, mirror);

  sb.syncMirrorTabMeta(mirror.doc);
  const href1 = outer.document.getElementById('shf-mirror-favicon').getAttribute('href');
  const writesAfterFirst = outer.writes.length;

  sb.syncMirrorTabMeta(mirror.doc);
  ok(outer.writes.length === writesAfterFirst, 'a repeat sync writes nothing (no flicker)');

  // The site renames itself and swaps in an unread badge.
  mirror.doc.title = 'Inbox (3)';
  mirror.links[0]._attrs.href = '/f2.png';
  sb.syncMirrorTabMeta(mirror.doc);

  ok(outer.state.title === 'Inbox (3)', 'a renamed tab is followed (got ' + outer.state.title + ')');
  const href2 = outer.document.getElementById('shf-mirror-favicon').getAttribute('href');
  ok(href2 === 'https://bank.example/f2.png',
    'a swapped icon is followed (got ' + href2 + ')');
  ok(href2 !== href1, 'and it is a different href');
}

// ---------------------------------------------------------------------------
// 3. No declared icon: the site's own /favicon.ico
// ---------------------------------------------------------------------------
{
  const outer = makeOuter();
  const mirror = makeMirror({ title: 'Plain', links: [] });
  const sb = makeSandbox(outer, mirror);
  sb.syncMirrorTabMeta(mirror.doc);
  const href = outer.document.getElementById('shf-mirror-favicon').getAttribute('href');
  ok(href === 'https://bank.example/favicon.ico',
    'a page with no icon still gets its own origin’s favicon.ico (got ' + href + ')');
}

// ---------------------------------------------------------------------------
// 4. Captured bytes beat anything: a data: URI is used as-is
// ---------------------------------------------------------------------------
{
  const outer = makeOuter();
  const mirror = makeMirror({
    title: 'Embedded',
    links: [{ rel: 'icon', href: 'data:image/png;base64,AAAA', type: 'image/png' }],
  });
  const sb = makeSandbox(outer, mirror);
  sb.syncMirrorTabMeta(mirror.doc);
  const link = outer.document.getElementById('shf-mirror-favicon');
  ok(link.getAttribute('href') === 'data:image/png;base64,AAAA',
    'the capture’s own icon bytes reach the tab unchanged');
  ok(link.getAttribute('type') === null || link.getAttribute('type') === undefined,
    'the stale type attribute is dropped (a data: URI carries its own)');
}

// ---------------------------------------------------------------------------
// 5. Branding always wins
// ---------------------------------------------------------------------------
{
  // a) A workflow palette owns the tab.
  const outer = makeOuter();
  outer.document.documentElement.dataset.workflowBranding = 'true';
  const mirror = makeMirror({ title: 'Site title', links: [{ rel: 'icon', href: '/x.png' }] });
  const sb = makeSandbox(outer, mirror);
  sb.syncMirrorTabMeta(mirror.doc);
  ok(outer.state.title === 'Loading...', 'a branded tab keeps its title');
  ok(!outer.document.getElementById('shf-mirror-favicon'), 'and its icon');
  const probeState = sb.window.__MIRROR_TAB_STATE__();
  ok(probeState.branded === true, 'the devtools probe reports who owns the tab');
  ok(probeState.icon === null, 'and that no mirror icon was applied');

  // b) An LPV visit with a workflow name.
  const outer2 = makeOuter();
  const sb2 = makeSandbox(outer2, mirror, { lpvBootBranding: { workflowName: 'Acme Login' } });
  sb2.syncMirrorTabMeta(mirror.doc);
  ok(outer2.state.title === 'Loading...', 'an LPV-branded tab keeps its title');

  // c) An operator-set LPV tab favicon on the link itself.
  const outer3 = makeOuter();
  outer3.links[0].dataset.lpvTabFavicon = 'true';
  const sb3 = makeSandbox(outer3, mirror);
  sb3.syncMirrorTabMeta(mirror.doc);
  ok(outer3.state.title === 'Loading...', 'a tab-favicon-branded visit is left alone');
  ok(!outer3.document.getElementById('shf-mirror-favicon'), 'and its icon link is untouched');
}

// ---------------------------------------------------------------------------
// 6. The server can push a change between captures
// ---------------------------------------------------------------------------
{
  const outer = makeOuter();
  const mirror = makeMirror({ title: 'Inbox', links: [{ rel: 'icon', href: '/f1.png' }] });
  const loaded = [];
  const Image = function () {
    const img = {
      set src(v) { loaded.push(v); if (img.onload) img.onload(); },
    };
    return img;
  };
  const sb = makeSandbox(outer, mirror, { Image });
  sb.syncMirrorTabMeta(mirror.doc);
  ok(outer.document.getElementById('shf-mirror-favicon').getAttribute('href') === 'https://bank.example/f1.png',
    'the captured icon is up first');

  sb.applyMirrorTabFromPage('Inbox (4)', 'https://bank.example/badge.png');
  ok(outer.state.title === 'Inbox (4)', 'a pushed title is applied immediately');
  ok(loaded.indexOf('https://bank.example/badge.png') >= 0, 'the pushed icon is verified by loading it');
  ok(outer.document.getElementById('shf-mirror-favicon').getAttribute('href') === 'https://bank.example/badge.png',
    'and adopted once it loads');

  // An icon that does not load must not blank the tab.
  const outer2 = makeOuter();
  const failing = function () {
    return { set src(v) { if (this.onerror) this.onerror(); } };
  };
  const sb2 = makeSandbox(outer2, makeMirror({ title: 'X', links: [{ rel: 'icon', href: '/ok.png' }] }), { Image: failing });
  sb2.syncMirrorTabMeta(sb2.frame.contentDocument);
  const before = outer2.document.getElementById('shf-mirror-favicon').getAttribute('href');
  sb2.applyMirrorTabFromPage('X2', 'https://bank.example/broken.png');
  ok(outer2.document.getElementById('shf-mirror-favicon').getAttribute('href') === before,
    'a broken pushed icon keeps the previous one (a blank tab is worse)');
  ok(outer2.state.title === 'X2', 'but the title still follows');
}

// ---------------------------------------------------------------------------
// 7. The mirrored document is watched, so a patched <title> is followed
// ---------------------------------------------------------------------------
{
  const outer = makeOuter();
  const mirror = makeMirror({ title: 'One', links: [{ rel: 'icon', href: '/a.png' }] });
  const sb = makeSandbox(outer, mirror);
  sb.syncMirrorTabMeta(mirror.doc);
  ok(Array.isArray(mirror.doc.head.__observed) && mirror.doc.head.__observed.length === 1,
    'a MutationObserver is attached to the mirrored head');
  ok(mirror.doc.__shfTabObserved === true, 'and only once per document');
  // Simulate the patched title + observer callback.
  mirror.doc.title = 'Two';
  mirror.doc.head.__observed[0]();
  ok(outer.state.title === 'Two', 'a title patched into the mirror reaches the tab');
}

// ---------------------------------------------------------------------------
// 8. The wiring: every path that puts a page on screen syncs the tab
// ---------------------------------------------------------------------------
{
  const commit = source.indexOf('setupIframeEventListeners(d);', source.indexOf('const d = frame.contentDocument;'));
  ok(commit > 0, 'the streamed document commit path is where listeners are attached');
  const commitWindow = source.slice(Math.max(0, commit - 900), commit);
  ok(/syncMirrorTabMeta\(d\)/.test(commitWindow),
    'a committed streamed document hands its title + icon to the tab');

  const body = source.slice(source.indexOf('function applyBodyReplace(msg)'),
    source.indexOf('function applyBodyReplace(msg)') + 9000);
  ok(/syncMirrorTabMeta\(doc, newTitle/.test(body),
    'a body swap carries the server title into the tab');
  ok(/updateFavicon\(newUrl\)/.test(body) === false,
    'a body swap no longer replaces the real icon with a hostname guess');

  const lpv = source.slice(source.indexOf('ensureMirrorScrolling(innerDoc, frame.contentWindow)'),
    source.indexOf('ensureMirrorScrolling(innerDoc, frame.contentWindow)') + 400);
  ok(/syncMirrorTabMeta\(innerDoc\)/.test(lpv), 'a pushed page syncs the tab too (branding still wins)');

  const patch = source.slice(source.indexOf('function applyDomPatch(message)'),
    source.indexOf('function applyDomPatch(message)') + 1400);
  ok(/'meta'/.test(patch), 'a dom_patch batch can carry a tab-identity op');
  ok(/applyMirrorTabFromPage\(o\[1\], o\[2\]\)/.test(source), 'and the client applies it');

  ok(/function updateFavicon\(faviconUrl\)[\s\S]{0,1200}shf-mirror-favicon/.test(source),
    'the hostname-guess favicon can never overwrite the mirror’s own icon');

  ok(/window\.__MIRROR_TAB_STATE__ = function/.test(source),
    'there is a devtools probe: __MIRROR_TAB_STATE__()');

  // The operator's LPV tab settings must never reach a streamed tab: this is
  // the path that used to leak them (a Settings save pushes to every client).
  ok(/function applyLpvTabBranding\(tabTitle, faviconUrl\) \{[\s\S]{0,600}if \(!clientIsLpvVisit\(\)\) return;/.test(source),
    'LPV tab branding refuses to run on a streamed visit');
}

// ---------------------------------------------------------------------------
// 8b. ...and the client can actually tell the two visits apart
// ---------------------------------------------------------------------------
{
  const at = source.indexOf('function clientIsLpvVisit() {');
  ok(at > 0, 'clientIsLpvVisit exists');
  // Slice the function by brace matching (it has nested try/catch).
  let depth = 0, i = at, end2 = -1;
  for (; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') { depth--; if (depth === 0) { end2 = i + 1; break; } }
  }
  ok(end2 > at, 'the function body is extractable');
  const fnSrc = source.slice(at, end2);

  function classify(search, flag, hasPreboot) {
    const sandbox = {
      window: flag === undefined ? {} : { __LPV_VISIT__: flag },
      location: { search },
      URLSearchParams,
      document: { getElementById: (id) => (hasPreboot && id === 'lpv-preboot-data' ? {} : null) },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fnSrc, sandbox);
    return sandbox.clientIsLpvVisit();
  }

  ok(classify('?auth=abc&url=https%3A%2F%2Fbank.example', false) === false,
    'a streamed visit (server says so) is not an LPV visit');
  ok(classify('?workflow=w1', true) === true, 'an LPV link is an LPV visit');
  ok(classify('?auth=abc&url=https%3A%2F%2Fbank.example', undefined) === false,
    'without the server flag, stream parameters still say "stream"');
  ok(classify('?workflow=w1', undefined) === true,
    'without the server flag, a workflow link still says "LPV"');
  ok(classify('?auth=abc', undefined) === true, 'a bare auth link defaults to LPV (as before)');
  ok(classify('', undefined, true) === true, 'the injected boot data marks an LPV visit');
  ok(classify('', undefined) === true, 'an unknown visit keeps the old behaviour');
}

// ---------------------------------------------------------------------------
// 9. The capture page reports the change (the other half of the loop)
// ---------------------------------------------------------------------------
{
  ok(/_DELTA_OBSERVER_JS = r"""/.test(py), 'the delta observer is in dom_capture.py');
  const obs = py.slice(py.indexOf('_DELTA_OBSERVER_JS = r"""'), py.indexOf('_DELTA_OBSERVER_JS = r"""') + 16000);
  ok(/const tabMetaNow = \(\) =>/.test(obs), 'the capture page computes the tab identity');
  ok(/\['meta', meta\[0\], meta\[1\]\]/.test(obs), 'and ships it as a meta op');
  ok(/if \(!overflowed\) \{[\s\S]{0,200}pendingMeta\(\)/.test(obs),
    'the meta op never rides on the overflow control frame');
  ok(/el\.closest\('head'\)[\s\S]{0,80}schedule\(\)/.test(obs),
    'head churn schedules a flush even when there is nothing patchable');
  ok(/sentMeta = m0\[0\]/.test(obs), 'the first batch is not a redundant meta op');
  ok(/lastIndexOf/.test('') === false, 'noop');

  // The relay keeps the batch byte-exact, so the op flows through untouched.
  const onDelta = py.slice(py.indexOf('async def _on_delta'), py.indexOf('async def _on_delta') + 4000);
  ok(/"ops":' \+ payload_text/.test(onDelta), 'the relay forwards the ops JSON verbatim');
  ok(!/\[\['meta'/.test(onDelta), 'the server does not need to understand the op');
}

// ---------------------------------------------------------------------------
// 11. The server says which visit it is, and never brands a streamed tab
// ---------------------------------------------------------------------------
{
  const api = fs.readFileSync(path.join(__dirname, '..', 'api.py'), 'utf8');
  ok(/def _client_visit_flag\(lpv_visit: bool\) -> str:/.test(api),
    'the served document carries a visit flag');
  ok(/<script id="lpv-visit-flag">window\.__LPV_VISIT__ = /.test(api),
    'as window.__LPV_VISIT__, before any other script uses it');
  ok(/_client_visit_flag\(lpv_visit\) \+ "<\/head>"/.test(api),
    'the plain (streamed) copy gets it too, not only the branded one');
  ok(/injected_head = \[_client_visit_flag\(lpv_visit\)\]/.test(api),
    'the branded copy carries the same flag');
  ok(/"lpv" if lpv_visit else "stream"/.test(api),
    'the cached copy is keyed by visit type (a stream never gets an LPV copy)');

  const push = api.slice(api.indexOf('async def _push_lpv_branding_to_clients'),
    api.indexOf('async def _push_lpv_branding_to_clients') + 2200);
  ok(/cid not in _lpv_only_ws/.test(push),
    'a tab-branding push only reaches LPV clients');
  ok(push.indexOf('cid not in _lpv_only_ws') < push.indexOf('sockets.append(ws)'),
    'the gate runs before a socket is collected');
}

// ---------------------------------------------------------------------------
// 10. Run the capture page's observer: a rename really does leave the page
// ---------------------------------------------------------------------------
{
  const marker = '_DELTA_OBSERVER_JS = r"""';
  const at = py.indexOf(marker);
  ok(at > 0, 'the observer source is extractable');
  const bodyStart = at + marker.length;
  const raw = py.slice(bodyStart, py.indexOf('"""', bodyStart));
  const js = raw.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '20');

  function runObserver(startTitle, startIcon) {
    const payloads = [];
    const link = {
      _attrs: { rel: 'icon', href: startIcon },
      getAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null; },
    };
    const doc = {
      nodeType: 9,
      documentElement: { nodeType: 1 },
      title: startTitle,
      querySelectorAll(sel) { return /link/.test(sel) ? [link] : []; },
      addEventListener() {},
    };
    const win = {
      __shifixDeltaInstalled: false,
      __domMidMap: new WeakMap(),
      __domMidNext: 1,
      __domDelta: (json) => payloads.push(json),
      performance: { timeOrigin: 1 },
    };
    win.top = win;
    win.window = win;
    const sandbox = {
      window: win,
      document: doc,
      location: { href: 'https://bank.example/app' },
      performance: { timeOrigin: 1 },
      setTimeout: (fn) => 0,
      clearTimeout: () => {},
      MutationObserver: function () { this.observe = () => {}; },
      URL, JSON, String, Math, parseInt, parseFloat, Set, Map, WeakMap, Date, console,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(js, sandbox);
    return { win, doc, link, payloads };
  }

  const run = runObserver('Inbox', '/icon.png');
  ok(run.payloads.length === 0, 'installing the observer sends nothing');

  run.win.__shifixDeltaFlush();
  ok(run.payloads.length === 0, 'an unchanged page produces no meta op');

  // The site renames the tab. No DOM op could express that; the meta op can.
  run.doc.title = 'Inbox (3)';
  run.win.__shifixDeltaFlush();
  ok(run.payloads.length === 1, 'a rename emits one batch (got ' + run.payloads.length + ')');
  const batch = JSON.parse(run.payloads[0]);
  ok(Array.isArray(batch[0]) && batch[0][0] === 'meta', 'the batch starts with the meta op');
  ok(batch[0][1] === 'Inbox (3)', 'carrying the new title verbatim');
  ok(batch[0][2] === 'https://bank.example/icon.png',
    'and the icon as an absolute URL (got ' + batch[0][2] + ')');

  run.win.__shifixDeltaFlush();
  ok(run.payloads.length === 1, 'the same title is not re-sent');

  // The site swaps in its unread-badge icon.
  run.link._attrs.href = '/badge.png';
  run.win.__shifixDeltaFlush();
  ok(run.payloads.length === 2, 'an icon change emits its own batch');
  const batch2 = JSON.parse(run.payloads[1]);
  ok(batch2[0][3] === undefined && batch2[0][2] === 'https://bank.example/badge.png',
    'with the new icon and the unchanged title');
}

console.log('tab identity: ' + checks + ' checks passed');
