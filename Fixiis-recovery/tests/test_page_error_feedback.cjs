// No dependencies beyond jsdom: node tests/test_page_error_feedback.cjs
//
// Page-side failure telemetry. The captured page's scripts never run in the
// mirror, so an uncaught exception there is invisible — this suite proves
// the observer catches it (and only it) and ships a control op, and the
// client's badge renders it without ever parsing it as HTML.
//
// Observer side (jsdom, real _DELTA_OBSERVER_JS from dom_capture.py):
//   1. an uncaught exception ships ['err','uncaught', message, source, line];
//   2. an unhandled rejection ships ['err','rejection', ...];
//   3. a dead subresource (capture-phase error) ships ['err','resource', ...];
//   4. identical failures inside 5 s are deduped to a single op;
//   5. at most 20 error ops per document;
//   6. the error op lands AFTER DOM ops in its batch (phase 7).
//
// Client side (jsdom, real functions extracted from client.html):
//   7. notePageError creates the badge and counts;
//   8. hostile payloads render as TEXT, never as elements (no <img>, no handler);
//   9. badge toggles the panel; Dismiss removes badge and panel.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.join(__dirname, '..');
const capture = fs.readFileSync(path.join(root, 'dom_capture.py'), 'utf8');
const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');

function literal(marker) {
  const start = capture.indexOf(marker);
  assert.ok(start >= 0, marker + ' not found');
  const bodyStart = capture.indexOf('"""', start) + 3;
  return capture.slice(bodyStart, capture.indexOf('"""', bodyStart));
}
const deltaSrc = literal('_DELTA_OBSERVER_JS = r"""');

let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/jsd/node_modules/jsdom', '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}
if (!jsdom) { console.log('skipped: jsdom not installed'); process.exit(0); }
const { JSDOM } = jsdom;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const flat = (batches) => batches.reduce((a, b) => a.concat(b), []);

let checks = 0;
const failures = [];
function ok(cond, label) {
  if (cond) { checks++; return; }
  failures.push(label);
}

(async () => {
  // ---------------- observer side ----------------
  {
    const dom = new JSDOM(
      '<!doctype html><html><head><title>t</title></head><body><div id="root"><p class="a">one</p></div></body></html>',
      { url: 'https://example.test/', runScripts: 'outside-only', pretendToBeVisual: true });
    const win = dom.window;
    const batches = [];
    win.__domDelta = (payload) => { try { batches.push(JSON.parse(payload)); } catch (e) { /* ignore */ } };
    win.eval(deltaSrc.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '15'));
    await sleep(60);
    const before = batches.length;

    // 1. uncaught exception
    win.eval(`window.dispatchEvent(new ErrorEvent('error', { message: 'TypeError: boom', filename: 'https://example.test/app.js', lineno: 42 }))`);
    await sleep(80);
    let ops = flat(batches.slice(before));
    const uncaught = ops.find((o) => o[0] === 'err' && o[1] === 'uncaught');
    ok(!!uncaught, 'an uncaught exception ships an err op');
    ok(uncaught && /boom/.test(String(uncaught[2])), 'the op carries the page error message');
    ok(uncaught && String(uncaught[3]).includes('app.js'), 'the op carries the source file');
    ok(uncaught && Number(uncaught[4]) === 42, 'the op carries the line number');

    // 2. unhandled rejection (synthetic event with a .reason)
    win.eval(`var e = new Event('unhandledrejection'); e.reason = new Error('async boom'); window.dispatchEvent(e);`);
    await sleep(80);
    ops = flat(batches.slice(before));
    ok(ops.some((o) => o[0] === 'err' && o[1] === 'rejection' && /async boom/.test(String(o[2]))),
      'an unhandled rejection ships an err op');

    // 3. dead subresource (capture-phase error on the element)
    win.eval(`var img = document.createElement('img'); img.src = 'https://example.test/missing.png'; document.getElementById('root').appendChild(img); img.dispatchEvent(new Event('error'));`);
    await sleep(120);
    ops = flat(batches.slice(before));
    ok(ops.some((o) => o[0] === 'err' && o[1] === 'resource' && /img/.test(String(o[2]))),
      'a dead image ships an err op');

    // 4. dedupe: same uncaught error 3x → exactly one op for it after the flush
    const baseline = flat(batches.slice(before)).filter((o) => o[0] === 'err' && /TypeError: boom/.test(String(o[2]))).length;
    win.eval(`for (var i = 0; i < 3; i++) window.dispatchEvent(new ErrorEvent('error', { message: 'TypeError: boom', filename: 'https://example.test/app.js', lineno: 42 }))`);
    await sleep(80);
    ops = flat(batches.slice(before)).filter((o) => o[0] === 'err' && /TypeError: boom/.test(String(o[2])));
    ok(ops.length === baseline, 'identical failures inside 5 s are deduped');

    // 6. ordering: a DOM mutation and an error under one flush → mutation first
    const mark = batches.length;
    win.eval(`
      var p = document.createElement('p'); p.textContent = 'ordered';
      document.getElementById('root').appendChild(p);
      window.dispatchEvent(new ErrorEvent('error', { message: 'ordering probe', filename: '', lineno: 0 }));
    `);
    await sleep(90);
    ops = flat(batches.slice(mark));
    const domIdx = ops.findIndex((o) => o[0] === 'i' || o[0] === 'a' || o[0] === 't');
    const errIdx = ops.findIndex((o) => o[0] === 'err' && /ordering probe/.test(String(o[2])));
    ok(domIdx >= 0 && errIdx >= 0, 'the batch carries both the mutation and the error');
    ok(domIdx >= 0 && errIdx >= 0 && domIdx < errIdx, 'the error op lands after the DOM ops');

    // 5. cap: 25 distinct failures → at most 20 err ops total in the document
    const dom2 = new JSDOM(
      '<!doctype html><html><head><title>t</title></head><body><div id="root"></div></body></html>',
      { url: 'https://example.test/', runScripts: 'outside-only', pretendToBeVisual: true });
    const win2 = dom2.window;
    const batches2 = [];
    win2.__domDelta = (payload) => { try { batches2.push(JSON.parse(payload)); } catch (e) { /* ignore */ } };
    win2.eval(deltaSrc.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '15'));
    await sleep(60);
    win2.eval(`for (var i = 0; i < 25; i++) window.dispatchEvent(new ErrorEvent('error', { message: 'distinct failure number ' + i, filename: '', lineno: 0 }))`);
    await sleep(120);
    const total = flat(batches2).filter((o) => o[0] === 'err').length;
    ok(total <= 20, 'at most 20 error ops per document (got ' + total + ')');
    win2.close();
    win.close();
  }

  // ---------------- client side ----------------
  {
    const A = client.indexOf('// ---- page health telemetry');
    const B = client.indexOf('function stripDeadMirrorNodes');
    ok(A >= 0 && B > A, 'the telemetry block is present in client.html');
    const src = client.slice(A, B) + '\nreturn { notePageError, pageErrorRing };\n';
    const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://x.test/', runScripts: 'outside-only' });
    const w = dom.window;
    const d = w.document;
    const api = new w.Function('window', 'document', src)(w, d);

    const hostile = '<img src=x onerror="window.__stolen = 1"><script>window.__stolen2 = 1</script>';
    api.notePageError('uncaught', hostile, 'https://x.test/app.js', 7);
    api.notePageError('rejection', 'second failure', '', 0);
    api.notePageError('resource', 'img failed to load', 'https://x.test/missing.png', 0);

    const badge = d.getElementById('pageHealthBadge');
    ok(!!badge, 'the first failure creates the badge');
    ok(badge && /3 page errors/.test(badge.textContent), 'the badge counts failures');
    ok(w.__stolen === undefined && w.__stolen2 === undefined, 'hostile payload never executed');

    badge.click();
    const panel = d.getElementById('pageHealthPanel');
    ok(!!panel, 'clicking the badge opens the panel');
    ok(panel && panel.querySelector('img') === null, 'hostile payload rendered no <img> element');
    ok(panel && panel.querySelector('script') === null, 'hostile payload rendered no <script> element');
    const rows = panel ? panel.querySelectorAll('[data-role="list"] > div') : [];
    ok(rows.length === 3, 'one row per failure');
    ok(rows.length === 3 && rows[0].textContent.includes('onerror='), 'the raw message is shown as text');

    const dismiss = panel ? Array.from(panel.querySelectorAll('button')).find((b) => b.textContent === 'Dismiss') : null;
    ok(!!dismiss, 'the panel has a Dismiss button');
    if (dismiss) {
      dismiss.click();
      ok(!d.getElementById('pageHealthBadge'), 'Dismiss removes the badge');
      ok(!d.getElementById('pageHealthPanel'), 'Dismiss removes the panel');
    }
    w.close();
  }

  if (failures.length) {
    console.error('FAIL page error feedback: ' + checks + ' passed, ' + failures.length + ' failed');
    for (const f of failures) console.error('  - ' + f);
    process.exit(1);
  }
  console.log('PASS page error feedback: ' + checks + ' checks passed');
  process.exit(0);
})().catch((e) => {
  console.error('FAIL page error feedback: harness error: ' + (e && e.stack || e));
  process.exit(1);
});
