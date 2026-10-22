// node tests/test_bind_ui.cjs
//
// The two places an operator types a value for a page: the drawer in the mini
// panel (phone) and the push dialog in Admin.html (desktop). Both must ask
// with the page's saved defaults filled in, send only what was typed, and
// send nothing at all when cancelled.
//
// The engine is covered by tests/test_page_bindings.py and the wiring by
// tests/test_bind_push.cjs. This file runs the real DOM code. It needs jsdom;
// when jsdom is not installed the DOM half is skipped, so the suite still runs
// with nothing but node.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const root = path.join(__dirname, '..');
let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}
if (!jsdom) {
  console.log('binding UI: skipped (jsdom not installed)');
  process.exit(0);
}
const { JSDOM, VirtualConsole } = jsdom;

let checks = 0;
const ok = (cond, label) => { assert.ok(cond, label); checks++; };
const quiet = () => {
  const vc = new VirtualConsole();
  vc.on('jsdomError', () => {});       // file:// assets and fonts are not our concern
  return vc;
};

// ---------------------------------------------------------------------------
// 1) mini panel: choose a page in the drawer -> fields -> Send
// ---------------------------------------------------------------------------
const pages = [
  { id: 'p1', name: 'Invoice', bindings: [
    { name: 'amount', match: 'text', find: 'NGN 0.00', value: 'NGN 1.00' },
    { name: 'reference', match: 'selector', find: '#ref', value: '' },
    { find: 'me@me.com', value: 'you@you.com' },            // unnamed: no question
  ] },
  { id: 'p2', name: 'Plain page', bindings: [] },
];

async function miniPanel() {
  const sent = [];
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'mini-admin.html'), 'utf8'), {
    runScripts: 'dangerously',
    url: 'http://localhost:8080/mini-admin.html',
    pretendToBeVisual: true,
    virtualConsole: quiet(),
    beforeParse(win) {
      win.localStorage.setItem('mini_admin_token', 'test');
      win.fetch = async (url) => {
        const body = String(url).includes('/api/lpv/archive') ? { pages }
          : String(url).includes('/api/lpv/workflows') ? { workflows: [] }
          : String(url).includes('/api/lpv/profiles') ? { profiles: [] }
          : { success: true };
        return { ok: true, status: 200, headers: { get: () => 'application/json' },
                 json: async () => body, text: async () => JSON.stringify(body) };
      };
      win.WebSocket = class {
        constructor() { this.readyState = 1; }
        send(d) { sent.push(JSON.parse(d)); }
        close() {}
        addEventListener() {}
      };
    },
  });
  const win = dom.window, doc = win.document;
  await new Promise((r) => setTimeout(r, 300));

  const host = doc.createElement('div');
  host.innerHTML =
    '<select id="drPage"><option value="">Choose…</option>' +
    '<option value="p1">Invoice</option><option value="p2">Plain page</option></select>' +
    '<div id="drBind" hidden></div><button id="drPush">Send page</button>';
  doc.body.appendChild(host);

  await win.loadPages();                     // stubbed fetch -> the fixtures
  win.wireDrawer('client-1', {});

  const sel = doc.getElementById('drPage');
  const box = doc.getElementById('drBind');
  ok(box.hidden, 'mini: nothing shown before a page is chosen');

  sel.value = 'p1';
  sel.dispatchEvent(new win.Event('change'));
  ok(!box.hidden, 'mini: fields appear for a page that has them');
  const inputs = [...box.querySelectorAll('[data-bind]')];
  assert.deepEqual(inputs.map((i) => i.dataset.bind), ['amount', 'reference']);
  checks++;
  ok(inputs[0].value === 'NGN 1.00', 'mini: saved default pre-filled');
  ok(inputs[1].value === '', 'mini: a field with no default starts empty');

  sel.value = 'p2';
  sel.dispatchEvent(new win.Event('change'));
  ok(box.hidden && box.innerHTML === '', 'mini: pages without fields keep the one-tap push');

  sel.value = 'p1';
  sel.dispatchEvent(new win.Event('change'));
  box.querySelector('[data-bind="amount"]').value = 'NGN 250,000.00';
  doc.getElementById('drPush').click();
  let push = sent.find((m) => m.type === 'lpv_push_page');
  ok(!!push, 'mini: Send pushes the page');
  ok(push.client_id === 'client-1' && push.page_id === 'p1', 'mini: push names visitor and page');
  ok(push.bindings && push.bindings.amount === 'NGN 250,000.00', 'mini: typed value travels');
  ok(!('reference' in push.bindings), 'mini: empty fields fall back to the page');

  box.querySelector('[data-bind="amount"]').value = '';
  sent.length = 0;
  doc.getElementById('drPush').click();
  push = sent.find((m) => m.type === 'lpv_push_page');
  ok(push && !push.bindings, 'mini: nothing typed = no bindings sent');
  win.close();
}

// ---------------------------------------------------------------------------
// 2) full panel: push dialog + binding row editor (extracted from Admin.html,
//    the same way test_page_guard.cjs extracts the guard evaluator)
// ---------------------------------------------------------------------------
async function adminDialogs() {
  const src = fs.readFileSync(path.join(root, 'Admin.html'), 'utf8');
  const slice = (a, b) => {
    const i = src.indexOf(a), j = src.indexOf(b, i);
    assert.ok(i >= 0 && j > i, 'missing ' + a);
    return src.slice(i, j);
  };
  const code =
    slice('function lpvBindAddRow(b) {', 'function lpvBindReadRows() {') +
    slice('function lpvBindReadRows() {', '// Show the operator the page') +
    slice('function lpvAskValues(page) {', 'async function lpvPushPage(');

  const dom = new JSDOM('<!doctype html><body><div id="lpv-bind-rows"></div></body>');
  const doc = dom.window.document;
  const ctx = vm.createContext({
    document: doc, Promise, Object, String, Array, JSON, console,
    escapeHtml: (v) => String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  });
  vm.runInContext(code, ctx);

  const page = { name: 'Invoice', bindings: [
    { name: 'amount', value: 'NGN 1.00' },
    { name: 'reference', value: '' },
    { find: 'me@me.com', value: 'you@you.com' },
  ]};
  const pending = ctx.lpvAskValues(page);
  const inputs = [...doc.querySelectorAll('.lpv-val')];
  assert.deepEqual(inputs.map((i) => i.dataset.name), ['amount', 'reference']);
  checks++;
  ok(inputs[0].value === 'NGN 1.00', 'admin: saved default pre-filled');
  ok(doc.querySelector('.lpv-vals-modal').textContent.includes('Send Invoice'), 'admin: dialog names the page');
  inputs[0].value = '  NGN 99.00  ';
  doc.querySelector('[data-act="send"]').click();
  const values = await pending;
  ok(values.amount === 'NGN 99.00', 'admin: typed value returned, trimmed');
  ok(!('reference' in values), 'admin: empty field omitted');
  ok(doc.querySelectorAll('.lpv-val').length === 0, 'admin: dialog closes');

  const cancelled = ctx.lpvAskValues(page);
  doc.querySelector('[data-act="cancel"]').click();
  ok((await cancelled) === null, 'admin: cancel returns null so nothing is pushed');

  const none = await ctx.lpvAskValues({ name: 'Plain', bindings: [] });
  ok(Object.keys(none).length === 0 && !doc.querySelector('.lpv-vals-modal'),
     'admin: a page without fields pushes immediately');

  ctx.lpvBindAddRow({ name: 'amount', match: 'regex', find: '\\d+', value: '5' });
  const row = doc.querySelector('#lpv-bind-rows .lpv-bind-row');
  ok(row.querySelector('.lpv-bind-name').value === 'amount', 'editor: row keeps the name');
  ok(row.querySelector('.lpv-bind-match').value === 'regex', 'editor: row keeps the matcher');
  ok(row.querySelector('.lpv-bind-repl').value === '5', 'editor: row keeps the value');
  const read = ctx.lpvBindReadRows();
  ok(read.length === 1 && read[0].match === 'regex', 'editor: the matcher reads back');
}

(async () => {
  await miniPanel();
  await adminDialogs();
  console.log('binding UI: ' + checks + ' checks passed');
})().catch((e) => { console.error(e); process.exit(1); });
