// No dependencies beyond jsdom: node tests/test_live_navigation.cjs
//
// Navigation used to be detected by POLLING page.url on a timer (watch_url
// every 100-500 ms).  Polling is gone; the page itself announces URL changes
// through the interaction trigger binding the moment they happen — SPA
// pushState/replaceState, back/forward — plus one DOMContentLoaded signal
// per full document.  These checks pin that contract:
//
//   1. pushState/replaceState to a new URL emits ("navigation", url) at once;
//   2. a pushState to the SAME URL is a no-op (no redundant recapture);
//   3. history.back() (popstate) emits with the new URL;
//   4. a full document (readyState "loading" at install) emits exactly once,
//      at DOMContentLoaded — even when the URL did not change (same-URL
//      reload IS a new document);
//   5. no interval-based URL polling was introduced into the trigger.
//
// The server-side half (this signal -> one full capture) is covered by the
// Python suites; this file only exercises the in-page half.
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
const triggerSrc = literal('_INTERACTION_TRIGGER_JS = r"""');

let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/jsd/node_modules/jsdom', '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}
if (!jsdom) { console.log('skipped: jsdom not installed'); process.exit(0); }
const { JSDOM } = jsdom;

function makeWindow(url, { loading = false } = {}) {
  const dom = new JSDOM('<!DOCTYPE html><html><head></head><body>hi</body></html>', {
    url, runScripts: 'dangerously', pretendToBeVisual: true,
  });
  const w = dom.window;
  const calls = [];
  w.__domCaptureRequest = (reason, target) => { calls.push([reason, String(target)]); };
  if (loading) {
    Object.defineProperty(w.document, 'readyState', { get: () => 'loading', configurable: true });
  }
  w.eval(triggerSrc);
  return { dom, w, calls };
}
const navs = (calls) => calls.filter(c => c[0] === 'navigation');

(async () => {
  // ---- 1+2+3: same-document navigations, pushed by the page itself ---------
  {
    const { dom, w, calls } = makeWindow('https://a.test/one');
    ok(navs(calls).length === 0, 'no navigation emitted on install (document already complete)');
    w.history.pushState({}, '', '/two');
    ok(navs(calls).length === 1, 'pushState emitted exactly one navigation');
    ok(navs(calls)[0][1] === 'https://a.test/two', 'pushState carried the new URL: ' + JSON.stringify(navs(calls)[0]));
    w.history.replaceState({}, '', '/three?q=1');
    ok(navs(calls).length === 2, 'replaceState emitted a navigation');
    ok(navs(calls)[1][1] === 'https://a.test/three?q=1', 'replaceState carried the URL');
    w.history.pushState({}, '', '/three?q=1');
    ok(navs(calls).length === 2, 'a pushState to the SAME URL is a no-op (no recapture storm)');
    w.history.pushState({}, '', '/four');
    calls.length = 0;
    w.history.back();
    await new Promise(r => setTimeout(r, 80));
    ok(navs(calls).length === 1, 'back/forward popstate emitted a navigation');
    ok(navs(calls)[0] && navs(calls)[0][1] === 'https://a.test/three?q=1',
      'popstate carried the URL: ' + JSON.stringify(navs(calls)[0]));
    dom.window.close();
  }

  // ---- 4: full document load — one announcement, at DOMContentLoaded -------
  {
    const { dom, w, calls } = makeWindow('https://a.test/same', { loading: true });
    ok(navs(calls).length === 0, 'nothing emitted before the DOM is usable');
    Object.defineProperty(w.document, 'readyState', { get: () => 'complete', configurable: true });
    w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
    ok(navs(calls).length === 1, 'DOMContentLoaded emitted exactly one navigation');
    ok(navs(calls)[0][1] === 'https://a.test/same',
      'same-URL reload announced (URL is not the signal — the document is)');
    w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
    ok(navs(calls).length === 1, 'the announcement is once per document');
    dom.window.close();
  }

  // ---- 5: no polling was smuggled in ----------------------------------------
  ok(!/setInterval/.test(triggerSrc), 'the interaction trigger installs zero interval timers');

  if (failures.length) {
    console.error('FAIL live navigation: ' + checks + ' passed, ' + failures.length + ' failed');
    for (const f of failures) console.error('  - ' + f);
    process.exit(1);
  }
  console.log('PASS live navigation: ' + checks + ' checks passed');
})().catch(e => {
  console.error('FAIL live navigation: harness error: ' + (e && e.stack || e));
  process.exit(1);
});
