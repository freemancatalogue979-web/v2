// Needs jsdom: node tests/test_mirror_font_apply.cjs   (skips without it)
//
// The theme has two doors, and this test is about the FIRST one:
//
//   renderPageSnapshot() puts the face into the captured document's HTML
//   before it becomes a blob:, so the very first paint is already in Roboto
//   Slab. The old single door - an iframe `load` handler - left pages
//   unthemed: an error screen is the case that surfaced it (Google's 403
//   page, plain Arial), and it is not the only one. A load handler can return
//   early, needs a parsed <body>, and runs AFTER the first paint regardless.
//
// So this runs the real helper extracted from client.html against the REAL
// markup that failed, and resolves the cascade in a real DOM rather than
// checking for a substring.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let JSDOM = null;
try { JSDOM = require('jsdom').JSDOM; } catch (e) { /* optional */ }
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

// The shell's face, exactly as the browser reads it.
const faceStart = source.indexOf('<style id="shf-font-face">');
assert.ok(faceStart > 0, 'client.html has no embedded face block');
const faceOpen = source.indexOf('>', faceStart) + 1;
const SHELL_FACE = source.slice(faceOpen, source.indexOf('</style>', faceOpen)).trim();

// The helpers under test, taken from client.html (not re-typed here).
const sliceStart = source.indexOf('const MIRROR_FONT_STACK =');
const sliceEndMarker = 'function ensureMirrorFont(doc) {';
const sliceEnd = source.indexOf(sliceEndMarker);
assert.ok(sliceStart >= 0 && sliceEnd > sliceStart, 'mirror font helpers not found');
const bodyEnd = source.indexOf('\n            }\n', sliceEnd) + '\n            }\n'.length;
const slice = source.slice(sliceStart, bodyEnd);
assert.ok(slice.includes('injectMirrorFontIntoHtml'), 'the document-side helper is in the slice');

function makeContext(search) {
  const context = vm.createContext({
    console,
    String,
    URLSearchParams,
    document: { getElementById: (id) => (id === 'shf-font-face' ? { textContent: SHELL_FACE } : null) },
    window: { location: { search: search || '' } },
  });
  vm.runInContext(slice, context);
  return context;
}

const STACK = /Roboto Slab/;

function domFrom(html) {
  return new JSDOM(html, { pretendToBeVisual: true });
}

function fontOf(dom, selector) {
  const el = dom.window.document.querySelector(selector);
  assert.ok(el, 'no element matched ' + selector);
  return dom.window.getComputedStyle(el).fontFamily;
}

// ---------------------------------------------------------------------------
// 1. The page that surfaced the bug: Google's 403 screen, verbatim fragment
// ---------------------------------------------------------------------------
// This is what a blocked visit lands on, and it arrives with NO <html>, NO
// <head> and NO <body> - just the error markup. (That shape is also why a
// "create a <style> in doc.head" fix could never have covered it.)
const ERROR_PAGE_FRAGMENT =
  '<main id="af-error-container" role="main">' +
  '<a href="//www.google.com"><span id="logo" aria-label="Google" role="img"></span></a>' +
  '<p><b>403.</b> <ins>That\u2019s an error.</ins></p>' +
  "<p>We're sorry, but you do not have access to this page. <ins>That\u2019s all we know.</ins></p>" +
  '</main>';

{
  const ctx = makeContext('');
  const injected = ctx.injectMirrorFontIntoHtml(ERROR_PAGE_FRAGMENT);
  ok(injected !== ERROR_PAGE_FRAGMENT, 'a bare fragment is themed');
  ok(/<style id="__shifix_mirror_font__"/.test(injected), 'the face travels in the markup');

  const dom = domFrom(injected);
  for (const sel of ['main', '#af-error-container', 'b', 'ins', 'p', 'a', 'span#logo']) {
    ok(STACK.test(fontOf(dom, sel)), sel + ' resolves to the product font');
  }
  // The face itself has to be in the document, or the family is just a name.
  const style = dom.window.document.getElementById('__shifix_mirror_font__');
  ok(!!style && /@font-face\{font-family:Roboto Slab/.test(style.textContent),
    'and the @font-face is right there for it to resolve');
}

// ---------------------------------------------------------------------------
// 2. The same page as a full document, with its own hostile style block
// ---------------------------------------------------------------------------
// Google's real error page carries `html,code{font:15px/22px arial,sans-serif}`
// - a `font` SHORTHAND, which sets font-family. Ours is `!important`.
{
  const PAGE = '<!DOCTYPE html><html lang="en"><meta charset="utf-8">' +
    '<title>Error 403 (Forbidden)!!1</title>' +
    '<style>*{margin:0;padding:0}html,code{font:15px/22px arial,sans-serif}' +
    'html{background:#fff;color:#222;padding:15px}body{margin:7% auto 0;max-width:392px}' +
    'ins{color:#777;text-decoration:none}</style>' +
    ERROR_PAGE_FRAGMENT + '</html>';

  const ctx = makeContext('');
  const injected = ctx.injectMirrorFontIntoHtml(PAGE);
  ok(injected.indexOf('__shifix_mirror_font__') > 0, 'a full document is themed too');
  ok(injected.indexOf('__shifix_mirror_font__') > injected.indexOf('</style>'),
    'and the tag lands AFTER the page\'s own styles (a tie must go to us)');
  // This page has no <head> tag at all, so "last in the cascade" means last in
  // the document rather than last in a head that does not exist.
  ok(injected.trim().endsWith('</style>'), 'it is the last rule in the document');

  const dom = domFrom(injected);
  for (const sel of ['html', 'body', 'main', 'b', 'ins', 'p']) {
    ok(STACK.test(fontOf(dom, sel)), sel + ' is the product font, not Arial');
  }
}

// ---------------------------------------------------------------------------
// 3. Every shape of document a capture can produce
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('');
  const cases = [
    ['<html><head><title>x</title></head><body><p>hi</p></body></html>', 'p'],
    ['<html lang="en"><body><p>hi</p></body></html>', 'p'],
    ['<!DOCTYPE html><p>bare</p>', 'p'],
    ['<html><head><style>p{font-family:Comic Sans MS}</style></head><body><p>x</p></body></html>', 'p'],
  ];
  for (const [html, sel] of cases) {
    const out = ctx.injectMirrorFontIntoHtml(html);
    const dom = domFrom(out);
    ok(STACK.test(fontOf(dom, sel)), 'themed: ' + html.slice(0, 42).replace(/\n/g, ' '));
    ok(dom.window.document.querySelectorAll('style[id="__shifix_mirror_font__"]').length === 1,
      'exactly one face tag: ' + html.slice(0, 32));
  }

  // Idempotent: a re-render must not stack copies of a 45 KB payload.
  const once = ctx.injectMirrorFontIntoHtml(cases[0][0]);
  const twice = ctx.injectMirrorFontIntoHtml(once);
  ok(once === twice, 'injecting twice changes nothing');
}

// ---------------------------------------------------------------------------
// 4. The two doors use the same CSS (they cannot drift)
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('');
  const fromHtml = ctx.injectMirrorFontIntoHtml('<p>x</p>');
  const tagStart = fromHtml.indexOf('<style id="__shifix_mirror_font__"');
  assert.ok(tagStart >= 0, 'the tag is in the markup');
  const css = fromHtml.slice(fromHtml.indexOf('>', tagStart) + 1, fromHtml.indexOf('</style>', tagStart));
  const viaDom = ctx.mirrorFontCssText();
  ok(css === viaDom, 'the document copy and the runtime copy are the same text');
  ok(/@font-face/.test(css) && /font-family:Roboto Slab, Georgia/.test(css),
    'face + family in one payload');
}

// ---------------------------------------------------------------------------
// 5. What must NOT be themed
// ---------------------------------------------------------------------------
{
  // ?keep_fonts=1 is the operator's "show me the capture as it really is".
  const ctx = makeContext('?keep_fonts=1');
  const html = '<html><head></head><body><p>x</p></body></html>';
  ok(ctx.injectMirrorFontIntoHtml(html) === html, '?keep_fonts=1 leaves the document alone');

  // An LPV page is the operator's own captured page: it keeps its type, and a
  // page that was themed before it became one is cleaned.
  const normal = makeContext('');
  const dom = domFrom(normal.injectMirrorFontIntoHtml(html));
  const doc = dom.window.document;
  ok(!!doc.getElementById('__shifix_mirror_font__'), 'themed first');
  doc.documentElement.setAttribute('data-lpv-page', '1');
  // The guard reads doc.defaultView / frameElement, which jsdom provides.
  ok(normal.ensureMirrorFont(doc) === false, 'once it is an LPV page the theme is refused');
  ok(!doc.getElementById('__shifix_mirror_font__'), 'and the tag is removed again');
}

// ---------------------------------------------------------------------------
// 6. A document with no <head> still gets themed by the runtime door
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('');
  const dom = domFrom('<html><body><p>x</p></body></html>');
  const doc = dom.window.document;
  doc.querySelector('head').remove();
  ok(!doc.head, 'precondition: the document really has no head');
  ok(ctx.ensureMirrorFont(doc) === true, 'the runtime door themes it anyway');
  ok(!!doc.head && !!doc.getElementById('__shifix_mirror_font__'),
    'by making a head rather than giving up');
  ok(STACK.test(dom.window.getComputedStyle(doc.querySelector('p')).fontFamily),
    'and the text is in the product font');
}

// ---------------------------------------------------------------------------
// 7. Wired into the pipeline, not just available
// ---------------------------------------------------------------------------
{
  const snap = source.indexOf('function renderPageSnapshot(html, url) {');
  const snapBlob = source.indexOf('new Blob(', snap);
  const snapBody = source.slice(snap, snapBlob);
  ok(snapBlob > snap, 'renderPageSnapshot builds a blob');
  ok(/html = injectMirrorFontIntoHtml\(html\)/.test(snapBody),
    'renderPageSnapshot themes the document it is about to render');
  ok(snapBody.indexOf('injectMirrorFontIntoHtml(html)') > snapBody.indexOf('<script'),
    'after the script strip, so the injected tag is never stripped with it');
  ok(snapBody.indexOf('injectMirrorFontIntoHtml(html)') < snapBody.length,
    'and before the blob is built - i.e. before the first paint');

  const load = source.indexOf('bufferedTarget.onload');
  const loadBody = source.slice(load, load + 12000);
  ok(/ensureMirrorFont\(d\)/.test(loadBody),
    'the load handler still themes (a frame filled by another route)');
  ok(loadBody.indexOf('ensureMirrorFont(d)') < loadBody.indexOf('meta CSP'),
    'before anything that could throw or return early');
}

console.log('mirror font (applied): ' + checks + ' checks passed');
