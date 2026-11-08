// No dependencies beyond jsdom: node tests/test_capture_convergence.cjs
//
// The mirror is maintained two ways: a FULL capture replaces the document, a
// DELTA patch updates it. For the mirror to be trustworthy those two paths must
// land on the same document. If they diverge, the mirror shows something the
// visitor's screen does not — and there is no error anywhere, just a page that
// "has problems changing".
//
// So this suite is a differential oracle, not a list of hand-written
// assertions. For each mutation shape it:
//
//   1. captures the page (full) and builds a mirror from those bytes;
//   2. performs ONE realistic mutation on the live page;
//   3. applies the delta ops the real observer emits — using the same four-op
//      semantics as client.html, eviction included;
//   4. compares the mirrored subtree with a FRESH full capture of the live
//      page. They must be identical.
//
// A scenario that differs is a bug with a name on it. Adding a scenario costs
// six lines, so the matrix can grow with every mirror bug reported.
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
const capture = fs.readFileSync(path.join(root, 'dom_capture.py'), 'utf8');

function literal(marker) {
  const start = capture.indexOf(marker);
  assert.ok(start >= 0, marker + ' not found');
  const bodyStart = capture.indexOf('"""', start) + 3;
  return capture.slice(bodyStart, capture.indexOf('"""', bodyStart));
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
// Canonical form: structure only. data-mid is identity, not content, and the
// oracle is about content.
// ---------------------------------------------------------------------------
function canonical(node) {
  const out = [];
  // Adjacent text nodes are indistinguishable in HTML — a full capture writes
  // them side by side and the re-parse merges them. So merge before comparing,
  // otherwise a correct mirror looks wrong for text the page split itself.
  let pendingText = '';
  const flushText = () => {
    const text = pendingText.replace(/\s+/g, ' ').trim();
    if (text) out.push('#text:' + text);
    pendingText = '';
  };
  const walk = (n, depth) => {
    if (depth > 40) return;
    if (n.nodeType === 3) {
      pendingText += n.nodeValue;
      return;
    }
    if (n.nodeType === 1 || n.nodeType === 8) flushText();
    if (n.nodeType === 8) return;                       // comments are cosmetic
    if (n.nodeType !== 1) return;
    const attrs = [];
    for (const a of n.attributes) {
      if (a.name === 'data-mid') continue;
      // Form state is what the visitor SEES: the mirror may carry it as a
      // property (`el.value = ...`, `el.checked = ...`) while a fresh capture
      // writes it as an attribute. Both paint the same control, so compare the
      // effective state rather than the spelling.
      if (n.localName === 'input' && (a.name === 'value' || a.name === 'checked')) continue;
      if (n.localName === 'option' && a.name === 'selected') continue;
      attrs.push(a.name + '=' + a.value);
    }
    attrs.sort();
    if (n.localName === 'input') {
      if (n.type !== 'checkbox' && n.type !== 'radio' && n.hasAttribute('value')) {
        attrs.push('value=' + String(n.value));
      }
      if (n.type === 'checkbox' || n.type === 'radio') {
        attrs.push('checked=' + (n.checked ? '1' : '0'));
      }
      attrs.sort();
    }
    if (n.localName === 'option') attrs.push('selected=' + (n.selected ? '1' : '0'));
    if (n.localName === 'textarea') {
      attrs.push('value=' + String(n.value == null ? '' : n.value));
      // Its text children ARE the default value; compare the live value only.
      flushText();
      out.push('<' + n.localName + (attrs.length ? ' ' + attrs.sort().join(' ') : '') + '>');
      for (const c of n.childNodes) if (c.nodeType === 1) walk(c, depth + 1);
      flushText();
      out.push('</' + n.localName + '>');
      return;
    }
    out.push('<' + n.localName + (attrs.length ? ' ' + attrs.join(' ') : '') + '>');
    // Declarative shadow roots live in a template's content; walk that too.
    if (n.localName === 'template' && n.content) {
      for (const c of n.content.childNodes) walk(c, depth + 1);
    }
    // An ATTACHED shadow root (what the mirror builds after an insert) and a
    // declarative <template shadowrootmode> (what a full capture writes) are
    // the same thing described two ways. Canonicalise both to the template
    // form so the comparison is about content, not representation.
    if (n.shadowRoot) {
      out.push('<template shadowrootmode=' + (n.shadowRoot.mode || 'open') + '>');
      for (const c of n.shadowRoot.childNodes) walk(c, depth + 1);
      flushText();
      out.push('</template>');
    }
    for (const c of n.childNodes) walk(c, depth + 1);
    flushText();                       // text belongs INSIDE its element
    out.push('</' + n.localName + '>');
  };
  walk(node, 0);
  flushText();
  return out.join('\n');
}

// ---------------------------------------------------------------------------
// Mini mirror: the four ops client.html applies, eviction included.
// ---------------------------------------------------------------------------
function makeMirror(html) {
  const m = new JSDOM(html, { url: 'https://mirror.invalid/' });
  const md = m.window.document;
  let map = new Map();
  // Mirrors client.html's eachMidDeep(): the id map must cross shadow
  // boundaries, because that is where a component's nodes live.
  const reindex = () => {
    map = new Map();
    const visit = (scope, depth) => {
      if (!scope || depth > 8) return;
      scope.querySelectorAll('[data-mid]').forEach((n) => {
        map.set(parseInt(n.getAttribute('data-mid'), 10), n);
      });
      scope.querySelectorAll('*').forEach((el) => {
        if (el.shadowRoot) visit(el.shadowRoot, depth + 1);
      });
    };
    visit(md, 0);
  };
  reindex();
  const apply = (op) => {
    const kind = op[0];
    if (kind === 'i') {
      let parent = map.get(op[1]);
      if (parent && op[4] === 's') parent = parent.shadowRoot || parent;
      if (!parent) return false;
      const tpl = md.createElement('template');
      tpl.innerHTML = op[3];
      const frag = tpl.content;
      // innerHTML does not attach declarative shadow DOM; the client does it
      // explicitly, so the mirror does too.
      frag.querySelectorAll('template[shadowrootmode]').forEach((t) => {
        const host = t.parentNode;
        if (!host || host.shadowRoot) return;
        try {
          const sr = host.attachShadow({ mode: 'open' });
          sr.appendChild(t.content.cloneNode(true));
          t.remove();
        } catch (e) { /* ignore */ }
      });
      const visit = (scope, depth) => {
        if (!scope || depth > 8) return;
        scope.querySelectorAll('[data-mid]').forEach((n) => {
          const id = parseInt(n.getAttribute('data-mid'), 10);
          const existing = map.get(id);
          if (existing && existing.parentNode && existing !== n) existing.remove();
          map.set(id, n);
        });
        scope.querySelectorAll('*').forEach((el) => {
          if (el.shadowRoot) visit(el.shadowRoot, depth + 1);
        });
      };
      visit(frag, 0);
      const ref = op[2] ? map.get(op[2]) : null;
      if (ref && ref.parentNode === parent) parent.insertBefore(frag, ref);
      else parent.appendChild(frag);
      reindex();
      return true;
    }
    if (kind === 'r') {
      const el = map.get(op[1]);
      if (!el) return false;
      el.remove();
      reindex();
      return true;
    }
    if (kind === 't') {
      let parent = map.get(op[1]);
      if (parent && op[4] === 's') parent = parent.shadowRoot || parent;
      if (!parent) return false;
      let i = 0;
      for (const c of parent.childNodes) {
        if (c.nodeType === 3) {
          if (i === op[2]) { c.nodeValue = op[3]; return true; }
          i++;
        }
      }
      // The client appends when the index is past the end; mirror that.
      parent.appendChild(md.createTextNode(op[3]));
      return true;
    }
    if (kind === 'a') {
      const el = map.get(op[1]);
      if (!el) return false;
      if (op[3] === null) el.removeAttribute(op[2]);
      else el.setAttribute(op[2], op[3]);
      if (op[2] === 'selected' && el.tagName === 'OPTION') {
        try { el.selected = op[3] !== null; } catch (e) { /* ignore */ }
      }
      return true;
    }
    if (kind === 'v') {
      const el = map.get(op[1]);
      if (!el) return false;
      try { el.value = op[2]; } catch (e) { /* ignore */ }
      if (op[3] !== null) { try { el.checked = !!op[3]; } catch (e) {} }
      return true;
    }
    return kind === 's' || kind === 'meta' || kind === 'navigation' || kind === 'overflow';
  };
  return { doc: md, apply };
}

// ---------------------------------------------------------------------------
// One scenario = one fresh page, so a failure names exactly one cause.
// ---------------------------------------------------------------------------
async function scenario(name, bodyHtml, mutate) {
  const dom = new JSDOM(
    `<!doctype html><html><head><title>t</title></head><body><div id="root">${bodyHtml}</div></body></html>`,
    { url: 'https://example.test/', runScripts: 'outside-only', pretendToBeVisual: true });
  const win = dom.window;
  const doc = win.document;
  const batches = [];
  win.__domDelta = (payload) => {
    try { batches.push(JSON.parse(payload)); } catch (e) { /* ignore */ }
  };
  const serialize = win.eval('(' + fastSrc + ')');
  const first = serialize({ deltaIds: true });
  win.eval(deltaSrc.replace(/__SHIFIX_DELTA_FLUSH_MS__/g, '15'));
  await new Promise((r) => setTimeout(r, 60));

  const mark = batches.length;
  try {
    win.eval('(() => {' + mutate + '})()');
  } catch (e) {
    failures.push(name + ': the scenario itself threw — ' + e.message);
    win.close();
    return;
  }
  await new Promise((r) => setTimeout(r, 90));

  const mirror = makeMirror(first);
  const ops = batches.slice(mark).reduce((a, b) => a.concat(b), []);
  let rejected = 0;
  for (const op of ops) if (!mirror.apply(op)) rejected++;

  const wantRoot = mirror.doc.getElementById('root');
  const fresh = new JSDOM(serialize({ deltaIds: true }), { url: 'https://mirror.invalid/' });
  const freshRoot = fresh.window.document.getElementById('root');
  ok(!!wantRoot && !!freshRoot, name + ': the page lost its #root');
  if (!wantRoot || !freshRoot) { win.close(); fresh.window.close(); return; }

  const got = canonical(wantRoot);
  const want = canonical(freshRoot);
  ok(rejected === 0, name + ': ' + rejected + ' delta op(s) could not be applied');
  ok(got === want, name + ': the mirror differs from a fresh capture\n' +
     '      delta path: ' + got.replace(/\n/g, ' ').slice(0, 300) + '\n' +
     '      full capture: ' + want.replace(/\n/g, ' ').slice(0, 300));
  win.close();
  fresh.window.close();
}

(async () => {
  // --- structural edits ---------------------------------------------------
  await scenario('append element',
    '<p class="a">one</p>',
    `const p = document.createElement('p'); p.className = 'b'; p.textContent = 'two';
     document.getElementById('root').appendChild(p);`);

  await scenario('remove element',
    '<p class="a">one</p><p class="b">two</p>',
    `document.querySelector('#root .a').remove();`);

  await scenario('replace element',
    '<p class="a">one</p>',
    `const old = document.querySelector('#root .a');
     const next = document.createElement('span'); next.textContent = 'new';
     old.parentNode.replaceChild(next, old);`);

  await scenario('insert before',
    '<p class="a">one</p>',
    `const first = document.querySelector('#root .a');
     const p = document.createElement('p'); p.textContent = 'zero';
     first.parentNode.insertBefore(p, first);`);

  await scenario('insertAdjacentHTML',
    '<ul id="l"><li>a</li></ul>',
    `document.getElementById('l').insertAdjacentHTML('beforeend', '<li>b</li><li>c</li>');`);

  await scenario('documentFragment',
    '<ul id="l"><li>a</li></ul>',
    `const f = document.createDocumentFragment();
     for (let i = 0; i < 3; i++) { const li = document.createElement('li'); li.textContent = 'f' + i; f.appendChild(li); }
     document.getElementById('l').appendChild(f);`);

  await scenario('move an existing node',
    '<div id="a"><p id="x">moved</p></div><div id="b"></div>',
    `document.getElementById('b').appendChild(document.getElementById('x'));`);

  await scenario('nested rebuild (innerHTML)',
    '<ul id="l"><li><b>a</b></li><li><b>b</b></li></ul>',
    `document.getElementById('l').innerHTML = '<li><b>c</b></li><li><b>d</b></li><li><b>e</b></li>';`);

  // --- text ---------------------------------------------------------------
  await scenario('textContent replace',
    '<p id="t">old label</p>',
    `document.getElementById('t').textContent = 'new label';`);

  await scenario('textContent of a child span',
    '<p id="t"><span id="s">old</span> tail</p>',
    `document.getElementById('s').textContent = 'new';`);

  await scenario('text appended in place',
    '<p id="t">count: </p>',
    `document.getElementById('t').firstChild.nodeValue = 'count: 5';`);

  await scenario('adjacent text nodes merged',
    '<p id="t"></p>',
    `const t = document.getElementById('t');
     t.appendChild(document.createTextNode('alpha '));
     t.appendChild(document.createTextNode('beta'));
     setTimeout(() => { t.normalize(); }, 5);`);

  await scenario('label where text becomes markup',
    '<p id="t">plain</p>',
    `document.getElementById('t').innerHTML = 'bold <b>word</b> here';`);

  await scenario('markup becomes plain text',
    '<p id="t">a <b>bold</b> c</p>',
    `document.getElementById('t').textContent = 'a bold c';`);

  // --- attributes ---------------------------------------------------------
  await scenario('class toggle',
    '<div id="d" class="on">x</div>',
    `document.getElementById('d').classList.toggle('on');`);

  await scenario('attribute added then removed',
    '<button id="b">go</button>',
    `const b = document.getElementById('b');
     b.setAttribute('disabled', '');
     setTimeout(() => b.removeAttribute('disabled'), 5);`);

  await scenario('inline style changes',
    '<div id="d">x</div>',
    `const d = document.getElementById('d');
     d.style.backgroundColor = 'red';
     setTimeout(() => { d.style.display = 'none'; }, 5);`);

  await scenario('data attribute with quotes',
    '<div id="d">x</div>',
    `document.getElementById('d').setAttribute('data-label', 'a "quoted" & <tagged> value');`);

  await scenario('svg attribute',
    '<svg id="s" viewBox="0 0 10 10"><circle id="c" cx="5" cy="5" r="4"></circle></svg>',
    `document.getElementById('c').setAttribute('r', '2');`);

  // --- forms --------------------------------------------------------------
  await scenario('option list rebuilt',
    '<select id="sel"><option value="1">One</option></select>',
    `const s = document.getElementById('sel');
     s.innerHTML = '<option value="2">Two</option><option value="3" selected>Three</option>';`);

  await scenario('checkbox toggled by property',
    '<input type="checkbox" id="cb">',
    `const cb = document.getElementById('cb');
     cb.checked = true;
     cb.dispatchEvent(new Event('change', { bubbles: true }));`);

  await scenario('input value by property + event',
    '<input type="text" id="i">',
    `const i = document.getElementById('i');
     i.value = 'typed text';
     i.dispatchEvent(new Event('input', { bubbles: true }));`);

  // --- web components -----------------------------------------------------
  await scenario('inserted element with a shadow root',
    '<div id="host"></div>',
    `const host = document.getElementById('host');
     const el = document.createElement('div');
     el.id = 'wc';
     const sr = el.attachShadow({ mode: 'open' });
     sr.innerHTML = '<style>.x{color:red}</style><span class="x">inside shadow</span>';
     host.appendChild(el);`);

  await scenario('declarative shadow root already present, then updated',
    '<div id="host"><div id="wc"><template shadowrootmode="open"><span>initial</span></template></div></div>',
    `document.querySelector('#wc template').content.querySelector('span').textContent = 'changed';`);

  // --- tables, svg, media -------------------------------------------------
  await scenario('table row appended',
    '<table><tbody id="tb"><tr><td>1</td></tr></tbody></table>',
    `const tr = document.createElement('tr');
     tr.innerHTML = '<td>2</td>';
     document.getElementById('tb').appendChild(tr);`);

  await scenario('table cell text updated',
    '<table><tbody id="tb"><tr id="r"><td id="cell">1</td></tr></tbody></table>',
    `document.getElementById('cell').textContent = 'updated';`);

  await scenario('image swapped by src',
    '<img id="im" src="/a.png" alt="a">',
    `document.getElementById('im').src = 'https://cdn.example.test/b.png';`);

  await scenario('element with background image',
    '<div id="d">x</div>',
    `document.getElementById('d').style.backgroundImage = 'url(/bg.png)';`);

  await scenario('deep nesting inserted',
    '<div id="root2"></div>',
    `let node = document.getElementById('root2');
     for (let i = 0; i < 6; i++) {
       const d = document.createElement('div');
       d.className = 'd' + i;
       node.appendChild(d);
       node = d;
     }
     node.textContent = 'bottom';`);

  // --- separate batches must still see every later change -----------------
  // The shadowing rule above suppresses ops for descendants of a subtree that
  // was inserted in the SAME batch. A child added after that batch has shipped
  // must still arrive.
  await scenario('node added, then a child added in a later batch',
    '<div id="host"></div>',
    `const host = document.getElementById('host');
     const d = document.createElement('div');
     d.id = 'first';
     host.appendChild(d);
     setTimeout(() => {
       const inner = document.createElement('span');
       inner.textContent = 'later child';
       d.appendChild(inner);
     }, 40);`);

  await scenario('two siblings appended in one tick',
    '<ul id="l"></ul>',
    `const l = document.getElementById('l');
     for (let i = 0; i < 4; i++) { const li = document.createElement('li'); li.textContent = 'i' + i; l.appendChild(li); }`);

  await scenario('insert then immediately style the same node',
    '<div id="host"></div>',
    `const d = document.createElement('div');
     d.textContent = 'made';
     document.getElementById('host').appendChild(d);
     d.style.color = 'red';
     d.className = 'styled';`);

  // --- harder shapes ------------------------------------------------------
  await scenario('details opened by property',
    '<details id="d"><summary>more</summary><p>hidden text</p></details>',
    `document.getElementById('d').open = true;`);

  await scenario('contenteditable typed into',
    '<div id="ed" contenteditable="true">start</div>',
    `const ed = document.getElementById('ed');
     ed.firstChild.nodeValue = 'start typed';
     ed.appendChild(document.createTextNode(' more'));`);

  await scenario('custom element upgraded with a shadow root',
    '<div id="host"></div>',
    `class XCard extends HTMLElement {
       constructor() { super(); const r = this.attachShadow({mode:'open'});
         r.innerHTML = '<b>card</b>'; }
     }
     customElements.define('x-card', XCard);
     const c = document.createElement('x-card');
     document.getElementById('host').appendChild(c);`);

  await scenario('nested shadow root content replaced',
    '<div id="host"><div id="wc"><template shadowrootmode="open"><p id="in">one</p></template></div></div>',
    `const tpl = document.querySelector('#wc template');
     tpl.content.getElementById('in').textContent = 'two';`);

  await scenario('list item moved to another list',
    '<ul id="a"><li id="x">moved</li><li>stay</li></ul><ul id="b"></ul>',
    `document.getElementById('b').appendChild(document.getElementById('x'));`);

  await scenario('sibling order reversed by re-insertion',
    '<div id="p"><span id="s1">1</span><span id="s2">2</span></div>',
    `const p = document.getElementById('p');
     p.insertBefore(document.getElementById('s2'), document.getElementById('s1'));`);

  await scenario('deep text replacement inside a table',
    '<table><tbody><tr id="r"><td id="c">old</td></tr></tbody></table>',
    `document.getElementById('c').innerHTML = 'new <b>bold</b>';`);

  await scenario('many rapid toggles in one batch',
    '<div id="t">x</div>',
    `const t = document.getElementById('t');
     for (let i = 0; i < 12; i++) { t.textContent = 'step ' + i; }
     t.classList.add('done');`);

  await scenario('form field replaced by a new input',
    '<form><input id="f" name="q" value="a"></form>',
    `const old = document.getElementById('f');
     const n = document.createElement('input');
     n.id = 'f'; n.name = 'q'; n.value = 'b';
     old.parentNode.replaceChild(n, old);`);

  await scenario('link stylesheet swapped',
    '<div class="k">x</div>',
    `const l = document.createElement('link');
     l.rel = 'stylesheet'; l.href = 'https://cdn.example.test/s.css';
     document.head.appendChild(l);`);

  await scenario('script-inserted element with text and class',
    '<div id="ads"></div>',
    `const d = document.createElement('div');
     d.className = 'ad-slot';
     d.innerHTML = '<span>ad</span>';
     document.getElementById('ads').appendChild(d);
     d.setAttribute('data-loaded', '1');`);

  // --- live updates inside a shadow root ---------------------------------
  // The root is not part of the document subtree, so it needs its own
  // observer; and its nodes are invisible to document.querySelectorAll, so the
  // mirror's id map has to walk into it. Both halves are exercised here.
  await scenario('text changes inside an already-attached shadow root',
    '<div id="host"></div>',
    `const host = document.getElementById('host');
     const wc = document.createElement('div');
     const sr = wc.attachShadow({ mode: 'open' });
     sr.innerHTML = '<span id="lbl">first</span>';
     host.appendChild(wc);
     setTimeout(() => {
       sr.getElementById('lbl').textContent = 'second';
     }, 50);`);

  await scenario('value set by property, no event',
    '<input type="text" id="i" value="before">',
    `document.getElementById('i').value = 'set silently';`);

  await scenario('checkbox set by property, no event',
    '<input type="checkbox" id="cb">',
    `document.getElementById('cb').checked = true;`);

  await scenario('field cleared after submit',
    '<form><input type="text" id="i" value="typed"><textarea id="t">typed</textarea></form>',
    `document.getElementById('i').value = '';
     document.getElementById('t').value = '';`);

  await scenario('option selected by property',
    '<select id="s"><option id="o1">1</option><option id="o2">2</option></select>',
    `document.getElementById('o2').selected = true;`);

  await scenario('select chosen by selectedIndex',
    '<select id="s"><option id="o1">1</option><option id="o2">2</option></select>',
    `document.getElementById('s').selectedIndex = 1;`);

  await scenario('form reset by script',
    '<form id="f"><input type="text" id="i" value="default"><input type="checkbox" id="cb" checked></form>',
    `const i = document.getElementById('i'), cb = document.getElementById('cb');
     i.value = 'user typed'; cb.checked = false;
     setTimeout(() => { i.value = ''; cb.checked = true; }, 40);`);

  await scenario('toggleAttribute on and off',
    '<div id="d" title="x">body</div>',
    `const d = document.getElementById('d');
     d.toggleAttribute('hidden'); d.toggleAttribute('title');`);

  await scenario('clone of a captured subtree inserted',
    '<div id="src"><p class="c">copy me</p></div>',
    `const clone = document.getElementById('src').cloneNode(true);
     clone.id = 'dst';
     document.getElementById('root').appendChild(clone);`);

  await scenario('replaceWith mid-batch',
    '<ul id="l"><li id="one">1</li><li id="two">2</li></ul>',
    `const two = document.getElementById('two');
     const li = document.createElement('li'); li.textContent = '3';
     two.replaceWith(li);`);

  await scenario('text node data assigned',
    '<p id="p">plain <b>bold</b></p>',
    `document.getElementById('p').firstChild.data = 'changed ';`);

  await scenario('text split then normalised',
    '<p id="p">splittable text</p>',
    `const p = document.getElementById('p');
     p.firstChild.splitText(6);
     setTimeout(() => p.normalize(), 40);`);

  await scenario('classList.replace',
    '<div id="d" class="old keep">x</div>',
    `document.getElementById('d').classList.replace('old', 'new');`);

  await scenario('range value by property',
    '<form><input type="range" id="r" min="0" max="100" value="10"></form>',
    `document.getElementById('r').value = '70';`);

  await scenario('value set before insertion',
    '<div id="host"></div>',
    `const i = document.createElement('input');
     i.type = 'text'; i.id = 'n'; i.value = 'prefilled';
     document.getElementById('host').appendChild(i);`);

  await scenario('textarea default text replaced',
    '<textarea id="t">old default</textarea>',
    `document.getElementById('t').textContent = 'new default';`);

  await scenario('attribute changed on a shadow host',
    '<div id="host"></div>',
    `const host = document.getElementById('host');
     const wc = document.createElement('div');
     wc.attachShadow({ mode: 'open' }).innerHTML = '<span>slot</span>';
     host.appendChild(wc);
     setTimeout(() => { wc.setAttribute('data-state', 'ready'); }, 40);`);

  await scenario('subtree inserted into a shadow root',
    '<div id="host"></div>',
    `const host = document.getElementById('host');
     const wc = document.createElement('div');
     const sr = wc.attachShadow({ mode: 'open' });
     sr.innerHTML = '<p>first</p>';
     host.appendChild(wc);
     setTimeout(() => {
       const li = document.createElement('p'); li.textContent = 'second';
       sr.appendChild(li);
     }, 50);`);

  await scenario('progress value by property',
    '<progress id="p" value="10" max="100"></progress>',
    `document.getElementById('p').value = 65;`);

  await scenario('duplicate id inserted next to the original',
    '<div id="dup">original</div>',
    `const d = document.createElement('div'); d.id = 'dup'; d.textContent = 'copy';
     document.getElementById('root').appendChild(d);`);

  await scenario('invalid number input clears itself',
    '<input type="number" id="n" value="5">',
    `document.getElementById('n').value = 'not a number';`);

  await scenario('bare text inside a shadow root updated',
    '<div id="host"></div>',
    `const host = document.getElementById('host');
     const wc = document.createElement('div');
     const sr = wc.attachShadow({ mode: 'open' });
     sr.appendChild(document.createTextNode('first'));
     host.appendChild(wc);
     setTimeout(() => { sr.firstChild.data = 'second'; }, 50);`);

  await scenario('element removed from a shadow root',
    '<div id="host"></div>',
    `const host = document.getElementById('host');
     const wc = document.createElement('div');
     const sr = wc.attachShadow({ mode: 'open' });
     sr.innerHTML = '<p id="keep">keep</p><p id="drop">drop</p>';
     host.appendChild(wc);
     setTimeout(() => { sr.getElementById('drop').remove(); }, 50);`);

  await scenario('select chosen by value property',
    '<select id="s"><option id="o1" value="a">A</option><option id="o2" value="b">B</option></select>',
    `document.getElementById('s').value = 'b';`);

  await scenario('radio group by property',
    '<form><input type="radio" name="g" id="r1" checked><input type="radio" name="g" id="r2"></form>',
    `const r2 = document.getElementById('r2');
     r2.checked = true;`);

  await scenario('component mounting inside a shadow root',
    '<div id="host"></div>',
    `const host = document.getElementById('host');
     const outer = document.createElement('div');
     const sr = outer.attachShadow({ mode: 'open' });
     sr.innerHTML = '<div id="inner"></div>';
     host.appendChild(outer);
     setTimeout(() => {
       const leaf = document.createElement('span');
       leaf.textContent = 'leaf';
       const sr2 = leaf.attachShadow({ mode: 'open' });
       sr2.innerHTML = '<b>deep</b>';
       sr.getElementById('inner').appendChild(leaf);
     }, 50);`);

  // --- report -------------------------------------------------------------
  // --- the mirror side: inserted shadow roots must actually attach --------
  //
  // The document itself is parsed by the browser (blob: iframe), so a full
  // capture's declarative shadow DOM attaches natively. An inserted subtree is
  // built with innerHTML, which does NOT attach it — the content would sit in
  // an inert <template> and the component would render as an empty box.
  {
    const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');
    const start = client.indexOf('function attachShadowRoots(');
    ok(start > 0, 'client.html carries attachShadowRoots()');
    if (start > 0) {
      const end = client.indexOf('\n}', start) + 2;
      const fnSrc = client.slice(start, end);
      const insertAt = client.indexOf("kind === 'i'");
      ok(insertAt > 0 && client.indexOf('attachShadowRoots(', insertAt) > insertAt &&
         client.indexOf('attachShadowRoots(', insertAt) < client.indexOf("kind === 'r'", insertAt),
        'the insert path attaches shadow roots before indexing the fragment');

      const dom = new JSDOM(
        '<!doctype html><html><body><div id="host"></div></body></html>',
        { url: 'https://mirror.invalid/' });
      const win = dom.window;
      win.eval('(function(){ ' + fnSrc + '\nreturn attachShadowRoots; })()') && (win.attachShadowRoots = win.eval(fnSrc + '\nattachShadowRoots'));
      const host = win.document.getElementById('host');
      const tpl = win.document.createElement('template');
      tpl.innerHTML = '<div id="wc"><template shadowrootmode="open">' +
        '<style>.x{color:red}</style><span class="x">inside shadow</span></template>' +
        '<span>light</span></div>';
      host.appendChild(tpl.content);
      const attached = win.attachShadowRoots(host);
      const wc = win.document.getElementById('wc');
      ok(attached === 1, 'one shadow root attached (got ' + attached + ')');
      ok(wc && wc.shadowRoot, 'the host has a shadow root after the insert');
      ok(wc && wc.shadowRoot && /inside shadow/.test(wc.shadowRoot.textContent),
        'the shadow content is live, not stuck in a template');
      ok(wc && !wc.querySelector('template[shadowrootmode]'),
        'the inert template is gone');
      ok(wc && /light/.test(wc.textContent), 'light-DOM content survives');
      ok(win.attachShadowRoots(host) === 0, 'a second pass is a no-op');
      win.close();
    }
  }

  if (failures.length) {
    console.log('FAIL  capture convergence: ' + checks + ' passed, ' + failures.length + ' failed');
    failures.forEach((f) => console.log('  - ' + f));
    process.exit(1);
  }
  console.log('PASS  capture convergence: ' + checks + ' checks, delta path == full capture');
})();
