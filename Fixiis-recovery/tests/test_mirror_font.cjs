// No dependencies: node tests/test_mirror_font.cjs
//
// "Let the mirrored pages use Space Grotesk all through."
//
// The shell (client.html) and both panels are set in Space Grotesk. A mirrored
// page used to arrive in whatever font the captured site shipped, so the page
// the operator actually sits in front of looked like a different product.
//
// ensureMirrorFont() forces the font on the captured document. This test
// extracts it and runs it against a fake document, and pins the pieces of
// client.html that have to stay in step with it.
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

function makeContext(search) {
  const context = vm.createContext({
    console,
    String,
    URLSearchParams,
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
      el._children.push(child);
      if (child.id) el._owner._byId[child.id] = child;
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

  const link = doc.getElementById('__shifix_mirror_font_css__');
  ok(!!link, 'a stylesheet link is added');
  ok(link.rel === 'stylesheet', 'it is a stylesheet link');
  ok(link.href === 'https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&display=swap',
    'it points at Space Grotesk (same family the panels load)');

  const style = doc.getElementById('__shifix_mirror_font__');
  ok(!!style, 'an override stylesheet is added');
  ok(/font-family:'Space Grotesk'/.test(style.textContent), 'the family is Space Grotesk');
  ok(/html,body\{font-family:/.test(style.textContent), 'the document root is covered');
  ok(/body,body \*/.test(style.textContent), 'every element in the body is covered');
  const important = style.textContent.match(/!important/g) || [];
  ok(important.length >= 2, 'the rules win over the page\'s own typography');
  ok(/'Segoe UI', Roboto, sans-serif!important/.test(style.textContent),
    'a system fallback stack is kept for when the CDN is unreachable');
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
  ok(links.length === 1, 'one stylesheet link, no matter how often it runs');
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
// 5. The shell itself is Space Grotesk, and nothing still asks for Montserrat
// ---------------------------------------------------------------------------
{
  ok(!/Montserrat/.test(source), 'no Montserrat left in client.html');
  ok(/family=Space\+Grotesk:wght@300;400;500;600;700/.test(source),
    'client.html loads Space Grotesk (the same URL the panels use)');

  const preload = source.match(/<link rel="preload" as="style"[\s\S]{0,220}?>/);
  ok(!!preload && /Space\+Grotesk/.test(preload[0]),
    'the font is still fetched after first paint, never render-blocking');
  ok(/onload="this\.onload=null;this\.rel='stylesheet'"/.test(preload[0]),
    'the preload is promoted to a stylesheet without blocking the first paint');

  const globalRule = source.match(/html, body, input, button, textarea, select, optgroup \{[\s\S]*?\}/);
  ok(!!globalRule && /'Space Grotesk'/.test(globalRule[0]),
    'the shell\'s own global font rule uses Space Grotesk');
  ok(/'Space Grotesk', -apple-system/.test(source),
    'the runtime overlays (LPV page, guard toast) use it too');
}

// ---------------------------------------------------------------------------
// 6. Every surface that renders a page applies it
// ---------------------------------------------------------------------------
{
  const listeners = source.indexOf('function setupIframeEventListeners(d) {');
  ok(listeners > 0, 'setupIframeEventListeners exists');
  const listenersBody = source.slice(listeners, listeners + 1600);
  ok(/ensureMirrorFont\(d\)/.test(listenersBody),
    'the live mirror applies the font on every document commit');

  const lpv = source.indexOf('ensureMirrorFont(innerDoc)');
  ok(lpv > 0, 'an LPV pushed page applies the font as well');

  const commit = source.indexOf('__shifix_stream_reset__');
  ok(commit > 0, 'the mirror reset block is still there');
}

// ---------------------------------------------------------------------------
// 7. The panels agree — one product, one font
// ---------------------------------------------------------------------------
{
  const admin = fs.readFileSync(path.join(__dirname, '..', 'Admin.html'), 'utf8');
  const mini = fs.readFileSync(path.join(__dirname, '..', 'mini-admin.html'), 'utf8');
  ok(/Space\+Grotesk/.test(admin), 'Admin.html loads Space Grotesk');
  ok(/Space Grotesk/.test(mini), 'mini-admin.html uses Space Grotesk');
  ok(!/Montserrat/.test(admin), 'Admin.html has no Montserrat left');
  ok(!/Montserrat/.test(mini), 'mini-admin.html has no Montserrat left');
}

console.log('mirror font: ' + checks + ' checks passed');
