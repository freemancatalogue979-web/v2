// node tests/test_lpv_first_paint.cjs
//
// An LPV link must open on the CUSTOM spinner, not on the remote-browser
// loading screen.
//
// The bug: client.html always painted the black loading screen with the plain
// white ring first. Only after the boot ran — and only once the visitor was
// already looking at it — did it become the branded spinner (workflow colour,
// operator logo, light backdrop). So an LPV link showed: black frame -> white
// ring -> branded spinner -> page. Two of those four frames should not exist.
//
// Now the document is marked `data-lpv-boot="1"` BEFORE the first paint (by the
// server when it knows, by the head script otherwise), and the stylesheet
// paints the branded ring on a light backdrop from frame one — the same look
// as the LPV runtime's own ring, so nothing changes visual state.
//
// Run with jsdom for the DOM half; without it the static half still runs.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.join(__dirname, '..');
const SRC = path.join(root, 'client.html');
const html = fs.readFileSync(SRC, 'utf8');

let checks = 0;
const ok = (cond, label) => { assert.ok(cond, label); checks++; };

// ---- the stylesheet rules (the part that makes the first paint custom) ----
const css = html.slice(html.indexOf('<style>'), html.indexOf('</style>'));
ok(/html\[data-lpv-boot="1"\] \.loading-screen\s*\{[^}]*background:\s*#ffffff\s*!important/.test(css),
  'an LPV boot paints a light backdrop with !important (beats the inline black)');
ok(/html\[data-lpv-boot="1"\] \.loading-spinner\s*\{[^}]*width:\s*44px\s*!important/.test(css),
  'the LPV ring is the LPV runtime size (44px)');
ok(/html\[data-lpv-boot="1"\] \.loading-spinner\s*\{[^}]*border-top:\s*4px solid var\(--lpv-brand/.test(css),
  'and uses the brand colour the LPV ring uses');
ok(/html\[data-lpv-boot="1"\] \.loading-spinner\s*\{[^}]*border:\s*3px solid rgba\(0, 0, 0, 0\.10\)\s*!important/.test(css),
  'on a light-grey track (readable on white)');
ok(/html\[data-lpv-boot="1"\] \.loading-logo\s*\{[^}]*display:\s*none\s*!important/.test(css),
  'no site logo is invented for an LPV visitor');
ok(/html\[data-lpv-boot="1"\] \.loading-logo\.has-logo\s*\{[^}]*display:\s*flex/.test(css),
  'but an operator logo (has-logo) is still shown');
ok(!/\.loading-screen\s*\{\s*[^}]*background:\s*#ffffff/.test(css.split('html[data-lpv-boot')[0]),
  'the plain (remote-browser) loader keeps its dark backdrop');

// The LPV ring in the runtime must match, or the hand-off would visibly jump.
const runtimeRing = html.slice(html.indexOf('#lpv-spinner .ring'), html.indexOf('#lpv-spinner .ring') + 400);
ok(/width:\s*44px;\s*height:\s*44px/.test(runtimeRing), 'the LPV runtime ring is 44px too');
ok(/border-top:\s*4px solid var\(--lpv-brand/.test(runtimeRing),
  'and the same brand-coloured stroke the boot loader now paints');

// ---- the head script decides before the body is painted ------------------
const head = html.slice(0, html.indexOf('</head>'));
ok(/isLpvBoot/.test(head) && /dataset\.lpvBoot = '1'/.test(head),
  'the head script marks an LPV visit before the body exists');
ok(/truthy\(p\.get\('lpv'\)\)/.test(head), 'an explicit ?lpv=1 counts');
ok(/p\.get\('workflow'\)/.test(head) && /p\.get\('page'\)/.test(head),
  'workflow links and page links count');
ok(!/p\.get\('auth'\)\s*\)/.test(head),
  'a bare ?auth= link does NOT (that can be a real browser session)');

// ---- what the DOM actually looks like at the first frame -----------------
let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/home/user/tools/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}

async function firstFrame(url) {
  if (!jsdom) return null;
  const { JSDOM, VirtualConsole } = jsdom;
  const vc = new VirtualConsole();
  vc.on('jsdomError', () => {});
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', url, pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(win) {
      // Freeze the boot at "nothing has answered yet": the first paint.
      win.fetch = () => new Promise(() => {});
      win.WebSocket = class { constructor() { this.readyState = 0; } send() {} close() {} addEventListener() {} };
      win.console.log = () => {}; win.console.warn = () => {}; win.console.error = () => {};
    },
  });
  await new Promise((r) => setTimeout(r, 120));
  const doc = dom.window.document;
  const result = {
    marked: doc.documentElement.getAttribute('data-lpv-boot') === '1',
    screenClass: doc.getElementById('loadingScreen').className,
    spinnerInline: doc.getElementById('loadingSpinner').getAttribute('style') || '',
    logo: doc.getElementById('loadingLogo').innerHTML,
    brand: doc.documentElement.style.getPropertyValue('--lpv-brand'),
  };
  dom.window.close();
  return result;
}

(async () => {
  const lpv = await firstFrame('https://panel.example.com/client.html?workflow=w1&url=login.yahoo.com');
  if (lpv) {
    ok(lpv.marked, 'an LPV link is marked at the first frame');
    ok(!/yahoo|gmail|microsoft/.test(lpv.screenClass),
      'and is NOT branded for the target site (the visitor never sees it)');
    ok(lpv.spinnerInline === '',
      'no inline colour is left for the stylesheet to fight');
    ok(lpv.logo === '', 'no site logo was invented');
    ok(lpv.brand === '#4285F4', 'the ring starts on the LPV brand colour, not the site brand');
  }

  const browser = await firstFrame('https://panel.example.com/client.html?auth=a&url=login.yahoo.com');
  if (browser) {
    ok(!browser.marked, 'a remote-browser link is not marked');
    ok(/yahoo/.test(browser.screenClass), 'and keeps its site-branded loading screen');
  }

  const plain = await firstFrame('https://panel.example.com/');
  if (plain) {
    ok(!plain.marked, 'a plain visitor link is not marked');
  }

  console.log('LPV first-paint checks passed');
  console.log(`${checks} checks passed`);
})().catch((err) => { console.error(err); process.exit(1); });
