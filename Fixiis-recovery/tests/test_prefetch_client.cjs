#!/usr/bin/env node
/* The client half of intent prefetch, executed.

   The mirror should not wait to be asked.  When the cursor rests on a link the
   client tells the server where it is about to go, so that page's assets are
   in the memo before the click lands.  The rules that make this a hint and not
   a liability all live on this side of the wire:

     * same-site links only, never the page you are already on, never a
       fragment, never a download;
     * a hover has to RESIST for a moment — a cursor sweeping across the page
       must not fire eight prefetches;
     * a tap fires at once, because a tap navigates faster than a hover;
     * a hard budget per page, reset when the page changes;
     * every failure is swallowed: a hint must never break the mirror.

       NODE_PATH=/tmp/jsd/node_modules node tests/test_prefetch_client.cjs
*/
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let JSDOM = null;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  const vendored = path.join('/tmp/jsd/node_modules/jsdom');
  try { ({ JSDOM } = require(vendored)); } catch (e2) { JSDOM = null; }
}
if (!JSDOM) {
  console.log('SKIP  intent prefetch client: jsdom not installed');
  process.exit(0);
}

const ROOT = path.resolve(__dirname, '..');
const CLIENT = fs.readFileSync(path.join(ROOT, 'client.html'), 'utf8');

let pass = 0;
const fails = [];
function ok(cond, label) {
  if (cond) { pass++; return; }
  fails.push(label);
}
function eq(actual, expected, label) {
  ok(actual === expected, label + '\n     got: ' + JSON.stringify(actual) +
     '\n    want: ' + JSON.stringify(expected));
}

/* ------------------------------------------------------------------ setup */
const startMark = '// --- Intent prefetch ---';
const endMark = '// --- Progressive assets ---';
const start = CLIENT.indexOf(startMark);
const end = CLIENT.indexOf(endMark, start);
ok(start > 0 && end > start, 'the intent-prefetch block was not found in client.html');
const block = CLIENT.slice(start, end);

const sent = [];
let timers = [];

const sandbox = {
  console,
  state: { targetUrl: 'https://site.example/login' },
  send: (msg) => sent.push(msg),
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout: () => { timers = []; },
  URL, RegExp, String, Object, Number, Math, JSON, Array, Date,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(block, sandbox);

const { prefetchTarget, notePrefetchIntent, sendPrefetchHint, installPrefetchHints,
        resetPrefetch } = sandbox;

const PAGE = 'https://site.example/login';

function flushTimers() {
  const pending = timers.slice();
  timers = [];
  pending.forEach((t) => t.fn());
}

/* ------------------------------------------------- 1. what may be prefetched */
{
  const dom = new JSDOM('<a id="ok" href="/next">next</a>'
    + '<a id="ext" href="https://other.example/x">external</a>'
    + '<a id="hash" href="#top">top</a>'
    + '<a id="self" href="/login">self</a>'
    + '<a id="js" href="javascript:void(0)">js</a>'
    + '<a id="dl" href="/report.pdf" download>pdf</a>'
    + '<a id="blank" href="/newtab" target="_blank">tab</a>'
    + '<a id="mail" href="mailto:a@b.c">mail</a>', { url: PAGE });
  const doc = dom.window.document;
  const at = (id) => doc.getElementById(id);

  eq(prefetchTarget(at('ok'), PAGE), 'https://site.example/next',
     'a same-site link is the case this exists for');
  eq(prefetchTarget(at('ext'), PAGE), '', 'a cross-site link was prefetched');
  eq(prefetchTarget(at('hash'), PAGE), '', 'a fragment was prefetched');
  eq(prefetchTarget(at('self'), PAGE), '', 'the page already on screen was prefetched');
  eq(prefetchTarget(at('js'), PAGE), '', 'a javascript: link was prefetched');
  eq(prefetchTarget(at('dl'), PAGE), '', 'a download was prefetched');
  eq(prefetchTarget(at('blank'), PAGE), '', 'a new-tab link was prefetched');
  eq(prefetchTarget(at('mail'), PAGE), '', 'a mailto: link was prefetched');
  eq(prefetchTarget(doc.getElementById('ok').firstChild, PAGE), 'https://site.example/next',
     'the nearest anchor must be found from a child node (that is what hover targets)');
  eq(prefetchTarget(null, PAGE), '', 'a null target must not throw');
  eq(prefetchTarget(at('ok'), ''), '', 'with no page URL nothing can be resolved');
}

/* --------------------------------------------- 2. the hover has to persist */
{
  sent.length = 0; timers = [];
  resetPrefetch('https://site.example/one');
  const dom = new JSDOM('<a id="l" href="/next">next</a>', { url: 'https://site.example/one' });
  const link = dom.window.document.getElementById('l');

  notePrefetchIntent(link, 'https://site.example/one');
  eq(sent.length, 0, 'the hover fired instantly instead of waiting to see if it persists');
  eq(timers.length, 1, 'no hover timer was armed');
  eq(timers[0].ms, 120, 'the hover threshold changed: ' + (timers[0] && timers[0].ms));

  flushTimers();
  eq(sent.length, 1, 'a resting cursor did not send a hint');
  eq(sent[0].type, 'prefetch', 'the hint has the wrong message type: ' + JSON.stringify(sent[0]));
  eq(sent[0].url, 'https://site.example/next', 'the hint names the wrong URL');

  /* ...and the same link is not asked for twice */
  notePrefetchIntent(link, 'https://site.example/one');
  flushTimers();
  eq(sent.length, 1, 'the same link was prefetched twice');

  /* leaving the link before the threshold cancels it */
  resetPrefetch('https://site.example/two');
  sent.length = 0; timers = [];
  const dom2 = new JSDOM('<a id="l" href="/later">later</a>', { url: 'https://site.example/two' });
  notePrefetchIntent(dom2.window.document.getElementById('l'), 'https://site.example/two');
  eq(timers.length, 1, 'no timer to cancel');
  timers = [];                    // what pointerout does
  eq(sent.length, 0, 'a cancelled hover still prefetched');
}

/* --------------------------------------------------------- 3. tap is instant */
{
  sent.length = 0; timers = [];
  resetPrefetch('https://site.example/three');
  const dom = new JSDOM('<a id="l" href="/fast">fast</a>', { url: 'https://site.example/three' });
  const doc = dom.window.document;
  const link = doc.getElementById('l');
  // The tap path calls sendPrefetchHint directly (no hover delay).
  sendPrefetchHint(prefetchTarget(link, 'https://site.example/three'));
  eq(sent.length, 1, 'a tap did not send its hint immediately');
  eq(sent[0].url, 'https://site.example/fast', 'the tap hint names the wrong URL');
}

/* ------------------------------------------------------- 4. the budget holds */
{
  sent.length = 0; timers = [];
  resetPrefetch('https://site.example/four');
  for (let i = 0; i < 20; i++) {
    sendPrefetchHint('https://site.example/p' + i);
  }
  eq(sent.length, 8, 'the per-page prefetch budget is not enforced (sent ' + sent.length + ')');
  // A new page starts a new budget.
  resetPrefetch('https://site.example/five');
  sendPrefetchHint('https://site.example/fresh');
  eq(sent.length, 9, 'the budget did not reset on a new page');
}

/* -------------------------------------------------- 5. the wiring in the doc */
{
  sent.length = 0; timers = [];
  resetPrefetch('https://site.example/six');
  const dom = new JSDOM('<a id="l" href="/wired">wired</a>', { url: 'https://site.example/six' });
  const doc = dom.window.document;
  installPrefetchHints(doc, 'https://site.example/six');
  installPrefetchHints(doc, 'https://site.example/six');
  eq(doc.__shfPrefetchWired, true, 'the document was never marked as wired');
  const link = doc.getElementById('l');
  link.dispatchEvent(new dom.window.MouseEvent('pointerover', { bubbles: true }));
  eq(timers.length, 1, 'pointerover did not arm a hint (double-wiring would arm two)');
  flushTimers();
  eq(sent.length, 1, 'a real pointerover did not reach the server');
}

/* -------------------------------------------- 6. a broken hint never throws */
{
  ok((() => {
    try {
      sendPrefetchHint('');
      sendPrefetchHint(null);
      notePrefetchIntent(null, null);
      installPrefetchHints(null, '');
      resetPrefetch(null);
      return true;
    } catch (e) { return false; }
  })(), 'malformed input threw out of the prefetch helpers');
}

/* ------------------------------------------------ 7. the page really wires it */
ok(/installPrefetchHints\(d,/.test(CLIENT),
   'client.html never installs the prefetch hints on the frame');
ok(/type: 'prefetch'/.test(CLIENT), 'client.html never sends a prefetch message');

console.log((fails.length ? 'FAIL' : 'PASS') + '  intent prefetch client: '
            + pass + ' checks, ' + fails.length + ' failed');
fails.forEach((f) => console.log('  - ' + f));
process.exit(fails.length ? 1 : 0);
