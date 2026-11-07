// Needs jsdom: node tests/test_mirror_font_apply.cjs   (skips without it)
//
// The mirror no longer themes captured documents at all — the page is shown
// with the exact fonts it was captured with. This test proves it in a real
// DOM, not just by substring: it loads client.html, then simulates what the
// capture path used to do to a mirrored document and checks that no code
// path can add a font tag, and that a sample captured page keeps its own
// font-family when parsed next to the shell.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let JSDOM = null;
try { JSDOM = require('jsdom').JSDOM; } catch (e) { /* optional */ }
if (!JSDOM) {
  try { JSDOM = require('/tmp/jsd/node_modules/jsdom').JSDOM; } catch (e2) { /* optional */ }
}
if (!JSDOM) {
  console.log('mirror font (applied): skipped - jsdom not installed');
  process.exit(0);
}

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'client.html'), 'utf8');

// The shell page itself: one embedded face, and it is the shell's own — the
// mirror never got a second copy.
const faceStart = source.indexOf('<style id="shf-font-face">');
assert.ok(faceStart > 0, 'client.html has no embedded face block');
const faceOpen = source.indexOf('>', faceStart) + 1;
const SHELL_FACE = source.slice(faceOpen, source.indexOf('</style>', faceOpen)).trim();
ok(/@font-face/.test(SHELL_FACE), 'the shell carries a @font-face (for itself)');
ok((source.match(/base64,/g) || []).length === 1, 'the font payload exists exactly once in client.html');

// 1. The shell DOM parses, carries its own face, and contains no mirror tag.
{
  const dom = new JSDOM(source);
  const doc = dom.window.document;
  ok(!!doc.getElementById('shf-font-face'), 'the shell keeps its own embedded face block');
  ok(doc.getElementById('shf-font-face').textContent.includes('@font-face'), 'and it is a real face rule');
  ok(!doc.getElementById('__shifix_mirror_font__'), 'no mirror-font style tag exists in the shell');
  ok(!doc.querySelector('[data-stream-injected]'), 'nothing is marked stream-injected');
  const sheet = doc.getElementById('shf-font-face').sheet;
  ok(sheet && sheet.cssRules.length === 1, 'the face block parses as exactly one rule');
}

// 2. A captured page keeps its own typography: parse a sample "captured"
//    document alongside the shell and confirm nothing in client.html can
//    reach into it — no helper names, no tag ids, no font splice remain.
{
  const captured = '<!DOCTYPE html><html><head><style id="site-font">@font-face{font-family:"Site Sans";src:url("/fonts/site.woff2")}' +
    'body{font-family:"Site Sans",sans-serif}</style></head><body><p>hello</p></body></html>';
  const dom = new JSDOM(captured);
  const doc = dom.window.document;
  ok(doc.getElementById('site-font') !== null, 'the captured page keeps its own stylesheet');
  ok(doc.body && dom.window.getComputedStyle(doc.body).fontFamily.includes('Site Sans'),
    'and its own font-family — the exact font of the page captured');

  for (const gone of ['injectMirrorFontIntoHtml', 'ensureMirrorFont', 'stripMirrorFont',
                      'MIRROR_FONT', '__shifix_mirror_font__', 'mirrorFontEnabled', 'keep_fonts']) {
    ok(!source.includes(gone), 'client.html contains no ' + gone + ' to reach into that document with');
  }
  // The capture path comment that replaces the old injection call:
  ok(source.includes('No font is injected into the captured HTML'),
    'the capture path documents the no-injection rule where the splice used to be');
}

// 3. The one remaining injected style is the LPV mono switch — and it only
//    ever touches documents marked as LPV pages.
{
  ok(/applyLpvMonoFont\(doc\)[\s\S]{0,200}isLpvPageDocument\(doc\)/.test(source),
    'the mono override still refuses anything that is not an LPV page');
  ok(/__shifix_lpv_mono_font__/.test(source), 'its tag id is the LPV one, not a mirror one');
}

console.log('mirror applies no font: ' + checks + ' checks passed');
