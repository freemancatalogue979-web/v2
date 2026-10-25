// The LPV tab identity, executed — not described.
//
// The head block is the first thing a visitor's tab shows, so it is tested by
// running the REAL script from client.html against a small document, not by
// grepping for it:
//
//   * a link that carries `?tab_name=…&tab_icon=…` names and pictures the tab
//     immediately (no socket, no fetch);
//   * the server's injected `__LPV_BOOT_BRANDING__` wins over the link;
//   * nothing set means nothing touched (the brand mark stays, the title stays
//     "Loading..." for the streamed-page path to own);
//   * a name without an icon does not switch the icon over, and does not claim
//     the tab's favicon.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let checks = 0;
function ok(cond, label) {
  checks++;
  if (!cond) { console.error('FAIL: ' + label); process.exit(1); }
}
function eq(actual, expected, label) {
  ok(actual === expected, label + ' (got ' + JSON.stringify(actual) + ')');
}

const source = fs.readFileSync(path.join(__dirname, '..', 'client.html'), 'utf8');
const OPEN = '<script id="lpv-preboot-branding">';
const start = source.indexOf(OPEN);
const end = source.indexOf('</script>', start);
ok(start > 0 && end > start, 'the branding block is in client.html');
const block = source.slice(start + OPEN.length, end);

// Position matters: the old copy ran before the icon link and the <title>
// existed and therefore found neither.
ok(source.indexOf('<link rel="icon" id="shf-brand-favicon"') < start,
  'the icon link exists before the block runs');
ok(source.indexOf('<title id="pageTitle">') < start,
  'and so does the title');

const BRAND = 'https://cdn.brandfetch.io/idaZJ2tIZc/theme/light/logo.svg?c=1x';

function makeDoc(search, boot) {
  const head = {
    children: [],
    appendChild(el) { el.parentNode = head; head.children.push(el); return el; },
    removeChild(el) {
      const i = head.children.indexOf(el);
      if (i >= 0) head.children.splice(i, 1);
      el.parentNode = null;
      return el;
    },
  };
  const links = [];
  const makeLink = (rel, href, id) => {
    const el = { tagName: 'LINK', rel, href, id: id || '', dataset: {}, parentNode: head };
    links.push(el);
    head.children.push(el);
    return el;
  };
  makeLink('icon', BRAND, 'shf-brand-favicon');

  const titleEl = {
    tagName: 'TITLE', id: 'pageTitle', textContent: 'Loading...', parentNode: head,
  };
  head.children.push(titleEl);
  let current = 'Loading...';

  const doc = {
    title: '',
    documentElement: { dataset: {} },
    head,
    body: { firstChild: null, insertBefore(el) { head.appendChild(el); } },
    createElement(tag) {
      return {
        tagName: String(tag).toUpperCase(), id: '', rel: '', href: '',
        textContent: '', dataset: {}, parentNode: null,
      };
    },
    getElementById(id) {
      if (id === 'pageTitle') return titleEl;
      return head.children.find((el) => el.id === id) || null;
    },
    querySelectorAll(sel) {
      if (sel.indexOf('icon') >= 0) {
        return head.children.filter((el) => el.tagName === 'LINK' && /icon/.test(el.rel));
      }
      return [];
    },
    querySelector(sel) {
      const all = doc.querySelectorAll(sel);
      return all.length ? all[0] : null;
    },
  };
  Object.defineProperty(doc, 'title', {
    configurable: true,
    get() { return current; },
    set(v) { current = String(v); titleEl.textContent = String(v); },
  });

  const win = {
    location: { search },
    __LPV_BOOT_BRANDING__: boot || undefined,
  };
  const sandbox = {
    window: win, document: doc, URLSearchParams, console,
  };
  sandbox.window.document = doc;
  vm.createContext(sandbox);
  vm.runInContext(block, sandbox);
  return { doc, win, head, links: () => doc.querySelectorAll('icon'), titleEl };
}

// ---- 1. the link alone brands the tab, instantly -------------------------
{
  const r = makeDoc('?auth=abc&url=bank.example&tab_name=Acme+Trust&tab_icon=https%3A%2F%2Fcdn.example%2Ft.png');
  eq(r.doc.title, 'Acme Trust', 'the tab is named from the link');
  eq(r.titleEl.textContent, 'Acme Trust', 'and so is the viewer header');
  const icons = r.links();
  eq(icons.length, 1, 'exactly one icon link survives');
  eq(icons[0].href, 'https://cdn.example/t.png', 'pointing at the operator icon');
  eq(icons[0].id, 'shf-lpv-brand-icon', 'as a NEW element (Chrome caches per link)');
  ok(!r.head.children.some((el) => el.id === 'shf-brand-favicon'),
    'the old brand-mark link is removed, not left to win the race');
  eq(r.doc.documentElement.dataset.workflowBranding, 'true',
    'and the mirror icon sync stands down');
  eq(r.win.__LPV_NAME_PREBOOT__, 'Acme Trust', 'the runtime is told what it must keep');
  eq(r.win.__LPV_ICON_PREBOOT__, 'https://cdn.example/t.png', 'and which icon is the tab');
}

// ---- 2. the server's boot data wins over the link -----------------------
{
  const r = makeDoc('?tab_name=From+The+Link&tab_icon=https%3A%2F%2Fcdn.example%2Flink.png', {
    tabTitle: 'From The Server',
    tabIcon: 'https://cdn.example/server.png',
    color: '#101820',
  });
  eq(r.doc.title, 'From The Server', 'the server-set tab name wins');
  eq(r.links()[0].href, 'https://cdn.example/server.png', 'and the server-set icon');
  eq(r.links()[0].dataset.lpvTabFavicon, 'true',
    'marked as the operator tab icon, so page icons never replace it');
}

// ---- 3. nothing set: nothing touched ------------------------------------
{
  const r = makeDoc('?auth=abc&url=bank.example');
  eq(r.doc.title, 'Loading...', 'the title is left for the streamed page');
  const icons = r.links();
  eq(icons.length, 1, 'the brand mark stays');
  eq(icons[0].id, 'shf-brand-favicon', 'untouched');
  eq(r.doc.documentElement.dataset.workflowBranding, undefined,
    'and the tab is still free to follow the mirrored page');
  eq(r.win.__LPV_NAME_PREBOOT__, undefined, 'the runtime has nothing to lock');
}

// ---- 4. a name without an icon does not claim the favicon ---------------
{
  const r = makeDoc('?workflow_name=Plain+Name');
  eq(r.doc.title, 'Plain Name', 'the name is applied');
  eq(r.links()[0].id, 'shf-brand-favicon', 'the icon is left alone');
  eq(r.doc.documentElement.dataset.workflowBranding, undefined,
    'and no icon claim is made');
}

// ---- 5. a workflow link (the server's own format) ----------------------
{
  const r = makeDoc('?auth=x&workflow=w1&workflow_name=Moonlight&brand_logo=https%3A%2F%2Fcdn.example%2Fm.png&brand_color=%23123456');
  eq(r.doc.title, 'Moonlight', 'a workflow link names the tab');
  eq(r.links()[0].href, 'https://cdn.example/m.png', 'and shows its logo');
  ok(r.head.children.some((el) => el.id === 'lpv-immediate-style' || el.tagName === 'STYLE'),
    'the brand colour is applied to the spinner before the first paint');
}

console.log('lpv branding boot: ' + checks + ' checks passed');
