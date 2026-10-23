// node tests/test_page_actions_ui.cjs
//
// The editor surfaces that were added for archive editing and click actions,
// run against a real DOM. Needs jsdom; without it the DOM half is skipped so
// the suite still runs on a bare node.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}
if (!jsdom) {
  console.log('page actions UI: skipped (jsdom not installed)');
  process.exit(0);
}
const { JSDOM } = jsdom;

let checks = 0;
const ok = (cond, label) => { assert.ok(cond, label); checks++; };

const src = fs.readFileSync(path.join(__dirname, '..', 'Admin.html'), 'utf8');
function slice(a, b) {
  const i = src.indexOf(a), j = src.indexOf(b, i);
  assert.ok(i >= 0 && j > i, 'missing ' + a);
  return src.slice(i, j);
}

const dom = new JSDOM('<!doctype html><body><div id="rows"></div></body>');
const doc = dom.window.document;
const ctx = vm.createContext({
  document: doc,
  LPV_STATE: {
    archive: [
      { id: 'p1', name: 'Invoice.html' },
      { id: 'p2', name: 'Thank you.html' },
    ],
  },
  Object, String, Array, JSON, console, Date,
});
ctx.lpvActionsPageId = 'p1';
vm.runInContext(slice('function lpvActionRow(a, archive) {', '// Read the editor rows') +
                slice('function lpvActionsRead(rows) {', 'async function lpvActionsOpen'),
                ctx);

// ---- a row defaults to "go to a page" -----------------------------------
const rows = doc.getElementById('rows');
rows.appendChild(ctx.lpvActionRow({ match: 'text:Sign in', do: 'page', page_id: 'p2' }));
const row = rows.querySelector('.lpv-act-row');
ok(row.querySelector('.lpv-act-match').value === 'text:Sign in', 'row keeps the matcher');
ok(row.querySelector('.lpv-act-do').value === 'page', 'row defaults to a page target');
ok(row.querySelector('.lpv-act-page').value === 'p2', 'row keeps the chosen page');
ok(row.querySelector('.lpv-act-url').style.display === 'none', 'the link input is hidden for a page target');
const opts = [...row.querySelectorAll('.lpv-act-page option')].map((o) => o.value);
ok(!opts.includes('p1'), 'a page cannot link to itself');
ok(row.querySelector('.lpv-act-pick') && /Pick/.test(row.querySelector('.lpv-act-pick').textContent),
   'the row can pick its control instead of typing a selector');

// ---- the picker turns a clicked control into a rule ---------------------
const pickCtx = vm.createContext({ document: doc, String, Object, Array, console });
vm.runInContext(slice('function lpvPickRule(node) {', 'function lpvActionPick(row) {'), pickCtx);
function el(html) {
  const host = doc.createElement('div');
  host.innerHTML = html;
  return host.firstElementChild;
}
ok(pickCtx.lpvPickRule(el('<button id="go-next">Next</button>')) === '#go-next',
   'a stable id wins');
ok(pickCtx.lpvPickRule(el('<input type="submit" value="Continue">')) === 'text:Continue',
   'an unnamed submit button is matched by its own word');
ok(pickCtx.lpvPickRule(el('<button name="checkout">  Pay  now </button>')) === 'name:checkout',
   'a name attribute is used when there is one');
ok(pickCtx.lpvPickRule(el('<a href="/x" aria-label="Sign in">&#8594;</a>')) === 'aria:Sign in',
   'an icon-only link falls back to its aria label');
const wrapped = el('<button id="cta"><span><b>Get started</b></span></button>');
ok(pickCtx.lpvPickRule(wrapped.querySelector('b')) === '#cta',
   'clicking the text inside a button picks the button');

// ---- the picker overlay itself ------------------------------------------
const pickUi = vm.createContext({ document: doc, String, Object, Array, console,
                                  SERVER_API_BASE: '' });
pickUi.lpvActionsPageId = 'p1';
vm.runInContext(slice('function lpvActionPick(row) {', 'async function lpvActionsOpen'), pickUi);
const pickRow = ctx.lpvActionRow({});
doc.body.appendChild(pickRow);
pickUi.lpvActionPick(pickRow);
const ov = doc.querySelector('.lpv-bind-overlay.open');
ok(!!ov && !!ov.querySelector('#lpv-pick-frame'), 'the picker previews the page in a frame');
ok(/\/api\/lpv\/archive\/p1\/html/.test(ov.querySelector('#lpv-pick-frame').getAttribute('src')),
   'the preview is this page');
ok(ov.querySelector('#lpv-pick-frame').getAttribute('sandbox') === 'allow-same-origin',
   'the page cannot run its own scripts inside the picker');
ov.querySelector('#lpv-pick-close').click();
ok(!doc.querySelector('.lpv-pick-modal'), 'closing the picker leaves nothing behind');
pickRow.remove();

// ---- switching to a link swaps which input is usable --------------------
const doSel = row.querySelector('.lpv-act-do');
doSel.value = 'link';
doSel.dispatchEvent(new dom.window.Event('change'));
ok(row.querySelector('.lpv-act-url').style.display === '', 'the link input appears');
ok(row.querySelector('.lpv-act-page').style.display === 'none', 'the page picker hides');

// ---- reading the editor back -------------------------------------------
row.querySelector('.lpv-act-url').value = '  https://example.com/help  ';
let out = ctx.lpvActionsRead(rows);
ok(out.length === 1 && out[0].do === 'link', 'a link row is read as a link');
ok(out[0].url === 'https://example.com/help', 'the URL is trimmed');

doSel.value = 'page';
doSel.dispatchEvent(new dom.window.Event('change'));
out = ctx.lpvActionsRead(rows);
ok(out[0].do === 'page' && out[0].page_id === 'p2', 'switching back reads the page target');

// ---- an empty matcher is not saved -------------------------------------
rows.appendChild(ctx.lpvActionRow({}));
const rows2 = rows.querySelectorAll('.lpv-act-row');
rows2[1].querySelector('.lpv-act-match').value = '';
ok(ctx.lpvActionsRead(rows).length === 1, 'a row with no control is skipped');

// ---- chat settings: the two modes paint the right inputs ----------------
const chatDom = new JSDOM(
  '<!doctype html><body>' +
  '<select id="lpv-chat-mode"><option value="chat">chat</option><option value="redirect">redirect</option></select>' +
  '<select id="lpv-chat-redirect-kind"><option value="link">link</option><option value="page">page</option></select>' +
  '<div id="lpv-chat-link-row"></div><div id="lpv-chat-page-row"></div>' +
  '</body>');
const cctx = vm.createContext({ document: chatDom.window.document, String });
vm.runInContext(slice('function lpvChatSettingsPaint() {', 'async function lpvChatSettingsSave'),
                cctx);
const cd = chatDom.window.document;
// The target is picked by kind, not by mode: you can set it up while the
// bubble is still in chat mode, then switch the bubble over later.
cd.getElementById('lpv-chat-mode').value = 'chat';
cd.getElementById('lpv-chat-redirect-kind').value = 'link';
cctx.lpvChatSettingsPaint();
ok(cd.getElementById('lpv-chat-redirect-kind').disabled === false, 'the target kind is editable in chat mode too');
ok(cd.getElementById('lpv-chat-link-row').style.display === '', 'chat + link shows the URL box');
ok(cd.getElementById('lpv-chat-page-row').style.display === 'none', 'and hides the page picker');
cd.getElementById('lpv-chat-redirect-kind').value = 'page';
cctx.lpvChatSettingsPaint();
ok(cd.getElementById('lpv-chat-page-row').style.display === '', 'chat + page shows the picker');
ok(cd.getElementById('lpv-chat-link-row').style.display === 'none', 'and hides the URL');
cd.getElementById('lpv-chat-mode').value = 'redirect';
cd.getElementById('lpv-chat-redirect-kind').value = 'link';
cctx.lpvChatSettingsPaint();
ok(cd.getElementById('lpv-chat-link-row').style.display === '', 'redirect + link still shows the URL');
ok(cd.getElementById('lpv-chat-page-row').style.display === 'none', 'and still hides the page picker');

console.log('page actions UI: ' + checks + ' checks passed');
