// No dependencies: node tests/test_slab_font.cjs
//
// The house font is Roboto Slab, and it ships INSIDE every page as a data: URI
// (see fonts.py for why a URL is not an option: a mirrored document lives in a
// blob: iframe with an opaque origin, so a URL font is a CORS-checked
// cross-origin request that fails silently).
//
// That decision has one real risk: the same 45 KB of base64 now exists in three
// HTML files, and three copies drift. This test is the seam:
//
//   * every copy is byte-identical to the asset in assets/fonts;
//   * the asset is a real WOFF2 and a real variable font;
//   * no surface still asks for a different face, and none of them fetches one;
//   * the family is named in a way that cannot break a JS string or an
//     attribute (the failure that has already cost this project a panel);
//   * LPV pages keep their own typography, and the mono switch is untouched.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');
const read = (f) => fs.readFileSync(path.join(root, f), 'utf8');

const FONT = path.join(root, 'assets', 'fonts', 'roboto-slab-latin-wght-normal.woff2');
const raw = fs.readFileSync(FONT);
const b64 = raw.toString('base64');

// ---------------------------------------------------------------------------
// 1. The asset is what we think it is
// ---------------------------------------------------------------------------
{
  ok(raw.subarray(0, 4).toString('latin1') === 'wOF2', 'the asset is a WOFF2 file');
  ok(raw.length > 20000 && raw.length < 80000,
    'it is a subset-sized file (' + raw.length + ' bytes), not the whole family');
  ok(fs.existsSync(path.join(root, 'assets', 'fonts', 'LICENSE-RobotoSlab.txt')),
    'the licence travels with the font (Apache-2.0)');
}

// ---------------------------------------------------------------------------
// 2. Every page carries exactly that font — no more, no less
// ---------------------------------------------------------------------------
// Admin.html is NOT in this list on purpose: its body is set in Helvetica
// from the OS. Its display chrome uses Corptic DEMO, embedded once as a data:
// URI in its own block - section 2b asserts that instead.
const PAGES = ['client.html', 'mini-admin.html'];
{
  for (const name of PAGES) {
    const src = read(name);
    const start = src.indexOf('<style id="shf-font-face">');
    ok(start > 0, name + ' carries the embedded face block');
    const open = src.indexOf('>', start) + 1;
    const close = src.indexOf('</style>', open);
    const css = src.slice(open, close).trim();

    ok(css.includes(b64),
      name + ': the payload is byte-identical to assets/fonts (this is the drift check)');
    ok((css.match(/@font-face/g) || []).length === 1,
      name + ': exactly one face is declared');
    ok(/font-weight:100 900/.test(css),
      name + ': the variable axis covers every weight the panels use');
    ok(/font-display:swap/.test(css),
      name + ': text is never invisible while the face is decoded');
    ok(/format\('woff2'\)/.test(css), name + ': the format is spelled out');
    ok(!/url\((?!data:)/.test(css),
      name + ': the src is a data: URI - nothing to fetch, no CORS to fail');
    ok(!/localhost|127\.0\.0\.1|https?:\/\//.test(css.replace(/data:font\/woff2;base64,[^)]+/, '')),
      name + ': no address or host leaks into the face');

    // The block is generated; a hand-edit is how the three copies diverge.
    ok(/deploy\/embed_font\.py/.test(src.slice(Math.max(0, start - 400), start)),
      name + ': the block says where it comes from');

    // And the page actually names the family.
    ok(/font-family:\s*Roboto Slab/.test(src), name + ' is set in Roboto Slab');
    ok(!/Helvetica Neue/.test(src), name + ' asks for no other house face');
    ok(!/Space\+?Grotesk/.test(src), name + ' asks for Space Grotesk nowhere');
    ok(!/Montserrat/.test(src), name + ' asks for Montserrat nowhere');
    // The ONE allowed reach for a font CDN is client.html's optional
    // "live pages in mono" switch, and it is opt-in and never part of the
    // house face. Any other occurrence - a preconnect, a stylesheet, a
    // fallback import - would be a request in front of the first paint.
    const cdn = (src.match(/fonts\.googleapis\.com/g) || []).length;
    ok(cdn === (name === 'client.html' ? 1 : 0),
      name + ': reaches a font CDN ' + (name === 'client.html' ? 'only for the mono switch' : 'never'));
    ok(!/fonts\.gstatic/.test(src), name + ' does not preconnect to a font host');
  }
}

// ---------------------------------------------------------------------------
// 2b. Admin.html: the Corptic display face, embedded once, nothing remote
// ---------------------------------------------------------------------------
{
  const admin = read('Admin.html');
  ok(!/<style id="shf-font-face">/.test(admin), 'Admin.html carries no Roboto Slab block');
  ok(/<style id="shf-corptic-face">/.test(admin),
    'Admin.html carries the Corptic face in its own block');
  ok((admin.match(/@font-face\s*\{/g) || []).length === 1,
    'exactly one @font-face in Admin.html');
  ok(/src:\s*url\(data:font\/woff2;base64,/.test(admin),
    'the face ships as a data: URI - nothing is fetched');
  const corpticB64 = fs.readFileSync(
    path.join(root, 'assets', 'fonts', 'corptic-demo-regular.woff2')).toString('base64');
  ok(admin.includes(corpticB64),
    'the embedded Corptic is byte-identical to assets/fonts/corptic-demo-regular.woff2');
  ok(fs.existsSync(path.join(root, 'assets', 'fonts', 'LICENSE-Corptic.txt')),
    'the licence travels with the font');
  ok(/font-family:\s*Helvetica Neue/.test(admin), 'the body is still set in Helvetica');
  ok(/--mc-display:\s*Corptic/.test(admin), 'and the display stack names Corptic first');
  ok(!/Roboto Slab/.test(admin), 'and asks for no other house face');
}

// ---------------------------------------------------------------------------
// 3. One decision, three surfaces: the panels are not "mostly" the same
// ---------------------------------------------------------------------------
{
  const stacksIn = (name) => {
    const src = read(name);
    return (src.match(/font-family:\s*([^;"'}]+)/g) || []).map((m) => m.split(':').slice(1).join(':').trim());
  };
  for (const name of PAGES) {
    const stacks = stacksIn(name);
    ok(stacks.length > 0, name + ' declares a family somewhere');
    const foreign = stacks.filter((stack) => {
      // A mono slot keeps its mono lead; everything else must start with ours.
      if (/^(ui-monospace|SFMono|Menlo|Consolas|monospace)/.test(stack)) return false;
      return !/^Roboto Slab/.test(stack) && !/inherit|var\(--/.test(stack);
    });
    ok(foreign.length === 0,
      name + ': every family starts with the house face (' + foreign.slice(0, 2).join(' | ') + ')');
  }
  // A slab serif needs a serif fallback, or a refused face flips the page to a
  // sans look mid-load.
  for (const name of PAGES) {
    const src = read(name);
    ok(/Roboto Slab[^;{}]*serif/.test(src), name + ': the fallback behind it is a serif');
  }
}

// ---------------------------------------------------------------------------
// 4. The family is spelled in a way that cannot break anything
// ---------------------------------------------------------------------------
{
  for (const name of PAGES) {
    const src = read(name);
    // 'Roboto Slab' quoted inside a JS string or an inline style attribute is
    // the exact shape that silently killed Admin.html's main script once.
    ok(!/'Roboto Slab'/.test(src), name + ': the family is never single-quoted');
    ok(!/"Roboto Slab"/.test(src), name + ': the family is never double-quoted');
    ok(!/&quot;Roboto Slab&quot;/.test(src), name + ': and never needs an HTML entity');
  }
  const client = read('client.html');
  ok(/const MIRROR_FONT_STACK = "Roboto Slab, Georgia/.test(client),
    'the mirror stack is an unquoted family, safe to concatenate into CSS');
  ok(/function mirrorFontFaceCss\(\)/.test(client),
    'the mirror reuses the shell\'s face instead of carrying a second payload');
  ok((client.match(/base64,/g) || []).length === 1,
    'client.html holds the payload exactly once');
  ok(/mirrorFontFaceCss\(\) \+/.test(client), 'and pastes it into the frame theme');
}

// ---------------------------------------------------------------------------
// 5. What must NOT change
// ---------------------------------------------------------------------------
{
  const client = read('client.html');
  // An LPV page is the operator's own captured page: its typography is part of
  // what it is, so the theme refuses it - face included.
  ok(/if \(isLpvPageDocument\(doc\)\)/.test(client), 'the LPV guard is still first');
  ok(/stripMirrorFont\(doc\)/.test(client), 'and it still strips a previous theme');
  ok(/LPV_MONO_STACK/.test(client) && /family=Space\+Mono/.test(client),
    'the live-pages mono switch is untouched');
  ok(/\?keep_fonts=1|keep_fonts/.test(client) && /mirrorFontEnabled/.test(client),
    '?keep_fonts=1 still hands the page its own type back');
  ok(/MIRROR_FONT_SKIP/.test(client) && /:not\(svg\)/.test(client),
    'icon fonts and svg are still skipped (their glyphs are characters in their family)');

  // The server-side loading screen is a separate, sub-second surface: it keeps
  // the system stack rather than paying 45 KB for the first thing a visitor sees.
  const api = read('api.py');
  ok(/font-family:'Helvetica Neue',Helvetica,Arial/.test(api) || /font-family:\s*Roboto Slab/.test(api),
    'the domain spinner keeps a declared fallback stack either way');
  ok(!/@font-face/.test(api), 'and does not carry the payload');
}

const py = read('fonts.py');
{
  ok(/FONT_STACK = "Roboto Slab, Georgia/.test(py), 'fonts.py is the Python source of truth');
  ok(/font-weight:100 900/.test(py), 'with the same variable weight range');
  ok(/lru_cache/.test(py), 'the base64 is computed once, not per request');
  ok(/except OSError/.test(py), 'a missing asset degrades to Georgia instead of crashing the server');
}

console.log('slab font: ' + checks + ' checks passed');
