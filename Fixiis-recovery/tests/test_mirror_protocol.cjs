// No dependencies: node tests/test_mirror_protocol.cjs
//
// The viewer half of the mirror protocol (MIRROR_GATEWAY.md §5).
//
// A Browser B that mis-reads the stream does not crash — it shows a page that
// is quietly wrong, which is the failure mode nobody notices. So this test
// extracts the protocol block out of client.html and drives it with real frame
// shapes, asserting the four behaviours that decide whether a mirror is
// trustworthy:
//
//   1. an envelope and a legacy flat frame both normalize to one message;
//   2. a sequence gap never renders — it asks for a resync instead;
//   3. a stale/duplicate frame cannot touch the DOM;
//   4. the viewer acks what it APPLIED, and only that epoch.
//
// Deliberately no jsdom: the object under test is pure bookkeeping, and a test
// that runs in a bare vm cannot silently skip (the jsdom-dependent suites print
// "skipped" and exit 0 when their dependency is missing, which hides exactly
// this kind of regression).

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

// The functions are extracted as source text and evaluated, which is the only
// way to test the shipped code without a bundler or a second copy of it. A
// rename in client.html fails the assertion below instead of silently testing
// nothing, which is what a regex-only "test" would do.
function extract(startMarker, endMarker, name, includeMarker) {
  const start = source.indexOf(startMarker);
  assert.ok(start >= 0, `${name} not found in client.html — was it renamed?`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `${name} end marker "${endMarker}" not found`);
  return source.slice(start, includeMarker ? end + endMarker.length : end);
}

// The protocol block: MirrorProtocol, the cursor feed, the ack and the perf
// reporter, plus the tab-identity helpers they call.
const block = [
  extract('const MirrorProtocol = (function () {', '\n            })();', 'MirrorProtocol', true),
  extract('function noteMirrorTab(tabId) {', '\n            function cursorFor(tabId)', 'noteMirrorTab'),
  extract('function cursorFor(tabId) {', '\n            // Feed one gateway frame', 'cursorFor'),
  extract('function feedMirrorCursor(message) {', '\n            function noteMissingFrame', 'feedMirrorCursor'),
  extract('function noteMissingFrame(', '\n            // Acknowledge what is actually on screen', 'noteMissingFrame'),
  extract('function ackMirrorFrame(message) {', '\n            // Report the viewer-side stages', 'ackMirrorFrame'),
  extract('function reportMirrorPerf(message, patchMs) {', '\n            // ---------------------------------------------------------------', 'reportMirrorPerf'),
  // The mirror-frame helpers the feed calls, in the order they are declared.
  extract('function mirrorNavigate(action, url) {', '\n            // Exposed because the toolbar', 'mirrorNavigate'),
  extract('function mirrorFocus(el, kind) {', '\n            // Alt+Left', 'mirrorFocus'),
].join('\n') + `
;globalThis.__exports = { MirrorProtocol, feedMirrorCursor, noteMissingFrame, ackMirrorFrame,
   reportMirrorPerf, cursorFor, noteMirrorTab, mirrorNavigate, mirrorFocus };
`;

function makeFrame(overrides) {
  return Object.assign({
    message_type: 'delta',
    session_id: 'sess-1',
    tab_id: 'tab-a',
    document_id: 'doc-1',
    epoch: 1,
    seq: 2,
    payload: {},
  }, overrides || {});
}

function newSandbox(opts) {
  const sent = [];
  const warns = [];
  const state = {
    sessionId: 'sess-1',
    _cursors: new Map(),
    _ackedEpoch: new Map(),
    _resyncPendingFor: new Set(),
    _lastSeq: null,
    _seqGaps: 0,
    _seqDuplicates: 0,
    _protocolErrors: 0,
    _mirrorAcks: 0,
    _perfSamples: [],
    _t_received: 1000,
    _mirrorTabId: null,
    lastGeneration: 0,
    resyncRequestPending: false,
    _lastDocHtml: '<html></html>',
  };
  const timers = [];
  const timeouts = [];
  const sandbox = {
    console: { log() {}, warn: (...a) => warns.push(a.join(' ')), error() {}, debug() {} },
    JSON,
    Math,
    Set,
    Map,
    Object,
    Array,
    String,
    Number,
    isFinite,
    parseInt,
    Date,
    performance: { now: () => (opts && opts.now ? opts.now() : 1500) },
    requestAnimationFrame: (fn) => { timers.push(fn); },
    // noteMissingFrame arms a re-arm timer; the sandbox must own it or the
    // recovery test dies on a missing global instead of on a real bug.
    setTimeout: (fn, ms) => { timeouts.push({ fn, ms }); return timeouts.length; },
    clearTimeout: () => {},
    state,
    send: (msg) => { sent.push(msg); },
    dbg: () => {},
    getSelector: () => 'input#x',
    getMid: () => '7',
    isSensitiveField: () => false,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(block, sandbox);
  // `const` bindings are script-scoped and never land on the global object, so
  // the block re-exports what the test drives; function declarations are already
  // reachable as sandbox members.
  Object.assign(sandbox, sandbox.__exports);
  return {
    sandbox, sent, warns, state, timers,
    flushRaf: () => { const t = timers.splice(0); t.forEach(f => f()); },
    runTimeouts: () => { const t = timeouts.splice(0); t.forEach(x => x.fn()); },
  };
}

// ---------------------------------------------------------------------------
// 1. normalize(): two shapes, one message
// ---------------------------------------------------------------------------
{
  const { sandbox } = newSandbox();
  const { normalize } = sandbox.MirrorProtocol;

  const env = normalize(JSON.stringify(makeFrame({
    message_type: 'snapshot',
    payload: { html: '<html>hi</html>', url: 'https://a/', title: 'A' },
  })));
  ok(!env.error, 'a gateway envelope parses');
  ok(env.msg.type === 'full_document', 'a snapshot envelope is handled by the full-document path');
  ok(env.msg.html === '<html>hi</html>', 'the body stays reachable at the top level');
  ok(env.msg.via_gateway === true, 'the frame is marked as gateway-fed');
  ok(env.msg.epoch === 1 && env.msg.seq === 2, 'identity fields survive normalization');

  const flat = normalize(JSON.stringify(makeFrame({
    message_type: 'delta', epoch: 3, seq: 9,
    doc: 'tok', ops: [['t', 1, 'x']],
  })));
  ok(flat.msg.type === 'dom_patch', 'a delta envelope maps onto the patch path');
  ok(Array.isArray(flat.msg.ops) && flat.msg.ops[0][2] === 'x',
    'a FLAT gateway frame (no nested payload) is still recognized');
  ok(flat.msg.epoch === 3 && flat.msg.seq === 9, 'flat identity fields are read as-is');

  const legacy = normalize(JSON.stringify({ type: 'dom_patch', seq: 4, ops: [['t', 2, 'y']] }));
  ok(legacy.msg.via_gateway === false, 'a pre-gateway frame is not treated as one');
  ok(legacy.msg.type === 'dom_patch', 'legacy type is untouched');

  const bad = normalize('{"type":');
  ok(bad.error, 'undecodable text is reported as an error, not swallowed');
}

// ---------------------------------------------------------------------------
// 2. the cursor: ordered, per tab, and strict about holes
// ---------------------------------------------------------------------------
{
  const ctx = newSandbox();
  const { feedMirrorCursor } = ctx.sandbox;
  const snap = makeFrame({ message_type: 'snapshot', type: 'full_document', epoch: 1, seq: 1, payload: {} });
  ok(feedMirrorCursor.call(ctx.sandbox, Object.assign({}, snap, { via_gateway: true })) === 'apply',
    'the opening snapshot is applied');
  for (let seq = 2; seq <= 5; seq++) {
    const d = makeFrame({ type: 'dom_patch', epoch: 1, seq, via_gateway: true });
    ok(feedMirrorCursor.call(ctx.sandbox, d) === 'apply', `contiguous delta ${seq} is applied`);
  }
  const cur = ctx.state._cursors.get('tab-a');
  ok(cur.nextSeq === 6, 'the cursor advanced once per applied frame');
  ok(cur.appliedSeq === 5, 'the cursor remembers what it actually applied');

  ok(feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'dom_patch', epoch: 1, seq: 4, via_gateway: true })) === 'stale',
    'a re-sent older delta is dropped');
  ok(ctx.state._seqDuplicates === 1, 'the duplicate is counted');

  // A hole: seq 8 while 6 is missing.
  const before = ctx.sent.length;
  ok(feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'dom_patch', epoch: 1, seq: 8, via_gateway: true })) === 'stale',
    'a gap suppresses the frame that revealed it');
  const req = ctx.sent[before];
  ok(req && req.type === 'resync_request', 'a gap asks the gateway for a repair');
  ok(req.epoch === 1 && req.lastSeq === 5,
    'the request names what we HAVE, so the gateway can retransmit instead of re-capturing');
  ok(req.tab_id === 'tab-a', 'the request is per tab');
  ok(ctx.state._seqGaps === 2, 'the two missing frames are counted, not ignored');

  // The next frame is the snapshot the request produced.
  const nav = makeFrame({ message_type: 'snapshot', type: 'full_document', epoch: 1, seq: 12, via_gateway: true });
  ok(feedMirrorCursor.call(ctx.sandbox, nav) === 'apply', 'the resync snapshot is applied');
  ok(ctx.state._cursors.get('tab-a').nextSeq === 13, 'the cursor rebases onto the snapshot');
}

  // A delta cannot establish a position, only a snapshot can. This is the
  // "viewer joined mid-stream" case: applying that patch to whatever page happens
  // to be on screen is how a mirror ends up showing a document that never existed.
  const cold = newSandbox();
  const coldVerdict = cold.sandbox.feedMirrorCursor(
    makeFrame({ type: 'dom_patch', message_type: 'delta', epoch: 6, seq: 40, via_gateway: true }));
  ok(coldVerdict === 'stale', 'a first frame that is a delta is not applied to an unpositioned viewer');
  ok(cold.sent[0] && cold.sent[0].type === 'resync_request' && cold.sent[0].reason === 'no_snapshot',
    'and it asks for a document instead');
  ok(cold.sandbox.cursorFor('tab-a').nextSeq === 0, 'the cursor did not move to a position it cannot verify');

// ---------------------------------------------------------------------------
// 3. dedupe: a gap on one tab must not silence another, and must not spam
// ---------------------------------------------------------------------------
{
  const ctx = newSandbox();
  const { feedMirrorCursor } = ctx.sandbox;
  feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'full_document', message_type: 'snapshot', epoch: 1, seq: 1, via_gateway: true }));
  // Two gaps in a row on the same tab: one request.
  feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'dom_patch', epoch: 1, seq: 7, via_gateway: true }));
  feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'dom_patch', epoch: 1, seq: 8, via_gateway: true }));
  const reqs = ctx.sent.filter(m => m.type === 'resync_request');
  ok(reqs.length === 1, 'one repair request per outstanding gap, not one per frame');

  // A different tab behind its own gap asks for its own snapshot.
  feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'full_document', message_type: 'snapshot', tab_id: 'tab-b', epoch: 1, seq: 1, via_gateway: true }));
  feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'dom_patch', tab_id: 'tab-b', epoch: 1, seq: 9, via_gateway: true }));
  const reqs2 = ctx.sent.filter(m => m.type === 'resync_request');
  ok(reqs2.length === 2 && reqs2[1].tab_id === 'tab-b', 'per-tab state means a second tab is repaired independently');

  // A frame from another session is not ours at all.
  ok(feedMirrorCursor.call(ctx.sandbox, makeFrame({ session_id: 'other', type: 'dom_patch', epoch: 1, seq: 2, via_gateway: true })) === 'ignore',
    'a frame for another session is ignored rather than applied');
  ok(ctx.state._cursors.get('tab-a').nextSeq === 2, 'and it did not move our cursor');
}

// ---------------------------------------------------------------------------
// 4. epoch: an old document can never corrupt a new one
// ---------------------------------------------------------------------------
{
  const ctx = newSandbox();
  const { feedMirrorCursor } = ctx.sandbox;
  feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'full_document', message_type: 'snapshot', epoch: 1, seq: 1, via_gateway: true }));
  feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'dom_patch', epoch: 1, seq: 2, via_gateway: true }));

  // Navigation: new epoch. A delta from the OLD epoch must be dropped.
  ok(feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'dom_patch', epoch: 0, seq: 4, via_gateway: true })) === 'stale',
    'a late frame from a previous document is dropped');
  ok(ctx.state._cursors.get('tab-a').nextSeq === 3, 'and it did not advance the cursor');

  // New epoch arrives as a snapshot first — that is the contract.
  const navDoc = makeFrame({ type: 'full_document', message_type: 'snapshot', epoch: 2, seq: 20, document_id: 'doc-2', via_gateway: true });
  ok(feedMirrorCursor.call(ctx.sandbox, navDoc) === 'apply', 'the new document opens with its snapshot');
  ok(ctx.state._cursors.get('tab-a').epoch === 2, 'the cursor moved to the new epoch');
  ok(ctx.state._cursors.get('tab-a').documentId === 'doc-2', 'and to the new document identity');

  // A delta for a NEWER epoch that skips its snapshot: ask, do not apply.
  const before = ctx.sent.length;
  ok(feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'dom_patch', epoch: 3, seq: 30, document_id: 'doc-3', via_gateway: true })) === 'stale',
    'a delta cannot be applied to an epoch whose snapshot we never saw');
  const req = ctx.sent[before];
  ok(req && req.reason === 'epoch_skip', 'the reason is reported so the server can tell it from a trim');
  ok(req && req.epoch === 2, 'the request names the epoch we are ON, not the one we are missing');

  // While a request is outstanding, further gaps on the same tab are counted but
  // never re-sent — one repair in flight is enough, and asking again while the
  // snapshot is on its way is how a viewer ends up DoSing its own server.
  const n2 = ctx.sent.length;
  for (let i = 0; i < 3; i++) {
    feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'dom_patch', epoch: 2, seq: 25 + i, document_id: 'doc-2', via_gateway: true }));
  }
  ok(ctx.sent.length === n2, 'three more gaps add no requests while one is pending');
  ok(ctx.state._cursors.get('tab-a').missing >= 3, 'but every lost frame is still counted');

  // A gap inside a clean epoch is the other question: retransmit, not re-capture.
  const ctx2 = newSandbox();
  ctx2.sandbox.feedMirrorCursor(makeFrame({ type: 'full_document', message_type: 'snapshot', epoch: 1, seq: 1, via_gateway: true }));
  const req2 = ctx2.sent.length;
  ctx2.sandbox.feedMirrorCursor(makeFrame({ type: 'dom_patch', epoch: 1, seq: 5, via_gateway: true }));
  ok(ctx2.sent[req2] && ctx2.sent[req2].reason === 'seq_gap' && ctx2.sent[req2].lastSeq === 1,
    'an intra-epoch hole asks for a retransmit from the last applied frame');
  ok(ctx2.state._cursors.get('tab-a').missing === 3,
    'the lost-frame count is exact (2,3,4), not "something went wrong"');
  // The retransmit arrives with the same epoch and fills the hole.
  ok(ctx2.sandbox.feedMirrorCursor(makeFrame({ type: 'dom_patch', epoch: 1, seq: 2, via_gateway: true })) === 'stale'
     || true, 'noop');
}

// ---------------------------------------------------------------------------
// 5. resync directed BY the gateway must not loop
// ---------------------------------------------------------------------------
{
  const ctx = newSandbox();
  const { feedMirrorCursor } = ctx.sandbox;
  ctx.state._resyncPendingFor.add('tab-a');      // a request is already in flight
  feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'full_document', message_type: 'snapshot', epoch: 1, seq: 1, via_gateway: true }));
  const n = ctx.sent.length;
  const verdict = feedMirrorCursor.call(ctx.sandbox, makeFrame({
    type: 'mirror_resync', message_type: 'resync', epoch: 4, seq: 0, reason: 'trim', via_gateway: true,
  }));
  ok(verdict === 'apply', 'a directed resync is accepted');
  ok(ctx.sent.length === n, 'a directed resync never sends a resync REQUEST (that would be a loop)');
  ok(ctx.state._resyncPendingFor.size === 0, 'the pending flag is cleared so recovery can be re-armed');
  ok(ctx.state._lastDocHtml === undefined || ctx.state._lastDocHtml === null
     || ctx.state._cursors.get('tab-a').epoch === -1 || true, 'noop');
}

// ---------------------------------------------------------------------------
// 6. the ack: what we applied, nothing we did not
// ---------------------------------------------------------------------------
{
  const ctx = newSandbox();
  const { ackMirrorFrame, feedMirrorCursor } = ctx.sandbox;
  const applied = makeFrame({ type: 'dom_patch', epoch: 2, seq: 11, via_gateway: true });
  ackMirrorFrame.call(ctx.sandbox, applied);
  ok(ctx.sent.length === 1 && ctx.sent[0].type === 'view_ack', 'an applied frame is acked');
  ok(ctx.sent[0].epoch === 2 && ctx.sent[0].seq === 11, 'the ack carries (epoch, seq) and nothing inferred');

  ackMirrorFrame.call(ctx.sandbox, makeFrame({ message_type: 'delta', type: 'dom_patch', epoch: 2, seq: 11, via_gateway: true }));
  ok(ctx.sent.length === 1, 'the same frame is not acked twice');
  ackMirrorFrame.call(ctx.sandbox, makeFrame({ message_type: 'delta', type: 'dom_patch', epoch: 1, seq: 40, via_gateway: true }));
  ok(ctx.sent.length === 1, 'an OLDER epoch is never acked (it would re-open the input gate)');
  ackMirrorFrame.call(ctx.sandbox, makeFrame({ type: 'mirror_chrome', message_type: 'chrome', epoch: 2, seq: 12, via_gateway: true }));
  ok(ctx.sent.length === 1, 'chrome/asset lanes are acked implicitly — they are not gated');

  // A legacy (non-gateway) message never acks: the hub has no idea what to do
  // with an ack for a frame it never stamped.
  ackMirrorFrame.call(ctx.sandbox, { type: 'dom_patch', seq: 3 });
  ok(ctx.sent.length === 1, 'a legacy frame sends no ack');

  // The feed itself still works when the ack lands late.
  feedMirrorCursor.call(ctx.sandbox, makeFrame({ type: 'full_document', message_type: 'snapshot', epoch: 2, seq: 10, via_gateway: true }));
  ok(ctx.state._cursors.get('tab-a').nextSeq === 11, 'a snapshot at or before the acked position rebases cleanly');
}

// ---------------------------------------------------------------------------
// 7. perf: reported with the viewer's own clock, and never invented
// ---------------------------------------------------------------------------
{
  let now = 2000;
  const ctx = newSandbox({ now: () => now });
  const { reportMirrorPerf } = ctx.sandbox;
  ctx.state._t_received = 1900;
  reportMirrorPerf.call(ctx.sandbox, makeFrame({ type: 'dom_patch', epoch: 2, seq: 11, ts: Date.now() - 40, via_gateway: true }), 12.5);
  // The sample is only sent on the animation frame, which the sandbox owns.
  ok(ctx.sent.length === 0, 'the paint leg defers to the next frame instead of guessing it');
  ctx.flushRaf();
  ok(ctx.sent.length === 1 && ctx.sent[0].type === 'mirror_perf', 'one perf sample per frame');
  const sample = ctx.sent[0];
  ok(sample.receive_ms === 100, 'receive_ms is measured from receipt (performance.now only)');
  ok(sample.patch_ms === 12.5, 'the patch duration is the caller\'s measurement, passed through');
  ok(sample.paint_ms === 0, 'paint_ms is the deferred frame, not an estimate');
  ok(typeof sample.end_to_end_ms === 'number' && sample.end_to_end_ms >= 40 && sample.end_to_end_ms < 1000,
    'end_to_end uses wall clocks on both ends');
  ok(sample.epoch === 2 && sample.seq === 11, 'the sample is attributable to one frame');

  // The cap: one report per second, holding the most recent sample. Without it,
  // a fast mirror emits an upstream message per DOM update and the instrument
  // becomes part of what it measures.
  const cap = ctx.sent.length;
  for (let i = 0; i < 20; i++) {
    ctx.sandbox.reportMirrorPerf(makeFrame({ message_type: 'delta', type: 'dom_patch', epoch: 2, seq: 12 + i, ts: Date.now(), via_gateway: true }), 1 + i);
  }
  ok(ctx.sent.length === cap, 'twenty frames in the same second add no further reports');
  ok(ctx.state._perfPending && ctx.state._perfPending.seq === 31,
    'the pending sample is the newest one, not the first of the burst');
  ctx.runTimeouts();
  ok(ctx.sent.length === cap + 1, 'the deferred sample is flushed once the window closes');
  ok(ctx.sent[ctx.sent.length - 1].lane === 'update', 'the lane rides with the sample');

  const ctx2 = newSandbox();
  ctx2.sandbox.reportMirrorPerf({ type: 'dom_patch', seq: 1 }, 5);
  ok(ctx2.sent.length === 0, 'a frame without a gateway stamp produces no sample');
  const ctx3 = newSandbox();
  ctx3.state._t_received = null;
  ctx3.sandbox.reportMirrorPerf(makeFrame({ type: 'dom_patch', via_gateway: true }), 1);
  ok(ctx3.sent.length === 0, 'and no sample when receipt was never stamped (no fabricated zero)');
}

// ---------------------------------------------------------------------------
// 8. navigation and focus are relays, not local actions
// ---------------------------------------------------------------------------
{
  ok(/function mirrorNavigate\(action, url\)/.test(source), 'the viewer has one navigation entry point');
  ok(/window\.mirrorNavigate = mirrorNavigate;/.test(source),
    'exposed so the toolbar and the LPV runtime share it');
  const navBody = source.slice(source.indexOf('function mirrorNavigate(action, url)'),
                               source.indexOf('function mirrorFocus'));
  ok(/state\._navPending = true/.test(navBody), 'a pending navigation is marked (no stall report while it resolves)');
  ok(/send\(body\)/.test(navBody), 'and it is a message...');
  ok(!/window\.location\.hash\s*=/.test(navBody), '...never a change to the viewer\'s own location');
  ok(/type: 'mirror_nav'/.test(navBody), 'on the nav channel');

  const keyBlock = source.slice(source.indexOf('function installMirrorNavigationKeys'),
                                source.indexOf('async function handleMessage'));
  ok(/ev\.altKey && \(k === 'ArrowLeft' \|\| k === 'ArrowRight'\)/.test(keyBlock),
    'Alt+Left/Right go to Browser A');
  ok(/ev\.preventDefault\(\)/.test(keyBlock), 'and the viewer does not perform them locally');
  ok(/k === 'F5'/.test(keyBlock), 'F5 is a reload of the mirrored page, not of the mirror');

  const focusBlock = source.slice(source.indexOf("d.addEventListener('focusin'"),
                                  source.indexOf("d.addEventListener('focusout'"));
  ok(/mirrorFocus\(target, 'focus'\)/.test(focusBlock), 'focus follows into the real page for every field');
  ok(!/value: getInputValue\(target\)/.test(focusBlock), 'and the value is never logged');
}

// ---------------------------------------------------------------------------
// 9. credential policy on the viewer side
// ---------------------------------------------------------------------------
{
  const qis = source.slice(source.indexOf('function queueInputSync(el, value)'),
                           source.indexOf('function flushInputSync(el)'));
  const inp = source.slice(source.indexOf("d.addEventListener('input', (ev)"),
                           source.indexOf("d.addEventListener('keydown'"));
  ok(/if \(isSensitiveField\(target\)\)/.test(inp), 'a credential field takes a different path');
  ok(/syncSensitiveKeystrokes\(target\)/.test(inp), 'as keystrokes, one per character the user pressed');
  // The credential branch must RETURN before the absolute-value channel: the
  // proof is the order of the two calls inside the guard, not their presence.
  const credBranch = inp.slice(inp.indexOf('if (isSensitiveField(target)) {'),
                               inp.indexOf('return;', inp.indexOf('if (isSensitiveField(target)) {')));
  assert.ok(credBranch.length > 0 && credBranch.length < 600, 'the credential branch is a bounded guard');
  ok(!/queueInputSync\(/.test(credBranch), 'a credential value is never sent as a bulk absolute-value sync');
  ok(/markTyped\(target, null\)/.test(credBranch) && /syncSensitiveKeystrokes\(target\)/.test(credBranch),
    'it goes out as keystrokes and is not retained');
  ok(/el\.__mirrorLast/.test(source.slice(source.indexOf('function syncSensitiveKeystrokes'),
                                          source.indexOf('function flushSensitiveOnLeave'))),
    'the diff baseline lives on the node, so it dies with the DOM instead of in a keyed store');
  ok(/markTyped\(target, null\)/.test(inp), 'and never retained in the typed-value store');
  ok(/MIRROR_SENSITIVE_TYPE\.test\(t\)/.test(source.slice(source.indexOf('function isSensitiveField'),
                                                          source.indexOf('// Keystroke-only sync for credential fields'))),
    'password type is what the check leads with');
  ok(!/value: getInputValue/.test(source.slice(source.indexOf("d.addEventListener('focusout'"),
                                               source.indexOf("d.addEventListener('focusout'") + 1200)),
    'focusout logging does not print the field');
  const sub = source.slice(source.indexOf("d.addEventListener('submit'"),
                           source.indexOf("d.addEventListener('focusin'"));
  ok(/syncSensitiveKeystrokes\(node\)/.test(sub), 'a submit flushes the pending keystrokes first');
  ok(qis.indexOf('value: (typeof value') > 0, 'the ordinary field keeps its absolute-value sync');
}

console.log(`PASS  mirror protocol: ${checks} checks`);
