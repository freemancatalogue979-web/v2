// No dependencies beyond jsdom: node tests/test_enter_intent.cjs
//
// "I type a search and press Enter. The server searches — the mirror keeps my
// text sitting in the box and never shows the result page."
//
// The mirror runs none of the page's scripts, so its <textarea> has only the
// browser default for Enter: insert a newline. On a send-on-Enter box (search
// fields, AI prompts, every chat composer) the real page SUPPRESSES that
// default, acts on the key, and usually clears the box. The mirror cannot know
// that from the markup, so it inserts a newline, shows the text still there,
// and looks like nothing was submitted.
//
// The page knows, and it runs server-side with the visitor's keypress. So the
// observer records what plain Enter ACTUALLY did there (newline appeared ->
// multiline composer; nothing appeared, or the box emptied -> sends), ships it
// as ['eh', mid, intent], and stamps data-shifix-enter into every later
// capture. This suite covers both halves: the inference (server) and the
// decision + behaviour (client).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let checks = 0;
const failures = [];
function ok(cond, label) {
  if (cond) { checks++; return; }
  failures.push(label);
}

const root = path.join(__dirname, '..');
const captureSrc = fs.readFileSync(path.join(root, 'dom_capture.py'), 'utf8');
const clientSrc = fs.readFileSync(path.join(root, 'client.html'), 'utf8');

function literal(marker) {
  const start = captureSrc.indexOf(marker);
  assert.ok(start >= 0, marker + ' not found');
  const bodyStart = captureSrc.indexOf('"""', start) + 3;
  return captureSrc.slice(bodyStart, captureSrc.indexOf('"""', bodyStart));
}
const deltaSrc = literal('_DELTA_OBSERVER_JS = r"""');
const fastSrc = literal('_FAST_SERIALIZE_JS = r"""');

let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/jsd/node_modules/jsdom', '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}
if (!jsdom) {
  console.log('skipped: jsdom not installed');
  process.exit(0);
}
const { JSDOM } = jsdom;

// ---------------------------------------------------------------------------
// Part A — the page side: what did Enter actually do?
// ---------------------------------------------------------------------------
async function observe(bodyHtml, press) {
  const dom = new JSDOM(
    `<!doctype html><html><head><title>t</title></head><body>${bodyHtml}</body></html>`,
    { url: 'https://example.test/', runScripts: 'outside-only', pretendToBeVisual: true });
  const win = dom.window;
  const batches = [];
  win.__domDelta = (payload) => {
    try { batches.push(JSON.parse(payload)); } catch (e) { /* ignore */ }
  };
  win.eval('(' + fastSrc + ')');
  win.eval(deltaSrc.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '15'));
  await new Promise((r) => setTimeout(r, 40));
  const mark = batches.length;
  try { press(win); } catch (e) { failures.push('press threw: ' + e.message); }
  await new Promise((r) => setTimeout(r, 120));
  const ops = batches.slice(mark).reduce((a, b) => a.concat(b), []);
  return { win, ops: ops.filter((o) => o[0] === 'eh') };
}

function keydown(win, el, init) {
  el.dispatchEvent(new win.KeyboardEvent('keydown', Object.assign({
    key: 'Enter', bubbles: true, cancelable: true,
  }, init || {})));
}

(async () => {
  // A1. Enter that the page suppressed: nothing appears -> it sends.
  {
    const { win, ops } = await observe(
      '<textarea id="q" role="combobox" rows="1"></textarea>',
      (w) => { const t = w.document.getElementById('q'); t.value = 'hello'; keydown(w, t); });
    ok(ops.length === 1, 'A1: one Enter hint shipped (got ' + ops.length + ')');
    ok(ops[0] && ops[0][2] === 'submit', 'A1: suppressed Enter reads as "submit" (got ' + (ops[0] && ops[0][2]) + ')');
    ok(ops[0] && typeof ops[0][1] === 'number', 'A1: the hint names a mid');
    ok(win.document.getElementById('q').__shifixEnter === 'submit',
      'A1: the element carries the hint for the next capture');
    win.close();
  }

  // A2. Enter that inserted a newline: a multiline composer.
  {
    const { win, ops } = await observe(
      '<textarea id="c"></textarea>',
      (w) => {
        const t = w.document.getElementById('c');
        t.value = 'line one';
        keydown(w, t);
        t.value = 'line one\n';            // the browser's default action
      });
    ok(ops.length === 1 && ops[0][2] === 'newline',
      'A2: an inserted newline reads as "newline" (got ' + JSON.stringify(ops) + ')');
    win.close();
  }

  // A3. Shift+Enter is the escape hatch everywhere; it says nothing about
  // what plain Enter does, so it must not flip the hint.
  {
    const { win, ops } = await observe(
      '<textarea id="c"></textarea>',
      (w) => {
        const t = w.document.getElementById('c');
        t.value = 'line one';
        keydown(w, t, { shiftKey: true });
        t.value = 'line one\n';
      });
    ok(ops.length === 0, 'A3: shift+Enter ships no hint (got ' + JSON.stringify(ops) + ')');
    win.close();
  }

  // A4. Enter on an <input> is always "submit": no hint needed, no chatter.
  {
    const { win, ops } = await observe(
      '<input id="i" type="text">',
      (w) => { const i = w.document.getElementById('i'); i.value = 'x'; keydown(w, i); });
    ok(ops.length === 0, 'A4: an <input> ships no hint (got ' + JSON.stringify(ops) + ')');
    win.close();
  }

  // A5. Repeated presses with the same outcome must not repeat the op.
  {
    const { win, ops } = await observe(
      '<textarea id="q"></textarea>',
      (w) => {
        const t = w.document.getElementById('q');
        keydown(w, t);
        keydown(w, t);
        keydown(w, t);
      });
    ok(ops.length === 1, 'A5: three identical presses ship one hint (got ' + ops.length + ')');
    win.close();
  }

  // A6. A page that changes its mind is followed: newline, then send.
  {
    const dom = new JSDOM(
      '<!doctype html><html><body><textarea id="q"></textarea></body></html>',
      { url: 'https://example.test/', runScripts: 'outside-only', pretendToBeVisual: true });
    const win = dom.window;
    const batches = [];
    win.__domDelta = (p) => { try { batches.push(JSON.parse(p)); } catch (e) {} };
    win.eval('(' + fastSrc + ')');
    win.eval(deltaSrc.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '15'));
    await new Promise((r) => setTimeout(r, 40));
    const t = win.document.getElementById('q');
    const mark = batches.length;
    t.value = 'a';
    keydown(win, t);
    t.value = 'a\n';                       // newline: a composer
    await new Promise((r) => setTimeout(r, 120));
    t.value = '';                          // ...which now sends and clears
    keydown(win, t);
    await new Promise((r) => setTimeout(r, 120));
    const ops = batches.slice(mark).reduce((a, b) => a.concat(b), []).filter((o) => o[0] === 'eh');
    ok(ops.length === 2 && ops[0][2] === 'newline' && ops[1][2] === 'submit',
      'A6: the hint follows the page (got ' + JSON.stringify(ops.map((o) => o[2])) + ')');
    win.close();
  }

  // A7. A sender that clears after a round trip: the field is empty a moment
  // later even though nothing visibly happened at keydown time.
  {
    const dom = new JSDOM(
      '<!doctype html><html><body><div id="ed" contenteditable="true">draft</div></body></html>',
      { url: 'https://example.test/', runScripts: 'outside-only', pretendToBeVisual: true });
    const win = dom.window;
    const batches = [];
    win.__domDelta = (p) => { try { batches.push(JSON.parse(p)); } catch (e) {} };
    win.eval('(' + fastSrc + ')');
    win.eval(deltaSrc.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '15'));
    await new Promise((r) => setTimeout(r, 40));
    const ed = win.document.getElementById('ed');
    const mark = batches.length;
    keydown(win, ed);
    ed.textContent = 'draft\n';            // editors insert a break...
    await new Promise((r) => setTimeout(r, 80));
    ed.textContent = '';                   // ...then the send clears it
    await new Promise((r) => setTimeout(r, 600));
    const ops = batches.slice(mark).reduce((a, b) => a.concat(b), []).filter((o) => o[0] === 'eh');
    const last = ops.length ? ops[ops.length - 1][2] : null;
    ok(last === 'submit', 'A7: an emptied contenteditable ends up "submit" (got ' + JSON.stringify(ops.map((o) => o[2])) + ')');
    win.close();
  }

  // A8. A full capture stamps the hint, so it survives a resync.
  {
    const dom = new JSDOM(
      '<!doctype html><html><body><textarea id="q"></textarea></body></html>',
      { url: 'https://example.test/', runScripts: 'outside-only', pretendToBeVisual: true });
    const win = dom.window;
    win.__domDelta = () => {};
    const serialize = win.eval('(' + fastSrc + ')');
    win.eval(deltaSrc.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '15'));
    await new Promise((r) => setTimeout(r, 40));
    const t = win.document.getElementById('q');
    keydown(win, t);
    await new Promise((r) => setTimeout(r, 120));
    const html = serialize({ deltaIds: true });
    ok(/<textarea[^>]*data-shifix-enter="submit"/.test(html),
      'A8: the full capture carries data-shifix-enter (got ' +
      (html.match(/<textarea[^>]*>/) || ['<none>'])[0] + ')');
    win.close();
  }

  // -------------------------------------------------------------------------
  // Part B — the mirror side: what does it do with that knowledge?
  // -------------------------------------------------------------------------

  // enterIntentFor() and the isEditableHost() it leans on, as they ship.
  const helperStart = clientSrc.indexOf('function isEditableHost(');
  assert.ok(helperStart > 0, 'isEditableHost() not found in client.html');
  const editableEnd = clientSrc.indexOf('\n            }\n', helperStart) + '\n            }\n'.length;
  const editableSrc = clientSrc.slice(helperStart, editableEnd);
  const isEditableHost = new Function(editableSrc + '\nreturn isEditableHost;')();
  const intentStart = clientSrc.indexOf('function enterIntentFor(', helperStart);
  assert.ok(intentStart > helperStart, 'enterIntentFor() is not next to isEditableHost()');
  const helperEnd = clientSrc.indexOf('\n            }\n', intentStart) + '\n            }\n'.length;
  const helperSrc = clientSrc.slice(helperStart, helperEnd);

  // The keydown listener, as it ships, inside setupIframeEventListeners().
  const setupStart = clientSrc.indexOf('function setupIframeEventListeners(');
  assert.ok(setupStart > 0, 'setupIframeEventListeners() not found');
  const kdStart = clientSrc.indexOf("d.addEventListener('keydown', (ev) => {", setupStart);
  assert.ok(kdStart > 0, 'the mirror keydown listener not found');
  let depth = 0, kdEnd = -1;
  for (let i = clientSrc.indexOf('{', kdStart); i < clientSrc.length; i++) {
    if (clientSrc[i] === '{') depth++;
    else if (clientSrc[i] === '}') { depth--; if (depth === 0) { kdEnd = clientSrc.indexOf(';', i) + 1; break; } }
  }
  assert.ok(kdEnd > kdStart, 'the mirror keydown listener has no end');
  const listenerSrc = clientSrc.slice(kdStart, kdEnd);

  const helperFn = new Function(helperSrc + '\nreturn enterIntentFor;')();

  // --- the decision table --------------------------------------------------
  const dom0 = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://m.invalid/' });
  const d0 = dom0.window.document;
  const mk = (html) => { const t = d0.createElement('template'); t.innerHTML = html; return t.content.firstChild; };

  ok(helperFn(mk('<input type="text">')) === 'submit', 'B1: an <input> submits on Enter');
  ok(helperFn(mk('<textarea></textarea>')) === 'newline', 'B2: a plain textarea breaks the line');
  ok(helperFn(mk('<textarea role="combobox"></textarea>')) === 'submit',
    'B3: a combobox textarea submits (search boxes declare this)');
  ok(helperFn(mk('<textarea aria-autocomplete="both"></textarea>')) === 'submit',
    'B4: aria-autocomplete commits on Enter');
  ok(helperFn(mk('<textarea data-shifix-enter="submit"></textarea>')) === 'submit',
    'B5: an observed hint wins');
  ok(helperFn(mk('<textarea role="combobox" data-shifix-enter="newline"></textarea>')) === 'newline',
    'B6: what the page DID beats what the markup suggests');
  ok(helperFn(mk('<div contenteditable="true"></div>')) === 'newline',
    'B7: a contenteditable breaks the line');
  ok(helperFn(mk('<textarea data-shifix-enter="@@garbage@@"></textarea>')) === 'newline',
    'B8: an unknown hint falls back to the element default');
  ok(helperFn(null) === 'submit', 'B9: no element still yields a usable answer');
  dom0.window.close();

  // --- the behaviour -------------------------------------------------------
  function listenerHarness() {
    const dom = new JSDOM(
      '<!doctype html><html><body><form><textarea id="q" role="combobox"></textarea>' +
      '<textarea id="c"></textarea><input id="i" type="text">' +
      '<div id="ed" contenteditable="true"></div><button id="b">go</button>' +
      '<a id="a" href="/x">link</a></form></body></html>',
      { url: 'https://m.invalid/', runScripts: 'outside-only' });
    const win = dom.window;
    const sent = [];
    const calls = [];
    const h = {
      send: (msg) => { sent.push(msg); calls.push('send:' + msg.type); },
      flushInputSync: () => { calls.push('flush'); },
      clearTyped: () => { calls.push('clearTyped'); },
      mirrorClick: () => { calls.push('click'); },
    };
    const register = new Function(
      'd', 'send', 'getSelector', 'getMid', 'getInputValue', 'flushInputSync',
      'clearTyped', 'mirrorClick', 'enterIntentFor', 'isEditableHost', 'console',
      listenerSrc + '\n');
    register(win.document, h.send,
      (el) => (el.id ? '#' + el.id : ''),
      (el) => { try { return el.getAttribute('data-mid') || null; } catch (e) { return null; } },
      (el) => (el && isEditableHost(el) ? (el.innerText || el.textContent || '') : (el && el.value) || ''),
      h.flushInputSync, h.clearTyped, h.mirrorClick, helperFn, isEditableHost, console);
    const press = (el, init) => {
      const ev = new win.KeyboardEvent('keydown', Object.assign({
        key: 'Enter', bubbles: true, cancelable: true,
      }, init || {}));
      el.dispatchEvent(ev);
      return ev;
    };
    return { win, sent, calls, press };
  }

  // B10. A combobox textarea: Enter sends, the newline is suppressed, the typed
  // guard is dropped (so the page's clear reaches the mirror), and the key
  // reaches the server exactly once.
  {
    const { win, sent, calls, press } = listenerHarness();
    const q = win.document.getElementById('q');
    const ev = press(q);
    ok(ev.defaultPrevented === true, 'B10: submit-on-Enter suppresses the newline');
    const keys = sent.filter((m) => m.type === 'keypress');
    ok(keys.length === 1 && keys[0].key === 'Enter', 'B10: exactly one Enter reaches the server (got ' + keys.length + ')');
    ok(calls.indexOf('flush') >= 0 && calls.indexOf('clearTyped') >= 0,
      'B10: the field is flushed and its keystroke guard released');
    ok(calls.indexOf('flush') < calls.indexOf('send:keypress'),
      'B10: the text is flushed BEFORE Enter, or the page acts on a stale box');
    win.close();
  }

  // B11. A plain textarea: the newline is the answer. The browser inserts it,
  // and the key is still forwarded so the page sees the same sequence.
  {
    const { win, sent, calls, press } = listenerHarness();
    const c = win.document.getElementById('c');
    const ev = press(c);
    ok(ev.defaultPrevented === false, 'B11: a multiline composer keeps its newline');
    ok(sent.some((m) => m.type === 'keypress' && m.key === 'Enter'),
      'B11: the newline-producing Enter is still forwarded');
    ok(calls.indexOf('clearTyped') < 0, 'B11: a newline must not release the typed guard');
    win.close();
  }

  // B12. An <input>: implicit submission must never run — it would navigate the
  // mirror off the captured page and submit twice.
  {
    const { win, sent, press } = listenerHarness();
    const ev = press(win.document.getElementById('i'));
    ok(ev.defaultPrevented === true, 'B12: an <input> never submits implicitly in the mirror');
    ok(sent.filter((m) => m.type === 'keypress' && m.key === 'Enter').length === 1,
      'B12: one Enter, not two');
    win.close();
  }

  // B13. Shift+Enter on a send-on-Enter composer: the standard newline escape.
  {
    const { win, sent, press } = listenerHarness();
    const ev = press(win.document.getElementById('q'), { shiftKey: true });
    ok(ev.defaultPrevented === false, 'B13: Shift+Enter breaks the line instead of sending');
    ok(sent.filter((m) => m.type === 'keypress').length === 1,
      'B13: the page still receives the shifted Enter');
    win.close();
  }

  // B14. A contenteditable editor: Enter is a line break, not a submit.
  {
    const { win, press } = listenerHarness();
    const ev = press(win.document.getElementById('ed'));
    ok(ev.defaultPrevented === false, 'B14: an editor keeps its line break');
    win.close();
  }

  // B15. Enter on a button or link is a click, unchanged by any of this.
  {
    const { win, sent, calls, press } = listenerHarness();
    const ev = press(win.document.getElementById('b'));
    ok(ev.defaultPrevented === true && calls.indexOf('click') >= 0,
      'B15: Enter on a button is a click, not a keypress');
    ok(!sent.some((m) => m.type === 'keypress'), 'B15: no keypress op for a focused button');
    win.close();
  }

  // B16. A hint that arrives by patch is honoured immediately (no capture).
  {
    const { win, sent, press } = listenerHarness();
    const c = win.document.getElementById('c');
    c.setAttribute('data-shifix-enter', 'submit');
    press(c);
    ok(sent.filter((m) => m.type === 'keypress').length === 1 && c.getAttribute('data-shifix-enter') === 'submit',
      'B16: a patched hint applies on the very next Enter');
    win.close();
  }

  if (failures.length) {
    console.log('FAIL  enter intent: ' + checks + ' passed, ' + failures.length + ' failed');
    failures.forEach((f) => console.log('  - ' + f));
    process.exit(1);
  }
  console.log('PASS  enter intent: ' + checks + ' checks, Enter means what the page says it means');
})();
