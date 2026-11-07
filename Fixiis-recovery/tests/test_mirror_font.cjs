// No dependencies: node tests/test_mirror_font.cjs
//
// Two surfaces, two answers:
//
//   * the LIVE mirror (the streamed browser) is set in Roboto Slab, the same
//     font as the shell and the panels, so what the operator watches all day
//     looks like one product. The face itself travels in the theme — a data:
//     URI copied out of the shell's own <style id="shf-font-face"> — so the
//     theme needs NO network request, and works even though a mirrored
//     document has an opaque (blob:) origin where a URL font would be a
//     blocked cross-origin request;
//   * an LPV page is NOT. A pushed Live-pages document is the operator's own
//     saved page shown to a visitor as it was captured, so restyling it in our
//     font would be changing their page. It keeps its own typography, always.
//
// ensureMirrorFont() forces the font on a captured document; this test extracts
// it — and the LPV guard beside it — and runs them against a fake document, and
// pins the pieces of client.html that have to stay in step.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const source = fs.readFileSync(path.join(__dirname, '..', 'client.html'), 'utf8');

// ---------------------------------------------------------------------------
// Pull the helper out of client.html and run it in a sandbox.
// ---------------------------------------------------------------------------
const start = source.indexOf('const MIRROR_FONT_STACK =');
const endMarker = 'function ensureMirrorFont(doc) {';
const endStart = source.indexOf(endMarker);
assert.ok(start >= 0 && endStart > start, 'mirror font helpers not found in client.html');
const end = source.indexOf('\n            }\n', endStart) + '\n            }\n'.length;
const slice = source.slice(start, end);

// The face the shell carries, exactly as the browser would read it: the
// mirror copies this text, it does not build its own copy of the payload.
const faceStart = source.indexOf('<style id="shf-font-face">');
assert.ok(faceStart > 0, 'client.html has no embedded face block');
const faceOpenEnd = source.indexOf('>', faceStart) + 1;
const faceClose = source.indexOf('</style>', faceOpenEnd);
const SHELL_FACE = source.slice(faceOpenEnd, faceClose).trim();
assert.ok(/@font-face/.test(SHELL_FACE), 'the embedded block is not a @font-face');

function makeContext(search, opts) {
  const options = opts || {};
  const context = vm.createContext({
    console,
    String,
    URLSearchParams,
    // `mirrorFontFaceCss()` reads the shell's own <style>; a page without it
    // must still theme (with the stack, just without the face).
    document: {
      getElementById: (id) => (id === 'shf-font-face' && !options.noFace
        ? { textContent: SHELL_FACE }
        : null),
    },
    window: { location: { search: search || '' } },
  });
  vm.runInContext(slice, context);
  return context;
}

// ---------------------------------------------------------------------------
// Minimal fake document: head + body, ids, appendChild, nextElementSibling.
// ---------------------------------------------------------------------------
function element(tag, attrs) {
  const el = {
    tagName: String(tag).toUpperCase(),
    id: (attrs && attrs.id) || '',
    _attrs: Object.assign({}, attrs || {}),
    _children: [],
    parentElement: null,
    textContent: '',
    rel: '',
    href: '',
    setAttribute(name, value) { el._attrs[name] = String(value); },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(el._attrs, name) ? el._attrs[name] : null;
    },
    appendChild(child) {
      child.parentElement = el;
      child.parentNode = el;          // the DOM has both; the code uses either
      el._children.push(child);
      if (child.id) el._owner._byId[child.id] = child;
      return child;
    },
    removeChild(child) {
      const i = el._children.indexOf(child);
      if (i >= 0) el._children.splice(i, 1);
      child.parentElement = null;
      child.parentNode = null;
      if (child.id) delete el._owner._byId[child.id];
      return child;
    },
    get nextElementSibling() {
      if (!el.parentElement) return null;
      const sibs = el.parentElement._children;
      const i = sibs.indexOf(el);
      return i >= 0 ? (sibs[i + 1] || null) : null;
    },
  };
  return el;
}

function makeDoc() {
  const doc = {
    _byId: {},
    getElementById(id) { return doc._byId[id] || null; },
    createElement(tag) {
      const el = element(tag);
      el._owner = doc;
      return el;
    },
  };
  const head = doc.createElement('head');
  const body = doc.createElement('body');
  doc.head = head;
  doc.body = body;
  doc.documentElement = doc.createElement('html');
  doc.documentElement.appendChild(head);
  doc.documentElement.appendChild(body);
  return doc;
}

// ---------------------------------------------------------------------------
// 1. The font is injected into the captured document
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('');
  const doc = makeDoc();

  const result = ctx.ensureMirrorFont(doc);

  ok(result === true, 'the helper reports that the page is themed');

  // A font <link> inside a mirrored page is a request the visitor pays for
  // and that can fail, so its absence is part of the design, not an omission.
  ok(!doc.getElementById('__shifix_mirror_font_css__'),
    'no font stylesheet is injected (the face travels inline)');
  ok(!/fonts\.googleapis/.test(doc.documentElement.innerHTML || ''),
    'nothing in the document points at a font CDN');

  const style = doc.getElementById('__shifix_mirror_font__');
  ok(!!style, 'an override stylesheet is added');
  ok(/font-family:Roboto Slab/.test(style.textContent),
    'the family is Roboto Slab');
  ok(!/'Roboto Slab'/.test(style.textContent) && !/"Roboto Slab"/.test(style.textContent),
    'the family is unquoted - a quoted family inside a JS string has broken this page before');
  ok(/@font-face\{font-family:Roboto Slab/.test(style.textContent),
    'the frame gets the @font-face itself, not just a name it cannot resolve');
  ok(style.textContent.indexOf('@font-face') < style.textContent.indexOf('html,body{'),
    'the face comes first');
  ok(/src:url\(data:font\/woff2;base64,[A-Za-z0-9+/=]+\)/.test(style.textContent),
    'the face is embedded as a data: URI (an opaque-origin frame cannot fetch a URL font)');
  ok(style.textContent.includes(SHELL_FACE),
    'the mirror copies the shell\'s face verbatim - one payload, not two');
  ok(/Georgia/.test(style.textContent) && /serif!important/.test(style.textContent),
    'a slab-flavoured fallback stack stays behind it');
  ok(/html,body\{font-family:/.test(style.textContent), 'the document root is covered');
  ok(/body,body \*/.test(style.textContent), 'every element in the body is covered');
  const important = style.textContent.match(/!important/g) || [];
  ok(important.length >= 2, 'the rules win over the page\'s own typography');
}

// ---------------------------------------------------------------------------
// 2. Icon fonts and svg are left alone — their glyphs are characters in their
//    own family, so replacing it would turn every icon into a letter
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('');
  const doc = makeDoc();
  ctx.ensureMirrorFont(doc);
  const css = doc.getElementById('__shifix_mirror_font__').textContent;

  ok(/:not\(svg\)/.test(css), 'svg is excluded');
  ok(!/:not\(svg \*\)/.test(css),
    'no complex :not() argument (some engines ignore the whole condition)');
  ok(/:not\(i\)/.test(css), 'the classic <i class="fa"> icon element is excluded');
  ok(/\[class\*="fa-"\]/.test(css), 'Font Awesome is excluded');
  ok(/\[class\*="glyphicon"\]/.test(css), 'Bootstrap glyphicons are excluded');
  ok(/\[class\*="material-icons"\]/.test(css), 'Material Icons are excluded');
  ok(/\[class\*="mdi"\]/.test(css), 'Material Design Icons are excluded');
  ok(/\[class\*="icon"\]/.test(css), 'anything declaring itself an icon is excluded');
}

// ---------------------------------------------------------------------------
// 3. Idempotent, and kept last in <head>
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('');
  const doc = makeDoc();

  ctx.ensureMirrorFont(doc);
  ctx.ensureMirrorFont(doc);
  ctx.ensureMirrorFont(doc);

  const links = doc.head._children.filter((c) => c.id === '__shifix_mirror_font_css__');
  const styles = doc.head._children.filter((c) => c.id === '__shifix_mirror_font__');
  ok(links.length === 0, 'still no font stylesheet, however often it runs');
  ok(styles.length === 1, 'one override stylesheet, no matter how often it runs');

  // A page stylesheet injected after ours (a themed capture does this).
  const late = doc.createElement('style');
  late.textContent = 'body { font-family: Arial !important; }';
  doc.head.appendChild(late);

  ctx.ensureMirrorFont(doc);
  const order = doc.head._children.map((c) => c.id || '(page)');
  ok(order[order.length - 1] === '__shifix_mirror_font__',
    'the override is moved back to the end after a page stylesheet (got ' + order.join(',') + ')');
}

// ---------------------------------------------------------------------------
// 4. ?keep_fonts=1 leaves a page with its own typography
// ---------------------------------------------------------------------------
{
  for (const flag of ['1', 'true', 'YES']) {
    const ctx = makeContext('?keep_fonts=' + flag);
    const doc = makeDoc();
    const result = ctx.ensureMirrorFont(doc);
    ok(result === false, '?keep_fonts=' + flag + ' disables the override');
    ok(!doc.getElementById('__shifix_mirror_font__'), '?keep_fonts=' + flag + ' injects nothing');
    ok(!doc.getElementById('__shifix_mirror_font_css__'), '?keep_fonts=' + flag + ' adds no font request');
  }
  for (const search of ['', '?workflow=abc', '?keep_fonts=0']) {
    const ctx = makeContext(search);
    const doc = makeDoc();
    ok(ctx.ensureMirrorFont(doc) === true, 'the font is forced for "' + search + '"');
  }
}

// ---------------------------------------------------------------------------
// 5. The shell itself is Roboto Slab, it fetches nothing for type, and
//    nothing still asks for Montserrat
// ---------------------------------------------------------------------------
{
  ok(!/Montserrat/.test(source), 'no Montserrat left in client.html');
  ok(!/family=Space\+Grotesk/.test(source),
    'client.html no longer downloads Space Grotesk');
  ok(!/<link[^>]+fonts\.googleapis[^>]*>/.test(source),
    'no font stylesheet is linked at all — the shell cannot wait on a CDN');
  ok(!/rel="preconnect"[^>]*fonts\.g/.test(source),
    'not even a preconnect to a font host is left behind');

  const globalRule = source.match(/html, body, input, button, textarea, select, optgroup \{[\s\S]*?\}/);
  ok(!!globalRule && /font-family:\s*Roboto Slab/.test(globalRule[0]),
    'the shell\'s own global font rule uses Roboto Slab');
  ok(!/Helvetica Neue/.test(source),
    'and nothing in client.html still asks for the old face');
  ok((source.match(/font-family:\s*Roboto Slab/g) || []).length >= 3,
    'the runtime overlays (loading screen, LPV page, guard toast) use it too');
  ok(source.indexOf('<style id="shf-font-face">') > 0,
    'the shell carries the face it names');
}

// ---------------------------------------------------------------------------
// 6. The live mirror applies it; an LPV page never does
// ---------------------------------------------------------------------------
{
  const listeners = source.indexOf('function setupIframeEventListeners(d) {');
  ok(listeners > 0, 'setupIframeEventListeners exists');
  const listenersBody = source.slice(listeners, listeners + 1600);
  ok(/ensureMirrorFont\(d\)/.test(listenersBody),
    'the live mirror applies the font on every document commit');

  ok(source.indexOf('ensureMirrorFont(innerDoc)') === -1,
    'the LPV pushed page no longer applies our font at all');
  ok(source.indexOf('stripMirrorFont(innerDoc)') > 0,
    'it removes the theme instead, in case something applied it first');

  const lpv = source.indexOf("frame.setAttribute('data-lpv-frame', 'true')");
  ok(lpv > 0, 'the LPV frame is still marked');
  const mark = source.indexOf("setAttribute('data-lpv-page', '1')");
  ok(mark > 0, 'and the pushed document itself is marked before anything else runs');
  ok(source.indexOf('frame.classList.add(\'lpv-page-frame\')') > 0,
    'with the frame class as a third tell-tale');

  const commit = source.indexOf('__shifix_stream_reset__');
  ok(commit > 0, 'the mirror reset block is still there');
}

// ---------------------------------------------------------------------------
// 7. An LPV document is refused — executed, not merely grepped
// ---------------------------------------------------------------------------
{
  function lpvDoc(mark) {
    const doc = makeDoc();
    if (mark === 'attribute') {
      doc.documentElement.setAttribute('data-lpv-page', '1');
    } else if (mark === 'window') {
      doc.defaultView = { __SHF_LPV_PAGE__: true };
    } else if (mark === 'frame') {
      doc.defaultView = {
        frameElement: {
          getAttribute: (n) => (n === 'data-lpv-frame' ? 'true' : null),
          className: '',
        },
      };
    } else if (mark === 'class') {
      doc.defaultView = {
        frameElement: { getAttribute: () => null, className: 'lpv-redirect-frame lpv-page-frame' },
      };
    }
    return doc;
  }

  for (const mark of ['attribute', 'window', 'frame', 'class']) {
    const ctx = makeContext('');
    const doc = lpvDoc(mark);
    ok(ctx.isLpvPageDocument(doc) === true, 'the ' + mark + ' marker identifies an LPV page');
    ok(ctx.ensureMirrorFont(doc) === false, 'ensureMirrorFont refuses it (' + mark + ')');
    ok(!doc.getElementById('__shifix_mirror_font__'), 'no override stylesheet is added (' + mark + ')');
    ok(!doc.getElementById('__shifix_mirror_font_css__'), 'no font request is made (' + mark + ')');
  }

  // The live mirror is still themed with the same markers absent.
  const plain = makeContext('');
  const doc = makeDoc();
  doc.defaultView = { frameElement: { getAttribute: () => null, className: 'mirror-frame' } };
  ok(plain.isLpvPageDocument(doc) === false, 'a streamed mirror document is not an LPV page');
  ok(plain.ensureMirrorFont(doc) === true, 'so it is themed, as before');

  // A document that was themed and then became an LPV page is cleaned up.
  const cleanup = makeContext('');
  const reused = makeDoc();
  cleanup.ensureMirrorFont(reused);
  ok(!!reused.getElementById('__shifix_mirror_font__'), 'it was themed first');
  reused.documentElement.setAttribute('data-lpv-page', '1');
  ok(cleanup.ensureMirrorFont(reused) === false, 'once it is an LPV page the theme is refused');
  ok(!reused.getElementById('__shifix_mirror_font__'), 'the override stylesheet is removed again');
  ok(!reused.getElementById('__shifix_mirror_font_css__'), 'and so is the font request');
  const leftover = reused.head._children.filter((c) => /__shifix_mirror_font/.test(c.id || ''));
  ok(leftover.length === 0, 'nothing of ours is left in that document');
}

// ---------------------------------------------------------------------------
// 8. The panels agree — one product, one font
// ---------------------------------------------------------------------------
{
  const admin = fs.readFileSync(path.join(__dirname, '..', 'Admin.html'), 'utf8');
  const mini = fs.readFileSync(path.join(__dirname, '..', 'mini-admin.html'), 'utf8');
  // The phone panel is set in the embedded slab face; the admin console keeps
  // Helvetica for its body and embeds Corptic DEMO once for its display chrome.
  ok(/font-family:\s*Roboto Slab/.test(mini), 'mini-admin.html is set in Roboto Slab');
  ok(!/Helvetica Neue/.test(mini), 'mini-admin.html mixes in no other face');
  ok(/<style id="shf-font-face">/.test(mini), 'mini-admin.html carries the face it names');
  ok(/--mc-font:\s*Corptic, Montserrat,/.test(admin), 'Admin.html names Corptic for letters, Montserrat for numbers');
  ok(!/Roboto Slab/.test(admin), 'Admin.html mixes in no other face');
  ok(/<style id="shf-corptic-face">/.test(admin) && /font-family:\s*Corptic/.test(admin),
    'Admin.html carries the Corptic display face inline');
  ok((admin.match(/@font-face\s*\{/g) || []).length === 3,
    'its payload is exactly three @font-face rules - Corptic, the Montserrat digit subset, DM Mono');
  ok(!/Space\+?Grotesk/.test(admin), 'Admin.html no longer asks for Space Grotesk');
  ok(!/Space\+?Grotesk/.test(mini), 'mini-admin.html no longer asks for Space Grotesk');
  ok(!/fonts\.googleapis/.test(admin), 'Admin.html fetches no webfont');
  ok(!/fonts\.googleapis/.test(mini), 'mini-admin.html fetches no webfont');
  ok(/<style id="shf-montserrat-face">/.test(admin) && /font-family:\s*Montserrat;/.test(admin),
    'Admin.html embeds Montserrat as the digit face');
  ok(!/Montserrat/.test(mini), 'mini-admin.html has no Montserrat left');
}

console.log('mirror font: ' + checks + ' checks passed');
