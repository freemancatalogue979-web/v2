// No dependencies: node tests/test_mirror_scroll.cjs
//
// "I can't scroll the mirrored page."
//
// A DOM capture is the page's HTML + CSS with its scripts stripped, so anything
// the original page scrolled with script is inert. The classic shape is a
// full-height shell (`height:100%` + `overflow:hidden`) around content that is
// several screens tall: the content is all in the document, but nothing moves.
//
// client.html answers with ensureMirrorScrolling(). This test extracts that
// function (plus pageScrolls / nudgeScrollRescue) out of client.html and runs it
// against a fake DOM, because a regression here is invisible until somebody
// opens a bank page on a phone and cannot move.
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

// ---------------------------------------------------------------------------
// Pull the scroll-rescue block out of client.html and run it in a sandbox.
// ---------------------------------------------------------------------------
const start = source.indexOf('const SCROLL_RESCUE_NODES =');
const endMarker = 'function nudgeScrollRescue(doc, win) {';
const endStart = source.indexOf(endMarker);
assert.ok(start >= 0 && endStart > start, 'scroll rescue helpers not found in client.html');
// The nudge helper ends at the first closing brace at its own indent.
const end = source.indexOf('\n            }\n', endStart) + '\n            }\n'.length;
const slice = source.slice(start, end);

function makeContext(clock) {
  // The gesture retry is deferred (that is the whole point: it must not run
  // while the visitor is scrolling), so the sandbox owns the timers.
  const timers = new Map();
  let nextTimer = 1;
  const context = vm.createContext({
    console,
    String,
    Math,
    Date: clock ? { now: () => clock.now } : Date,
    // The slice also defines the devtools probe on `window`; the sandbox needs
    // an object to hang it on.
    window: {},
    setTimeout: (fn) => { const id = nextTimer++; timers.set(id, fn); return id; },
    clearTimeout: (id) => { timers.delete(id); },
  });
  vm.runInContext(slice, context);
  // Run whatever a deferred retry queued (the "gesture ended" moment).
  context.__flushTimers = () => {
    const pending = Array.from(timers.values());
    timers.clear();
    for (const fn of pending) { try { fn(); } catch (e) { /* ignore */ } }
  };
  return context;
}

// ---------------------------------------------------------------------------
// Minimal fake DOM
// ---------------------------------------------------------------------------
function node(tag, opts = {}) {
  const el = {
    tagName: String(tag).toUpperCase(),
    id: opts.id || '',
    _attrs: Object.assign({}, opts.attrs || {}),
    parentElement: null,
    _children: [],
    clientHeight: opts.clientHeight || 0,
    scrollHeight: opts.scrollHeight || 0,
    clientWidth: opts.clientWidth != null ? opts.clientWidth : (opts.clientHeight || 0),
    scrollWidth: opts.scrollWidth != null ? opts.scrollWidth : (opts.scrollHeight || 0),
    scrollTop: 0,
    scrollLeft: 0,
    textContent: opts.text || '',
    tagName2: null,
    getBoundingClientRect() {
      return Object.assign({ top: 0, left: 0, width: 800, height: el.clientHeight || 0 },
        opts.rect || {});
    },
    closest(sel) {
      const names = String(sel).split(',').map((x) => x.trim().toUpperCase());
      let cur = el;
      while (cur) {
        const tag = String(cur.tagName || '').toLowerCase();
        if (names.indexOf(cur.tagName) >= 0 || names.indexOf(tag) >= 0) return cur;
        if (cur.getAttribute && cur.getAttribute('contenteditable') === 'true') return cur;
        cur = cur.parentElement;
      }
      return null;
    },
    _style: {},
    setAttribute(name, value) { el._attrs[name] = String(value); },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(el._attrs, name) ? el._attrs[name] : null;
    },
    get firstElementChild() { return el._children[0] || null; },
    get nextElementSibling() {
      if (!el.parentElement) return null;
      const sibs = el.parentElement._children;
      return sibs[sibs.indexOf(el) + 1] || null;
    },
    appendChild(child) {
      child.parentElement = el;
      el._children.push(child);
      return child;
    },
  };
  // style records both `el.style.x = v` and `el.style.setProperty(x, v, prio)`.
  el.style = new Proxy(el._style, {
    get(target, key) {
      if (key === 'setProperty') {
        return (name, value, prio) => { target[name] = prio ? value + ' !' + prio : value; };
      }
      return target[key];
    },
    set(target, key, value) { target[key] = value; return true; },
  });
  return el;
}

function makeDoc(opts = {}) {
  const doc = {
    head: null,
    body: null,
    documentElement: null,
    scrollingElement: null,
    _byId: {},
    createElement(tag) { return node(tag); },
    getElementById(id) { return doc._byId[id] || null; },
  };
  const html = node('html', {
    clientHeight: opts.viewport || 800,
    scrollHeight: opts.htmlScrollHeight != null ? opts.htmlScrollHeight : (opts.viewport || 800),
    attrs: { id: '' },
  });
  const head = node('head');
  const body = node('body', {
    clientHeight: opts.bodyClientHeight != null ? opts.bodyClientHeight : (opts.viewport || 800),
    scrollHeight: opts.bodyScrollHeight != null ? opts.bodyScrollHeight : (opts.viewport || 800),
  });
  html.appendChild(head);
  html.appendChild(body);
  doc.documentElement = html;
  doc.head = head;
  doc.body = body;
  doc.scrollingElement = html;
  // appendChild keeps a parent, but getElementById needs the id map; keep both
  // in sync for the elements the rescue creates.
  const realAppend = head.appendChild.bind(head);
  head.appendChild = (child) => {
    const out = realAppend(child);
    if (child.id) doc._byId[child.id] = child;
    return out;
  };
  return doc;
}

// getComputedStyle stub: read from a per-element `_computed` bag.
function makeView(viewport, overrides) {
  return {
    innerHeight: viewport,
    innerWidth: 800,
    getComputedStyle(el) {
      return Object.assign(
        { overflowY: 'visible', overflow: 'visible', position: 'static' },
        (overrides && overrides.get(el)) || el._computed || {},
      );
    },
  };
}

// ---------------------------------------------------------------------------
// 1. A page that already scrolls is left completely alone
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 5000 });
  const shell = node('div', { clientHeight: 800, scrollHeight: 5000, attrs: { id: 'shell' } });
  shell._computed = { overflowY: 'hidden' };
  doc.body.appendChild(shell);
  const before = Object.keys(shell._style).length;

  const result = ctx.ensureMirrorScrolling(doc, makeView(800));

  ok(result === true, 'a scrolling page reports success');
  ok(Object.keys(shell._style).length === before,
    'a scrolling page is not restyled (no new inline styles on its wrapper)');
  ok(!shell.getAttribute('data-scroll-rescued'),
    'a scrolling page is not marked as rescued');
}

// ---------------------------------------------------------------------------
// 2. The reported bug: a full-height clipping shell around tall content
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 800, bodyScrollHeight: 800 });
  const shell = node('div', { clientHeight: 780, scrollHeight: 4200, attrs: { id: 'app' } });
  shell._computed = { overflowY: 'hidden', position: 'static' };
  doc.body.appendChild(shell);

  const result = ctx.ensureMirrorScrolling(doc, makeView(800));

  ok(result === true, 'the clipped page reports that scrolling was restored');
  ok(shell._style['overflow-y'] === 'auto !important',
    'the clipping wrapper becomes a scroll container (important)');
  ok(shell._style['-webkit-overflow-scrolling'] === 'touch',
    'iOS momentum scrolling is enabled on the rescued wrapper');
  ok(!shell._style['overscroll-behavior'],
    'the rescued box chains its scroll naturally (no clamped overscroll)');
  ok(shell.getAttribute('data-scroll-rescued') === '1',
    'the rescued wrapper is marked so it can be recognised later');
  ok(doc.documentElement._style.overflow === 'auto',
    'the document element is scrollable as well');
  ok(doc.body._style.overflow === 'auto', 'the body is scrollable as well');
}

// ---------------------------------------------------------------------------
// 2b. A page that only LOOKS scrollable (an author `overflow:hidden
//     !important` beats our reset) is still rescued
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  // More content than the body shows, but the body itself cannot move: this is
  // exactly the case a naive "scrollHeight > clientHeight" check skips.
  const doc = makeDoc({ viewport: 800, bodyClientHeight: 800, bodyScrollHeight: 3200 });
  doc.body._computed = { overflowY: 'hidden', position: 'static' };
  const shell = node('div', { clientHeight: 780, scrollHeight: 3200, attrs: { id: 'app' } });
  shell._computed = { overflowY: 'hidden', position: 'static' };
  doc.body.appendChild(shell);

  const result = ctx.ensureMirrorScrolling(doc, makeView(800));

  ok(result === true, 'a page that only looks scrollable still gets the rescue');
  ok(shell._style['overflow-y'] === 'auto !important', 'its clipped wrapper becomes a scroller');
}

// ---------------------------------------------------------------------------
// 3. Overlays are left alone — making a modal scroll would be a new bug
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 800 });
  doc.body.textContent = 'x'.repeat(4000);
  const modal = node('div', { clientHeight: 500, scrollHeight: 3000, attrs: { id: 'cookie' }, text: 'cookies?' });
  modal._computed = { overflowY: 'hidden', position: 'fixed' };
  doc.body.appendChild(modal);
  const sticky = node('header', { clientHeight: 700, scrollHeight: 2000 });
  sticky._computed = { overflowY: 'hidden', position: 'sticky' };
  doc.body.appendChild(sticky);

  ctx.ensureMirrorScrolling(doc, makeView(800));

  ok(!modal._style['overflow-y'], 'a dialog-sized fixed overlay keeps its clipping');
  ok(!sticky._style['overflow-y'], 'a sticky bar keeps its clipping');
  ok(!modal.getAttribute('data-scroll-rescued'), 'a fixed overlay is not marked');
}

// ---------------------------------------------------------------------------
// 3b. A FULL-VIEWPORT fixed shell is the app itself: with the page otherwise
//     dead it may scroll, because a page that cannot move at all is worse.
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 800 });
  const app = node('div', { clientHeight: 800, scrollHeight: 4200, attrs: { id: 'app' } });
  app._computed = { overflowY: 'hidden', position: 'fixed' };
  doc.body.appendChild(app);

  ctx.ensureMirrorScrolling(doc, makeView(800));

  ok(app._style['overflow-y'] === 'auto !important',
    'a dead page whose layout is one fixed full-screen shell is rescued');
  ok(app.getAttribute('data-scroll-rescued') === '1', 'and the shell is marked');
}

// ---------------------------------------------------------------------------
// 3c. ...but a page that already scrolls never gets its fixed shell touched
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 4000 });
  const banner = node('div', { clientHeight: 800, scrollHeight: 3000, attrs: { id: 'hero' } });
  banner._computed = { overflowY: 'hidden', position: 'fixed' };
  doc.body.appendChild(banner);

  ctx.ensureMirrorScrolling(doc, makeView(800));

  ok(!banner._style['overflow-y'], 'a scrolling page keeps its fixed hero exactly as captured');
}

// ---------------------------------------------------------------------------
// 4. A small clipped card is not turned into a scroller
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 800 });
  const card = node('div', { clientHeight: 60, scrollHeight: 900 });
  card._computed = { overflowY: 'hidden' };
  doc.body.appendChild(card);

  ctx.ensureMirrorScrolling(doc, makeView(800));

  ok(!card._style['overflow-y'], 'a short clipped card is left alone');
}

// ---------------------------------------------------------------------------
// 5. Only a handful of wrappers are rescued per pass (a page cannot turn into
//    50 tiny scrollers)
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 800 });
  for (let i = 0; i < 20; i++) {
    const shell = node('div', { clientHeight: 780, scrollHeight: 3000 });
    shell._computed = { overflowY: 'hidden' };
    doc.body.appendChild(shell);
  }

  ctx.ensureMirrorScrolling(doc, makeView(800));

  const rescued = doc.body._children.filter((c) => c.getAttribute('data-scroll-rescued')).length;
  ok(rescued === 6, 'at most six wrappers are rescued in one pass (got ' + rescued + ')');
}

// ---------------------------------------------------------------------------
// 6. Deep trees are scanned within a node budget instead of hanging the page
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 800 });
  let parent = doc.body;
  for (let i = 0; i < 4000; i++) {
    const el = node('div', { clientHeight: 10, scrollHeight: 10 });
    parent.appendChild(el);
    parent = el;
  }
  const started = Date.now();
  ctx.ensureMirrorScrolling(doc, makeView(800));
  const elapsed = Date.now() - started;

  ok(elapsed < 500, 'a 4000-node page is scanned in well under half a second (' + elapsed + 'ms)');
}

// ---------------------------------------------------------------------------
// 7. Last resort: nothing clips, nothing scrolls, so the document gets its
//    natural height back and scrolls itself
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800 });
  const normal = node('div', { clientHeight: 800, scrollHeight: 800 });
  normal._computed = { overflowY: 'visible' };
  doc.body.appendChild(normal);

  // A dead page: neither the document, nor the body, nor any wrapper can move.
  doc.documentElement.scrollHeight = 800;
  doc.body.scrollHeight = 800;
  ctx.ensureMirrorScrolling(doc, makeView(800));

  const style = doc.getElementById('__shifix_scroll_rescue__');
  ok(!!style, 'the last-resort stylesheet is injected');
  ok(/height:auto!important/.test(style.textContent), 'it lets html/body take their natural height');
  ok(!/overflow:hidden/.test(style.textContent), 'it never clips anything itself');

  ctx.ensureMirrorScrolling(doc, makeView(800));
  const again = doc.head._children.filter((c) => c.id === '__shifix_scroll_rescue__').length;
  ok(again === 1, 'running it twice does not duplicate the stylesheet');
}

// ---------------------------------------------------------------------------
// 7b. A page that scrolls never gets the last-resort stylesheet
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 4000 });
  ctx.ensureMirrorScrolling(doc, makeView(800));
  ok(!doc.getElementById('__shifix_scroll_rescue__'),
    'a scrolling page keeps its document untouched');
}

// ---------------------------------------------------------------------------
// 8. The nudge is throttled: a gesture is a stream of events, not one
// ---------------------------------------------------------------------------
{
  const clock = { now: 5000 }; // past the module's initial throttle window
  const ctx = makeContext(clock);
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 800 });
  const shell = node('div', { clientHeight: 780, scrollHeight: 4000, attrs: { id: 'a' } });
  shell._computed = { overflowY: 'hidden' };
  doc.body.appendChild(shell);

  ctx.nudgeScrollRescue(doc, makeView(800));
  ok(!shell.getAttribute('data-scroll-rescued'),
    'nothing is touched while the visitor is still scrolling');

  ctx.__flushTimers();
  ok(shell.getAttribute('data-scroll-rescued') === '1',
    'once the gesture is over, a page that still cannot move is rescued');

  const late = node('div', { clientHeight: 780, scrollHeight: 4000, attrs: { id: 'b' } });
  late._computed = { overflowY: 'hidden' };
  doc.body.appendChild(late);
  ctx.nudgeScrollRescue(doc, makeView(800));
  ctx.__flushTimers();
  ok(!late.getAttribute('data-scroll-rescued'), 'a retry 0 ms later is throttled away');

  clock.now += 1600;
  ctx.nudgeScrollRescue(doc, makeView(800));
  ctx.__flushTimers();
  ok(late.getAttribute('data-scroll-rescued') === '1', 'a retry after the window runs again');
}

// ---------------------------------------------------------------------------
// 8b. ...and a page that scrolls natively is never even looked at
// ---------------------------------------------------------------------------
{
  const clock = { now: 9000 };
  const ctx = makeContext(clock);
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 4200 });
  const shell = node('div', { clientHeight: 780, scrollHeight: 4000, attrs: { id: 'hero' } });
  shell._computed = { overflowY: 'hidden', position: 'static' };
  doc.body.appendChild(shell);

  ctx.nudgeScrollRescue(doc, makeView(800));
  ctx.__flushTimers();

  ok(!shell.getAttribute('data-scroll-rescued'),
    'a healthy page gets no rescue pass after a gesture either');
  ok(!doc.getElementById('__shifix_scroll_rescue__'), 'and no last-resort stylesheet');
}

// ---------------------------------------------------------------------------
// 9. The stylesheet that resets a captured page is last in <head>
// ---------------------------------------------------------------------------
{
  ok(/head2\.appendChild\(resetCss\)/.test(source),
    'the reset stylesheet is appended to <head>, not prepended (an author !important must not outrank it)');
  ok(!/insertBefore\(resetCss/.test(source), 'the old prepend is gone');
  ok(/max-height: none !important/.test(source), 'the reset lets html/body grow past the frame');
  ok(/-webkit-overflow-scrolling: touch !important/.test(source),
    'the reset enables iOS momentum scrolling on the document');
  ok(/if \(existingReset\.nextElementSibling\)/.test(source),
    'an existing reset is moved back to the end when something was inserted after it');
}

// ---------------------------------------------------------------------------
// 10. Every path that shows a captured page hands it to the rescue
// ---------------------------------------------------------------------------
{
  const listeners = source.indexOf('function setupIframeEventListeners(d) {');
  ok(listeners > 0, 'setupIframeEventListeners is where the mirror binds its handlers');
  const listenersBody = source.slice(listeners, listeners + 1200);
  ok(/ensureMirrorScrolling\(d, d\.defaultView\)/.test(listenersBody),
    'setupIframeEventListeners hands the document to the rescue');

  const commit = source.indexOf('__shifix_stream_reset__');
  const commitCall = source.indexOf('ensureMirrorScrolling(d, doc.defaultView)', commit);
  ok(commitCall > commit, 'a committed mirror document is rescued right after the reset');

  const lpv = source.indexOf('ensureMirrorScrolling(innerDoc, frame.contentWindow)');
  ok(lpv > 0, 'an LPV pushed page is rescued as well');
  ok(/ensureMirrorScrolling\(_pd, _pw\);/.test(source),
    'the LPV page is re-checked after its layout settles');
  ok(!/_pd, _pw, true/.test(source),
    'the late re-check does not force a pass over a page that already scrolls');

  ok(/nudgeScrollRescue\(d, d\.defaultView\)[\s\S]{0,400}subtype: 'wheel'/.test(source),
    'a wheel gesture retries the rescue');
  ok(/'touchmove', \(ev\) => \{[\s\S]{0,300}nudgeScrollRescue/.test(source),
    'a finger drag retries the rescue');
}

// ---------------------------------------------------------------------------
// 11. The frames themselves are iOS-scrollable
// ---------------------------------------------------------------------------
{
  const frames = source.match(/webkitOverflowScrolling = 'touch';/g) || [];
  ok(frames.length >= 4, 'both mirror buffers and the document get momentum scrolling (found ' + frames.length + ')');
  ok(/newFrame\.style\.webkitOverflowScrolling = 'touch';/.test(source),
    'the first mirror frame is scrollable on iOS');
  ok(/mirrorFrameB\.style\.webkitOverflowScrolling = 'touch';/.test(source),
    'the double-buffered frame is scrollable on iOS');
}

// ---------------------------------------------------------------------------
// 12. The shell that actually blocks scrolling on app-style sites: a
//     `position:fixed` frame that is taller than a card but shorter than the
//     viewport, with the scrolling body inside it. The rescue used to skip it
//     purely because of `position:fixed`, so the page could not move at all.
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 800, bodyScrollHeight: 800 });
  doc.body._computed = { overflowY: 'hidden', position: 'static' };
  // Top bar 80px + scrolling panel filling the rest of the screen.
  doc.body.textContent = 'y'.repeat(5000);
  const shell = node('div', { clientHeight: 720, scrollHeight: 3800, attrs: { id: 'app' }, text: 'y'.repeat(4800) });
  shell._computed = { overflowY: 'hidden', position: 'fixed' };
  doc.body.appendChild(shell);

  const result = ctx.ensureMirrorScrolling(doc, makeView(800));

  ok(result === true, 'a fixed app shell layout reports scrolling restored');
  ok(shell._style['overflow-y'] === 'auto !important',
    'a fixed shell shorter than the viewport becomes a scroll container');
  ok(shell.getAttribute('data-scroll-rescued') === '1', 'and it is marked');
}

// ---------------------------------------------------------------------------
// 12b. A real dialog in the same shape is still left alone
// ---------------------------------------------------------------------------
{
  const ctx = makeContext();
  const doc = makeDoc({ viewport: 800, htmlScrollHeight: 800 });
  doc.body.textContent = 'z'.repeat(6000);
  const dialog = node('div', {
    clientHeight: 700,
    scrollHeight: 3600,
    attrs: { role: 'dialog', 'aria-modal': 'true' },
    rect: { top: 40, width: 780 },
    text: 'z'.repeat(5000),
  });
  dialog._computed = { overflowY: 'hidden', position: 'fixed' };
  doc.body.appendChild(dialog);
  // A centred, narrower modal panel in the same page.
  const panel = node('div', { clientHeight: 700, scrollHeight: 3600, rect: { top: 60, width: 420 } });
  panel._computed = { overflowY: 'hidden', position: 'fixed' };
  doc.body.appendChild(panel);

  ctx.ensureMirrorScrolling(doc, makeView(800));

  ok(!dialog._style['overflow-y'], 'a full-screen dialog keeps its own clipping');
  ok(!panel._style['overflow-y'], 'a centred modal panel keeps its own clipping');
}

// ---------------------------------------------------------------------------
// 13. Scrolling is the browser's, not ours
// ---------------------------------------------------------------------------
// "It scrolls, but it is not smooth." A client that applies the gesture itself
// — scrollTop = scrollTop + deltaY after every wheel tick — fights the
// browser: no acceleration, no momentum, no rubber-banding, and a page that
// stutters while it is meant to be the real site. So the floor under the
// rescue is not a second scroller, it is a promise: nothing is intercepted and
// nothing is moved by hand.
{
  ok(source.indexOf('installFreeScroll') === -1, 'no manual scroll layer is installed');
  ok(source.indexOf('nudgeChain') === -1, 'a gesture is never applied by hand');
  ok(source.indexOf('FREE_SCROLL_KEYS') === -1, 'keys are not translated into scrollTop writes');
  ok(!/scrollTop\s*=/.test(source.slice(source.indexOf('function ensureMirrorScrolling'),
      source.indexOf('function nudgeScrollRescue'))),
    'the rescue never sets scrollTop (it only changes CSS)');
  ok(!/\.scrollTop\s*=\s*[^=]/.test(source.slice(source.indexOf("d.addEventListener('wheel'"),
      source.indexOf("d.addEventListener('wheel'") + 1500)),
    'the wheel handler does not move the page itself');
  ok(/\.scrollTop\s*=\s*part\.top/.test(source) || /\.scrollTop = /.test(source),
    'scroll POSITIONS are still restored after a patch (that is not a gesture)');
  ok((source.match(/data-free-scroll/g) || []).length === 0, 'no free-scroll markers are left behind');
}

// ---------------------------------------------------------------------------
// 13b. The retry waits for the gesture to finish
// ---------------------------------------------------------------------------
// A rescue pass is up to 600 getComputedStyle reads. Doing that per wheel tick
// drops frames; doing it 350 ms after the last tick costs nothing.
{
  const at = source.indexOf('function nudgeScrollRescue(doc, win) {');
  ok(at > 0, 'the gesture retry exists');
  const body = source.slice(at, source.indexOf('\n            }', at));
  ok(/scrollRescueTimer = setTimeout/.test(body), 'the retry is deferred, never run inside the gesture');
  ok(/\}, 350\);/.test(body), 'by a short idle delay');
  ok(/if \(pageScrolls\(doc, win\)\) return;/.test(body),
    'and it does nothing at all when the page already scrolls natively');
  ok(/clearTimeout\(scrollRescueTimer\)/.test(body),
    'a new gesture restarts the idle timer instead of stacking passes');
}

// ---------------------------------------------------------------------------
// 13c. Nothing in the scroll path can preventDefault
// ---------------------------------------------------------------------------
{
  // The two listeners that ride along with a scroll gesture: the wheel that is
  // forwarded to the server page, and the finger tracking that keeps a scroll
  // position from being restored under the visitor's thumb. Both must be
  // passive observers — a scroll handler that can cancel the browser is how a
  // mirror stops scrolling like the real thing.
  const listenerFrom = (startAt, label) => {
    ok(startAt > 0, label + ' exists');
    const from = source.lastIndexOf("d.addEventListener('", startAt);
    const opts = source.indexOf('}, {', startAt);
    const to = opts > startAt ? source.indexOf('});', opts) : source.indexOf('});', startAt);
    ok(to > startAt, label + ' has listener options');
    const chunk = source.slice(from, to + 3);
    ok(/passive: true/.test(chunk), label + ' is passive');
    ok(!/preventDefault/.test(chunk), label + ' never cancels the browser default');
  };
  listenerFrom(source.indexOf("subtype: 'wheel'"), 'the wheel listener');
  listenerFrom(source.indexOf("addEventListener('touchmove'"), 'the touch listener');

  // Keys are forwarded to the server page; they are never turned into scrolls.
  const keyAt = source.indexOf("d.addEventListener('keydown'");
  ok(keyAt > 0, 'the keyboard path exists');
  ok(!/scrollTop/.test(source.slice(keyAt, keyAt + 2000)),
    'a key is never translated into a scrollTop write');
}

// ---------------------------------------------------------------------------
// 15. There is a one-liner for the live page
// ---------------------------------------------------------------------------
{
  ok(/window\.__MIRROR_SCROLL_STATE__ = function/.test(source),
    'a devtools probe exists for "is scrolling wired up in this frame?"');
  const probe = source.slice(source.indexOf('window.__MIRROR_SCROLL_STATE__'),
    source.indexOf('window.__MIRROR_SCROLL_STATE__') + 1600);
  ok(/intercepted: false/.test(probe), 'it reports that nothing intercepts the gesture');
  ok(/data-scroll-rescued/.test(probe), 'it counts the rescued boxes');
  ok(/scrollable:/.test(probe), 'and whether the document can move at all');
}

console.log('mirror scroll: ' + checks + ' checks passed');
