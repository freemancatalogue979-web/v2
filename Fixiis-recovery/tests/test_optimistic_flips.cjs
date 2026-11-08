// No dependencies beyond jsdom: node tests/test_optimistic_flips.cjs
//
// Tier 1 optimistic UI: the mirror flips pure-UI state at CLICK time
// instead of waiting for the server's answer — then the server's answer
// (or, failing that, time itself) has the last word:
//
//   1. aria-expanded / its aria-controls panel flip instantly on click;
//   2. <details> open/closed flips on a summary click;
//   3. role=tab selection (+ panels) flips instantly;
//   4. role=switch / role=checkbox flips aria-checked;
//   5. if the server NEVER echoes the attribute, the flip reverts after ~900ms;
//   6. if the server echoes the attribute (applyDomPatch route), the flip is
//      confirmed — no revert ever happens;
//   7. a flip whose element leaves the document is pruned, not reverted.
//
// The flip machinery is the exact block shipped in client.html — extracted by
// source markers and evaluated, so this suite tests what runs, not a copy.
const fs = require('node:fs');
const path = require('node:path');

let checks = 0;
const failures = [];
function ok(cond, label) {
  if (cond) { checks++; return; }
  failures.push(label);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const root = path.join(__dirname, '..');
const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');
const A = client.indexOf('// ---- Tier 1 optimistic UI');
const B = client.indexOf('// Do not cancel mousedown/pointerdown');
if (A < 0 || B < 0 || B <= A) {
  console.error('FAIL optimistic flips: Tier 1 block not found in client.html');
  process.exit(1);
}
const flipBlock = client.slice(A, B)
  + '\nwindow.__testFlip = applyOptimisticFlip;\n';

let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/jsd/node_modules/jsdom', '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}
if (!jsdom) { console.log('skipped: jsdom not installed'); process.exit(0); }
const { JSDOM } = jsdom;

const HTML = `<!doctype html><html><head><title>t</title></head><body>
  <button id="menuBtn" aria-expanded="false" aria-controls="panel">Menu</button>
  <div id="panel" hidden>panel body</div>
  <details id="d"><summary id="sum">more</summary><p>hi</p></details>
  <div role="tablist" id="list">
    <button role="tab" id="tab1" aria-selected="true" aria-controls="pn1">One</button>
    <button role="tab" id="tab2" aria-selected="false" aria-controls="pn2">Two</button>
  </div>
  <div role="tabpanel" id="pn1">first</div>
  <div role="tabpanel" id="pn2" hidden>second</div>
  <div role="switch" id="sw" aria-checked="false">Switch</div>
</body></html>`;

function makeHarness() {
  const dom = new JSDOM(HTML, { url: 'https://x.test/', runScripts: 'outside-only' });
  const w = dom.window;
  const d = w.document;
  const fn = new w.Function('d', 'window', 'document',
    flipBlock + '\nreturn window.__testFlip;');
  const flip = fn(d, w, d);
  return { dom, w, d, flip };
}

(async () => {
  // ---- 1-4: instan: flips happen at click time -------------------------
  {
    const { w, d, flip } = makeHarness();
    flip(d.getElementById('menuBtn'));
    ok(d.getElementById('menuBtn').getAttribute('aria-expanded') === 'true', 'menu flips expanded instantly');
    ok(!d.getElementById('panel').hasAttribute('hidden'), 'its panel unhides instantly');
    flip(d.getElementById('sum'));
    ok(d.getElementById('d').hasAttribute('open'), '<details> opens on summary click');
    flip(d.getElementById('tab2'));
    ok(d.getElementById('tab2').getAttribute('aria-selected') === 'true', 'tab2 selected instantly');
    ok(d.getElementById('tab1').getAttribute('aria-selected') === 'false', 'tab1 deselected');
    ok(!d.getElementById('pn2').hasAttribute('hidden'), 'panel 2 shown');
    ok(d.getElementById('pn1').hasAttribute('hidden'), 'panel 1 hidden');
    flip(d.getElementById('sw'));
    ok(d.getElementById('sw').getAttribute('aria-checked') === 'true', 'switch flips checked');
    w.close();
  }

  // ---- 5: the server never speaks -> everything reverts -----------------
  {
    const { w, d, flip } = makeHarness();
    flip(d.getElementById('menuBtn'));
    flip(d.getElementById('sum'));
    flip(d.getElementById('tab2'));
    flip(d.getElementById('sw'));
    await sleep(1300);
    ok(d.getElementById('menuBtn').getAttribute('aria-expanded') === 'false', 'menu reverted');
    ok(d.getElementById('panel').hasAttribute('hidden'), 'panel hidden again');
    ok(!d.getElementById('d').hasAttribute('open'), 'details reverted');
    ok(d.getElementById('tab2').getAttribute('aria-selected') === 'false', 'tab2 deselected again');
    ok(d.getElementById('pn2').hasAttribute('hidden'), 'panel 2 hidden again');
    ok(d.getElementById('sw').getAttribute('aria-checked') === 'false', 'switch reverted');
    ok((w.__shfOptimisticFlips || []).length === 0, 'the flip registry drains');
    w.close();
  }

  // ---- 6: the server echoes -> confirmed, NO revert ---------------------
  {
    const { w, d, flip } = makeHarness();
    flip(d.getElementById('menuBtn'));
    // Simulate applyDomPatch: op ['a', mid, 'aria-expanded', 'true']
    const btn = d.getElementById('menuBtn');
    btn.setAttribute('aria-expanded', 'true');
    w.__shfNoteOptimisticEcho(btn, 'aria-expanded');
    await sleep(1300);
    ok(btn.getAttribute('aria-expanded') === 'true', 'a confirmed flip is never reverted');
    ok(btn.getAttribute('data-shf-optimistic') === null, 'the echo clears the marker');
    ok((w.__shfOptimisticFlips || []).length === 0, 'the flip registry drains after an echo');
    w.close();
  }

  // ---- 6b: echo of a DIFFERENT attribute does not confirm this flip -----
  {
    const { w, d, flip } = makeHarness();
    flip(d.getElementById('menuBtn'));
    const btn = d.getElementById('menuBtn');
    w.__shfNoteOptimisticEcho(btn, 'class');   // unrelated op: not authoritative
    await sleep(1300);
    ok(btn.getAttribute('aria-expanded') === 'false', 'an unrelated echo does not confirm the flip');
    w.close();
  }

  // ---- 7: node removed mid-flight -> pruned, no revert attempted --------
  {
    const { w, d, flip } = makeHarness();
    flip(d.getElementById('menuBtn'));
    d.getElementById('menuBtn').remove();
    await sleep(1300);
    ok((w.__shfOptimisticFlips || []).length === 0, 'a removed element is pruned, not reverted');
    w.close();
  }

  if (failures.length) {
    console.error('FAIL optimistic flips: ' + checks + ' passed, ' + failures.length + ' failed');
    for (const f of failures) console.error('  - ' + f);
    process.exit(1);
  }
  console.log('PASS optimistic flips: ' + checks + ' checks passed');
})().catch((e) => {
  console.error('FAIL optimistic flips: harness error: ' + (e && e.stack || e));
  process.exit(1);
});
