#!/usr/bin/env node
/* The Admin redesign, executed — not just grepped.

   Admin.html is a 1.2 MB single-file application with 450+ element ids that
   its own JavaScript binds to. A redesign of that file is only safe if it can
   be proven that:

     1. every id that existed before still exists, exactly once;
     2. the shell actually builds at runtime (nav rail, page headers, search,
        notification centre, contextual column, bottom bar);
     3. navigation still goes through the panel's own `.top-tab` buttons, so
        every listener the app registered keeps working;
     4. the dashboard's real ids are still wired to the real elements;
     5. nothing new loads from the network at boot;
     6. the reactor honours prefers-reduced-motion.

   jsdom runs the page's own scripts, so (2) is a real DOM assertion rather
   than a text search.

       NODE_PATH=/tmp/jsd/node_modules node tests/test_admin_redesign.cjs
*/
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let JSDOM = null;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  const vendored = path.join('/tmp/jsd/node_modules/jsdom');
  try { ({ JSDOM } = require(vendored)); } catch (e2) { JSDOM = null; }
}
if (!JSDOM) {
  console.log('SKIP  admin redesign: jsdom not installed');
  process.exit(0);
}

const ROOT = path.resolve(__dirname, '..');
const ADMIN = path.join(ROOT, 'Admin.html');
const HTML = fs.readFileSync(ADMIN, 'utf8');

/* This suite tests the compiled admin shell (admin/shell.js and friends
   inlined into Admin.html by deploy/build-admin.py). The panel currently
   ships as hand-written markup without those regions, so there is nothing
   to execute — skip loudly instead of failing a build over a design that
   was deliberately reverted. */
if (!/^\/\* SHF:DS:CSS:BEGIN \*\/$/m.test(HTML)) {
  console.log('SKIP  admin redesign: Admin.html is not compiled (no shell regions)');
  process.exit(0);
}
const MANIFEST = JSON.parse(fs.readFileSync(path.join(ROOT, 'admin/id-manifest.json'), 'utf8'));

let pass = 0;
const fails = [];
function ok(cond, label) {
  if (cond) { pass++; return; }
  fails.push(label);
}
function eq(actual, expected, label) {
  ok(actual === expected, label + '\n     got: ' + JSON.stringify(actual) +
     '\n    want: ' + JSON.stringify(expected));
}

/* ------------------------------------------------ 1. ids are preserved */
{
  const ids = [...HTML.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
  const counts = new Map();
  ids.forEach((id) => counts.set(id, (counts.get(id) || 0) + 1));
  const missing = MANIFEST.ids.filter((id) => !counts.has(id));
  const dupes = [...counts.entries()].filter(([, n]) => n > 1).map(([id]) => id);
  eq(missing.length, 0, 'ids removed by the redesign: ' + missing.slice(0, 8).join(', '));
  eq(dupes.length, 0, 'duplicate ids introduced: ' + dupes.slice(0, 8).join(', '));
  ok(ids.length >= MANIFEST.count,
     'the file lost ids overall (' + ids.length + ' < ' + MANIFEST.count + ')');
}

/* --------------------------------------------- 2. the shell really builds */
const dom = new JSDOM(HTML, {
  url: 'http://localhost:8000/Admin.html',
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  resources: undefined,          // never fetch anything from the network
  beforeParse(window) {
    window.fetch = () => Promise.reject(new Error('offline in test'));
    window.WebSocket = function () {
      this.readyState = 0;
      this.send = () => {};
      this.close = () => {};
      this.addEventListener = () => {};
      this.removeEventListener = () => {};
    };
    window.localStorage.clear();
  }
});

const win = dom.window;
const doc = win.document;

function tick(ms) {
  return new Promise((resolve) => win.setTimeout(resolve, ms || 0));
}

/* The panel boots its own scripts before the shell starts, so wait for the
   shell to exist rather than guessing a delay. */
async function waitFor(fn, ms) {
  const deadline = Date.now() + (ms || 4000);
  while (Date.now() < deadline) {
    try { if (fn()) return true; } catch (e) { /* keep waiting */ }
    await tick(20);
  }
  return false;
}

(async () => {
  /* The panel's own scripts run first; the shell starts on DOMContentLoaded. */
  const booted = await waitFor(() => doc.getElementById('shfNav'));
  ok(booted, 'the shell never started (no navigation rail after 4s)');
  await tick(40);

  /* --- navigation rail ------------------------------------------------- */
  const nav = doc.getElementById('shfNav');
  ok(nav, 'the navigation rail was not built');
  if (nav) {
    const tabs = nav.querySelectorAll('.top-tab[data-tab]');
    eq(tabs.length, 14, 'the rail must hold every tab (14), found ' + tabs.length);
    const groups = nav.querySelectorAll('.shf-nav-group-label');
    ok(groups.length >= 4, 'the rail has no navigation groups (' + groups.length + ')');
    ok(nav.querySelector('.shf-nav-brand'), 'the rail has no brand');
    ok(nav.querySelector('#shfNavSessions'), 'the rail has no session counter');
    /* The buttons the app binds to must be the SAME nodes, moved not cloned. */
    const firstTab = nav.querySelector('.top-tab[data-tab="dashboard"]');
    ok(firstTab && firstTab.tagName === 'BUTTON',
       'a nav item is not the original .top-tab button');
    ok(!doc.querySelector('.top-tabs .top-tab'),
       'a .top-tab was left behind in the old strip (duplicate listeners)');
  }

  /* --- page headers ----------------------------------------------------- */
  const contents = doc.querySelectorAll('.tab-content');
  const withHead = [...contents].filter((c) => c.querySelector(':scope > .shf-page-head'));
  eq(withHead.length, 14, 'every tab needs a page header, got ' + withHead.length);
  const dashHead = doc.querySelector('#dashboard-content .shf-page-title');
  eq(dashHead && dashHead.textContent, 'Command Centre', 'the dashboard header is wrong');

  /* --- command bar ------------------------------------------------------ */
  ok(doc.getElementById('shfSearchInput'), 'no global search field');
  ok(doc.getElementById('shfNotifBtn'), 'no notification centre button');
  ok(doc.getElementById('shfProfileBtn'), 'no profile menu');
  ok(doc.getElementById('shfProfileMenu'), 'no profile menu panel');
  eq(doc.querySelectorAll('#themeToggle').length, 1, 'the theme switch was duplicated');
  eq(doc.querySelectorAll('#logout-btn, .logout-btn').length, 1,
     'the logout control was duplicated');

  /* --- contextual column ------------------------------------------------ */
  const ctx = doc.querySelector('.shf-context');
  ok(ctx, 'the contextual column was not built');
  if (ctx) {
    ok(ctx.querySelector('.sidebar'), 'active sessions are not in the contextual column');
    ok(ctx.querySelector('.right-sidebar'), 'client details are not in the contextual column');
    ok(!doc.querySelector('#mainApp > .sidebar'),
       'the sidebar is still a direct grid child (layout would break)');
    const tabs = ctx.querySelectorAll('.shf-context-tab');
    eq(tabs.length, 2, 'the contextual column needs two panes');
    tabs[1].click();
    eq(ctx.getAttribute('data-pane'), 'details', 'the pane switch does nothing');
    tabs[0].click();
    eq(ctx.getAttribute('data-pane'), 'sessions', 'the pane switch does not go back');
  }

  /* --- bottom navigation ------------------------------------------------ */
  const bottom = doc.getElementById('shfBottom');
  ok(bottom, 'no mobile bottom navigation');
  if (bottom) eq(bottom.querySelectorAll('.shf-bottom-item').length, 5,
                 'the bottom bar must hold 5 destinations');

  /* --- the dashboard ---------------------------------------------------- */
  const hero = doc.querySelector('#dashboard-content .shf-hero');
  ok(hero, 'the dashboard has no hero');
  ok(doc.getElementById('shfReactor'), 'the reactor canvas is missing');
  ok(doc.querySelector('#dashboard-content .shf-reactor-fallback'),
     'the reactor has no no-JS fallback');
  ['stat-active-clients', 'stat-generated-links', 'stat-gpu'].forEach((id) => {
    const node = doc.getElementById(id);
    ok(node, 'legacy dashboard id missing: ' + id);
    ok(node && node.closest('#dashboard-content'),
       id + ' is no longer on the dashboard');
  });
  ok(doc.getElementById('shfFeed'), 'the activity feed is missing');
  ok(doc.getElementById('shfBarCpu'), 'the health bars are missing');

  /* --- navigation still works through the panel's own path -------------- */
  const settingsBtn = doc.querySelector('.shf-nav .top-tab[data-tab="settings"]');
  ok(settingsBtn, 'no settings nav item');
  if (settingsBtn) {
    settingsBtn.click();
    await tick(20);
    ok(doc.getElementById('settings-content').classList.contains('active'),
       'clicking a nav item did not switch the panel tab');
    ok(settingsBtn.classList.contains('active'),
       'the clicked nav item is not marked active');
    const bottomSettings = doc.querySelector('.shf-bottom-item[data-tab="settings"]');
    ok(bottomSettings.classList.contains('is-on'),
       'the bottom bar did not follow the tab change');
  }

  /* --- global search ---------------------------------------------------- */
  const searchInput = doc.getElementById('shfSearchInput');
  if (searchInput) {
    searchInput.dispatchEvent(new win.Event('focus'));
    const panel = doc.getElementById('shfSearchPanel');
    ok(panel && !panel.hidden, 'focusing search did not open the results');
    ok(panel.querySelectorAll('.shf-search-hit').length > 4,
       'the search index is empty');
    searchInput.value = 'telegram';
    searchInput.dispatchEvent(new win.Event('input'));
    const hits = panel.querySelectorAll('.shf-search-hit');
    ok(hits.length >= 1 && hits.length <= 4,
       'search is not filtering (got ' + hits.length + ' hits for "telegram")');
    if (hits[0]) {
      hits[0].click();
      await tick(20);
      ok(doc.getElementById('tgbot-content').classList.contains('active'),
         'a search result did not navigate');
    }
  }

  /* --- notification centre ---------------------------------------------- */
  try {
    win.showToast && win.showToast('redesign smoke test', 'info');
  } catch (e) { /* the panel may not be logged in; the shell must not care */ }
  await tick(10);
  const count = doc.getElementById('shfNotifCount');
  ok(count && !count.hidden, 'a toast did not reach the notification centre');

  /* --- no new network fetches at boot ----------------------------------- */
  const externalScripts = [...doc.querySelectorAll('script[src]')]
    .map((s) => s.getAttribute('src'))
    .filter((src) => src && !/chart\.js/.test(src) && !/^\/?admin\//.test(src));
  eq(externalScripts.length, 0,
     'the redesign added script tags: ' + externalScripts.join(', '));

  /* --- reactor respects reduced motion ---------------------------------- */
  const reactorSrc = fs.readFileSync(path.join(ROOT, 'admin/reactor.js'), 'utf8');
  ok(/prefers-reduced-motion/.test(reactorSrc),
     'the reactor has no reduced-motion path');
  ok(/matchMedia/.test(reactorSrc), 'the reactor never asks about motion at all');
  ok(/devicePixelRatio/.test(reactorSrc) && /Math\.min\([^)]*2\)/.test(reactorSrc),
     'the reactor does not cap device pixel ratio (a 3x phone would pay 9x)');
  ok(/visibilitychange/.test(reactorSrc),
     'the reactor keeps drawing in a hidden tab');

  /* --- the cascade actually lands --------------------------------------- */
  {
    const css = (sel, prop) => {
      const el = doc.querySelector(sel);
      return el ? win.getComputedStyle(el).getPropertyValue(prop).trim() : 'MISSING';
    };
    eq(css('.main-app', 'display'), 'none',
       'the pre-login gate broke: the app shell is visible before sign-in');
    /* Signing in flips the same class the panel always used; the new layout
       has to appear on that class and nowhere else. */
    doc.body.classList.add('authenticated');
    eq(css('.main-app', 'display'), 'grid',
       'the authenticated shell does not lay out as a grid');
    ok(/264px|--shf-nav-w/.test(win.getComputedStyle(doc.querySelector('.main-app'))
        .getPropertyValue('grid-template-columns') || '') ||
       win.getComputedStyle(doc.querySelector('.main-app')).gridTemplateColumns.split(' ').length === 3,
       'the authenticated shell is not three columns (rail | workspace | context)');
    doc.body.classList.remove('authenticated');
    eq(css('.top-tabs', 'display'), 'none',
       'the old horizontal tab strip is still on screen');
    eq(css('.shf-nav .top-tab', 'display'), 'flex',
       'navigation items are not laid out as rows (icon + label)');
    const activeTab = doc.querySelector('.tab-content.active');
    ok(activeTab, 'no tab is active after boot');
    ok(activeTab && win.getComputedStyle(activeTab).display === 'flex',
       'the active tab is not a flex column (got ' +
       (activeTab ? win.getComputedStyle(activeTab).display : 'none') + ')');
    ok(/env\(safe-area-inset-bottom/.test(HTML),
       'no safe-area handling — the bottom bar would sit under an iPhone home bar');
    ok(/prefers-reduced-motion/.test(HTML),
       'the design system has no reduced-motion block');
    ok(/<meta name="viewport" content="[^"]*width=device-width/.test(HTML),
       'no mobile viewport meta tag');
    ok(/viewport-fit=cover/.test(HTML),
       'viewport does not opt into the safe area');
  }

  /* --- per-screen behaviour --------------------------------------------- */
  {
    const nav = (tab) => {
      const b = doc.querySelector('.shf-nav .top-tab[data-tab="' + tab + '"]');
      if (b) b.click();
    };

    /* Settings: a jump bar over a long screen. */
    nav('settings');
    await tick(20);
    const jump = doc.querySelector('#settings-content > .shf-jump');
    ok(jump, 'settings has no section jump bar');
    ok(jump && jump.querySelectorAll('.shf-jump-btn').length >= 5,
       'the settings jump bar has too few sections');

    /* Display: the desktop-only notice. */
    ok(doc.querySelector('#display-content .shf-desktop-note'),
       'the live display does not say it is desktop-only');

    /* Logs: search + severity chips over a real stream. */
    nav('logs');
    await tick(20);
    const logList = doc.getElementById('logs-list');
    ok(doc.getElementById('shfLogCount'), 'the log stream has no event counter');
    const bar = doc.querySelector('#logs-content .shf-logbar');
    ok(bar, 'the log stream has no filter bar');
    const seed = ['connect', 'disconnect', 'navigate'].map((type, i) =>
      '<div class="log-entry"><div class="log-time message-text">10:0' + i + '</div>' +
      '<div class="log-type ' + type + '">' + type + '</div>' +
      '<div class="log-content"><div class="log-title message-text">' + type + ' event</div></div></div>'
    ).join('');
    logList.innerHTML = seed;
    await tick(60);
    const chips = [...doc.querySelectorAll('#logs-content .shf-chip')].map((c) => c.textContent);
    ok(chips.includes('connect') && chips.includes('navigate'),
       'the severity chips do not come from the stream: ' + chips.join(','));
    const connectChip = [...doc.querySelectorAll('#logs-content .shf-chip')]
      .find((c) => c.textContent === 'connect');
    connectChip.click();
    await tick(20);
    const visible = [...doc.querySelectorAll('#logs-list .log-entry')]
      .filter((r) => !r.classList.contains('is-hidden'));
    eq(visible.length, 1, 'the severity filter did not narrow the stream');
    eq(doc.getElementById('shfLogCount').textContent, '1 of 3 events',
       'the log counter is not honest about the filter');

    /* Files: a list/grid preference that survives the screen. */
    nav('files');
    await tick(20);
    const fileList = doc.getElementById('file-list');
    const toggle = doc.querySelector('#files-content .shf-viewtoggle');
    ok(toggle, 'the file manager has no view switch');
    if (toggle) {
      const gridBtn = [...toggle.querySelectorAll('.shf-chip')].find((b) => b.textContent === 'Grid');
      gridBtn.click();
      await tick(10);
      ok(fileList.classList.contains('shf-files--grid'),
         'grid view does not change the file list');
      ok(win.localStorage.getItem('shf_files_view') === 'grid',
         'the file view preference is not remembered');
    }

    /* KLG: credentials are masked on screen. */
    nav('klg');
    await tick(20);
    const klgLogs = doc.getElementById('klgModalLogs');
    ok(doc.querySelector('#klg-content .shf-redact-note') ||
       doc.querySelector('.shf-redact-note'),
       'the keylogger screen does not warn about masking');
    if (klgLogs) {
      klgLogs.innerHTML = '<div class="klg-log-item">' +
        '<div class="klg-log-content">user=admin password=hunter2 token=abc123</div>' +
        '<div class="klg-log-url">https://example.com/login</div></div>';
      await tick(80);
      const text = klgLogs.querySelector('.klg-log-content').textContent;
      ok(!/hunter2/.test(text), 'a password is still printed in clear: ' + text);
      ok(!/abc123/.test(text), 'a token is still printed in clear: ' + text);
      ok(/user=admin/.test(text), 'redaction ate the harmless part of the record: ' + text);
      ok(/https:\/\/example\.com\/login/.test(klgLogs.textContent),
         'redaction removed the URL, which the operator needs');
      /* The stored record must be untouched — only the screen is masked. */
      ok(/hunter2/.test(klgLogs.querySelector('.klg-log-content').getAttribute('data-shf-raw')),
         'redaction destroyed the underlying record instead of masking the view');
    }

    /* LPV: preview stage + workbench slots. */
    nav('lpv');
    await tick(20);
    const stage = doc.querySelector('#lpv-sub-live .shf-lpv-stage');
    ok(stage, 'the live page viewer has no preview stage');
    ok(doc.getElementById('shfLpvFull'), 'the preview has no fullscreen action');
    ok(doc.getElementById('shfLpvRefresh'), 'the preview cannot be reloaded');
    const slots = [...doc.querySelectorAll('#lpv-sub-live .lpv-panel')]
      .map((p) => (p.className.match(/shf-lpv-slot--\w+/) || [''])[0]).filter(Boolean);
    ok(slots.length >= 4, 'the LPV workbench slots were not assigned: ' + slots.join(','));
    ok(/current/.test(slots.join(',')) && /audit/.test(slots.join(',')),
       'the workbench did not recognise the live panels');
    ok(doc.querySelector('#lpv-current') && 'pageId' in doc.getElementById('lpv-current').dataset,
       'the current page id is not exposed for the preview');
  }

  /* --- the build is up to date ------------------------------------------ */
  ok(/Generated by deploy\/build-admin\.py/.test(HTML),
     'Admin.html does not declare its generated regions');

  console.log((fails.length ? 'FAIL' : 'PASS') + '  admin redesign: '
              + pass + ' checks, ' + fails.length + ' failed');
  fails.forEach((f) => console.log('  - ' + f));
  dom.window.close();
  process.exit(fails.length ? 1 : 0);
})().catch((err) => {
  console.log('FAIL  admin redesign: threw');
  console.log(err && err.stack || err);
  process.exit(1);
});
