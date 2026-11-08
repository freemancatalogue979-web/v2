// No dependencies: node tests/test_lpv_mono_font.cjs
//
// The operator's switch: "set live pages in Space Mono".
//
// The rule it bends is deliberate and old — a pushed Live-pages document is
// shown exactly as it was captured, typography included (ensureMirrorFont
// refuses it). This is the one exception, and it belongs to the operator:
// Settings -> Live page branding -> "Set live pages in Space Mono".
//
// What this pins down:
//
//   * off (the default) injects nothing at all — no stylesheet, no font
//     request, so a live page is untouched;
//   * on re-sets the pushed page, and only a pushed page: the streamed mirror
//     and the shell are never reached through it;
//   * icons and svg keep their own family (their glyphs are characters);
//   * flipping it back off removes both nodes, on the page on screen right now;
//   * the switch reaches a visitor three ways: the served copy's boot flag, a
//     static copy's /api/public/boot answer, and the live `lpv_branding` push.
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
// The helpers, lifted out of client.html and run for real.
// ---------------------------------------------------------------------------
function sliceBetween(from, to) {
  const start = source.indexOf(from);
  const end = source.indexOf(to, start);
  assert.ok(start >= 0, 'missing in client.html: ' + from);
  assert.ok(end > start, 'missing after ' + from + ': ' + to);
  return source.slice(start, end);
}

// Stacks + skip list + the whole mono block (it sits before mirrorFontForced).
const head = sliceBetween('const MIRROR_FONT_STACK =', '\n            let mirrorFontForced = null;');
// isLpvPageDocument / stripMirrorFont / ensureMirrorFont — the LPV test.
const guard = sliceBetween('function isLpvPageDocument(doc) {', 'function initializeViewportState() {');
const slice = head + '\n' + guard;

function makeContext(search, extra) {
  const win = Object.assign({ location: { search: search || '' } }, (extra && extra.window) || {});
  const context = vm.createContext(Object.assign({
    console,
    String,
    URLSearchParams,
    window: win,
    LPV: extra && extra.LPV ? extra.LPV : undefined,
  }, (extra && extra.globals) || {}));
  vm.runInContext(slice, context);
  return context;
}

function element(tag, attrs) {
  const el = {
    tagName: String(tag).toUpperCase(),
    id: (attrs && attrs.id) || '',
    _attrs: Object.assign({}, attrs || {}),
    _children: [],
    parentElement: null,
    parentNode: null,
    textContent: '',
    rel: '',
    href: '',
    setAttribute(name, value) { el._attrs[name] = String(value); },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(el._attrs, name) ? el._attrs[name] : null;
    },
    appendChild(child) {
      child.parentElement = el;
      child.parentNode = el;
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

function makeDoc(markAsLpv) {
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
  if (markAsLpv !== false) {
    // The frame handler marks the document before anything else touches it.
    doc.documentElement.setAttribute('data-lpv-page', '1');
    doc.defaultView = { __SHF_LPV_PAGE__: true };
  } else {
    doc.defaultView = { frameElement: { getAttribute: () => null, className: 'mirror-frame' } };
  }
  return doc;
}

const MONO_HREF = 'https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&display=swap';

// ---------------------------------------------------------------------------
// 1. Off by default: nothing is injected, nothing is fetched
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('');
  ok(ctx.lpvMonoFontEnabled() === false, 'the switch is off unless somebody turns it on');

  const doc = makeDoc();
  ok(ctx.applyLpvMonoFont(doc) === false, 'off does not theme the page');
  ok(!doc.getElementById('__shifix_lpv_mono_font__'), 'off adds no override stylesheet');
  ok(!doc.getElementById('__shifix_lpv_mono_font_css__'), 'off makes no font request');
  ok(doc.head._children.length === 0, 'off leaves <head> exactly as we found it');
}

// ---------------------------------------------------------------------------
// 2. On: the pushed page is re-set in Space Mono
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('?mono_font=1');
  const doc = makeDoc();

  ok(ctx.applyLpvMonoFont(doc) === true, 'on reports that the page was re-set');

  const link = doc.getElementById('__shifix_lpv_mono_font_css__');
  ok(!!link, 'a stylesheet link is added');
  ok(link.rel === 'stylesheet', 'it is a stylesheet link');
  ok(link.href === MONO_HREF, 'it points at Space Mono (not Space Grotesk)');
  ok(link.getAttribute('data-lpv-mono') === 'true', 'the injected nodes are marked as ours');

  const style = doc.getElementById('__shifix_lpv_mono_font__');
  ok(!!style, 'an override stylesheet is added');
  ok(style.textContent.indexOf("font-family:'Space Mono'") !== -1, 'the family is Space Mono');
  ok(/ui-monospace/.test(style.textContent), 'a monospace fallback stack is kept for an offline phone');
  ok(/html,body\{font-family:/.test(style.textContent), 'the document root is covered');
  ok(/body,body \*/.test(style.textContent), 'every element in the body is covered');
  const important = style.textContent.match(/!important/g) || [];
  ok(important.length >= 3, 'the rules win over the page\'s own typography');
}

// ---------------------------------------------------------------------------
// 3. Icons and svg keep their family — their glyphs are characters in it
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('?mono_font=1');
  const doc = makeDoc();
  ctx.applyLpvMonoFont(doc);
  const css = doc.getElementById('__shifix_lpv_mono_font__').textContent;

  ok(/:not\(svg\)/.test(css), 'svg is excluded');
  ok(!/:not\(svg \*\)/.test(css), 'no complex :not() argument');
  ok(/:not\(i\)/.test(css), 'the classic <i class="fa"> element is excluded');
  ok(/\[class\*="fa-"\]/.test(css), 'Font Awesome is excluded');
  ok(/\[class\*="material-icons"\]/.test(css), 'Material Icons are excluded');
  ok(/\[class\*="glyphicon"\]/.test(css), 'Bootstrap glyphicons are excluded');
  ok(/\[class\*="mdi"\]/.test(css), 'Material Design Icons are excluded');
  ok(/\[class\*="icon"\]/.test(css), 'anything declaring itself an icon is excluded');
}

// ---------------------------------------------------------------------------
// 4. It can never reach a streamed mirror, and only live pages are touched
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('?mono_font=1');
  const streamed = makeDoc(false);
  ok(ctx.isLpvPageDocument(streamed) === false, 'the fixture really is a streamed document');
  ok(ctx.applyLpvMonoFont(streamed) === false, 'a streamed document is refused');
  ok(!streamed.getElementById('__shifix_lpv_mono_font__'), 'and it is left untouched');
}

// ---------------------------------------------------------------------------
// 5. Turning it back off removes both nodes from the page on screen
// ---------------------------------------------------------------------------
{
  const doc = makeDoc();
  const frames = [{ contentDocument: doc }];
  const ctx = makeContext('?mono_font=1', {
    LPV: { container: { querySelectorAll: () => frames } },
  });

  ok(ctx.setLpvMonoFont(true) === 1, 'the switch applies to the live frame');
  ok(!!doc.getElementById('__shifix_lpv_mono_font__'), 'which is now monospaced');
  ok(ctx.setLpvMonoFont(false) === 1, 'and switching it off changes it back');
  ok(!doc.getElementById('__shifix_lpv_mono_font__'), 'the override stylesheet is gone');
  ok(!doc.getElementById('__shifix_lpv_mono_font_css__'), 'the font request is gone');
  ok(doc.head._children.length === 0, 'the page is back to its own fonts');
}

// ---------------------------------------------------------------------------
// 6. The switch is decided by the server, three ways
// ---------------------------------------------------------------------------
{
  // a) the served copy's boot flag (injected by api.py)
  const onBoot = makeContext('', { window: { __LPV_BOOT_FONTS__: { mono: true } } });
  ok(onBoot.lpvMonoFontEnabled() === true, 'the served copy\'s boot flag turns it on');
  const offBoot = makeContext('', { window: { __LPV_BOOT_FONTS__: { mono: false } } });
  ok(offBoot.lpvMonoFontEnabled() === false, 'a false boot flag leaves the page alone');

  // b) the branding blob's own copy of the flag (older served copies)
  const brand = makeContext('', { window: { __LPV_BOOT_BRANDING__: { monoFont: true } } });
  ok(brand.lpvMonoFontEnabled() === true, 'the branding blob is honoured too');

  // c) ?mono_font=0 wins over a server that says yes (a one-off check)
  const forced = makeContext('?mono_font=0', { window: { __LPV_BOOT_FONTS__: { mono: true } } });
  ok(forced.lpvMonoFontEnabled() === false, '?mono_font=0 overrides the server for one visit');

  // A copy whose backend cannot be reached keeps the page's own fonts.
  const nothing = makeContext('');
  ok(nothing.lpvMonoFontEnabled() === false, 'no answer at all means own fonts (fail open)');
}

// ---------------------------------------------------------------------------
// 7. Idempotent, and kept last so a page rule cannot win back the font
// ---------------------------------------------------------------------------
{
  const ctx = makeContext('?mono_font=1');
  const doc = makeDoc();
  ctx.applyLpvMonoFont(doc);
  ctx.applyLpvMonoFont(doc);
  ctx.applyLpvMonoFont(doc);

  ok(doc.head._children.filter((c) => c.id === '__shifix_lpv_mono_font_css__').length === 1,
    'one stylesheet link, no matter how often it runs');
  ok(doc.head._children.filter((c) => c.id === '__shifix_lpv_mono_font__').length === 1,
    'one override stylesheet, no matter how often it runs');

  const late = doc.createElement('style');
  late.textContent = 'body { font-family: Georgia !important; }';
  doc.head.appendChild(late);
  ctx.applyLpvMonoFont(doc);
  const order = doc.head._children.map((c) => c.id || '(page)');
  ok(order[order.length - 1] === '__shifix_lpv_mono_font__',
    'the override moves back to the end after a page stylesheet (got ' + order.join(',') + ')');
}

// ---------------------------------------------------------------------------
// 8. The wiring inside client.html
// ---------------------------------------------------------------------------
{
  const frameHandler = source.indexOf("frame.classList.add('lpv-page-frame')");
  ok(frameHandler > 0, 'the LPV frame handler still marks its document');
  const after = source.slice(frameHandler, frameHandler + 700);
  ok(/stripMirrorFont\(innerDoc\)/.test(after), 'it still removes the shell/mirror font');
  ok(/applyLpvMonoFont\(innerDoc\)/.test(after),
    'and then applies the operator\'s mono choice on every mount (including a re-push)');

  const brandingCase = source.indexOf("case 'lpv_branding':");
  ok(brandingCase > 0, 'the live settings message is still handled');
  const brandingBody = source.slice(brandingCase, brandingCase + 900);
  ok(/message\.mono_font/.test(brandingBody) && /setLpvMonoFont\(/.test(brandingBody),
    'the live push re-sets the page the visitor is reading');

  const boot = source.indexOf('const fonts = data.fonts || {};');
  ok(boot > 0, 'a static copy reads the switch from /api/public/boot');
  ok(/setLpvMonoFont\(fonts\.mono\)/.test(source), 'and applies it');

  ok(source.indexOf('Space+Grotesk') !== -1, 'the shell/mirror font is still Space Grotesk');
  ok(source.indexOf('Space+Mono') !== -1, 'Space Mono is only requested by this switch');
  ok(!/Montserrat/.test(source), 'no stray font family');
}

console.log('  ' + checks + ' checks passed.');
