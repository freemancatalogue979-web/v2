// No dependencies (jsdom optional): node tests/test_duplicate_mids.cjs
//
// "elements like this have problem changing" — a Google-style search box whose
// autocomplete list re-renders as the visitor types. In the mirror, rows
// appeared, vanished, and updated in the wrong place: typing produced a
// suggestion in position 1 while positions 2..n stayed blank, and dismissing
// one removed a different row.
//
// Root cause: `data-mid` was adopted from the ATTRIBUTE whenever a node had no
// entry in the page-lifetime WeakMap. `cloneNode()` copies attributes, and
// every virtualised list on the web (Google's suggestions, React keyed lists,
// agenda grids, any "reuse this template row" loop) builds its rows by cloning
// a row that the previous capture ALREADY stamped. So each clone arrived
// carrying its source's mid, several live siblings shared one id, and:
//
//   * the mirror's querySelector('[data-mid="7"]') always resolved to the
//     FIRST of them, so a patch meant for row 3 repainted row 1;
//   * the client's insert handler evicts any existing node with the incoming
//     mid, so inserting row 2 DELETED row 1 — the rows that "would not change";
//   * click dispatch by mid (handle_click) landed on the wrong element.
//
// The fix: a data-mid belongs to exactly one live element. An attribute mid is
// adopted only when no other element owns it — checked against the owner map
// and against the live tree — otherwise the clone is stamped with a fresh id.
//
// The DOM half runs the REAL delta observer from dom_capture.py inside jsdom
// and replays its ops into a miniature mirror, so the assertion is "the row
// that changed is the row that changes", not "the code contains a keyword".
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

function literal(marker) {
  const start = capture.indexOf(marker);
  assert.ok(start >= 0, marker + ' not found in dom_capture.py');
  const bodyStart = capture.indexOf('"""', start) + 3;
  const bodyEnd = capture.indexOf('"""', bodyStart);
  assert.ok(bodyEnd > bodyStart, 'unterminated literal for ' + marker);
  return capture.slice(bodyStart, bodyEnd);
}

const deltaSrc = literal('_DELTA_OBSERVER_JS = r"""');
const fastSrc = literal('_FAST_SERIALIZE_JS = r"""');

// ---------------------------------------------------------------------------
// 1. Both scripts issue ids the same way (source checks: always run)
// ---------------------------------------------------------------------------
for (const [name, src] of [['delta observer', deltaSrc], ['full serializer', fastSrc]]) {
  ok(src.includes('__domMidOwner'), name + ' keeps an owner map for its mids');
  ok(src.includes('midTaken'), name + ' asks whether a mid is already taken');
  ok(/midTaken\(el, parsed\)/.test(src),
    name + ' refuses an attribute mid that another element owns');
  ok(/setAttribute\(MID, String\(m\)\)/.test(src),
    name + ' rewrites the attribute when it has to re-stamp');
  ok(!/if \(parsed\) \{\s*const other = midOwner\(parsed\);\s*if \(!other \|\| other === el\) m = parsed;/.test(src),
    name + ' no longer adopts an attribute mid on the owner map alone');
}

// The full serializer must not carry a second, mid-issuing code path: every
// place that prints data-mid goes through midOf().
const midPrints = (fastSrc.match(/MID \+ '="/g) || []).length + (fastSrc.match(/MID, '="', String\(/g) || []).length;
const midOfCalls = (fastSrc.match(/midOf\(el\)/g) || []).length;
ok(midOfCalls >= 3, 'the serializer routes every stamp through midOf() (' + midOfCalls + ')');
ok(midPrints >= 2, 'and still prints the attribute (' + midPrints + ' sites)');

// ---------------------------------------------------------------------------
// 2. The real observer, against an autocomplete-shaped list
// ---------------------------------------------------------------------------
let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/jsd/node_modules/jsdom', '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}

if (!jsdom) {
  console.log(`${checks} checks passed (jsdom not installed: DOM half skipped).`);
  process.exit(0);
}

const { JSDOM } = jsdom;

// The captured page as it arrives: one row already stamped by a previous
// capture (data-mid 7 / 8), exactly as the real capture leaves it.
const SEED = `<!doctype html><html><head><title>Search</title></head><body>
  <form action="/search">
    <textarea id="q" name="q" data-mid="5"></textarea>
    <ul id="suggestions">
      <li data-mid="7">row <span data-mid="8">label</span></li>
    </ul>
  </form></body></html>`;

const dom = new JSDOM(SEED, { url: 'https://www.google.com/', runScripts: 'outside-only', pretendToBeVisual: true });
const win = dom.window;
const doc = win.document;

const batches = [];
win.__domDelta = (payload) => {
  try { batches.push(JSON.parse(payload)); } catch (e) { /* ignore */ }
};
win.eval(deltaSrc.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '20'));

const flatten = (from) => batches.slice(from).reduce((all, b) => all.concat(b), []);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/* A miniature mirror: the same four ops the client applies, minus the chrome.
   `i` follows client.html exactly, including its eviction of a node that
   already claims the incoming mid — that eviction is what made the bug
   visible, so the test has to keep it. */
function makeMirror(html) {
  const m = new JSDOM(html, { url: 'https://mirror.local/' });
  const md = m.window.document;
  const map = new Map();
  const reindex = () => {
    map.clear();
    md.querySelectorAll('[data-mid]').forEach((n) => {
      map.set(parseInt(n.getAttribute('data-mid'), 10), n);
    });
  };
  reindex();
  const apply = (op) => {
    const kind = op[0];
    if (kind === 'i') {
      const parent = map.get(op[1]);
      if (!parent) return 'missing-parent';
      const tpl = md.createElement('template');
      tpl.innerHTML = op[3];
      const frag = tpl.content;
      frag.querySelectorAll('[data-mid]').forEach((n) => {
        const id = parseInt(n.getAttribute('data-mid'), 10);
        const existing = map.get(id);
        if (existing && existing.parentNode && existing !== n) existing.remove();
        map.set(id, n);
      });
      // The fragment root itself can carry a mid too.
      const ref = op[2] ? map.get(op[2]) : null;
      if (ref && ref.parentNode === parent) parent.insertBefore(frag, ref);
      else parent.appendChild(frag);
      reindex();
      return 'ok';
    }
    if (kind === 'r') {
      const el = map.get(op[1]);
      if (!el) return 'missing';
      el.remove();
      reindex();
      return 'ok';
    }
    if (kind === 't') {
      const parent = map.get(op[1]);
      if (!parent) return 'missing';
      let idx = 0;
      for (const c of parent.childNodes) {
        if (c.nodeType === 3) {
          if (idx === op[2]) { c.nodeValue = op[3]; return 'ok'; }
          idx++;
        }
      }
      const t = md.createTextNode(op[3]);
      parent.appendChild(t);
      return 'ok';
    }
    if (kind === 'a') {
      const el = map.get(op[1]);
      if (!el) return 'missing';
      if (op[3] === null) el.removeAttribute(op[2]);
      else el.setAttribute(op[2], op[3]);
      return 'ok';
    }
    if (kind === 'v') {
      const el = map.get(op[1]);
      if (!el) return 'missing';
      if (el.value !== undefined) el.value = op[2];
      if (op[3] !== null && el.checked !== undefined) el.checked = !!op[3];
      return 'ok';
    }
    return 'skip';
  };
  return { doc: md, apply, map };
}

(async () => {
  await wait(80);                                  // install + navigation op

  // A full capture runs first, exactly as the server does: it stamps every
  // element and produces the document the mirror is built from.
  const serialize = win.eval('(' + fastSrc + ')');
  const mirrorHtml = serialize({ deltaIds: true });
  ok(typeof mirrorHtml === 'string' && mirrorHtml.indexOf('data-mid=') > 0,
    'the full serializer stamps the capture');
  const captureIds = (mirrorHtml.match(/data-mid="(\d+)"/g) || [])
    .map((t) => parseInt(t.replace(/\D+/g, ''), 10));
  ok(new Set(captureIds).size === captureIds.length,
    'a full capture never emits the same mid twice');

  const baseline = batches.length;

  // The page renders three suggestions the way real autocomplete does: by
  // cloning the cached row (which the capture already stamped).
  win.eval(`(() => {
    const seed = document.querySelector('#suggestions li');
    for (let i = 0; i < 3; i++) {
      const clone = seed.cloneNode(true);
      clone.lastChild.textContent = 'suggestion ' + i;
      document.getElementById('suggestions').appendChild(clone);
    }
  })()`);
  await wait(120);

  const ops = flatten(baseline);
  const inserts = ops.filter((o) => o[0] === 'i');
  ok(inserts.length === 3, 'three cloned rows produced three inserts, got ' + inserts.length);

  // --- the ids themselves ------------------------------------------------
  const rows = [...doc.querySelectorAll('#suggestions li')];
  const mids = rows.map((r) => parseInt(r.getAttribute('data-mid'), 10));
  ok(rows.length === 4, 'four rows live, got ' + rows.length);
  ok(new Set(mids).size === mids.length,
    'every live row has its own mid (got ' + mids.join(',') + ')');
  ok(mids[0] === 7, 'the original row keeps the id the mirror already knows');
  mids.slice(1).forEach((m) => ok(m !== 7, 'a clone never re-uses the source mid'));

  // No op may carry an id that belongs to a different live sibling.
  const bornWith = (html) => {
    const ids = [];
    const re = /data-mid="(\d+)"/g;
    let m;
    while ((m = re.exec(html))) {
      const id = parseInt(m[1], 10);
      if (ids.indexOf(id) < 0) ids.push(id);
    }
    return ids;
  };
  const insertedIds = inserts.flatMap((o) => bornWith(o[3]));
  ok(insertedIds.indexOf(7) === -1,
    'no insert op ships the original row\'s mid (' + insertedIds.join(',') + ')');
  ok(insertedIds.filter((id) => id === 8).length <= 1,
    'the nested <span> mid is unique per subtree');

  // --- replay into a mirror and read the result --------------------------
  const mirror = makeMirror(mirrorHtml);
  let applied = 0;
  for (const op of ops) {
    if (mirror.apply(op) === 'ok') applied++;
  }
  ok(applied >= inserts.length, 'the mirror accepts the batch (' + applied + '/' + ops.length + ')');

  const mirrored = [...mirror.doc.querySelectorAll('#suggestions li')]
    .map((r) => r.textContent.replace(/\s+/g, ' ').trim());
  ok(mirrored.length === 4,
    'the mirror still holds four rows after three inserts (got ' + mirrored.length + ': ' + mirrored.join(' | ') + ')');
  ok(mirrored[0].indexOf('label') !== -1,
    'the original row was not evicted by a clone (' + mirrored[0] + ')');
  ['suggestion 0', 'suggestion 1', 'suggestion 2'].forEach((text, i) => {
    ok(mirrored[i + 1].indexOf(text) !== -1,
      'row ' + (i + 1) + ' shows ' + text + ' (got ' + mirrored[i + 1] + ')');
  });

  // --- a patch aimed at the LAST row must hit the last row ----------------
  const before = batches.length;
  win.eval(`(() => {
    const rows = document.querySelectorAll('#suggestions li');
    rows[rows.length - 1].firstChild.nodeValue = 'row CHANGED ';
  })()`);
  await wait(120);
  const patchOps = flatten(before).filter((o) => o[0] === 't');
  ok(patchOps.length === 1, 'one text change produced one text op, got ' + patchOps.length);

  const lastMid = parseInt(rows[rows.length - 1].getAttribute('data-mid'), 10);
  ok(patchOps[0][1] === lastMid,
    'the text op addresses the row that changed (' + patchOps[0][1] + ' vs ' + lastMid + ')');
  ok(patchOps[0][1] !== 7, 'and not the first row of the list');

  const m2 = makeMirror(mirrorHtml);
  for (const op of ops.concat(patchOps)) m2.apply(op);
  const after = [...m2.doc.querySelectorAll('#suggestions li')].map((r) => r.textContent.replace(/\s+/g, ' ').trim());
  ok(after[after.length - 1].indexOf('CHANGED') !== -1,
    'the mirror repainted the last row (' + after[after.length - 1] + ')');
  ok(after[0].indexOf('label') !== -1, 'and left the first row alone');

  // --- a removal aimed at a clone must not remove the original ------------
  const beforeR = batches.length;
  win.eval(`(() => {
    const rows = document.querySelectorAll('#suggestions li');
    rows[2].remove();
  })()`);
  await wait(120);
  const removals = flatten(beforeR).filter((o) => o[0] === 'r');
  ok(removals.length === 1, 'one removal, got ' + removals.length);
  ok(removals[0][1] !== 7, 'removing a clone never names the original row\'s mid');

  const m3 = makeMirror(mirrorHtml);
  for (const op of ops.concat(removals)) m3.apply(op);
  const survived = [...m3.doc.querySelectorAll('#suggestions li')].map((r) => r.textContent.replace(/\s+/g, ' ').trim());
  ok(survived.length === 3,
    'three rows left after one removal (got ' + survived.length + ': ' + survived.join(' | ') + ')');
  ok(survived[0].indexOf('label') !== -1, 'the original row survived the removal');
  ok(survived.join('|').indexOf('suggestion 1') === -1, 'the removed row is the one that vanished');

  // --- re-stamping is idempotent -----------------------------------------
  const stable = [...doc.querySelectorAll('#suggestions li')]
    .map((r) => r.getAttribute('data-mid')).join(',');
  const again = win.eval(`(() => {
    const rows = [...document.querySelectorAll('#suggestions li')];
    return rows.map((r) => r.getAttribute('data-mid')).join(',');
  })()`);
  ok(again === stable, 'reading the ids twice gives the same ids');

  // --- an element created by the page keeps ONE id across many reads ------
  win.eval(`(() => {
    const ul = document.getElementById('suggestions');
    for (let i = 0; i < 5; i++) {
      const li = document.createElement('li');
      li.textContent = 'fresh ' + i;
      ul.appendChild(li);
    }
  })()`);
  await wait(120);
  const all = [...doc.querySelectorAll('#suggestions li')]
    .map((r) => parseInt(r.getAttribute('data-mid'), 10));
  ok(new Set(all).size === all.length,
    'a burst of brand-new rows gets unique ids (' + all.join(',') + ')');

  // --- the list is RE-RENDERED on every keystroke, as autocomplete does ---
  // innerHTML replacement = a removal batch and an insert batch for rows that
  // are clones of the same cached row. Without unique ids the mirror ends up
  // with whatever survived the eviction race instead of the current list.
  const beforeType = batches.length;
  win.eval(`(() => {
    const ul = document.getElementById('suggestions');
    const seedRow = () => {
      const li = document.createElement('li');
      li.innerHTML = '<style>.x{color:red}</style><span class="x">pick</span>';
      return li;
    };
    const cached = seedRow();
    ul.innerHTML = '';
    for (let round = 0; round < 3; round++) {
      ul.innerHTML = '';
      for (let i = 0; i < 4; i++) {
        const row = cached.cloneNode(true);
        row.lastChild.textContent = 'keystroke ' + round + '.' + i;
        ul.appendChild(row);
      }
    }
  })()`);
  await wait(140);
  const typingOps = flatten(beforeType);

  const liveRows = [...doc.querySelectorAll('#suggestions li')];
  const liveIds = liveRows.map((r) => r.getAttribute('data-mid'));
  ok(new Set(liveIds).size === liveIds.length,
    're-rendered rows keep unique ids (' + liveIds.join(',') + ')');
  ok(liveRows.every((r) => r.querySelector('style')),
    'each row kept its inline <style>');

  // Replay the whole session into one mirror: the last render must be the
  // one on screen, exactly four rows, with no leftovers from earlier rounds.
  const m4 = makeMirror(mirrorHtml);
  for (const op of typingOps) m4.apply(op);
  const typed = [...m4.doc.querySelectorAll('#suggestions li')]
    .map((r) => r.textContent.replace(/\s+/g, ' ').trim());
  ok(typed.length === 4,
    'the mirror shows the current four rows (got ' + typed.length + ': ' + typed.join(' | ') + ')');
  ok(typed.join(' ').indexOf('keystroke 0') === -1,
    'no rows from the earlier renders survived (' + typed.join(' | ') + ')');
  ok(typed.every((t) => t.indexOf('keystroke 2.') !== -1),
    'every mirrored row is from the final render (' + typed.join(' | ') + ')');
  const mirrorIds = [...m4.doc.querySelectorAll('[data-mid]')].map((n) => n.getAttribute('data-mid'));
  ok(new Set(mirrorIds).size === mirrorIds.length,
    'the mirrored document has no duplicate mids after the churn');

  // --- a row whose content changes in place must repaint THAT row ---------
  const beforeContent = batches.length;
  win.eval(`(() => {
    const rows = document.querySelectorAll('#suggestions li');
    rows[1].lastChild.textContent = 'keystroke 2.1 — highlighted';
    rows[1].setAttribute('class', 'sbhl');
  })()`);
  await wait(140);
  const contentOps = flatten(beforeContent);
  const liveNow = [...doc.querySelectorAll('#suggestions li')];
  const rowMid = parseInt(liveNow[1].getAttribute('data-mid'), 10);
  const spanMid = parseInt(liveNow[1].lastChild.getAttribute('data-mid'), 10);
  const firstSpanMid = parseInt(liveNow[0].lastChild.getAttribute('data-mid'), 10);
  const textOp = contentOps.find((o) => o[0] === 't');
  const attrOp = contentOps.find((o) => o[0] === 'a' && o[2] === 'class');
  // `span.textContent = ...` replaces a text node: the op must SET the text
  // (t) and not append a second copy (i).
  ok(textOp && textOp[1] === spanMid,
    'the text op addresses the span that changed (' + (textOp && textOp[1]) + ' vs ' + spanMid + ')');
  ok(textOp && textOp[1] !== firstSpanMid, 'and not the first row\'s span');
  ok(!contentOps.some((o) => o[0] === 'i'),
    'a textContent update ships no insert op (' + JSON.stringify(contentOps.filter((o) => o[0] === 'i')) + ')');
  ok(attrOp && attrOp[1] === rowMid,
    'the class op addresses the row itself (' + (attrOp && attrOp[1]) + ' vs ' + rowMid + ')');

  const m5 = makeMirror(mirrorHtml);
  for (const op of typingOps.concat(contentOps)) m5.apply(op);
  const painted = [...m5.doc.querySelectorAll('#suggestions li')]
    .map((r) => r.textContent.replace(/\s+/g, ' ').trim());
  ok(painted[1].indexOf('highlighted') !== -1,
    'the mirror repainted row 1 (' + painted[1] + ')');
  ok(painted[0].indexOf('keystroke 2.0') !== -1 && painted[2].indexOf('keystroke 2.2') !== -1,
    'and left its neighbours alone (' + painted.join(' | ') + ')');
  ok(m5.doc.querySelectorAll('#suggestions li.sbhl').length === 1,
    'exactly one row carries the highlighted class');

  // --- ids must not drift between captures --------------------------------
  // A mid is only useful if it means the same element next time; a re-capture
  // that renumbers the page makes every later patch land somewhere else.
  const idsBefore = [...doc.querySelectorAll('#suggestions li')]
    .map((r) => r.getAttribute('data-mid')).join(',');
  const recapture = serialize({ deltaIds: true });
  const idsAfter = [...doc.querySelectorAll('#suggestions li')]
    .map((r) => r.getAttribute('data-mid')).join(',');
  ok(idsBefore === idsAfter,
    'a re-capture keeps every id (' + idsBefore + ' -> ' + idsAfter + ')');
  const recaptured = (recapture.match(/data-mid="(\d+)"/g) || [])
    .map((t) => parseInt(t.replace(/\D+/g, ''), 10));
  ok(new Set(recaptured).size === recaptured.length,
    'and still emits each id once');

  // --- the same element keeps its id across many parses -------------------
  win.eval(`window.__touchedMid = document.querySelector('#suggestions li').getAttribute('data-mid')`);
  for (let i = 0; i < 3; i++) serialize({ deltaIds: true });
  const stableMid = win.eval(`document.querySelector('#suggestions li').getAttribute('data-mid')`);
  ok(stableMid === win.eval('window.__touchedMid'),
    'repeated captures never renumber a live element (' + stableMid + ')');

  console.log(`${checks} checks passed.`);
  dom.window.close();
})().catch((err) => {
  console.error(err && err.stack || err);
  process.exit(1);
});
