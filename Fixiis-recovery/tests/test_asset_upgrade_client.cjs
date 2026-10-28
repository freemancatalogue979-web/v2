#!/usr/bin/env node
/* The client half of progressive assets, executed.

   The server now sends a page BEFORE it has fetched that page's fonts,
   backgrounds and sprites, then follows up with an `asset_map`: "this text
   became this".

   Two things have to be true for that to be an upgrade rather than a
   regression, and both are about the client:

     1. applying the map to the document it already rendered produces exactly
        the document the old one-message path would have delivered;
     2. it does so IN PLACE — same document object, no re-render, no flash,
        scroll and focus untouched.

   The functions below are lifted out of client.html and run against a real
   DOM (jsdom), so this cannot pass while the page itself is broken.

       NODE_PATH=/tmp/jsd/node_modules node tests/test_asset_upgrade_client.cjs
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
  console.log('SKIP  asset upgrade client: jsdom not installed');
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
const startMark = '// --- Progressive assets ---';
const endMark = 'function renderPageSnapshot(html, url) {';
const start = CLIENT.indexOf(startMark);
const end = CLIENT.indexOf(endMark, start);
ok(start > 0 && end > start, 'the progressive-asset block was not found in client.html');
const block = CLIENT.slice(start, end);

const state = {
  _assetMaps: new Map(),
  _assetUrl: null,
  _docGen: null,
};
let frame = null;
const logs = [];

const sandbox = {
  console: { log: (...a) => logs.push(a.join(' ')), warn: (...a) => logs.push('WARN ' + a.join(' ')) },
  state,
  get frame() { return frame; },
  dispatchAssetList: () => {},
  RegExp, Map, Array, String, Object, JSON, console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(block, sandbox);

const { mergeAssetSwaps, applyAssetSwapsToHtml, applyAssetSwapsLive, handleAssetMap } = sandbox;

const FONT = 'data:font/woff2;base64,d09GMgAAA';
const HERO = 'data:image/png;base64,iVBORw0KGgoAAA';

const SWAP = [
  { from: '/f/x.woff2', to: FONT },
  { from: '/img/hero.png', to: HERO },
  { from: '/sprite.svg#i', to: '/assets/abc.svg#i' },
  { from: '/css/site.css', to: '<style data-shfcss="https://site.example/css/site.css">.x{background:url(' + HERO + ')}</style>', tag: true },
];

const RAW = '<!DOCTYPE html><html><head>'
  + '<link rel="stylesheet" href="/css/site.css">'
  + '<style>@font-face{font-family:H;src:url("/f/x.woff2")}</style>'
  + '</head><body>'
  + '<img src="/img/hero.png">'
  + '<div style="background:url(/img/hero.png)">hero</div>'
  + '<svg><use href="/sprite.svg#i"></use></svg>'
  + '</body></html>';

/* What the server's own rewrite produces for the same page (taken from
   tests/test_asset_upgrade.py, which asserts the two are equal). */
const REWRITTEN = '<!DOCTYPE html><html><head>'
  + '<style data-shfcss="https://site.example/css/site.css">.x{background:url(' + HERO + ')}</style>'
  + '<style>@font-face{font-family:H;src:url("' + FONT + '")}</style>'
  + '</head><body>'
  + '<img src="' + HERO + '">'
  + '<div style="background:url(' + HERO + ')">hero</div>'
  + '<svg><use href="/assets/abc.svg#i"></use></svg>'
  + '</body></html>';

/* --------------------------------------------------- 1. the render path */
mergeAssetSwaps('https://site.example/page', SWAP);
eq(applyAssetSwapsToHtml(RAW, 'https://site.example/page'), REWRITTEN,
   'replaying the map at render time must equal the server rewrite');
eq(applyAssetSwapsToHtml(RAW, 'https://site.example/page'), REWRITTEN,
   'applying the map twice must be idempotent (the swap is by authored text)');

/* a page with nothing to swap is untouched */
{
  const plain = '<html><body><p>nothing</p></body></html>';
  eq(applyAssetSwapsToHtml(plain, 'https://site.example/page'), plain,
     'a document with no matching refs must be returned as-is');
}

/* ------------------------------------------------- 2. per-document cache */
{
  const html = '<div style="background:url(/img/hero.png)"></div>';
  eq(applyAssetSwapsToHtml(html, 'https://other.example/x'), html,
     'a swap cached for one site must not be applied on another');
  mergeAssetSwaps('https://other.example/x', [{ from: '/img/hero.png', to: 'data:image/gif;base64,AA' }]);
  eq(applyAssetSwapsToHtml(html, 'https://other.example/x'),
     '<div style="background:url(data:image/gif;base64,AA)"></div>',
     'that site gets its own mapping for the same authored text');
  eq(applyAssetSwapsToHtml(html, 'https://site.example/page'),
     '<div style="background:url(' + HERO + ')"></div>',
     'and the first site is unaffected');
}

/* ------------------------------------------------------ 3. the live apply */
{
  const dom = new JSDOM(RAW, { url: 'https://site.example/page' });
  const doc = dom.window.document;
  const bodyBefore = doc.body;
  const imgBefore = doc.querySelector('img');

  frame = { contentDocument: doc };
  state._docGen = 7;

  handleAssetMap({ type: 'asset_map', gen: 7, url: 'https://site.example/page', swap: SWAP });

  eq(doc.querySelector('img').getAttribute('src'), HERO, 'the <img> src was not upgraded');
  ok(doc.querySelector('img') === imgBefore,
     'the element was replaced — an upgrade must not rebuild the document');
  ok(doc.body === bodyBefore, 'the document was rebuilt (that is the flash)');
  const hero = [...doc.querySelectorAll('div')].find((d) => d.textContent === 'hero');
  ok(hero && hero.getAttribute('style').indexOf(HERO) !== -1,
     'the inline background was not upgraded: ' + (hero && hero.getAttribute('style')));
  const use = doc.querySelector('use');
  eq(use.getAttribute('href'), '/assets/abc.svg#i', 'the sprite <use> was not upgraded');
  ok(use.getAttribute('href').endsWith('#i'), 'the fragment was lost');
  const styles = doc.querySelectorAll('style');
  const inline = [...styles].find((s) => s.textContent.indexOf('@font-face') !== -1);
  ok(inline && inline.textContent.indexOf(FONT) !== -1,
     'the <style> font was not upgraded: ' + (inline && inline.textContent.slice(0, 60)));
  ok(!doc.querySelector('link[rel="stylesheet"]'),
     'the <link> should have been replaced by the inlined <style>');
  ok([...doc.querySelectorAll('style')].some((s) => s.textContent.indexOf(HERO) !== -1
     && s.textContent.indexOf('.x{') !== -1),
     'the inlined stylesheet is missing its rewritten background');
}

/* ------------------------------- 4. a map for another generation is cached */
{
  const dom = new JSDOM(RAW, { url: 'https://site.example/page' });
  const doc = dom.window.document;
  frame = { contentDocument: doc };
  state._docGen = 9;          // the client has moved on
  state._assetMaps = new Map();
  handleAssetMap({ type: 'asset_map', gen: 8, url: 'https://site.example/page', swap: SWAP });
  eq(doc.querySelector('img').getAttribute('src'), '/img/hero.png',
     'a map for an older generation must not touch the live document');
  ok(state._assetMaps.get('https://site.example/page').size === SWAP.length,
     'but it must still be kept for the next render');
  // ...and the next render does use it.
  eq(applyAssetSwapsToHtml(RAW, 'https://site.example/page'), REWRITTEN,
     'a cached map must apply on the next render');
}

/* --------------------------------- 5. junk must not break the mirror */
{
  const dom = new JSDOM(RAW, { url: 'https://site.example/page' });
  frame = { contentDocument: dom.window.document };
  state._docGen = 1;
  state._assetMaps = new Map();       // no cached edits for this page
  let threw = false;
  try {
    handleAssetMap({ type: 'asset_map', gen: 1, url: 'https://site.example/page',
                     swap: [null, {}, { from: 5, to: 6 }, { from: 'x' }] });
    handleAssetMap({});
    handleAssetMap(null);
    applyAssetSwapsToHtml(null, 'https://site.example/page');
  } catch (e) { threw = true; }
  ok(!threw, 'malformed asset_map input threw');
  eq(dom.window.document.querySelector('img').getAttribute('src'), '/img/hero.png',
     'junk must not rewrite anything');
}

/* --------------------------- 6. the wiring exists in the page itself */
ok(/case 'asset_map':/.test(CLIENT), 'client.html has no asset_map case');
ok(/state\._docGen = state\._deltaGen;/.test(CLIENT),
   'the render path does not record the generation it displayed');

console.log((fails.length ? 'FAIL' : 'PASS') + '  asset upgrade client: '
            + pass + ' checks, ' + fails.length + ' failed');
fails.forEach((f) => console.log('  - ' + f));
process.exit(fails.length ? 1 : 0);
