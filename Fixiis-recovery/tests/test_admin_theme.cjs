#!/usr/bin/env node
/* Exercise the actual theme code with lightweight DOM/storage fakes. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '..', 'Admin.html'), 'utf8');

assert.match(html, /<html\b[^>]*data-theme="light"/);
for (const id of ['themeToggle', 'loginThemeToggle']) {
  const tag = html.match(new RegExp(`<button\\b[^>]*id="${id}"[^>]*>`));
  assert(tag, `${id} is a button`);
  assert.match(tag[0], /role="switch"/);
  assert.match(tag[0], /aria-checked="false"/);
  assert.doesNotMatch(tag[0], /\bonclick=/);
}
assert.match(html, /\.header \.theme-toggle\s*\{\s*display:\s*block/);

const head = html.match(/<script>\s*\/\/ Set the saved preference[\s\S]*?<\/script>/)[0];
const earlyScript = head.replace(/^<script>/, '').replace(/<\/script>$/, '');
const start = html.indexOf('        function applyTheme(theme) {');
const end = html.indexOf('            document.addEventListener(\'keydown\', (e) => {', start);
assert(start !== -1 && end !== -1);
// Close initEventListeners after the theme-specific wiring, leaving the rest of
// the panel untouched. These are the exact functions from the production page.
const code = html.slice(start, end) + '\n}';

function setup(stored, blocked = false) {
  const storage = {
    value: stored,
    getItem(key) { assert.equal(key, 'admin-theme'); if (blocked) throw Error('blocked'); return this.value; },
    setItem(key, value) { assert.equal(key, 'admin-theme'); if (blocked) throw Error('blocked'); this.value = value; }
  };
  const documentElement = {
    theme: 'light',
    setAttribute(key, value) { assert.equal(key, 'data-theme'); this.theme = value; },
    getAttribute(key) { assert.equal(key, 'data-theme'); return this.theme; }
  };
  function control() {
    return {
      attributes: {}, switched: false, events: [],
      classList: { toggle(name, state) { assert.equal(name, 'switched'); this.owner.switched = state; }, owner: null },
      setAttribute(k, v) { this.attributes[k] = v; },
      addEventListener(type, fn) { assert.equal(type, 'click'); this.events.push(fn); },
      click() { this.events.forEach(fn => fn()); }
    };
  }
  const header = control(), login = control();
  header.classList.owner = header; login.classList.owner = login;
  const context = vm.createContext({document: {documentElement}, localStorage: storage, themeToggle: header, loginThemeToggle: login});
  vm.runInContext(earlyScript, context);
  const firstPaint = documentElement.theme;
  vm.runInContext(code, context);
  vm.runInContext('loadTheme(); initEventListeners();', context);
  return {context, storage, root: documentElement, header, login, firstPaint};
}
for (const value of [null, '', 'garbage', 'light']) {
  const x = setup(value);
  assert.equal(x.firstPaint, 'light'); assert.equal(x.root.theme, 'light');
  assert.equal(x.header.attributes['aria-checked'], 'false'); assert.equal(x.login.switched, false);
}
{
  const x = setup('dark');
  assert.equal(x.firstPaint, 'dark'); assert.equal(x.root.theme, 'dark');
  assert.equal(x.header.attributes['aria-checked'], 'true'); assert.equal(x.login.switched, true);
  assert.equal(x.header.events.length, 1); assert.equal(x.login.events.length, 1);
  x.header.click(); assert.equal(x.root.theme, 'light'); assert.equal(x.storage.value, 'light');
  assert.equal(x.login.attributes['aria-checked'], 'false');
  x.login.click(); assert.equal(x.root.theme, 'dark'); assert.equal(x.storage.value, 'dark');
  assert.equal(x.header.attributes['aria-checked'], 'true');
}
{
  const x = setup(null, true);
  assert.equal(x.root.theme, 'light');
  x.login.click(); assert.equal(x.root.theme, 'dark');
  x.header.click(); assert.equal(x.root.theme, 'light');
}
console.log('admin theme: default, persistence, synchronization, blocked storage and single click passed');
