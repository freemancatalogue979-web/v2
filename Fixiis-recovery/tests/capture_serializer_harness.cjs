// No dependencies: node tests/capture_serializer_harness.cjs
//
// The capture serializer is the one piece of the pipeline that decides what a
// mirorred page is made of, and it runs inside the captured page — the hardest
// thing in this repo to exercise without a browser. This test pulls
// `_FAST_SERIALIZE_JS` straight out of dom_capture.py and runs it in a `vm`
// against a hand-built DOM, so the accuracy rules are pinned:
//
//   * generated content (`::before` / `::after`) is materialized in flow
//   * hover/focus-only generated content is NOT (it would be wrong on load)
//   * `disabled` / `readonly` set as PROPERTIES reach the markup
//   * a <picture> collapses onto the candidate the page actually displayed
//   * `loading="lazy"` never ships (the mirror is a photograph of a loaded page)
//
// A quiet regression here does not throw anywhere in production: pages just
// come out looking slightly emptier than the real thing.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

// ---------------------------------------------------------------------------
// Extract the serializer source
// ---------------------------------------------------------------------------
const py = fs.readFileSync(path.join(__dirname, '..', 'dom_capture.py'), 'utf8');
const startMark = '_FAST_SERIALIZE_JS = r"""';
const start = py.indexOf(startMark);
assert.ok(start >= 0, '_FAST_SERIALIZE_JS not found in dom_capture.py');
const bodyStart = start + startMark.length;
const bodyEnd = py.indexOf('"""', bodyStart);
assert.ok(bodyEnd > bodyStart, 'unterminated serializer literal');
const serializerSource = py.slice(bodyStart, bodyEnd);
ok(/::before/.test(serializerSource) || /PSEUDO/.test(serializerSource),
  'the serializer knows about generated content');
ok(/disabled/.test(serializerSource), 'the serializer carries control state');

// ---------------------------------------------------------------------------
// Tiny DOM
// ---------------------------------------------------------------------------
function el(tag, opts = {}) {
  const node = {
    nodeType: 1,
    localName: String(tag).toLowerCase(),
    get tagName() { return node.localName.toUpperCase(); },
    attributes: (opts.attrs || []).map(([name, value]) => ({ name, value: String(value) })),
    childNodes: [],
    parentElement: null,
    shadowRoot: null,
    style: {},
    checked: !!opts.checked,
    selected: !!opts.selected,
    disabled: !!opts.disabled,
    readOnly: !!opts.readOnly,
    value: opts.value == null ? '' : opts.value,
    currentSrc: opts.currentSrc || '',
    offsetWidth: opts.offsetWidth || 0,
    offsetHeight: opts.offsetHeight || 0,
    sheet: opts.sheet || null,
    getAttribute(name) {
      const a = node.attributes.find((x) => x.name === name);
      return a ? a.value : null;
    },
    hasAttribute(name) {
      return node.attributes.some((x) => x.name === name);
    },
    setAttribute(name, value) {
      const a = node.attributes.find((x) => x.name === name);
      if (a) a.value = String(value);
      else node.attributes.push({ name, value: String(value) });
    },
    get textContent() {
      return node.childNodes.map((c) => (c.nodeType === 3 ? c.nodeValue : c.textContent)).join('');
    },
    appendChild(child) {
      child.parentElement = node;
      node.childNodes.push(child);
      return child;
    },
  };
  (opts.children || []).forEach((c) => node.appendChild(c));
  return node;
}
function text(value) {
  return { nodeType: 3, nodeValue: value };
}
function comment(value) {
  return { nodeType: 8, nodeValue: value };
}
function rule(selectorText, cssText) {
  return { type: 1, selectorText, cssText: cssText || selectorText + '{}' };
}
function media(cssText, rules) {
  return { type: 4, cssText, cssRules: rules };
}

// A page with the shapes that used to be lost.
const liA = el('li', { attrs: [['class', 'item']], children: [text('One')] });
const liB = el('li', { attrs: [['class', 'item']], children: [text('Two')] });
const ul = el('ul', { children: [liA, liB] });
const badge = el('span', { attrs: [['class', 'badge']], children: [text('Inbox')] });
const caret = el('a', { attrs: [['class', 'chevron']], children: [text('Next')] });
const quote = el('blockquote', { attrs: [['class', 'quote']], children: [text('To be.')] });
const inert = el('div', { attrs: [['class', 'clearfix']], children: [el('p', { children: [text('x')] })] });

const sheetRules = [
  rule('li.item::before', 'li.item::before{content:"• ";position:static}'),
  rule('.badge::after', '.badge::after{content:" new"}'),
  rule('a.chevron:hover::after', 'a.chevron:hover::after{content:"→"}'),
  rule('blockquote.quote::before', 'blockquote.quote::before{content:"\\201C"}'),
  rule('.clearfix::after', '.clearfix::after{content:""}'),
  media('@media screen{.promo::before{content:"!"}}', [rule('.promo::before', '.promo::before{content:"!"}')]),
];

// Resolution table for getComputedStyle(el, '::before'|'::after').
const pseudo = new Map();
pseudo.set(liA, { before: '"• "' });
pseudo.set(liB, { before: '"• "' });
pseudo.set(badge, { after: '" new"' });
// a:hover::after must not be applied to the element itself.
pseudo.set(quote, { before: '"\\201C"' });
pseudo.set(inert, { after: '""' });

const source = el('source', { attrs: [['srcset', 'big.jpg 2x']] });
const img = el('img', {
  attrs: [['src', 'placeholder.gif'], ['srcset', 'small.jpg 1x, big.jpg 2x'], ['loading', 'lazy'], ['alt', 'photo']],
  currentSrc: 'https://cdn.example/big.jpg',
});
const picture = el('picture', { children: [source, img] });

const button = el('button', { attrs: [['type', 'submit']], children: [text('Pay')], disabled: true });
const roInput = el('input', { attrs: [['type', 'text'], ['value', '']], readOnly: true });
const cb = el('input', { attrs: [['type', 'checkbox']], checked: true });

const head = el('head', { children: [el('style', { children: [text('.x{color:red}')], sheet: { cssRules: sheetRules } })] });
const body = el('body', {
  children: [ul, badge, caret, quote, inert, picture, button, roInput, cb],
});
const html = el('html', { children: [head, body] });

const selectorMap = {
  'li.item': [liA, liB],
  '.badge': [badge],
  'a.chevron': [caret],
  'blockquote.quote': [quote],
  '.clearfix': [inert],
};

const styleSheets = [{ cssRules: sheetRules }];

const document = {
  documentElement: html,
  head,
  body,
  styleSheets,
  querySelectorAll(sel) {
    return selectorMap[String(sel)] || [];
  },
};

const window = {
  innerWidth: 1200,
  innerHeight: 900,
  getComputedStyle(node, which) {
    if (!which) return { position: 'static', display: 'block', visibility: 'visible', content: 'normal' };
    const which2 = String(which).replace(/^::?/, '');
    const entry = pseudo.get(node) || {};
    return {
      content: entry[which2] || 'none',
      display: 'inline',
      visibility: 'visible',
      position: 'static',
    };
  },
};

const sandbox = { document, window, console, String, Math, Map, Set, URL, WeakMap, parseInt, RegExp };
sandbox.globalThis = sandbox;
const context = vm.createContext(sandbox);
const serialize = vm.runInContext('(' + serializerSource + ')', context);

const out = serialize({ deltaIds: false });
ok(typeof out === 'string' && out.length > 100, 'the serializer produced HTML');

// ---------------------------------------------------------------------------
// 1. Generated content
// ---------------------------------------------------------------------------
ok(out.includes('<span data-shf-pseudo="before">• </span>'),
  'a ::before bullet is materialized inside the element it decorates');
ok((out.match(/data-shf-pseudo="before"/g) || []).length === 3,
  'every matching element gets exactly one ::before (2 items + the quote)');
ok(/<span class="badge">Inbox<span data-shf-pseudo="after"> new<\/span><\/span>/.test(out),
  'an ::after badge rides after the element content');
ok(!/data-shf-pseudo="after">→</.test(out),
  'hover-only generated content is not baked into the page');
ok(out.includes('<span data-shf-pseudo="before">“</span>'),
  'CSS escapes in content are decoded to the character the page drew');
ok(!/<div class="clearfix">.*?data-shf-pseudo/.test(out),
  'an empty ::after (the clearfix idiom) is not turned into a node');

// ---------------------------------------------------------------------------
// 2. Live control state
// ---------------------------------------------------------------------------
ok(/<button type="submit" value="" disabled>/.test(out) || /<button[^>]*disabled/.test(out),
  'a button disabled from script ships disabled');
ok(/<input type="text"[^>]*readonly/.test(out), 'a read-only input ships read-only');
ok(/<input type="checkbox" checked value="on">/.test(out) || /<input[^>]*type="checkbox"[^>]*checked/.test(out),
  'a checked checkbox ships checked');

// ---------------------------------------------------------------------------
// 3. Images
// ---------------------------------------------------------------------------
const imgOut = out.slice(out.indexOf('<img'));
ok(!out.includes('<source'), 'a <picture> source list is dropped in favour of the chosen image');
ok(imgOut.includes('src="https://cdn.example/big.jpg"'),
  'the image is the candidate the page actually displayed (currentSrc)');
ok(!/loading="lazy"/.test(out), 'lazy images are forced eager in the mirror');
ok(!/srcset=/.test(out), 'the stale srcset does not outrank the resolved src');
ok(/alt="photo"/.test(out), 'ordinary attributes survive');

// ---------------------------------------------------------------------------
// 4. Nothing else regressed
// ---------------------------------------------------------------------------
ok(out.startsWith('<!DOCTYPE html>'), 'a doctype is emitted');
ok(out.includes('<ul>') && out.includes('</ul>'), 'the document tree still serializes');
ok(!out.includes('data-mid'), 'delta ids stay off unless asked for');

const withIds = serialize({ deltaIds: true });
ok(/<li class="item" data-mid="\d+"/.test(withIds), 'delta ids come back when the caller asks');

console.log('capture serializer: ' + checks + ' checks passed');
