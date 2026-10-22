// No dependencies: node tests/test_mini_simple.cjs
//
// The mini panel is the one people open on a phone. It was reported as "too
// complex to operate", so it is now four jobs and nothing else:
//
//   1. Clients   — who is on your pages
//   2. Pages     — send one page to a visitor
//   3. Flows     — run a sequence for a visitor
//   4. Telegram  — is my bot working, what does it say
//
// "Past visitors" is reached from Clients, and live chat is gone. These
// checks keep it that way: a new tab, a second place to do the same thing,
// or settings spread back across the screen all fail the build.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');
const mini = fs.readFileSync(path.join(root, 'mini-admin.html'), 'utf8');

// ---- four tabs, in the order that matches the four jobs -------------------
const tabs = [...mini.matchAll(/<button class="tab[^"]*" data-view="([a-z]+)"/g)].map((m) => m[1]);
assert.deepEqual(tabs, ['clients', 'pages', 'flows', 'telegram']);
checks++;

const navs = [...mini.matchAll(/<button class="nav-item[^"]*" data-view="([a-z]+)"/g)].map((m) => m[1]);
assert.deepEqual(navs, tabs, 'the sidebar offers the same four things as the phone tab bar');
checks++;

// Every tab must route to a renderer that exists.
const renderFn = { clients: 'renderClients', pages: 'renderPages', flows: 'renderFlows', telegram: 'renderTelegram' };
for (const view of tabs) {
  if (view === 'clients') {
    // Clients is also the default screen, so it is the fallback return.
    ok(/return renderClients\(\);\s*\n\}/.test(mini), 'clients routes to renderClients()');
  } else {
    ok(new RegExp(`if\\(v === '${view}'\\)\\s*return ${renderFn[view]}\\(\\);`).test(mini),
       `${view} routes to ${renderFn[view]}()`);
  }
  ok(new RegExp(`function ${renderFn[view]}\\(`).test(mini), `${renderFn[view]} is defined`);
}

// ---- chat is gone, not hidden -------------------------------------------
ok(!/data-view="chat"/.test(mini), 'no chat tab');
ok(!/function renderChat\(/.test(mini), 'no chat view');
ok(!/function doChatSend\(/.test(mini), 'no chat send path');
ok(!/lpv\/chat/.test(mini), 'the mini panel no longer calls the chat endpoints');
ok(!/S\.chat\b/.test(mini), 'no leftover chat state');

// ---- one obvious action per screen --------------------------------------
for (const [view, word] of [['pages', 'Send a page'], ['flows', 'Run a flow']]) {
  const start = mini.indexOf(`function ${renderFn[view]}(`);
  const body = mini.slice(start, start + 2000);
  ok(body.includes(`<b>${word}</b>`), `${view} explains its single job in one line`);
  ok(body.includes('class="howto"'), `${view} shows the one-line how-to strip`);
}

// ---- advanced settings stay folded away ---------------------------------
for (const title of ['Advanced — how brand-new visitors start', 'What should alert me?', 'Send a message now']) {
  ok(mini.includes(`<details class="adv"`) && mini.includes(title), `"${title}" lives in a folded section`);
}
// the three "how new visitors start" controls moved out of the landing view
ok(!/function renderPages\(\)\{[\s\S]{0,600}id="stLpvOnly"/.test(mini),
   'the advanced controls are not in the first screenful of Pages');
ok(/id="stLpvOnly"/.test(mini), 'they still exist (nothing was dropped)');

// ---- Telegram says whether it works before asking for anything ----------
ok(/status-card/.test(mini), 'Telegram opens with a status card');
ok(/Alerts are on/.test(mini) && /No bot connected yet/.test(mini), 'the three states are spelled out');
ok(/function tgBadge\(/.test(mini), 'the menu itself says whether alerts are on');
ok(/id="tgToken"/.test(mini) && /id="tgChat"/.test(mini), 'bot token and chat id are still editable');
ok(/id="tgSave"/.test(mini) && /id="tgTest"/.test(mini), 'save and send-test are still there');
ok(/id="tgSend"/.test(mini), 'sending a message is still possible');
ok(/class="tg-notify"/.test(mini), 'per-event alert choices are still there');

// ---- clients screen: reachable history, honest empty state -------------
ok(/go\('profiles'\)/.test(mini), 'Past visitors is one tap from Clients');
ok(/function renderProfiles\(/.test(mini), 'and still exists');
ok(/Back to who is here/.test(mini), 'with a way back');
ok(/nobody is here yet/.test(mini), 'empty state is plain language');
ok(/Share a page/.test(mini) && /Share a flow/.test(mini), 'and offers the next step');

// ---- the panel must not grow a fifth job back --------------------------
ok(tabs.length === 4, 'still four jobs');
// 2,564 lines / ~150 KB after removing chat; a regression that re-adds a
// whole console would blow past this.
ok(mini.split('\n').length < 2700, 'the file did not grow back to its old size');

console.log('mini-admin simplicity checks passed');
console.log(`${checks} checks passed`);
