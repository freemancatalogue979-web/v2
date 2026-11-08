// No dependencies beyond jsdom: node tests/test_spa_nav_sync.cjs
//
// Spec: URL changes must synchronize IMMEDIATELY — a same-document SPA
// navigation (pushState/replaceState/popstate/hashchange) moves the address
// with no document load, so it ships as a ['nav'] control op on the delta
// channel instead of waiting for polling or a capture cycle.
//
//   1. the observer's SPA watcher wraps pushState and queues ['nav', url];
//   2. the same for replaceState;
//   3. popstate and hashchange push ['nav', url] too;
//   4. an unchanged URL is not re-announced (dedup);
//   5. the op is pushed even when no DOM mutation occurred (flush scheduled);
//   6. through the real observer: a pushState ships a batch containing nav;
//   7. client source contract: the op loop applies 'nav', and a stale
//      full_document (generation older than lastGeneration) is refused —
//      rapid navigations can never overwrite a newer state.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.join(__dirname, '..');
const capture = fs.readFileSync(path.join(root, 'dom_capture.py'), 'utf8');
const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/jsd/node_modules/jsdom', '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}
if (!jsdom) { console.log('skipped: jsdom not installed'); process.exit(0); }
const { JSDOM } = jsdom;

let checks = 0;
const failures = [];
function ok(cond, label) {
  if (cond) { checks++; return; }
  failures.push(label);
}

function literal(marker) {
  const start = capture.indexOf(marker);
  assert.ok(start >= 0, marker + ' not found');
  const bodyStart = capture.indexOf('"""', start) + 3;
  return capture.slice(bodyStart, capture.indexOf('"""', bodyStart));
}
const deltaSrc = literal('_DELTA_OBSERVER_JS = r"""');

// ---- watcher behavior, in isolation --------------------------------------
{
  const W = capture.indexOf('// SPA navigation watcher');
  const Z = capture.indexOf('const push = (op) => {', W);
  assert.ok(W >= 0 && Z > W, 'SPA watcher block not found in dom_capture.py');
  const watcherSrc = capture.slice(W, Z);

  // A fresh window per scenario: the watcher installs history wrappers and
  // listeners on the shared window, so reusing one would cross-contaminate.
  const fresh = () => {
    const dom = new JSDOM('<!doctype html><html><body></body></html>',
      { url: 'https://example.test/', runScripts: 'outside-only' });
    const win = dom.window;
    const ops = [];
    const env = {
      win, ops, scheduled: 0,
      OP_CAP: 5000,
      markOverflow() { ops.push(['overflow']); },
      schedule() { env.scheduled++; },
    };
    const fn = new win.Function('window', 'location', 'ops', 'OP_CAP', 'markOverflow', 'schedule', watcherSrc);
    fn(win, win.location, ops, env.OP_CAP, env.markOverflow, env.schedule);
    return env;
  };

  for (const scenario of [
    { name: 'pushState', act: (w) => w.history.pushState({}, '', '/next') },
    { name: 'replaceState', act: (w) => w.history.replaceState({}, '', '/other') },
  ]) {
    const env = fresh();
    scenario.act(env.win);
    const nav = env.ops.find((o) => o[0] === 'nav');
    ok(!!nav, scenario.name + " queues a ['nav'] op");
    ok(nav && String(nav[1]).startsWith('https://example.test/'), scenario.name + ' op carries the absolute URL');
    ok(nav && nav[2] === 'spa', scenario.name + ' op is marked as spa');
    ok(env.scheduled === 1, scenario.name + ' flush is scheduled (ships without any DOM mutation)');
    env.win.close();
  }

  { // dedupe: identical push twice -> one op
    const env = fresh();
    env.win.history.pushState({}, '', '/only-once');
    env.scheduled = 0; env.ops.length = 0;
    env.win.history.pushState({}, '', '/only-once');
    ok(env.ops.filter((o) => o[0] === 'nav').length === 0, 'an unchanged URL is not re-announced');
    env.win.close();
  }

  { // popstate: the listener ships ['nav'] when the URL differs from the last one seen
    const env = fresh();
    // Simulate "the last URL the watcher saw was elsewhere" (exactly the
    // situation a back/forward traversal creates): then a popstate must
    // re-announce the current location.
    env.win.__shifixSpaNavLast = 'https://example.test/somewhere-else';
    env.win.dispatchEvent(new env.win.Event('popstate'));
    ok(env.ops.some((o) => o[0] === 'nav' && String(o[1]).startsWith('https://example.test/')), "popstate queues a ['nav'] op");
    ok(env.scheduled === 1, 'popstate schedules a flush');
    // and an immediate duplicate popstate is deduped (same URL, same last)
    env.scheduled = 0; env.ops.length = 0;
    env.win.dispatchEvent(new env.win.Event('popstate'));
    ok(env.ops.length === 0 && env.scheduled === 0, 'a popstate for the same URL is deduped');
    env.win.close();
  }
}

// ---- through the real observer --------------------------------------------
(async () => {
  const dom = new JSDOM(
    '<!doctype html><html><head><title>t</title></head><body><div id="root"><p>one</p></div></body></html>',
    { url: 'https://example.test/start', runScripts: 'outside-only', pretendToBeVisual: true });
  const win = dom.window;
  const batches = [];
  win.__domDelta = (payload) => { try { batches.push(JSON.parse(payload)); } catch (e) { /* ignore */ } };
  win.eval(deltaSrc.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '15'));
  await sleep(60);
  const before = batches.length;
  win.eval(`window.history.pushState({}, '', '/end')`);
  await sleep(80);
  const ops = batches.slice(before).reduce((a, b) => a.concat(b), []);
  const nav = ops.find((o) => o[0] === 'nav');
  ok(!!nav, 'a pushState ships a [\'nav\'] op inside a real observer batch');
  ok(nav && String(nav[1]).endsWith('/end'), 'the batch nav op carries the new URL');
  win.close();

  // ---- client source contracts -------------------------------------------
  ok(/kind === 'nav'/.test(client), "the client's op loop handles kind 'nav'");
  ok(/reportPageChange\(op\[1\]\)/.test(client), 'the nav branch updates the client URL state');
  ok(/message\.generation < state\.lastGeneration/.test(client),
     'a stale full_document (older generation) is refused');
  ok(!/#transition-overlay/.test(client), 'the transition overlay is gone from the client');
  ok(!/showTransitionOverlay|hideTransitionOverlay/.test(client), 'no overlay entry points remain');

  if (failures.length) {
    console.error('FAIL spa nav sync: ' + checks + ' passed, ' + failures.length + ' failed');
    for (const f of failures) console.error('  - ' + f);
    process.exit(1);
  }
  console.log('PASS spa nav sync: ' + checks + ' checks passed');
  process.exit(0);
})().catch((e) => {
  console.error('FAIL spa nav sync: harness error: ' + (e && e.stack || e));
  process.exit(1);
});
