// No dependencies: node tests/test_mirror_font.cjs
//
// The mirror shows a captured page EXACTLY as it was captured — its own
// typography included. There used to be a "mirror font" theme here: it copied
// the embedded Roboto Slab face (a ~45 KB data: URI) plus a font-family
// !important rule into every captured HTML and re-asserted it from the
// frame's load handler. That was a real bottleneck in the capture pipeline
// (a big string splice on every single page, plus a DOM write per frame), so
// it is gone: nothing is injected into a mirrored document, and the page
// keeps the exact fonts it was captured with.
//
// What this file guards now:
//
//   * the injection machinery is really gone from client.html (no helper, no
//     tag id, no ?keep_fonts opt-out, no per-capture font splice);
//   * client.html still carries its OWN embedded face exactly once — the
//     shell page itself is still Roboto Slab, embedded as a data: URI;
//   * the one remaining font switch (Space Mono on LPV pushed pages, an
//     operator's choice, off by default) is untouched, and the LPV guard
//     that keeps any theme off pushed pages is still in place.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'client.html'), 'utf8');

// ---------------------------------------------------------------------------
// 1. The mirror-font machinery is gone — every piece of it
// ---------------------------------------------------------------------------
{
  for (const gone of [
    'MIRROR_FONT_STACK', 'MIRROR_FONT_SKIP', 'MIRROR_FONT_TAG_ID',
    'mirrorFontFaceCss', 'mirrorFontCssText', 'mirrorFontStyleHtml',
    'injectMirrorFontIntoHtml', 'mirrorFontEnabled', 'mirrorFontForced',
    'stripMirrorFont', 'ensureMirrorFont',
    '__shifix_mirror_font__', '__shifix_mirror_font_css__',
    'keep_fonts', 'keep_font=',
  ]) {
    ok(!source.includes(gone), 'client.html no longer contains ' + gone);
  }

  // The capture path must do no font work at all on the HTML it streams.
  const captureNote = source.indexOf('No font is injected into the captured HTML');
  ok(captureNote > 0, 'the capture path states the page keeps its own fonts');
  const after = source.slice(captureNote, captureNote + 400);
  ok(!/font-family\s*:|@font-face\s*\{|<style/.test(after),
    'and injects no font rule or style tag right there');

  // No <style> tag is ever built for a mirrored document.
  ok(!/data-stream-injected="true"/.test(source) && !/<style[^>]*data-stream-injected/.test(source),
    'no stream-injected <style> tag is built anywhere (the CSP meta sets its attribute differently)');
}

// ---------------------------------------------------------------------------
// 2. The shell page itself is still Roboto Slab — embedded exactly once
// ---------------------------------------------------------------------------
{
  ok(/<style id="shf-font-face">/.test(source), 'client.html carries its own embedded face block');
  ok(/@font-face\{font-family:Roboto Slab/.test(source), 'the face is Roboto Slab');
  ok((source.match(/base64,/g) || []).length === 1,
    'the payload exists exactly once in the file (no second copy for a mirror)');
  const fontUrls = source.match(/https:\/\/fonts\.[a-z.]+[^'"\s]*/g) || [];
  ok(fontUrls.length === 1 && /family=Space\+Mono/.test(fontUrls[0]),
    'the only font URL anywhere is the operator-opt-in Space Mono stylesheet (' + fontUrls.length + ' found)');
  ok(!/@import|fontsource|typekit|use\.typekit/.test(source), 'no font @import or font CDN');
  const faceStart = source.indexOf('<style id="shf-font-face">');
  const faceOpen = source.indexOf('>', faceStart) + 1;
  const SHELL_FACE = source.slice(faceOpen, source.indexOf('</style>', faceOpen)).trim();
  ok(/src:url\(data:font\/woff2;base64,/.test(SHELL_FACE), 'the face is a data: URI — the shell fetches nothing for type');
  ok(!/https?:\/\//.test(SHELL_FACE), 'the face rule has no remote URL');
}

// ---------------------------------------------------------------------------
// 3. What must NOT change: LPV pages keep their own fonts, mono switch intact
// ---------------------------------------------------------------------------
{
  ok(/function isLpvPageDocument\(doc\)/.test(source), 'the LPV-page detector is still there');
  ok(/LPV_MONO_STACK/.test(source) && /family=Space\+Mono/.test(source),
    'the live-pages mono switch (operator opt-in, off by default) is untouched');
  ok(/applyLpvMonoFont\(innerDoc\)/.test(source), 'and it still applies on every LPV mount');
  ok(/data-lpv-page/.test(source), 'the LPV marker is still set before anything could theme the page');
  ok(/FONT_SKIP/.test(source) && /:not\(svg\)/.test(source),
    'icon fonts and svg are still skipped by the one rule that exists (their glyphs are characters in their family)');
  ok(/applyLpvMonoFont\(d\)/.test(source) && /setLpvMonoFont/.test(source),
    'the live settings push still re-applies the switch without a re-push');
}

console.log('mirror keeps captured fonts: ' + checks + ' checks passed');
