// No dependencies beyond node: node tests/test_nav_transition.cjs
//
// "The mirror should transition into the new page as soon as it has enough to
// say, and it should never sit on a spinner waiting for something unrelated."
//
// Two things made a navigation feel broken on the client, and both were
// ordering bugs rather than speed bugs:
//
//   1. the spinner only ended when the document arrived, so a redirect chain or
//      a failed capture left the visitor staring at it until a watchdog fired;
//   2. an SPA route change (pushState/popstate) was indistinguishable from a
//      real navigation, so a page that was NOT being replaced had its iframe
//      hidden and a spinner raised over it.
//
// This file pins the rules that replace both:
//
//   * a navigation frame moves the URL immediately, whatever else is running;
//   * `same_document` means no transition at all - the patch stream keeps
//     updating a live mirror;
//   * a cross-document navigation opens a bounded transition that a later
//     `failed` phase can close early;
//   * a frame for an OLDER navigation is discarded, never applied (the race a
//     concurrent pipeline would otherwise introduce);
//   * patches buffered during the transition are drained onto the new document.
//
// The transition block is run for real in a `vm`; the message-switch guards are
// asserted against the source, in the order a message actually meets them.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let checks = 0;
function ok(cond, label) {
  if (!cond) {
    console.error('FAIL: ' + label);
    process.exitCode = 1;
    return;
  }
  checks++;
}

const client = fs.readFileSync(path.join(__dirname, '..', 'client.html'), 'utf8');

// ---------------------------------------------------------------------------
// Extract the real transition block
// ---------------------------------------------------------------------------
const startMark = '            // Navigation announcements (server -> client, before any document)';
const endMark = '            // Helper: pull out <script> tags from an HTML string.';
const start = client.indexOf(startMark);
const end = client.indexOf(endMark, start);
ok(start > 0 && end > start, 'the navigation transition block is gone from client.html');
const block = client.slice(start, end);

ok(/function handleNavigationMessage\(msg\)/.test(block),
  'the client has no handler for navigation frames');
ok(/NAV_DOC_WAIT_MS/.test(block),
  'the transition has no bounded wait - a lost document would strand the spinner');
ok(/phase === 'failed'/.test(block),
  'a failed navigation cannot close the transition early');
ok(!/5000\)/.test(block.replace(/NAV_DOC_WAIT_MS/g, '')),
  'the watchdog still hard-codes the old 5 s wait instead of the named bound');

// ---------------------------------------------------------------------------
// Harness: the block, with just enough surroundings to run
// ---------------------------------------------------------------------------
function makeHarness() {
  const log = { overlays: [], url: [], dbg: [], timers: [], patched: [] };
  const state = {
    _navId: null,
    _navUrl: null,
    _navSameDocument: false,
    _navDocExpected: true,
    _navPhase: null,
    _navAnnouncedAt: 0,
    _navPending: false,
    _navPendingUrl: null,
    _navPatchBuffer: [],
    _navBufferedGen: null,
    _navBufferedDoc: null,
    _navWatchdog: null,
    _navDocArrivedAt: 0,
    _navPartialDoc: false,
    deltaDocumentToken: null,
  };
  let timerId = 1;
  const ctx = {
    state,
    console,
    Date,
    dbg: (...a) => log.dbg.push(a.join(' ')),
    reportPageChange: (u) => { log.url.push(u); state.targetUrl = u; },
    showTransitionOverlay: () => log.overlays.push('show'),
    hideTransitionOverlay: () => log.overlays.push('hide'),
    mirrorFrameA: { style: {} },
    mirrorFrameB: { style: {} },
    frame: { style: {} },
    applyDomPatch: (msg) => { log.patched.push(msg); return true; },
    requestAnimationFrame: (fn) => { fn(); },
    setTimeout: (fn, ms) => { log.timers.push({ fn, ms, id: timerId }); return timerId++; },
    clearTimeout: (id) => { log.timers.push({ cleared: id }); },
  };
  vm.createContext(ctx);
  vm.runInContext(block + '\n; this.handleNavigationMessage = handleNavigationMessage;'
    + ' this.beginNavigationTransition = beginNavigationTransition;'
    + ' this.endNavigationTransition = endNavigationTransition;'
    + ' this.navTransitionTimeout = navTransitionTimeout;', ctx);
  return { ctx, state, log };
}

// ---------------------------------------------------------------------------
// 1. A route change: the URL moves, the page stays
// ---------------------------------------------------------------------------
{
  const { ctx, state, log } = makeHarness();
  ctx.handleNavigationMessage({
    type: 'navigation', nav_id: 4, phase: 'detected',
    url: 'https://site.example/inbox', prev_url: 'https://site.example/',
    same_document: true, doc_expected: false, reason: 'url_change',
    source: 'history:pushState',
  });
  ok(log.url.length === 1 && log.url[0] === 'https://site.example/inbox',
    'a same-document route change did not update the client URL immediately');
  ok(state._navId === 4 && state._navSameDocument === true,
    'the navigation id / kind were not recorded');
  ok(state._navPending === false,
    'a route change started a transition - the live page would be hidden and ' +
    'a spinner raised over content that is not being replaced');
  ok(log.overlays.length === 0,
    'the transition overlay was shown for a same-document route change');
  ok(log.timers.length === 0, 'a route change armed a document watchdog');
}

// ---------------------------------------------------------------------------
// 2. A real navigation: URL now, bounded transition, document ends it
// ---------------------------------------------------------------------------
{
  const { ctx, state, log } = makeHarness();
  ctx.handleNavigationMessage({
    type: 'navigation', nav_id: 1, url: 'https://site.example/results',
    same_document: false, doc_expected: true, reason: 'url_change',
  });
  ok(log.url.length === 1, 'a cross-document navigation did not update the URL first');
  ok(state._navPending === true, 'the transition was not opened for a new document');
  ok(log.overlays.includes('show'), 'the overlay was never raised');
  ok(log.timers.length === 1 && log.timers[0].ms > 0 && log.timers[0].ms <= 5000,
    'no bounded watchdog armed: %r' % log.timers);
}

// ---------------------------------------------------------------------------
// 3. An older navigation arriving late is history, not a page
// ---------------------------------------------------------------------------
{
  const { ctx, state, log } = makeHarness();
  ctx.handleNavigationMessage({
    type: 'navigation', nav_id: 2, url: 'https://site.example/b',
    same_document: true, doc_expected: false,
  });
  const before = log.url.length;
  ctx.handleNavigationMessage({
    type: 'navigation', nav_id: 1, url: 'https://site.example/a',
    same_document: true, doc_expected: false,
  });
  ok(log.url.length === before,
    'a late frame for an older navigation moved the client back to a page it ' +
    'had already left');
  ok(state._navId === 2, 'the newer navigation id was overwritten by an older frame');
}

// ---------------------------------------------------------------------------
// 4. The server says the capture failed: leave the transition now
// ---------------------------------------------------------------------------
{
  const { ctx, state, log } = makeHarness();
  ctx.handleNavigationMessage({
    type: 'navigation', nav_id: 7, url: 'https://site.example/slow',
    same_document: false, doc_expected: true,
  });
  ok(state._navPending === true, 'setup: the transition should be open');
  ctx.handleNavigationMessage({
    type: 'navigation', nav_id: 7, phase: 'failed',
    url: 'https://site.example/slow', detail: 'capture_failed',
  });
  ok(state._navPending === false,
    'a failed navigation left the visitor on a spinner until the watchdog fired');
  ok(log.overlays.includes('hide'), 'the overlay was never dismissed');
}

// ---------------------------------------------------------------------------
// 5. Buffered patches are drained onto the document that ended the wait
// ---------------------------------------------------------------------------
{
  const { ctx, state, log } = makeHarness();
  ctx.handleNavigationMessage({
    type: 'navigation', nav_id: 9, url: 'https://site.example/x',
    same_document: false, doc_expected: true,
  });
  state._navPatchBuffer = [['a', '1', 'class', 'x'], ['a', '2', 'class', 'y']];
  state._navBufferedGen = 12;
  state._navBufferedDoc = 'doc-12';
  ctx.endNavigationTransition();
  ok(state._navPending === false, 'the transition did not end');
  ok(log.patched.length === 1, 'the buffered ops were not applied at all');
  ok(log.patched[0].gen === 12 && log.patched[0].doc === 'doc-12',
    'the buffered ops were applied against the wrong generation: %r', log.patched[0]);
  ok(log.patched[0].ops.length === 2, 'the buffered ops were dropped');
}

// ---------------------------------------------------------------------------
// 6. The message switch: guards in the order a message meets them
// ---------------------------------------------------------------------------
{
  const switchStart = client.indexOf('async function handleMessage(message)');
  ok(switchStart > 0, 'the message handler is gone');
  const navCase = client.indexOf("case 'navigation':", switchStart);
  ok(navCase > switchStart, 'navigation frames are not handled at all');
  ok(client.indexOf('handleNavigationMessage(message)', navCase) > navCase,
    'the navigation case does not call the handler');

  // full_document must reject an older navigation BEFORE it renders anything.
  const docCase = client.indexOf("case 'full_document':", switchStart);
  const docGuard = client.indexOf('message.nav_id < state._navId', docCase);
  const docRender = client.indexOf('applyFullDocument(message)', docCase);
  ok(docCase > 0 && docGuard > docCase,
    'the full_document case has no stale-navigation guard');
  ok(docGuard < docRender,
    'the stale-navigation guard runs AFTER the document was already rendered');

  // dom_patch, same rule.
  const patchCase = client.indexOf("case 'dom_patch':", switchStart);
  const patchGuard = client.indexOf('message.nav_id < state._navId', patchCase);
  const patchApply = client.indexOf('applyDomPatch(message', patchCase);
  ok(patchGuard > patchCase && patchGuard < patchApply,
    'a patch from a superseded navigation is applied before it is checked');

  // The legacy page_transition must not raise a spinner over a route change the
  // navigation pipeline already classified.
  const transCase = client.indexOf("case 'page_transition':", switchStart);
  const transBody = client.slice(transCase, client.indexOf("case 'sync_noop':", transCase));
  ok(/_navSameDocument/.test(transBody),
    'the legacy page_transition ignores whether the change is same-document, so ' +
    'it can hide a page that was never being replaced');

  // Patch generations: older means superseded (drop), newer means missing
  // (resync). Conflating them made a concurrent pipeline spam resyncs.
  const applyStart = client.lastIndexOf('function applyDomPatch(message)');
  const applyBody = client.slice(applyStart, applyStart + 2600);
  const older = applyBody.indexOf('message.gen < state._deltaGen');
  const newer = applyBody.indexOf('message.gen > state._deltaGen');
  ok(older > 0 && newer > older,
    'the patch path does not distinguish a superseded patch from a missing one');
  ok(/return true;/.test(applyBody.slice(older, newer)),
    'a superseded patch is not dropped silently (it would ask for a resync the ' +
    'server has already answered)');

  // The document token arrives with the document, so a patch for a different
  // document can be recognised instead of "the first patch decides".
  ok(/state\.deltaDocumentToken\s*=\s*\n?\s*\(typeof message\.doc === 'string'/.test(client),
    'the client still infers the document token from the first patch instead of ' +
    'taking it from the capture');
}

// ---------------------------------------------------------------------------
// 7. The state these functions rely on exists before a message can arrive
// ---------------------------------------------------------------------------
for (const field of ['_navId', '_navPending', '_navPatchBuffer', '_navWatchdog',
                     '_navSameDocument', '_navDocExpected']) {
  ok(new RegExp(field + ':').test(client.slice(0, client.indexOf('const elements = {'))),
    'state.' + field + ' is used but never initialised');
}

console.log('navigation transition: %d checks passed' + (process.exitCode ? ' (FAILURES)' : ''), checks);
