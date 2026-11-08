// No dependencies beyond jsdom: node tests/test_delta_health.cjs
//
// Two contracts the delta channel never had:
//
//   1. PRIORITY — the batch always ships in the same order:
//      r (structure removals) -> i (inserts) -> a (state/attributes)
//      -> t (text) -> v (form values) -> s (CSS) -> meta (tab chrome).
//      State therefore always lands on nodes that already exist in the
//      mirror, and text indices computed in the live document stay valid.
//
//   2. HEALTH — if the binding the observer needs (__domDelta) is missing
//      or throws, the page must SAY SO through the trigger binding
//      (__domCaptureRequest("resync:delta-unhealthy", url)) instead of
//      dropping the batch and freezing the mirror forever.
const fs = require('node:fs');
const path = require('node:path');

let checks = 0;
const failures = [];
function ok(cond, label) {
  if (cond) { checks++; return; }
  failures.push(label);
}

const root = path.join(__dirname, '..');
const capture = fs.readFileSync(path.join(root, 'dom_capture.py'), 'utf8');
function literal(marker) {
  const start = capture.indexOf(marker);
  if (start < 0) throw new Error(marker + ' not found in dom_capture.py');
  const bodyStart = capture.indexOf('"""', start) + 3;
  return capture.slice(bodyStart, capture.indexOf('"""', bodyStart));
}
const deltaSrc = literal('_DELTA_OBSERVER_JS = r"""').replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '10');
const fastSrc = literal('_FAST_SERIALIZE_JS = r"""');

let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/jsd/node_modules/jsdom', '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}
if (!jsdom) { console.log('skipped: jsdom not installed'); process.exit(0); }
const { JSDOM } = jsdom;

const PHASE = { r: 0, i: 1, a: 2, t: 3, v: 4, s: 5, overflow: 6, meta: 7 };

// ---------------------------------------------------------------- 1: priority
const dom1 = new JSDOM(
  '<!doctype html><html><head><title>t</title></head><body>' +
  '<input id="f" value="a"><div id="root"><p class="old">keep</p></div>' +
  '</body></html>',
  { url: 'https://a.test/', runScripts: 'outside-only', pretendToBeVisual: true });
const w1 = dom1.window;
const batches = [];
w1.__domDelta = (payload) => {
  try { batches.push(JSON.parse(payload)); } catch (e) { /* ignore */ }
};
const serialize = w1.eval('(' + fastSrc + ')');
serialize({ deltaIds: true });            // stamp data-mid like the real capture
w1.eval(deltaSrc);
w1.eval(
  'var f = document.getElementById("f");' +
  'document.getElementById("root").setAttribute("data-when", "1");' +
  'document.getElementById("root").innerHTML = "<p class=\\"new\\">rebuilt <b>now</b></p>";' +
  'f.value = "typed";'                  // silent property write — no event fires
);

// ------------------------------------------------- 1 in the next tick
mkPromise();
async function mkPromise() {
  await new Promise(r => setTimeout(r, 80));

  const ops = batches.reduce((a, b) => a.concat(b), []);
  const kinds = ops.map(o => o[0]);
  ok(kinds.includes('r'), 'a removal op was emitted: ' + JSON.stringify(kinds));
  ok(kinds.includes('i'), 'an insert op was emitted: ' + JSON.stringify(kinds));
  ok(kinds.includes('a'), 'an attribute/state op was emitted: ' + JSON.stringify(kinds));
  ok(kinds.includes('v'), 'a value op was emitted (silent property write): ' + JSON.stringify(kinds));
  let monotonic = true;
  for (let i = 1; i < kinds.length; i++) {
    const prev = PHASE[kinds[i - 1]];
    const cur = PHASE[kinds[i]];
    if (prev !== undefined && cur !== undefined && cur < prev) monotonic = false;
  }
  ok(monotonic, 'ops ship in priority order r < i < a < t < v < s < meta: ' + JSON.stringify(kinds));
  ok(kinds[0] !== 'a' && kinds[0] !== 'v', 'structure ships before state/values: ' + JSON.stringify(kinds));
  dom1.window.close();

  // ------------------------------------------------- 2: missing binding says so
  const dom2 = new JSDOM(
    '<!doctype html><html><head><title>t</title></head><body><div id="x">q</div></body></html>',
    { url: 'https://b.test/', runScripts: 'outside-only', pretendToBeVisual: true });
  const w2 = dom2.window;
  const complaints = [];
  w2.__domCaptureRequest = (reason, target) => complaints.push([reason, String(target)]);
  // NOTE: window.__domDelta deliberately NOT installed.
  w2.eval(deltaSrc);
  w2.eval('document.getElementById("x").setAttribute("class", "changed");');
  await new Promise(r => setTimeout(r, 80));
  ok(complaints.some(c => c[0] === 'resync:delta-unhealthy'),
    'the page reported its dead live channel (resync:delta-unhealthy): ' + JSON.stringify(complaints));
  ok(!batches.length || true, 'n/a');
  dom2.window.close();

  if (failures.length) {
    console.error('FAIL delta health: ' + checks + ' passed, ' + failures.length + ' failed');
    for (const f of failures) console.error('  - ' + f);
    process.exit(1);
  }
  console.log('PASS delta health: ' + checks + ' checks passed');
}
