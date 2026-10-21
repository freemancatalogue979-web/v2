// No dependencies: node tests/test_page_guard.cjs
//
// The click guard decides which controls on a pushed LPV page are allowed to
// react. It is the difference between "the visitor can only press Sign in"
// and "any stray tap advances the workflow". The evaluator lives in
// client.html; this test extracts it and runs it against a tiny fake DOM so a
// regression is caught without a browser.
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
const start = source.indexOf('const GUARD_MAX_RULES = 40;');
const end = source.indexOf('// Fetch a server-provided HTML document', start);
assert.ok(start >= 0 && end > start, 'guard helpers not found in client.html');

const context = vm.createContext({ console, JSON, String, Array, Object });
vm.runInContext(source.slice(start, end), context);
const { normaliseGuard, guardAllows, guardMatchesRule, guardActionable } = context;

// ---------------------------------------------------------------------------
// Minimal element: tag, id, class, attributes, text, parent chain, closest()
// ---------------------------------------------------------------------------
function el(tag, opts = {}) {
  const node = {
    tagName: String(tag).toUpperCase(),
    id: opts.id || '',
    className: opts.className || '',
    _attrs: Object.assign({}, opts.attrs || {}),
    textContent: opts.text || '',
    value: opts.value || '',
    parentElement: null,
    getAttribute(name) {
      if (name === 'class') return node.className || null;
      return Object.prototype.hasOwnProperty.call(node._attrs, name)
        ? node._attrs[name] : null;
    },
    hasAttribute(name) {
      return Object.prototype.hasOwnProperty.call(node._attrs, name);
    },
    closest(selector) {
      // Only the compound selector lists the client itself passes.
      const parts = String(selector).split(',').map((s) => s.trim());
      let cur = node;
      while (cur) {
        for (const raw of parts) {
          const m = raw.match(/^([a-zA-Z0-9-]+)?(?:\[([^\]=]+)(?:=["']?([^"'\]]*)["']?)?\])?$/);
          if (!m) continue;
          const [, tagName, attrName, attrValue] = m;
          let hit = true;
          if (tagName && cur.tagName !== tagName.toUpperCase()) hit = false;
          if (hit && attrName) {
            const val = cur.getAttribute(attrName);
            if (val === null) hit = false;
            else if (attrValue && val !== attrValue) hit = false;
          }
          if (hit) return cur;
        }
        cur = cur.parentElement;
      }
      return null;
    },
  };
  return node;
}
function nest(parent, child) { child.parentElement = parent; return child; }

// ---- normaliseGuard -------------------------------------------------------
{
  const off = normaliseGuard(null);
  ok(off.enabled === false && off.allow.length === 0, 'null guard is unlocked');
  ok(guardAllows(off, el('button')) === true, 'unlocked guard allows everything');

  const fromString = normaliseGuard('{"enabled":true,"allow":["#a","#a"," .b "]}');
  ok(fromString.enabled === true, 'guard parsed from JSON string');
  ok(fromString.allow.length === 2, 'duplicate rules dropped, blanks ignored');
  ok(fromString.allow[1] === '.b', 'rules trimmed');

  const flat = normaliseGuard({ enabled: 'yes', allow: '#signin, text:Sign in\nbutton' });
  ok(flat.enabled === true && flat.allow.length === 3, 'comma/newline allowlist split');
  ok(flat.notify === true && flat.feedback === true, 'notify/feedback default on');

  const noisy = normaliseGuard({ enabled: 1, allow: Array(60).fill('#x').concat(['#y']) });
  ok(noisy.allow.length === 2, 'rules de-duplicated before the cap');
  const capped = normaliseGuard({ enabled: true, allow: Array.from({ length: 60 }, (_, i) => '#r' + i) });
  ok(capped.allow.length === 40, 'rules capped at 40');
  const longRule = normaliseGuard({ enabled: true, allow: ['text:' + 'x'.repeat(500)] });
  ok(longRule.allow[0].length === 200, 'rule length capped');
}

// ---- locked page: only the allowlisted control passes --------------------
{
  const page = el('div');
  const signIn = nest(page, el('button', { id: 'signin', text: 'Sign in' }));
  const submit = nest(page, el('button', { attrs: { type: 'submit' }, id: 'go', text: 'Continue' }));
  const label = nest(signIn, el('span', { text: 'Sign in' }));
  const cancel = nest(page, el('button', { text: 'Cancel', id: 'cancel', className: 'btn secondary' }));

  const locked = normaliseGuard({ enabled: true, allow: ['#signin'] });
  ok(guardAllows(locked, signIn) === true, '#rule matches the id');
  ok(guardAllows(locked, label) === true, 'rule matches the label inside the button');
  ok(guardAllows(locked, submit) === false, 'other buttons are blocked');
  ok(guardAllows(locked, cancel) === false, 'unknown controls are blocked');
  ok(guardAllows(locked, null) === false, 'a click with no target is blocked');

  ok(guardAllows(normaliseGuard({ enabled: true, allow: ['*'] }), cancel) === true,
    '"*" disables the lock');
  ok(guardAllows(normaliseGuard({ enabled: false, allow: ['#signin'] }), cancel) === true,
    'a disabled guard allows everything');

  const byText = normaliseGuard({ enabled: true, allow: ['text:sign in'] });
  ok(guardAllows(byText, label) === true, 'text: matches visible text (case-insensitive)');
  ok(guardAllows(byText, cancel) === false, 'text: does not match other labels');

  const byAttr = normaliseGuard({ enabled: true, allow: ['button[type=submit]'] });
  ok(guardAllows(byAttr, submit) === true, 'CSS attribute selector matches');
  ok(guardAllows(byAttr, signIn) === false, 'CSS attribute selector rejects others');

  const byName = normaliseGuard({ enabled: true, allow: ['name:identifier'] });
  const named = nest(page, el('input', { attrs: { name: 'identifier' } }));
  ok(guardAllows(byName, named) === true, 'name: rule matches');
  const byAria = normaliseGuard({ enabled: true, allow: ['aria:continue'] });
  const aria = nest(page, el('button', { attrs: { 'aria-label': 'Continue to bank' } }));
  ok(guardAllows(byAria, aria) === true, 'aria: rule matches');
  const byClass = normaliseGuard({ enabled: true, allow: ['.secondary'] });
  ok(guardAllows(byClass, cancel) === true, '.class rule matches');
  const byTag = normaliseGuard({ enabled: true, allow: ['button'] });
  ok(guardAllows(byTag, cancel) === true, 'tag rule matches');

  // Garbage rules must never crash or silently unlock the page.
  const garbage = normaliseGuard({ enabled: true, allow: ['[[[', ':::', '#'] });
  ok(guardAllows(garbage, cancel) === false, 'invalid rules fail closed');
  ok(guardActionable(label) === signIn, 'the actionable element is the button, not the span');
}

// ---- the wiring is in place ---------------------------------------------
ok(source.includes('if (!LPV.allows(ev && ev.target)) {'),
  'iframe click handler consults the guard');
ok(source.includes("case 'lpv_guard':"), 'live guard updates are handled');
ok(source.includes('_guardBlocked('), 'blocked clicks are reported');

console.log('client.html click-guard checks passed');
console.log(`${checks} checks passed`);
