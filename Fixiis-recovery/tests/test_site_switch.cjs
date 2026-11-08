// node tests/test_site_switch.cjs
//
// The switch in the mini panel's header: "are the pages visitors open alive?"
//
//   ON  -> anyone with the link opens the page
//   OFF -> / and /client.html answer 404, pushed pages answer 404, and a
//          visitor's tab that reconnects is told to stop
//
// This runs the real panel in a DOM: the row must say what a visitor sees, one
// tap must send exactly one setting, a refusal from the server must put the
// switch back, and another panel flipping it must repaint this one without a
// reload. jsdom is optional, so the suite still runs with nothing but node.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.join(__dirname, '..');
let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}
if (!jsdom) {
  console.log('site switch: skipped (jsdom not installed)');
  process.exit(0);
}
const { JSDOM, VirtualConsole } = jsdom;

let checks = 0;
const ok = (cond, label) => { assert.ok(cond, label); checks++; };

const quiet = () => {
  const vc = new VirtualConsole();
  vc.on('jsdomError', () => {});
  return vc;
};

// The panel as the server would hand it: settings on, one page, no clients.
async function panel(settings, opts = {}) {
  const posted = [];
  const confirms = [];
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'mini-admin.html'), 'utf8'), {
    runScripts: 'dangerously',
    url: 'http://localhost:8080/mini-admin.html',
    pretendToBeVisual: true,
    virtualConsole: quiet(),
    beforeParse(win) {
      win.localStorage.setItem('mini_admin_token', 'test');
      win.confirm = (msg) => { confirms.push(msg); return opts.confirm !== false; };
      win.fetch = async (url, init) => {
        const u = String(url);
        const body = u.includes('/api/settings')
          ? (init && init.method === 'POST'
              ? (opts.reject
                  ? { __status: 500, error: 'nope' }
                  : { settings: Object.assign({}, settings, JSON.parse(init.body)) })
              : { settings })
          : u.includes('/api/lpv/archive') ? { pages: [] }
          : u.includes('/api/lpv/workflows') ? { workflows: [] }
          : u.includes('/api/lpv/profiles') ? { profiles: [] }
          : u.includes('/api/admin/config/telegram') ? { config: {} }
          : { success: true };
        if (init && init.method === 'POST' && u.includes('/api/settings') && !opts.reject) {
          posted.push(JSON.parse(init.body));
        }
        const status = body.__status || 200;
        return { ok: status < 400, status,
                 headers: { get: () => 'application/json' },
                 json: async () => body, text: async () => JSON.stringify(body) };
      };
      win.WebSocket = class {
        constructor() { this.readyState = 1; }
        send() {}
        close() {}
        addEventListener() {}
      };
    },
  });
  const win = dom.window;
  await new Promise((r) => setTimeout(r, 300));
  return { win, doc: win.document, posted, confirms, close: () => win.close() };
}

async function run() {
  // ---- the row exists, is visible, and says what is true right now -------
  {
    const { win, doc, close } = await panel({ public_pages_enabled: true });
    await win.loadSettings();
    const row = doc.getElementById('siteRow');
    const sw = doc.getElementById('siteSwitch');
    ok(!!row && !!sw, 'the header has a visitor-pages switch');
    ok(!row.hidden && win.getComputedStyle(row).display !== 'none',
      'it is on screen, not hidden inside a menu');
    ok(sw.classList.contains('on') && sw.getAttribute('aria-checked') === 'true',
      'it reads ON and says so to a screen reader');
    ok(doc.getElementById('siteTag').textContent === 'ON', 'the ON tag matches');
    ok(/live/i.test(doc.getElementById('siteTitle').textContent),
      'the row says the pages are live');
    ok(/404/i.test(doc.getElementById('siteSub').textContent) === false,
      'and does not talk about 404s while everything is fine');
    close();
  }

  // ---- OFF at load: the panel must not claim the pages are live ----------
  {
    const { win, doc, close } = await panel({ public_pages_enabled: false });
    await win.loadSettings();
    const row = doc.getElementById('siteRow');
    ok(row.classList.contains('off'), 'the row turns red when pages are off');
    ok(doc.getElementById('siteTag').textContent === 'OFF', 'the tag reads OFF');
    ok(/404/i.test(doc.getElementById('siteSub').textContent),
      'and says what a visitor sees: 404');
    ok(doc.getElementById('siteSwitch').getAttribute('aria-checked') === 'false',
      'the switch reads OFF');
    close();
  }

  // ---- one tap off: confirm, then exactly one setting ---------------------
  {
    const { win, doc, posted, confirms, close } = await panel({ public_pages_enabled: true });
    await win.loadSettings();
    await win.toggleSitePages();
    ok(confirms.length === 1, 'switching the site off asks first');
    ok(/404/i.test(confirms[0]), 'the question says what will happen');
    ok(posted.length === 1, 'exactly one request');
    ok(posted[0].public_pages_enabled === false, 'it sends the switch, nothing else');
    ok(Object.keys(posted[0]).length === 1, 'and nothing else at all');
    ok(doc.getElementById('siteRow').classList.contains('off'), 'the row shows OFF at once');
    ok(doc.getElementById('siteSwitch').disabled === false, 'the switch is usable again');
    close();
  }

  // ---- one tap on: no question, and it goes back to live ----------------
  {
    const { win, doc, posted, confirms, close } = await panel({ public_pages_enabled: false });
    await win.loadSettings();
    await win.toggleSitePages();
    ok(confirms.length === 0, 'switching it back on never nags');
    ok(posted.length === 1 && posted[0].public_pages_enabled === true, 'one request, switch on');
    ok(!doc.getElementById('siteRow').classList.contains('off'), 'the red row clears');
    ok(/live/i.test(doc.getElementById('siteTitle').textContent), 'and it says live again');
    close();
  }

  // ---- cancelled: nothing is sent, nothing changes ----------------------
  {
    const { win, doc, posted, close } = await panel({ public_pages_enabled: true }, { confirm: false });
    await win.loadSettings();
    await win.toggleSitePages();
    ok(posted.length === 0, 'a cancelled switch sends nothing');
    ok(!doc.getElementById('siteRow').classList.contains('off'), 'and leaves the pages on');
    close();
  }

  // ---- server refuses: the switch must not lie --------------------------
  {
    const { win, doc, close } = await panel({ public_pages_enabled: true }, { reject: true });
    await win.loadSettings();
    await win.toggleSitePages();
    ok(!doc.getElementById('siteRow').classList.contains('off'),
      'a failed change puts the row back to live');
    ok(doc.getElementById('siteSwitch').getAttribute('aria-checked') === 'true',
      'the switch does not stay off after a failure');
    close();
  }

  // ---- another panel flips it: this one repaints ------------------------
  {
    const { win, doc, close } = await panel({ public_pages_enabled: true });
    await win.loadSettings();
    win.handleAdminMsg({ type: 'server_settings_update', settings: { public_pages_enabled: false } });
    ok(doc.getElementById('siteRow').classList.contains('off'),
      'a change made elsewhere shows here without a reload');
    win.handleAdminMsg({ type: 'server_settings_update', settings: { public_pages_enabled: true } });
    ok(!doc.getElementById('siteRow').classList.contains('off'), 'and back again');
    close();
  }

  // ---- the Pages tab warns before you send something nobody can open ----
  {
    const { win, doc, close } = await panel({ public_pages_enabled: false });
    await win.loadSettings();
    win.go('pages');
    const view = doc.getElementById('view').textContent;
    ok(/visitor pages are off/i.test(view),
      'the Pages tab says a page sent now will not open');
    win.handleAdminMsg({ type: 'server_settings_update', settings: { public_pages_enabled: true } });
    ok(!/visitor pages are off/i.test(doc.getElementById('view').textContent),
      'and the warning goes away the moment the switch is back on');
    close();
  }

  // ---- unknown settings: treat as on (fail open, like the server) -------
  {
    const { win, doc, close } = await panel({});
    await win.loadSettings();
    ok(!doc.getElementById('siteRow').classList.contains('off'),
      'a settings payload without the key does not black out the panel');
    close();
  }

  console.log('site switch checks passed');
  console.log(`${checks} checks passed`);
}

run().catch((err) => { console.error(err); process.exit(1); });
