// No dependencies: node tests/test_link_base.cjs
// Exercise the actual helpers embedded in both standalone panels.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let checks = 0;
function equal(actual, expected) { assert.equal(actual, expected); checks++; }
for (const name of ['Admin.html', 'mini-admin.html']) {
  const source = fs.readFileSync(path.join(__dirname, '..', name), 'utf8');
  const full = name === 'Admin.html';
  const start = source.indexOf(full ? '        function lpvNormalizeBase(' : 'function _normBase(');
  const end = source.indexOf(full ? '        function lpvLinkBaseAttrEsc(' : 'function lpvLinkBaseControlHtml(', start);
  assert(start >= 0 && end > start);
  const storage = new Map();
  const location = { href: 'https://panel.example/Admin.html', origin: 'https://panel.example', protocol: 'https:' };
  const context = vm.createContext({ URL, window: {location}, location,
    SERVER_API_BASE: 'https://backend.example/api-prefix', apiBase: () => 'https://backend.example/api-prefix',
    LINK_BASE_MODE_KEY: 'lpv_link_base_mode', LINK_BASE_CUSTOM_KEY: 'lpv_link_base_custom',
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)} });
  vm.runInContext(source.slice(start, end), context);
  const apply = context.lpvApplyLinkBase;
  const raw = 'https://backend.example/api-prefix/client.html?auth=abc&workflow=wf1&url=https%3A%2F%2Fexample.com#test';
  const suffix = '/client.html?auth=abc&workflow=wf1&url=https%3A%2F%2Fexample.com#test';
  equal(apply(raw), raw);
  equal(context.lpvLinkBaseForBuild(), 'https://backend.example/api-prefix');
  storage.set('lpv_link_base_mode', 'custom');
  for (const [input, base] of [
    ['https://brand.example/client.html', 'https://brand.example'],
    ['brand.example', 'https://brand.example'],
    ['https://brand.example/pages/client.html', 'https://brand.example/pages'],
    ['https://brand.example/pages/', 'https://brand.example/pages'],
  ]) {
    storage.set('lpv_link_base_custom', input);
    equal(apply(raw), base + suffix);
    equal(context.lpvLinkBaseForBuild(), base);
  }
  for (const invalid of ['not a url', 'javascript:alert(1)', 'file:///tmp/client.html', 'https://name:password@brand.example']) {
    storage.set('lpv_link_base_custom', invalid);
    equal(apply(raw), raw);
  }
  storage.set('lpv_link_base_mode', 'origin');
  equal(apply(raw), 'https://panel.example' + suffix);
  location.protocol = 'file:';
  equal(apply(raw), raw);
  location.protocol = 'https:';
  storage.set('lpv_link_base_mode', 'server');
  equal(apply(raw), raw); // restoring server must not reuse the rewritten URL
  equal(apply(''), '');
  console.log(`${name}: link-base checks passed`);
}
console.log(`${checks} checks passed`);
