// No dependencies: node tests/test_pc_only_gate.cjs
//
// "These dashboards shouldn't work on mobile — blank page, say 'view on PC
// only'." — clarified: not the PAGES, the DISPLAY. The CDP cast screens (the
// live view of a server-side browser: Live view, PCM, Access) are the part that
// cannot work on a phone — a desktop-sized pointer and a keyboard drive somebody
// else's real browser. So:
//
//   * Admin.html keeps working on a phone. Each cast AREA (`[data-pc-cast]`)
//     goes blank and says "View on PC only", and the device never subscribes to
//     a cast: no `subscribe` message, no `/ws/pcm`, no `/ws/access`.
//   * mini-admin.html is the PHONE panel. It has no cast screen, so it is not
//     gated at all.
//   * client.html (the visitor's page) is never gated.
//
// This runs the real gate bytes from the real file in a `vm` against a tiny
// fake DOM, once as a phone and once as a desktop browser.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');
const PHONE_UA = 'Mozilla/5.0 (Linux; Android 15; Pixel 7) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36';
const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) '
  + 'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';
const IPAD_UA = 'Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 '
  + '(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';
const DESKTOP_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36';

const admin = fs.readFileSync(path.join(root, 'Admin.html'), 'utf8');
const mini = fs.readFileSync(path.join(root, 'mini-admin.html'), 'utf8');
const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');

// ---------------------------------------------------------------------------
// 1. The gate the page actually ships
// ---------------------------------------------------------------------------
const gateStart = admin.indexOf('<!-- PC-only CAST SCREENS');
ok(gateStart >= 0, 'Admin.html ships the cast-screen gate');
ok(gateStart < admin.indexOf('</head>'), 'it runs from <head>, before the app');
const gateEnd = admin.indexOf('</script>', gateStart);
ok(gateEnd > gateStart, 'the gate script is closed');
const gateBlock = admin.slice(gateStart, gateEnd);
const gateJs = gateBlock.slice(gateBlock.indexOf('<script>') + '<script>'.length);

ok(/View on PC only/.test(gateBlock), 'the gate says "View on PC only"');
ok((admin.match(/__FIXIIS_PC_ONLY__/g) || []).length === 1,
  'the "not a PC" flag is set in exactly one place');
ok(!/location\.(href|replace|assign)/.test(gateBlock), 'the gate never redirects');
ok(!/body\s*>\s*\*:not\(/.test(gateBlock),
  'the PAGE is not blanked — only the cast areas are');
ok(!/window\.WebSocket\s*=/.test(gateBlock) && !/window\.fetch\s*=/.test(gateBlock),
  'the dashboard keeps its sockets and requests on a phone');

// The cast areas, marked in the markup.
const marked = admin.match(/<[^>]*data-pc-cast="1"[^>]*>/g) || [];
ok(marked.length === 3, `exactly three cast screens are marked (got ${marked.length})`);
const markedIds = marked.join('\n');
ok(/id="access-stage"/.test(markedIds), 'the Access live view is one of them');
ok(/id="pcm-canvas-wrap"/.test(markedIds), 'the PCM live view is one of them');
ok(/class="screenshot-panel"/.test(markedIds), 'the Display live view is one of them');

// Nothing subscribes to a cast on a device that cannot show it.
ok((admin.match(/if \(window\.__FIXIIS_CAST_BLOCKED__\)/g) || []).length === 3,
  'the three cast entry points check the flag (Display, PCM, Access)');
const selectBody = admin.slice(admin.indexOf('function selectClient'),
  admin.indexOf('function deselectClient'));
ok(/if \(window\.__FIXIIS_CAST_BLOCKED__\) \{[\s\S]{0,400}?get_info/.test(selectBody),
  'a phone selects a client for its info, not for a screen');
ok(selectBody.indexOf('__FIXIIS_CAST_BLOCKED__') < selectBody.lastIndexOf("type: 'subscribe'"),
  'and the browser-subscribe that follows is never reached');
ok(/function pcmConnect\(\)\{\s*[\s\S]{0,300}?__FIXIIS_CAST_BLOCKED__/.test(admin),
  'PCM never opens /ws/pcm on a phone');
ok(/function accessOpenSocket\(\) \{\s*[\s\S]{0,300}?__FIXIIS_CAST_BLOCKED__/.test(admin),
  'Access never opens /ws/access on a phone');

// ---------------------------------------------------------------------------
// 2. Fake DOM, only what the gate touches
// ---------------------------------------------------------------------------
function makeNode(tag, attrs) {
  const node = {
    tagName: String(tag || '').toUpperCase(),
    children: [],
    attributes: Object.assign({}, attrs || {}),
    innerHTML: '',
    textContent: '',
    className: '',
    firstChild: null,
    appendChild(child) {
      this.children.push(child);
      if (!this.firstChild) this.firstChild = child;
      return child;
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name]; },
    classList: {
      _items: [],
      add(cls) { if (this._items.indexOf(cls) < 0) this._items.push(cls); },
      contains(cls) { return this._items.indexOf(cls) >= 0; },
      remove(cls) { this._items = this._items.filter((c) => c !== cls); },
    },
    querySelector(sel) {
      const want = String(sel).replace(/^\./, '');
      const walk = (n) => {
        for (const child of n.children || []) {
          if (String(child.className || '').split(/\s+/).indexOf(want) >= 0) return child;
          const hit = walk(child);
          if (hit) return hit;
        }
        return null;
      };
      return walk(this);
    },
  };
  return node;
}

// The three areas, as the markup really declares them.
const areas = marked.map((tag) => {
  const id = /id="([^"]+)"/.exec(tag);
  const cls = /class="([^"]+)"/.exec(tag);
  const node = makeNode('div', id ? { id: id[1] } : {});
  node.className = cls ? cls[1] : '';
  const img = makeNode('img');
  img.className = 'stream-image';
  node.appendChild(img);
  return node;
});

function makeWorld(ua, screenSize, opts) {
  const options = opts || {};
  const document = {
    head: makeNode('head'),
    body: makeNode('body'),
    documentElement: makeNode('html'),
    createElement: (tag) => makeNode(tag),
    createTextNode: (text) => ({ tagName: '#text', textContent: String(text), children: [] }),
    querySelectorAll(sel) {
      if (/data-pc-cast/.test(String(sel))) return areas.slice();
      return [];
    },
    addEventListener() {},
  };
  const sandbox = {
    window: {
      matchMedia: (q) => ({ matches: Boolean(options.coarse) && /coarse/.test(q) }),
    },
    document,
    navigator: { userAgent: ua, maxTouchPoints: options.maxTouchPoints || 0 },
    screen: { width: screenSize[0], height: screenSize[1] },
    setTimeout: (fn) => 0,
    MutationObserver: function () { this.observe = () => {}; },
    console,
  };
  sandbox.globalThis = sandbox;
  return sandbox;
}

function boot(ua, screenSize, opts) {
  // Every run starts from clean areas.
  for (const area of areas) {
    area.children = area.children.filter((c) => String(c.className).indexOf('fixiis-cast-note') < 0);
    area.classList.remove('fixiis-cast-blocked');
  }
  const sandbox = makeWorld(ua, screenSize, opts);
  vm.runInNewContext(gateJs, sandbox, { filename: 'Admin.html:cast-gate' });
  return sandbox;
}

function notesIn(area) {
  return area.children.filter((c) => String(c.className).indexOf('fixiis-cast-note') >= 0);
}

function visibleText(node, out) {
  if (!node) return out;
  if (node.innerHTML) out.push(node.innerHTML);
  if (node.textContent) out.push(node.textContent);
  for (const child of node.children || []) visibleText(child, out);
  return out;
}

// ---------------------------------------------------------------------------
// 3. A phone: the screens go blank, the dashboard does not
// ---------------------------------------------------------------------------
const phone = boot(PHONE_UA, [412, 915], { coarse: true });
ok(phone.window.__FIXIIS_CAST_BLOCKED__ === true, 'a phone is flagged');
ok(phone.window.__FIXIIS_PC_ONLY__ === true, 'and the old probe name agrees');
ok(areas.length === 3, 'three screens exist to blank');
for (const area of areas) {
  const name = area.attributes.id || area.className;
  ok(notesIn(area).length === 1, `${name}: gets the notice, once`);
  ok(area.classList.contains('fixiis-cast-blocked'), `${name}: is marked blocked`);
  ok(/View on PC only/.test(visibleText(notesIn(area)[0], []).join('\n')),
    `${name}: the notice reads "View on PC only"`);
  ok(notesIn(area)[0].getAttribute('role') === 'alert', `${name}: it is an alert`);
}
ok(phone.document.documentElement.getAttribute('data-pc-only-screens') === '1',
  'the document says its screens are PC-only');

const styles = phone.document.head.children.filter((n) => n.tagName === 'STYLE');
ok(styles.length === 1, 'one stylesheet enforces the blank screen');
const cssText = visibleText(styles[0], []).join('\n');
ok(/\[data-pc-cast\]\.fixiis-cast-blocked\s*>\s*\*:not\(\.fixiis-cast-note\)\s*\{\s*display\s*:\s*none/i.test(cssText),
  'the area hides everything but the notice');

// The panel still works: sockets and fetch are the browser's own.
ok(typeof phone.window.WebSocket === 'undefined', 'the control socket is NOT disabled');
ok(typeof phone.window.fetch === 'undefined', 'nor is fetch');

// iPhones and iPads are refused too.
ok(boot(IPHONE_UA, [390, 844], { coarse: true, maxTouchPoints: 5 })
  .window.__FIXIIS_CAST_BLOCKED__ === true, 'an iPhone is flagged');
ok(boot(IPAD_UA, [820, 1180], { coarse: true, maxTouchPoints: 5 })
  .window.__FIXIIS_CAST_BLOCKED__ === true, 'an iPad is flagged');

// ---------------------------------------------------------------------------
// 4. A desktop is never touched — not even in a narrow window
// ---------------------------------------------------------------------------
for (const size of [[1920, 1080], [1280, 800], [900, 700]]) {
  const desktop = boot(DESKTOP_UA, size, {});
  ok(desktop.window.__FIXIIS_CAST_BLOCKED__ === false,
    `${size[0]}x${size[1]} desktop: the cast screens are allowed`);
  for (const area of areas) {
    ok(notesIn(area).length === 0, `${size[0]}x${size[1]}: no notice is painted`);
    ok(!area.classList.contains('fixiis-cast-blocked'), `${size[0]}x${size[1]}: no area is blocked`);
  }
  ok(desktop.document.head.children.length === 0,
    `${size[0]}x${size[1]}: no CSS is injected`);
}

// A touch laptop (fine pointer available) keeps its screens.
const touchLaptop = boot(DESKTOP_UA, [1440, 900], { coarse: false });
ok(touchLaptop.window.__FIXIIS_CAST_BLOCKED__ === false,
  'a keyboard-and-mouse machine keeps the cast screens');

// ---------------------------------------------------------------------------
// 5. The phone panel is not gated, and the visitor page never is
// ---------------------------------------------------------------------------
ok(!/__FIXIIS_PC_ONLY__/.test(mini), 'mini-admin.html (the phone panel) is not gated');
ok(!/View on PC only/.test(mini), 'mini-admin.html never shows the PC-only notice');
ok(!/data-pc-cast/.test(mini), 'mini-admin.html has no cast screen to blank');
ok(!/__FIXIIS_PC_ONLY__/.test(client), 'client.html (visitor page) is not gated');
ok(!/View on PC only/.test(client), 'client.html never shows the PC-only notice');
ok(!/data-pc-cast/.test(client), 'client.html has no cast screen');

console.log(`\n${checks} checks passed.`);
