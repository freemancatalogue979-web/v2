// No dependencies (jsdom optional): node tests/test_cssom_styles.cjs
//
// "elements like this are not captured well" — a Microsoft sign-in step whose
// six one-time-code boxes render in the mirror as full-width default <input>s,
// in the wrong place, with none of their rounded cells.
//
// Root cause: Fluent UI v9 (like Emotion and styled-components) writes its CSS
// with `CSSStyleSheet.insertRule()` into an <style> element that already
// exists. That changes no DOM text and fires no MutationObserver record, so the
// delta channel never carried those rules: a component rendered AFTER the first
// capture arrived in the mirror with its classes (`fui-Input__input r12stul0`)
// and none of their declarations — the browser's default input box, 177px wide.
//
// The fix has three parts, and this suite pins all three:
//   1. the delta serializer reads `<style>` text from the CSSOM, never from
//      stale DOM text;
//   2. a cheap rule-count signature notices insertRule() growth (even with no
//      DOM change) and re-sends the affected stylesheets as `s` ops;
//   3. the client applies `s` (including the adopted-stylesheet dump).
//
// The DOM half runs the REAL delta observer from dom_capture.py inside jsdom;
// without jsdom the source checks still run.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');
const capture = fs.readFileSync(path.join(root, 'dom_capture.py'), 'utf8');
const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');

// ---------------------------------------------------------------------------
// 1. The shipped bytes
// ---------------------------------------------------------------------------
const deltaStart = capture.indexOf('_DELTA_OBSERVER_JS = r"""');
ok(deltaStart > 0, 'the delta observer is in dom_capture.py');
const deltaBodyStart = capture.indexOf('"""', deltaStart) + 3;
const deltaSrc = capture.slice(deltaBodyStart, capture.indexOf('"""', deltaBodyStart));

ok(deltaSrc.includes('sheetRuleCount'), 'the observer can count a sheet\'s rules');
ok(deltaSrc.includes('cssSignature'), 'it keeps a cheap signature of the cascade');
ok(deltaSrc.includes('flushStyleOps'), 'and a routine that ships a changed sheet');
ok(deltaSrc.includes("pushCoalesced('s|' + id, ['s', id, css])"),
  'a changed <style> is re-sent from the CSSOM as an `s` op');
ok(deltaSrc.includes("pushCoalesced('s|0', ['s', 0, adopted])"),
  'adopted stylesheets ride the same op (id 0)');
ok(/const flush = \(\) => \{\s*scheduled = false;\s*\/\/[\s\S]{0,200}?flushStyleOps\(\)/.test(deltaSrc),
  'every batch looks at the CSSOM first');
ok(deltaSrc.includes('setInterval(') && deltaSrc.indexOf('setInterval(') > deltaSrc.indexOf('window.__shifixDeltaFlush = flush;'),
  'and a slow timer catches growth that never coincides with a DOM change');
ok(deltaSrc.includes("(liveSheetCss(el.sheet) || el.textContent || '')"),
  'a re-sent <style> carries the live cascade, not stale text');

ok(client.includes("kind === 's'"), 'the mirror applies the `s` op');
ok(client.includes("doc.querySelector('style[data-shf-adopted=\"1\"]')"),
  'including the adopted-stylesheet dump, created when the document has none');
ok(client.includes('el.textContent = cssText'), 'by replacing the stylesheet text');
ok(client.includes('if (!el) { continue; }'),
  'a style element the mirror does not have never forces a resync storm');

// The full serializer has always read the CSSOM; keep it that way.
const fastStart = capture.indexOf('_FAST_SERIALIZE_JS = r"""');
const fastBodyStart = capture.indexOf('"""', fastStart) + 3;
const fastSrc = capture.slice(fastBodyStart, capture.indexOf('"""', fastBodyStart));
ok(fastSrc.includes('raw = liveSheetCss(el.sheet) || el.textContent'),
  'the full capture reads <style> from the CSSOM too');

// ---------------------------------------------------------------------------
// 2. The real observer, against a Fluent-shaped page
// ---------------------------------------------------------------------------
let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/jsd/node_modules/jsdom', '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}

if (!jsdom) {
  console.log(`\n${checks} checks passed (jsdom not installed: DOM half skipped).`);
  process.exit(0);
}

const { JSDOM } = jsdom;
const CELLS = 6;
const html = `<!doctype html><html><head><style id="griffel"></style></head><body>
  <div class="card"><h1>Enter your code</h1>
    <div id="code"></div>
  </div></body></html>`;

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
const win = dom.window;
const doc = win.document;

// Griffel's first pass: rules exist, element text is EMPTY (this is the bug).
const sheet = doc.getElementById('griffel').sheet;
sheet.insertRule('#code{display:flex;gap:8px}', 0);

const batches = [];
win.__domDelta = (payload) => {
  try { batches.push(JSON.parse(payload)); } catch (e) { /* ignore */ }
};

const script = deltaSrc.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '25');
win.eval(script);

const opsSince = (n) => batches.slice(n).reduce((all, b) => all.concat(b), []);

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

(async () => {
  await wait(60);                       // the install flush (navigation op)
  const installed = batches.length;
  ok(installed >= 1, 'the observer installs and speaks');

  // The step renders: six inputs whose classes the page has styled with
  // insertRule() — no DOM text, no mutation of the stylesheet element.
  win.eval(`(() => {
    const sheet = document.getElementById('griffel').sheet;
    sheet.insertRule('#code .cell{width:44px;height:44px;border-radius:6px;text-align:center}', sheet.cssRules.length);
    const row = document.getElementById('code');
    for (let i = 0; i < ${CELLS}; i++) {
      const el = document.createElement('input');
      el.type = 'text';
      el.maxLength = 1;
      el.id = 'codeEntry-' + i;
      el.className = 'fui-Input__input cell';
      el.setAttribute('aria-label', 'Enter code digit ' + (i + 1));
      row.appendChild(el);
    }
  })()`);

  await wait(200);                      // flush + the style interval
  const ops = opsSince(installed);

  const styleOps = ops.filter((op) => op[0] === 's');
  ok(styleOps.length >= 1, 'the new CSS is shipped as an `s` op');
  const css = styleOps.map((op) => String(op[2])).join('\n');
  ok(/width:\s*44px/.test(css), 'carrying the rule the new cells need');
  ok(/border-radius:\s*6px/.test(css), 'and the rest of the declarations');
  ok(/\.cell/.test(css), 'the selector reaches the inputs by class');

  const styleEl = doc.getElementById('griffel');
  const mid = Number(styleEl.getAttribute('data-mid'));
  ok(mid > 0, 'the stylesheet has a mirror id (so the op has a target)');
  ok(styleOps.some((op) => Number(op[1]) === mid),
    'the `s` op targets that stylesheet');

  // The inputs themselves are carried too, attributes and all.
  const inserts = ops.filter((op) => op[0] === 'i');
  const insertedHtml = inserts.map((op) => String(op[3])).join('');
  ok(/codeEntry-0/.test(insertedHtml), 'the six inputs are inserted in the mirror');
  ok(/fui-Input__input/.test(insertedHtml), 'with their real classes');
  ok(/maxlength="1"/.test(insertedHtml), 'and their maxlength — one digit per box');

  // A page that mutates its CSS again (a validation state) is followed.
  const before = batches.length;
  win.eval(`(() => {
    const sheet = document.getElementById('griffel').sheet;
    sheet.insertRule('#code .cell.is-error{border-color:#d13438}', sheet.cssRules.length);
  })()`);
  await wait(1500);
  const later = opsSince(before).filter((op) => op[0] === 's');
  ok(later.length >= 1, 'a second insertRule() pass is shipped as well');
  ok(/is-error/.test(later.map((op) => String(op[2])).join('')), 'with the new rule');

  // No change at all: the interval must stay quiet (no battery/bandwidth cost).
  const quietFrom = batches.length;
  await wait(1500);
  ok(opsSince(quietFrom).filter((op) => op[0] === 's').length === 0,
    'an unchanged cascade sends nothing (the signature is stable)');

  console.log(`\n${checks} checks passed.`);
  win.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
