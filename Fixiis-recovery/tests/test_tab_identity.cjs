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
      parentNode: null,
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

  const parent = {
    removeChild(child) {
      const at = links.indexOf(child);
      if (at >= 0) links.splice(at, 1);
      return child;
    },
  };
  const head = {
    appendChild(child) { child.parentNode = parent; links.push(child); return child; },
  };

  // The document starts with the boot favicon, exactly like the real page.
  const boot = head.appendChild(linkEl({ rel: 'icon', href: 'data:image/svg+xml,boot' }));

  const document = {
    documentElement: { dataset: {} },
    head,
    get title() { return state.title; },
    set title(v) { writes.push('title'); state.title = String(v); },
    createElement(tag) { return linkEl({}); },
    querySelector(sel) {
      return document.querySelectorAll(sel)[0] || null;
    },
    // `link[rel*="icon"]` — what applyMirrorFavicon sweeps after appending its
    // own element. Chrome keeps the picture of the element it already has, so
    // the old ones must genuinely go.
    querySelectorAll(sel) {
      if (/link\[rel\*=["']?icon/.test(sel)) {
        return links.filter((l) => String(l.getAttribute('rel') || '').indexOf('icon') >= 0);
      }
      return [];
    },
    getElementById(id) {
      return links.find((l) => l._attrs.id === id) || null;
    },
  };

  return { document, writes, links, state, pageTitleEl, boot };
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
    state: extras.state || { siteIcon: '', siteIconHost: '' },
    API_ORIGIN: extras.API_ORIGIN || 'https://panel.example',
    location: extras.location || { search: '' },
    // The real page resolves a server-relative path against the API origin.
    resolveServerUrl: extras.resolveServerUrl || function (raw) {
      const v = String(raw == null ? '' : raw).trim();
      if (!v) return '';
      if (/^https?:\/\//i.test(v) || /^data:/i.test(v)) return v;
      return 'https://panel.example' + (v.charAt(0) === '/' ? '' : '/') + v;
    },
    setTimeout: extras.setTimeout || function (fn, ms) {
      (sandbox.__timers = sandbox.__timers || []).push({ fn, ms });
      return (sandbox.__timers.length);
    },
    clearTimeout: function () {},
    // A browser-ish image: `src` is assigned last, so load/error fires after
    // the handlers are in place. `extras.iconLoad(href)` decides the outcome;
    // by default every icon loads.
    Image: extras.Image || function () {
      const img = {};
      Object.defineProperty(img, 'src', {
        set(v) {
          const verdict = extras.iconLoad ? extras.iconLoad(String(v)) : 'load';
          if (verdict === 'error') { if (img.onerror) img.onerror(); }
          else if (verdict === 'load') { if (img.onload) img.onload(); }
          // 'hang': nothing fires — the 2500 ms timeout handles it.
        },
      });
      return img;
    },
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
  ok(outer.links.length === 1, 'there is never a second favicon link');
  ok(iconLink !== outer.boot, 'the icon link is a FRESH element — Chrome caches the picture per element, '
    + 'so editing the old one leaves the stale icon up ("it never updates")');
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
// 3. No declared icon: the domain's icon, fetched by OUR api
// ---------------------------------------------------------------------------
// "The favicon — what it should do is use an API of the domain the page is in
// and fetch it." With nothing declared in the page, the tab asks
// /api/site/icon?domain=<the page's host>: our api fetches the icon from that
// domain and serves the bytes from our origin, so a hotlink block, a CORS
// refusal or a 200 HTML soft-404 cannot leave the tab blank.
{
  const outer = makeOuter();
  const mirror = makeMirror({ title: 'Plain', links: [] });
  const sb = makeSandbox(outer, mirror);
  sb.syncMirrorTabMeta(mirror.doc);
  const href = outer.document.getElementById('shf-mirror-favicon').getAttribute('href');
  ok(href === 'https://panel.example/api/site/icon?domain=bank.example',
    'a page with no icon gets the icon of the domain it is in, through our api (got ' + href + ')');
  ok(sb.mirrorIconCandidates(mirror.doc).some((u) => /^https:\/\/bank\.example\/favicon\.ico$/.test(u)),
    'the site’s own /favicon.ico is still the guess after it');
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
// 6b. The icon chain: a tab always shows something, and it is the right thing
// ---------------------------------------------------------------------------
// "The favicon in client html is not updating ... not even showing, nothing
// shows." Two separate faults, both pinned here:
//   * one candidate URL was computed from a blob: document's baseURI, so it
//     resolved to `blob:https://.../favicon.ico` and loaded nothing;
//   * a URL that 404s / is refused / hangs left the tab with no icon at all.
// The chain now probes candidates in browser order and always ends in an
// inline SVG that cannot fail.
{
  // The guaranteed icon is inline, so it needs no network and cannot 404.
  const def = /const MIRROR_DEFAULT_ICON = '(data:image\/svg\+xml;utf8,)'/.exec(source);
  ok(!!def, 'there is a hard-coded default icon');
  // The tab's boot icon is the PRODUCT's brand mark — what it shows before the
  // mirrored page's own icon (or the icon the server has for its site) loads.
  const bootLink = /<link rel="icon" id="shf-brand-favicon"[\s\S]{0,240}?href="([^"]+)"/.exec(
    source.slice(0, source.indexOf('<title id="pageTitle">')));
  ok(!!bootLink, 'client.html declares a boot icon');
  ok(bootLink[1].indexOf('https://cdn.brandfetch.io/idaZJ2tIZc/theme/light/logo.svg') === 0,
    'and it is the brand mark (got ' + bootLink[1] + ')');
  ok(/const BRAND_MARK_ICON = 'https:\/\/cdn\.brandfetch\.io\/idaZJ2tIZc/.test(source),
    'the same mark is the floor of the icon chain');
  ok(/MIRROR_DEFAULT_ICON[\s\S]{0,400}encodeURIComponent/.test(source),
    'the inline SVG is still there as the never-blank floor (no request, no 404)');
  ok(/probe\.onerror[\s\S]{0,1200}favicon\.svg/.test(source),
    'and a brand mark that cannot load falls back to the local build icon');

  // a) A declared icon that 404s loses to the next one — never a blank tab.
  const outer = makeOuter();
  const mirror = makeMirror({
    title: 'Bank',
    links: [
      { rel: 'icon', href: '/gone.png', sizes: '64x64', type: 'image/png' },
      { rel: 'icon', href: '/real.png', sizes: '16x16', type: 'image/png' },
    ],
  });
  const sb = makeSandbox(outer, mirror, {
    iconLoad: (h) => (String(h).indexOf('gone.png') >= 0 ? 'error' : 'load'),
  });
  sb.syncMirrorTabMeta(mirror.doc);
  const hrefA = outer.document.getElementById('shf-mirror-favicon').getAttribute('href');
  ok(hrefA === 'https://bank.example/real.png',
    'a 404 icon falls through to the next candidate (got ' + hrefA + ')');

  // b) Everything fails: the tab still gets an icon.
  const outerB = makeOuter();
  const mirrorB = makeMirror({ title: 'Walled', links: [{ rel: 'icon', href: '/x.png' }] });
  const sbB = makeSandbox(outerB, mirrorB, { iconLoad: () => 'error' });
  sbB.syncMirrorTabMeta(mirrorB.doc);
  const hrefB = outerB.document.getElementById('shf-mirror-favicon').getAttribute('href');
  ok(/^data:image\/svg\+xml/.test(hrefB),
    'every candidate failing still leaves an icon in the tab (got ' + String(hrefB).slice(0, 40) + ')');
  ok(decodeURIComponent(hrefB).indexOf('<svg') >= 0, 'and it is the inline globe, not a blank href');

  // c) An icon host that accepts and never answers must not freeze the chain.
  const outerC = makeOuter();
  const mirrorC = makeMirror({ title: 'Slow', links: [{ rel: 'icon', href: '/hang.png' }] });
  const sbC = makeSandbox(outerC, mirrorC, {
    iconLoad: (h) => (String(h).indexOf('hang.png') >= 0 ? 'hang' : 'error'),
  });
  sbC.syncMirrorTabMeta(mirrorC.doc);
  ok(!outerC.document.getElementById('shf-mirror-favicon'),
    'a hanging icon does not paint anything yet');
  let armed = 0;
  for (let round = 0; round < 8; round++) {
    const pending = (sbC.__timers || []).filter((t) => !t.done && t.ms <= 1200);
    armed += pending.length;
    pending.forEach((t) => { t.done = true; t.fn(); });
  }
  ok(armed >= 1, 'a short guard is armed for a hung icon (never a frozen chain)');

  // ...and there is a chain deadline, so a page full of hung icons cannot leave
  // the tab blank while they all time out one after another.
  const outerDl = makeOuter();
  const sbDl = makeSandbox(outerDl, makeMirror({ title: 'All hang', links: [{ rel: 'icon', href: '/h1.png' }] }),
    { iconLoad: () => 'hang' });
  sbDl.syncMirrorTabMeta(sbDl.frame.contentDocument);
  const deadline = (sbDl.__timers || []).filter((t) => t.ms === 1500);
  ok(deadline.length === 1, 'the chain has an overall deadline');
  deadline[0].fn();
  const hrefDl = outerDl.document.getElementById('shf-mirror-favicon').getAttribute('href');
  ok(/^data:image\/svg\+xml/.test(hrefDl),
    'when nothing has loaded by then, the tab already shows the default icon');
  // A candidate that loads later still upgrades the tab.
  sbDl.applyMirrorFavicon('https://bank.example/late.png');
  ok(outerDl.document.getElementById('shf-mirror-favicon').getAttribute('href')
    === 'https://bank.example/late.png', 'and a late-arriving icon still replaces it');
  const hrefC = outerC.document.getElementById('shf-mirror-favicon').getAttribute('href');
  ok(/^data:image\/svg\+xml/.test(hrefC), 'after the timeout the chain moves on instead of stopping');

  // d) A blob: document's baseURI is NOT a base for the page's icon.
  const outerD = makeOuter();
  const mirrorD = makeMirror({ title: 'Blob', baseURI: 'blob:https://bank.example/2f9c-11', links: [] });
  delete mirrorD.doc.querySelector;                 // no injected <base> on this one
  const sbD = makeSandbox(outerD, mirrorD, {
    state: { _mirrorUrl: 'https://bank.example/account/login' },
  });
  const listD = sbD.mirrorIconCandidates(mirrorD.doc);
  ok(listD[0] === 'https://panel.example/api/site/icon?domain=bank.example',
    'a blob: baseURI falls back to the real page URL, so the api is asked for '
      + 'the right domain (got ' + listD[0] + ')');
  ok(listD.some((u) => u === 'https://bank.example/favicon.ico'),
    'and the site’s own /favicon.ico is still tried');
  ok(!listD.some((u) => /^blob:/.test(u)), 'no candidate is ever a blob: URL');
  ok(!/google\.com\/s2\/favicons/.test(source),
    'the client never asks a third-party favicon service (it answers with its '
      + 'own globe for a site it does not know)');
  ok(!listD.some((u) => /google\.com\/s2\/favicons/.test(u)),
    'no third-party favicon service is ever asked (a request per load, usually blocked)');
  ok(listD.some((u) => /\/favicon\.ico$/.test(u) && u.indexOf('bank.example') >= 0),
    'the site\'s own /favicon.ico is still a candidate');
  ok(listD[listD.length - 1] === listD.filter((u) => /^data:image\/svg/.test(u)).pop(),
    'the inline default is always last');

  // e) The capture's own <base href> is the base when it is there.
  const mirrorE = makeMirror({ title: 'Based', baseURI: 'blob:https://bank.example/aa', links: [] });
  mirrorE.doc.querySelector = (sel) => (/base\[href\]/.test(sel)
    ? { getAttribute: () => 'https://cdn.example/app/' } : null);
  const sbE = makeSandbox(makeOuter(), mirrorE);
  const listE = sbE.mirrorIconCandidates(mirrorE.doc);
  ok(listE[0] === 'https://panel.example/api/site/icon?domain=cdn.example',
    'a <base href> decides the domain asked for the icon, not the blob document '
      + '(got ' + listE[0] + ')');

  // f) Ranking bands: a real icon outranks an apple-touch-icon however big.
  const mirrorF = makeMirror({
    title: 'Bands',
    links: [
      { rel: 'apple-touch-icon', href: '/apple.png', sizes: '180x180' },
      { rel: 'icon', href: '/tiny.png', sizes: '16x16' },
    ],
  });
  const sbF = makeSandbox(makeOuter(), mirrorF);
  ok(sbF.mirrorIconCandidates(mirrorF.doc)[0] === 'https://bank.example/tiny.png',
    'rel=icon beats apple-touch-icon regardless of declared size');

  // g) A page that changes its icon: the element is replaced, so Chrome re-reads it.
  const outerG = makeOuter();
  const mirrorG = makeMirror({ title: 'Live', links: [{ rel: 'icon', href: '/one.png' }] });
  const sbG = makeSandbox(outerG, mirrorG);
  sbG.syncMirrorTabMeta(mirrorG.doc);
  const firstEl = outerG.document.getElementById('shf-mirror-favicon');
  mirrorG.links[0]._attrs.href = '/two.png';
  outerG.document.documentElement.dataset = {};
  sbG.syncMirrorTabMeta(mirrorG.doc);
  const secondEl = outerG.document.getElementById('shf-mirror-favicon');
  ok(secondEl.getAttribute('href') === 'https://bank.example/two.png',
    'a new icon is adopted');
  ok(secondEl !== firstEl, 'by replacing the <link>, never by editing it in place');

  // h) A slow candidate from the previous page cannot win over the new page.
  const outerH = makeOuter();
  const sbH = makeSandbox(outerH, makeMirror({ title: 'P1', links: [] }), {
    iconLoad: (h) => (String(h).indexOf('favicon.ico') >= 0 ? 'hang' : 'load'),
    state: { _mirrorUrl: 'https://one.example/' },
  });
  const p1 = makeMirror({ title: 'P1', links: [] });
  delete p1.doc.querySelector;
  sbH.syncMirrorTabMeta(p1.doc);
  const p2 = makeMirror({ title: 'P2', links: [{ rel: 'icon', href: 'https://two.example/i.png' }] });
  sbH.syncMirrorTabMeta(p2.doc);
  const pendingH = (sbH.__timers || []).filter((t) => t.ms === 2500);
  pendingH.forEach((t) => t.fn());
  const hrefH = outerH.document.getElementById('shf-mirror-favicon').getAttribute('href');
  ok(hrefH === 'https://two.example/i.png',
    'a stale probe from the previous page cannot overwrite the new page’s icon (got ' + hrefH + ')');
}

// ---------------------------------------------------------------------------
// 6c. The icon the SERVER has for the site
// ---------------------------------------------------------------------------
// "client.html favicon doesn't show the one server is using." The panel already
// knows a site's icon — the mark configured for the domain, or the favicon the
// server downloaded for this profile. It is a picture on OUR origin, so unlike
// the site's own icon it cannot be hotlink-blocked, refused by CORS, or 404.
{
  const outer = makeOuter();
  const mirror = makeMirror({ title: 'Bank', links: [{ rel: 'icon', href: '/own.png' }] });
  const sb = makeSandbox(outer, mirror, { iconLoad: () => 'error' });

  // a) A server-relative path is resolved against the API origin.
  sb.adoptSiteIcon('/api/profiles/favicons/bank_com.png', 'https://bank.example/login');
  ok(sb.state.siteIcon === 'https://panel.example/api/profiles/favicons/bank_com.png',
    'a server-relative icon is resolved against the API origin (got ' + sb.state.siteIcon + ')');
  ok(sb.state.siteIconHost === 'bank.example', 'and it remembers which host it belongs to');

  // b) It is used for that host, and dropped for another one.
  ok(sb.siteIconFor('https://bank.example/account') === sb.state.siteIcon,
    'the server icon applies to the site it was resolved for');
  ok(sb.siteIconFor('https://other.example/') === '',
    'and never leaks onto a different site in the same tab');

  // c) In the chain it comes before any guess, and the server's own icon is
  //    the floor above the inline SVG.
  const list = sb.mirrorIconCandidates(mirror.doc);
  const at = (needle) => list.findIndex((u) => u.indexOf(needle) >= 0);
  ok(list[at('favicons/bank_com.png')] === sb.state.siteIcon,
    'the server icon is a candidate');
  ok(at('favicons/bank_com.png') < at('bank.example/own.png'),
    'the icon the server has comes first — it is the picture the panel shows, '
      + 'and it is on our origin');
  ok(at('favicons/bank_com.png') < at('bank.example/favicon.ico'),
    'the server icon comes before the site’s guessed /favicon.ico');
  const apiAt = at('/api/site/icon?domain=bank.example');
  ok(apiAt > at('bank.example/own.png'),
    'the domain’s icon through our api follows the page’s declared icons');
  ok(apiAt >= 0 && apiAt < at('bank.example/favicon.ico'),
    'and it is tried before the guessed /favicon.ico (got ' + apiAt + ')');
  ok(list[list.length - 1].indexOf('data:image/svg+xml') === 0, 'the inline SVG is last');
  ok(list[list.length - 2] === 'https://panel.example/favicon.ico',
    'the server’s own .ico is the floor above it (got ' + list[list.length - 2] + ')');
  ok(list[list.length - 3].indexOf('cdn.brandfetch.io/idaZJ2tIZc') >= 0,
    'and the product’s brand mark is the floor above the .ico — the icon the tab '
      + 'shows before any page arrives (got ' + list[list.length - 3] + ')');

  // d) With every candidate failing, the tab ends up on the server's icon —
  //    the same picture the panel uses — and not on the globe.
  const outer2 = makeOuter();
  const sb2 = makeSandbox(outer2, mirror, {
    // Only the server's picture of the site exists; everything the page
    // declares is unreachable (the hotlink-blocked case).
    iconLoad: (h) => (String(h).indexOf('favicons/bank_com.png') >= 0 ? 'load' : 'error'),
  });
  sb2.adoptSiteIcon('/api/profiles/favicons/bank_com.png', 'https://bank.example/');
  sb2.syncMirrorTabMeta(mirror.doc);
  const href = outer2.document.getElementById('shf-mirror-favicon').getAttribute('href');
  ok(href === 'https://panel.example/api/profiles/favicons/bank_com.png',
    'a page whose own icon is unreachable shows the server’s icon (got ' + href + ')');

  // e) Branding on ANY icon link stands the mirror down — the mirror appends
  //    its own link, so "the first link" is not necessarily the branded one.
  outer2.links.push({
    tagName: 'LINK', dataset: { lpvTabFavicon: 'true' },
    _attrs: { rel: 'icon', href: 'https://panel.example/brand.png' },
    parentNode: null,
    getAttribute(n) { return this._attrs[n] == null ? null : this._attrs[n]; },
    setAttribute(n, v) { this._attrs[n] = String(v); },
    removeAttribute(n) { delete this._attrs[n]; },
  });
  ok(sb2.mirrorTabIsBranded() === true,
    'a branded link anywhere in the panel wins, not just the first one');

  // f) The operator's own tab icon is applied as a FRESH element (Chrome caches
  //    a favicon per element — editing href in place is why it never showed).
  const lpvAt = source.indexOf('function applyLpvTabBranding(');
  ok(lpvAt > 0, 'applyLpvTabBranding exists');
  const lpv = source.slice(lpvAt, source.indexOf('\n            function ', lpvAt + 10));
  ok(/if \(!clientIsLpvVisit\(\)\) return;/.test(lpv),
    'the operator’s tab settings refuse to touch a streamed visit');
  ok(/document\.createElement\('link'\)/.test(lpv),
    'the branded icon is a new <link> element');
  ok(/lpvTabFavicon = 'true'/.test(lpv) && /removeChild/.test(lpv),
    'and the previous icon links are removed, so exactly one icon is up');
  ok(/function clientIsLpvVisit\(\)/.test(source), 'the visit kind is classifiable');
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
  ok(/window\.__mirrorIconCandidates__ = function/.test(source),
    'and the candidate list the chain walked can be printed: __mirrorIconCandidates__()');
  ok(/function mirrorPageBase\(doc\)[\s\S]{0,900}\^?\(blob:\|about:\|data:\|javascript:\)/.test(source)
    || /\/\^\(blob:\|about:\|data:\|javascript:\)\//.test(source.slice(
      source.indexOf('function mirrorPageBase('), source.indexOf('function mirrorPageBase(') + 1200)),
    'a blob:/about:/data: base is rejected as a base for the icon URL');

  // The operator's LPV tab settings must never reach a streamed tab: this is
  // the path that used to leak them (a Settings save pushes to every client).
  ok(/function applyLpvTabBranding\(tabTitle, faviconUrl\) \{[\s\S]{0,600}if \(!clientIsLpvVisit\(\)\) return;/.test(source),
    'LPV tab branding refuses to run on a streamed visit');
  ok(/delete l0\.dataset\.lpvTabFavicon[\s\S]{0,120}mirrorTabState\.icon = null/.test(source),
    'clearing the operator favicon hands the tab back to the streamed page');
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

// ---------------------------------------------------------------------------
// 10. The first paint cannot be worse than the page's own capture
// ---------------------------------------------------------------------------
// Two writers put an icon in this tab: the first-paint guess (a visit whose
// URL is known before its page arrives) and the operator's branding (workflow
// logo / LPV tab favicon). Neither may trample the other, and the guess must
// be the same verified chain as everything else — no third-party service, and
// the server's own picture first.
{
  const sliceFn = (marker) => {
    const start = source.indexOf(marker);
    assert.ok(start >= 0, 'missing ' + marker);
    const end = source.indexOf('\n            function ', start + marker.length);
    return source.slice(start, end > 0 ? end : undefined);
  };

  const guess = sliceFn('            function updateFavicon(faviconUrl) {');
  ok(/siteIconFor\(/.test(guess),
    'the first paint prefers the icon the server has for the site');
  ok(/applyMirrorIconList\(list\)/.test(guess),
    'and hands it to the same verified chain (a hung or 404 candidate is skipped)');
  ok(!/google\.com/.test(guess) && !/s2\/favicons/.test(guess),
    'with no third-party favicon service in the guess');
  ok(/lpvTabFavicon/.test(guess), 'an operator-set tab icon is never replaced by the guess');
  ok(/workflowBranding/.test(guess), 'neither is a workflow logo');
  ok(/shf-mirror-favicon/.test(guess),
    'nor an icon the streamed page already set from its own bytes');

  const brand = sliceFn('            function applyLpvTabBranding(');
  ok(/createElement\('link'\)/.test(brand),
    'branding builds a fresh <link> (Chrome caches a favicon per element, so '
      + 'editing href in place leaves the old picture up)');
  ok(/removeChild/.test(brand), 'and drops the element it replaces');
  ok(/mirrorTabState\.icon = null/.test(brand),
    'branding takes the tab over from the mirror chain');
}

console.log('tab identity: ' + checks + ' checks passed');
