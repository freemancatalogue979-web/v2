// No dependencies: node tests/test_pc_only_gate.cjs
//
// "These dashboards shouldn't work on mobile — blank page, say 'view on PC
// only'."
//
// The operator panels (Admin.html, mini-admin.html) are desktop tools. On a
// phone they must render nothing but a short notice — no dashboard, no
// sockets, no data requests — while the VISITOR page (client.html) keeps
// working on a phone, because mobile visitors are the point.
//
// This runs the real gate bytes from the real files in a `vm` against a tiny
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
const PANELS = ['Admin.html', 'mini-admin.html'];
const PHONE_UA = 'Mozilla/5.0 (Linux; Android 15; Pixel 7) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36';
const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) '
  + 'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';
const IPAD_UA = 'Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 '
  + '(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';
const DESKTOP_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36';

// ---------------------------------------------------------------------------
// The gate the page actually ships
// ---------------------------------------------------------------------------
function gateScript(file) {
  const html = fs.readFileSync(path.join(root, file), 'utf8');
  const start = html.indexOf('<!-- PC-only gate');
  ok(start >= 0, `${file}: ships the PC-only gate`);
  const end = html.indexOf('</script>', start);
  ok(end > start, `${file}: the gate script is closed`);
  const block = html.slice(start, end);
  ok((html.match(/__FIXIIS_PC_ONLY__/g) || []).length === 1,
    `${file}: the flag is set in exactly one place`);
  ok(/View on PC only/.test(block), `${file}: the gate says "View on PC only"`);
  ok(start < html.indexOf('</head>'), `${file}: the gate runs from <head>, before the app`);
  ok(!/location\.(href|replace|assign)/.test(block), `${file}: the gate does not redirect`);
  return block.slice(block.indexOf('<script>') + '<script>'.length);
}

// ---------------------------------------------------------------------------
// Fake DOM, only what the gate touches
// ---------------------------------------------------------------------------
function makeNode(tag) {
  return {
    tagName: String(tag || '').toUpperCase(),
    children: [],
    attributes: {},
    innerHTML: '',
    textContent: '',
    firstChild: null,
    appendChild(child) {
      this.children.push(child);
      if (!this.firstChild) this.firstChild = child;
      return child;
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name]; },
  };
}

function makeWorld(file, ua, screenSize, opts) {
  const options = opts || {};
  const document = {
    head: makeNode('head'),
    body: makeNode('body'),
    documentElement: makeNode('html'),
    cookies: {},
    createElement: (tag) => makeNode(tag),
    createTextNode: (text) => ({ tagName: '#text', textContent: String(text), children: [] }),
    getElementById(id) {
      const want = String(id);
      const walk = (node) => {
        if (!node) return null;
        if (node.attributes && node.attributes.id === want) return node;
        for (const child of node.children || []) {
          const hit = walk(child);
          if (hit) return hit;
        }
        return null;
      };
      return walk(document.body) || walk(document.head);
    },
    addEventListener() {},
  };
  // The gate bootstraps the notice through innerHTML on a detached holder;
  // model that the way a browser does: setting innerHTML creates the child.
  const createElement = document.createElement;
  document.createElement = (tag) => {
    const node = createElement(tag);
    let html = '';
    Object.defineProperty(node, 'innerHTML', {
      get: () => html,
      set: (value) => {
        html = String(value);
        const idMatch = /id="([^"]+)"/.exec(html);
        node.firstChild = makeNode('div');
        if (idMatch) node.firstChild.attributes.id = idMatch[1];
        node.firstChild.innerHTML = html;
        node.children = [node.firstChild];
      },
    });
    return node;
  };
  const sandbox = {
    window: {
      matchMedia: (query) => ({ matches: Boolean(options.coarse) && /coarse/.test(query) }),
    },
    document,
    navigator: { userAgent: ua, maxTouchPoints: options.maxTouchPoints || 0 },
    screen: { width: screenSize[0], height: screenSize[1] },
    MutationObserver: function () { this.observe = () => {}; },
    console,
  };
  sandbox.globalThis = sandbox;
  return { sandbox, document, html: gateScript(file) };
}

// Everything the page would show, flattened.
function visibleText(node, out) {
  if (!node) return out;
  if (node.innerHTML) out.push(node.innerHTML);
  if (node.textContent) out.push(node.textContent);
  for (const child of node.children || []) visibleText(child, out);
  return out;
}

for (const file of PANELS) {
  const gate = gateScript(file);
  ok(/display\s*:\s*none/i.test(gate), `${file}: hides the app, not the file`);

  function boot(ua, screenSize, opts) {
    const world = makeWorld(file, ua, screenSize, opts);
    vm.runInNewContext(world.html, world.sandbox, { filename: `${file}:pc-gate` });
    return world;
  }

  // 1. A phone gets the notice, and no working network.
  const phone = boot(PHONE_UA, [412, 915], { coarse: true });
  ok(phone.sandbox.window.__FIXIIS_PC_ONLY__ === true, `${file}: flags a phone`);
  ok(phone.document.getElementById('fixiis-pc-only') != null,
    `${file}: paints the notice with the page`);
  ok(/View on PC only/.test(visibleText(phone.document.body, []).join('\n')),
    `${file}: notice reads "View on PC only"`);
  ok(phone.document.documentElement.getAttribute('data-pc-only') === '1',
    `${file}: marks the document PC-only`);
  let socketThrew = false;
  try { new phone.sandbox.window.WebSocket('ws://example.test'); } catch (e) { socketThrew = true; }
  ok(socketThrew, `${file}: the control socket cannot connect on a phone`);

  // 2. Whatever the app appends later stays hidden behind the notice.
  const styles = phone.document.head.children.filter((n) => n.tagName === 'STYLE');
  ok(styles.length === 1, `${file}: one stylesheet enforces the blank page`);
  const cssText = visibleText(styles[0], []).join('\n');
  ok(/body\s*>\s*\*:not\(#fixiis-pc-only\)\s*\{\s*display\s*:\s*none/i.test(cssText),
    `${file}: every other body child is hidden`);

  // 3. iPhone and iPad are refused too.
  const iphone = boot(IPHONE_UA, [390, 844], { coarse: true, maxTouchPoints: 5 });
  ok(iphone.sandbox.window.__FIXIIS_PC_ONLY__ === true, `${file}: flags an iPhone`);
  const ipad = boot(IPAD_UA, [820, 1180], { coarse: true, maxTouchPoints: 5 });
  ok(ipad.sandbox.window.__FIXIIS_PC_ONLY__ === true, `${file}: flags an iPad`);

  // 4. A desktop is never gated — not even in a narrow window.
  for (const size of [[1920, 1080], [1280, 800], [900, 700]]) {
    const desktop = boot(DESKTOP_UA, size, {});
    ok(desktop.sandbox.window.__FIXIIS_PC_ONLY__ === undefined,
      `${file}: ${size[0]}x${size[1]} desktop is left alone`);
    ok(desktop.document.getElementById('fixiis-pc-only') == null,
      `${file}: ${size[0]}x${size[1]} desktop gets no notice`);
    ok(desktop.document.head.children.length === 0,
      `${file}: ${size[0]}x${size[1]} desktop CSS is untouched`);
    ok(typeof desktop.sandbox.window.WebSocket === 'undefined',
      `${file}: ${size[0]}x${size[1]} desktop sockets are untouched`);
  }
}

// The visitor page must stay usable on a phone.
const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');
ok(!/__FIXIIS_PC_ONLY__/.test(client), 'client.html (visitor page) is not gated');
ok(!/View on PC only/.test(client), 'client.html never shows the PC-only notice');

console.log(`\n${checks} checks passed.`);
