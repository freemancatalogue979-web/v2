// node tests/test_chat_bubble_ui.cjs
//
// "Turn the bubble on, say it goes to a page or a link, and pick one."
//
// That has to be true in all three places an operator can do it:
//   * Admin → Settings → Live chat bubble
//   * Admin → Live pages → Chat → Bubble settings
//   * mini panel → Pages → Advanced
//
// Each needs the real archive list, the row for the chosen target, and a save
// that carries the target. Skipped (with a message) when jsdom is missing.
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
  console.log('chat bubble UI: skipped (jsdom not installed)');
  process.exit(0);
}
const { JSDOM, VirtualConsole } = jsdom;

let checks = 0;
const ok = (cond, label) => { assert.ok(cond, label); checks++; };
const root = path.join(__dirname, '..');
const quiet = () => { const vc = new VirtualConsole(); vc.on('jsdomError', () => {}); return vc; };

const PAGES = [
  { id: 'pg1', name: 'Invoice.html', version: 2 },
  { id: 'pg2', name: 'Thank you.html', version: 1 },
];

function stub(w, { settings, posts }) {
  w.localStorage.setItem('admin_token', 'test');
  w.localStorage.setItem('mini_admin_token', 'test');
  w.WebSocket = class { constructor() { this.readyState = 1; } send() {} close() {} addEventListener() {} };
  w.fetch = async (url, opts) => {
    const u = String(url);
    if (opts && opts.method === 'POST' && u.includes('/api/settings')) {
      try { posts.push(JSON.parse(opts.body)); } catch (e) { /* ignore */ }
    }
    const body = u.includes('/api/settings') ? { settings, settings_file: 'x' }
      : u.includes('/api/lpv/archive') ? { pages: PAGES }
      : u.includes('/api/lpv/workflows') ? { workflows: [] }
      : u.includes('/api/lpv/profiles') ? { profiles: [] }
      : u.includes('/api/lpv/chat') ? { threads: [] }
      : { success: true };
    return { ok: true, status: 200, headers: { get: () => 'application/json' },
             json: async () => body, text: async () => JSON.stringify(body) };
  };
}

const S = (over) => Object.assign({
  chat_enabled: true, chat_devices: 'both', chat_mode: 'redirect',
  chat_redirect_kind: 'page', chat_redirect_page_id: 'pg2',
  chat_redirect_url: '', chat_display_name: 'Support',
  chat_bubble_color: '#7c5cff', chat_text_color: '#ffffff', chat_greeting: '',
}, over || {});

// ---------------------------------------------------------------------------
// 1) Admin → Settings tab
// ---------------------------------------------------------------------------
async function settingsTab() {
  const posts = [];
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'Admin.html'), 'utf8'), {
    runScripts: 'dangerously', url: 'http://localhost:8080/admin',
    pretendToBeVisual: true, virtualConsole: quiet(),
    beforeParse: (w) => stub(w, { settings: S(), posts }),
  });
  const w = dom.window, d = w.document;
  await new Promise((r) => setTimeout(r, 700));

  const sel = d.getElementById('settings-chat-page');
  ok(!!sel, 'Settings has an archived-page picker');
  assert.deepEqual([...sel.options].map((o) => o.value), ['', 'pg1', 'pg2']);
  checks++;
  ok([...sel.options].some((o) => /Invoice\.html/.test(o.textContent)),
     'the picker lists the archives by name');
  ok(sel.value === 'pg2', 'the page already chosen is selected');
  ok(d.getElementById('settings-chat-mode').value === 'redirect', 'mode is shown');
  ok(d.getElementById('settings-chat-page-row').style.display === '', 'the page row is visible for a page target');
  ok(d.getElementById('settings-chat-url-row').style.display === 'none', 'the link row is hidden for a page target');

  // switch to a link
  const kind = d.getElementById('settings-chat-kind');
  kind.value = 'link';
  kind.dispatchEvent(new w.Event('change'));
  ok(d.getElementById('settings-chat-url-row').style.display === '', 'choosing a link reveals the URL box');
  ok(d.getElementById('settings-chat-page-row').style.display === 'none', 'and hides the archive picker');

  // In chat mode the target rows stay usable: set it up now, switch on later.
  d.getElementById('settings-chat-mode').value = 'chat';
  d.getElementById('settings-chat-mode').dispatchEvent(new w.Event('change'));
  ok(kind.disabled === false, 'in chat mode the target kind can still be chosen');
  ok(d.getElementById('settings-chat-url-row').style.display === '',
     'and the link box stays available to set up in advance');
  d.getElementById('settings-chat-mode').value = 'redirect';
  d.getElementById('settings-chat-mode').dispatchEvent(new w.Event('change'));

  // save from this tab carries the target
  d.getElementById('settings-chat-url').value = 'https://example.com/offer';
  sel.value = 'pg1';
  d.getElementById('settings-save-btn').click();
  await new Promise((r) => setTimeout(r, 400));
  const body = posts[posts.length - 1];
  ok(body && body.chat_mode === 'redirect', 'Settings save sends the bubble mode');
  ok(body.chat_redirect_kind === 'link', 'and the target kind');
  ok(body.chat_redirect_url === 'https://example.com/offer', 'and the link');
  ok(body.chat_redirect_page_id === 'pg1', 'and the chosen page');
  w.close();
}

// ---------------------------------------------------------------------------
// 2) Admin → Live pages → Chat pane
// ---------------------------------------------------------------------------
async function chatPane() {
  const posts = [];
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'Admin.html'), 'utf8'), {
    runScripts: 'dangerously', url: 'http://localhost:8080/admin',
    pretendToBeVisual: true, virtualConsole: quiet(),
    beforeParse: (w) => stub(w, { settings: S({ chat_redirect_kind: 'link', chat_redirect_page_id: '', chat_redirect_url: 'https://example.com/x' }), posts }),
  });
  const w = dom.window, d = w.document;
  await new Promise((r) => setTimeout(r, 700));
  d.querySelector('.lpv-subtab[data-lpv-sub="chat"]').click();
  await new Promise((r) => setTimeout(r, 500));

  const sel = d.getElementById('lpv-chat-redirect-page');
  ok(!!sel, 'the Chat pane has an archived-page picker');
  assert.deepEqual([...sel.options].map((o) => o.value), ['', 'pg1', 'pg2']);
  checks++;
  ok(d.getElementById('lpv-chat-link-row').style.display === '', 'a link target shows the URL box');

  const kind = d.getElementById('lpv-chat-redirect-kind');
  kind.value = 'page';
  kind.dispatchEvent(new w.Event('change'));
  ok(d.getElementById('lpv-chat-page-row').style.display === '', 'switching to a page reveals the picker');
  sel.value = 'pg2';
  d.getElementById('lpv-chat-settings-save').click();
  await new Promise((r) => setTimeout(r, 400));
  const body = posts[posts.length - 1];
  ok(body && body.chat_redirect_kind === 'page', 'the Chat pane saves the target kind');
  ok(body.chat_redirect_page_id === 'pg2', 'and the chosen page');
  w.close();
}

// ---------------------------------------------------------------------------
// 3) A page that was deleted after being chosen is not silently dropped
// ---------------------------------------------------------------------------
function missingPage() {
  const src = fs.readFileSync(path.join(root, 'Admin.html'), 'utf8');
  const i = src.indexOf('function lpvChatFillPages(sel, selectedId) {');
  const j = src.indexOf('function lpvChatTargetPaint() {', i);
  ok(i > 0 && j > i, 'the page-picker helper is present');
  const dom = new JSDOM('<!doctype html><body><select id="s"></select></body>');
  const doc = dom.window.document;
  const ctx = vm.createContext({ document: doc, LPV_STATE: { archive: [] }, String, Array });
  vm.runInContext(src.slice(i, j), ctx);
  const sel = doc.getElementById('s');

  ctx.lpvChatFillPages(sel, '');
  ok(sel.options.length === 1 && /No pages yet/.test(sel.options[0].textContent),
     'an empty archive says so instead of showing a blank list');

  ctx.LPV_STATE.archive = PAGES;
  ctx.lpvChatFillPages(sel, 'gone-page-id');
  ok(sel.value === 'gone-page-id', 'a page that no longer exists stays selected');
  ok(/missing page/.test(sel.options[sel.options.length - 1].textContent),
     'and is labelled, so it is obvious it needs re-picking');
}

// ---------------------------------------------------------------------------
// 4) mini panel → Pages → Advanced
// ---------------------------------------------------------------------------
async function miniPanel() {
  const posts = [];
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'mini-admin.html'), 'utf8'), {
    runScripts: 'dangerously', url: 'http://localhost:8080/mini-admin.html',
    pretendToBeVisual: true, virtualConsole: quiet(),
    beforeParse: (w) => stub(w, { settings: S(), posts }),
  });
  const w = dom.window, d = w.document;
  await new Promise((r) => setTimeout(r, 400));
  await w.loadPages();
  w.go('pages');
  await new Promise((r) => setTimeout(r, 200));

  const mode = d.getElementById('stChatMode');
  const kind = d.getElementById('stChatKind');
  const page = d.getElementById('stChatPage');
  const url = d.getElementById('stChatUrl');
  ok(!!mode && !!kind && !!page && !!url, 'the mini panel offers mode, kind, page and link');
  ok(mode.value === 'redirect', 'the saved mode is shown');
  ok(page.value === 'pg2', 'the saved page is selected');
  assert.deepEqual([...page.options].map((o) => o.value), ['', 'pg1', 'pg2']);
  checks++;

  kind.value = 'link';
  url.value = 'https://example.com/mini';
  page.value = 'pg1';
  d.getElementById('stSave').click();
  await new Promise((r) => setTimeout(r, 300));
  const body = posts[posts.length - 1];
  ok(body && body.chat_mode === 'redirect', 'the mini panel saves the mode');
  ok(body.chat_redirect_kind === 'link', 'and the kind');
  ok(body.chat_redirect_url === 'https://example.com/mini', 'and the link');
  ok(body.chat_redirect_page_id === 'pg1', 'and the page');
  w.close();
}

(async () => {
  await settingsTab();
  await chatPane();
  missingPage();
  await miniPanel();
  console.log('chat bubble UI: ' + checks + ' checks passed');
})().catch((e) => { console.error(e); process.exit(1); });
