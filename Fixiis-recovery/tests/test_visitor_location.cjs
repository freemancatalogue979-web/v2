// node tests/test_visitor_location.cjs
//
// "Where is this person?" in the phone panel must read like a place, not like
// the server's plumbing.
//
// A page you push is served from /api/lpv/archive/<id>/html — usually as a
// RELATIVE url, because the client is on the same origin. The client card used
// to print hostOf(current_url), and a relative url has no host, so the card
// showed "/api/lpv/archive/6f2a…/html": an operator watching somebody sit on
// Page One saw the API path instead of the page name and could not tell which
// page the visitor was on. The drawer printed the whole url.
//
// The rule now: a pushed page is named (Page One, Page Two), a real website is
// its hostname, and an /api/… path is never shown to the person using the
// panel. This runs the real panel code in a DOM; jsdom is optional, so the
// suite still runs with nothing but node.
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
  console.log('visitor location: skipped (jsdom not installed)');
  process.exit(0);
}
const { JSDOM, VirtualConsole } = jsdom;

let checks = 0;
const ok = (cond, label) => { assert.ok(cond, label); checks++; };

const quiet = () => {
  const vc = new VirtualConsole();
  vc.on('jsdomError', () => {});       // fonts/assets are not our concern
  return vc;
};

const PAGES = [
  { id: 'p-one', name: 'Page One.html', bindings: [] },
  { id: 'p-two', name: 'Page Two.html', bindings: [] },
];

async function panel() {
  const state = { pages: [] };
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'mini-admin.html'), 'utf8'), {
    runScripts: 'dangerously',
    url: 'http://localhost:8080/mini-admin.html',
    pretendToBeVisual: true,
    virtualConsole: quiet(),
    beforeParse(win) {
      win.localStorage.setItem('mini_admin_token', 'test');
      win.fetch = async (url) => {
        const u = String(url);
        const body = u.includes('/api/lpv/archive') ? { pages: state.pages }
          : u.includes('/api/lpv/workflows') ? { workflows: [] }
          : u.includes('/api/lpv/profiles') ? { profiles: [] }
          : u.includes('/api/lpv/audit') ? { events: [] }
          : u.includes('/api/settings') ? { settings: {} }
          : { success: true };
        return { ok: true, status: 200, headers: { get: () => 'application/json' },
                 json: async () => body, text: async () => JSON.stringify(body) };
      };
      win.WebSocket = class {
        constructor() { this.readyState = 1; }
        send() {}
        close() {}
        addEventListener() {}
      };
      win.alert = () => {};
    },
  });
  const win = dom.window;
  await new Promise((r) => setTimeout(r, 300));
  return { win, doc: win.document, state, close: () => win.close() };
}

// One client row as the server sends it (fields copied from
// _touch_lpv_admin_client: current_page_id/current_page_name plus the url it
// derives from the page id).
function client(over = {}) {
  return Object.assign({
    client_id: 'client-aaaaaaaaaaaa',
    is_lpv: true,
    lpv_online: true,
    is_online: true,
    is_sleeping: false,
  }, over);
}

function cardOf(doc) {
  const card = doc.querySelector('#clientGrid .ccard');
  assert.ok(card, 'a client card was rendered');
  return card;
}
function cardWhere(doc) {
  return cardOf(doc).querySelector('.cc-url');
}

async function run() {
  const { win, doc, state, close } = await panel();
  ok(typeof win.handleAdminMsg === 'function', 'the panel can be fed a client list');

  // ---- a pushed page with only the relative url, no name yet ------------
  state.pages = PAGES;
  await win.loadPages();                       // the archive: id -> name
  win.handleAdminMsg({ type: 'clients', clients: [client({
    current_page_id: 'p-one',
    current_url: '/api/lpv/archive/p-one/html',
  })] });
  let where = cardWhere(doc);
  ok(where.textContent.trim() === 'Page One.html',
    'relative push url shows the page NAME, got: ' + JSON.stringify(where.textContent));
  ok(!/api\/lpv/i.test(where.textContent), 'the card never prints an /api/ path');
  ok(!/api\/lpv/i.test(where.getAttribute('title') || ''),
    'not even in the hover text');
  ok(where.classList.contains('page'), 'a page name is styled as a name, not as a url');

  // ---- the absolute url the server builds when a host is configured -----
  win.handleAdminMsg({ type: 'clients', clients: [client({
    current_page_id: 'p-two',
    current_url: 'http://203.0.113.9:8446/api/lpv/archive/p-two/html?b=tok123',
  })] });
  ok(cardWhere(doc).textContent.trim() === 'Page Two.html',
    'an absolute push url (with the binding token) also shows the name');

  // ---- the name the client reported wins, even if the archive differs ---
  win.handleAdminMsg({ type: 'clients', clients: [client({
    current_page_id: 'p-two',
    current_page_name: 'Page Two',
    current_url: '/api/lpv/archive/p-two/html',
  })] });
  ok(cardWhere(doc).textContent.trim() === 'Page Two',
    'the name the page reported is used as-is');

  // ---- a page that was deleted after the push ---------------------------
  win.handleAdminMsg({ type: 'clients', clients: [client({
    current_page_id: 'gone',
    current_url: '/api/lpv/archive/gone/html',
  })] });
  where = cardWhere(doc);
  ok(where.textContent.trim() === 'Page you pushed',
    'an unknown page still reads as a page, got: ' + JSON.stringify(where.textContent));
  ok(!/api\/lpv/i.test(where.textContent + where.getAttribute('title')),
    'a deleted page does not leak its url either');

  // ---- a real website (remote browser) ----------------------------------
  win.handleAdminMsg({ type: 'clients', clients: [client({
    is_lpv: false,
    mode: 'browser',
    current_url: 'https://www.example.com/account/settings',
  })] });
  ok(cardWhere(doc).textContent.trim() === 'example.com',
    'a remote browser keeps showing the site it is on');
  win.openDrawer('client-aaaaaaaaaaaa');
  ok(doc.getElementById('drNow').textContent.trim() === 'https://www.example.com/account/settings',
    'the drawer still names the exact page of a real site');
  ok(doc.getElementById('drSub').textContent.trim() === 'example.com',
    'while the header stays short');

  // ---- a visitor who has been sent nothing yet --------------------------
  win.handleAdminMsg({ type: 'clients', clients: [client({ current_url: '' })] });
  ok(/waiting/i.test(cardWhere(doc).textContent),
    'a visitor waiting for a page is described in words');

  // ---- the drawer: header and "Currently on" ----------------------------
  win.handleAdminMsg({ type: 'clients', clients: [client({
    current_page_id: 'p-one',
    current_url: '/api/lpv/archive/p-one/html',
  })] });
  win.openDrawer('client-aaaaaaaaaaaa');
  ok(doc.getElementById('drSub').textContent.trim() === 'Page One.html',
    'the drawer header names the page');
  ok(doc.getElementById('drNow').textContent.trim() === 'Page One.html',
    '"Currently on" names the page');
  const drawerText = doc.getElementById('drawer').textContent;
  ok(!/api\/lpv/i.test(drawerText), 'no /api/ path anywhere in the drawer');

  // ---- page names that arrive AFTER the clients -------------------------
  // The card renders on the clients message and the archive may still be
  // loading; the name must fill in when it lands (no reload needed).
  state.pages = [];
  await win.loadPages();                       // the archive is momentarily empty
  win.handleAdminMsg({ type: 'clients', clients: [client({
    current_page_id: 'p-two',
    current_url: '/api/lpv/archive/p-two/html',
  })] });
  ok(cardWhere(doc).textContent.trim() === 'Page you pushed',
    'before the archive loads the card degrades to a word, not a path');
  state.pages = PAGES;
  await win.loadPages();
  ok(cardWhere(doc).textContent.trim() === 'Page Two.html',
    'the name appears when the archive arrives, without reconnecting');

  // ---- the event feed ---------------------------------------------------
  // An event can carry the page id but no name (older rows, a page whose html
  // did not report one). The feed should still say which page it was.
  win.handleAdminMsg({
    type: 'lpv_event', client_id: 'client-aaaaaaaaaaaa', event_type: 'field_final',
    page_id: 'p-one', page_name: '', payload: { value: 'x' },
  });
  const feed = doc.getElementById('feed').textContent;
  ok(feed.includes('Page One.html'), 'the live feed fills in the page name from the archive');
  ok(!/api\/lpv/i.test(feed), 'the live feed never prints an /api/ path');

  close();
  console.log('visitor location checks passed');
  console.log(`${checks} checks passed`);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
