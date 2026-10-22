// No dependencies: node tests/test_backend_address.cjs
//
// "Connection error" on the admin login screen, then nothing working at all
// (no workflows, no Telegram activity), is almost always the wrong backend
// address rather than a dead server:
//
//   * the address is remembered in localStorage ('lpv_server_endpoint') and
//     shared by Admin.html, mini-admin.html and client.html;
//   * a bare "192.0.2.10:8446" used to be turned into https://<that>, which
//     this server never answers (it speaks http unless TLS is in front);
//   * nothing in the panel showed or allowed changing that address.
//
// These checks pin the fixes: the scheme mirrors the page, the panel and the
// viewer fall back to the origin that served them, and the login screen shows
// (and can change) the address.
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
const admin = fs.readFileSync(path.join(root, 'Admin.html'), 'utf8');
const mini = fs.readFileSync(path.join(root, 'mini-admin.html'), 'utf8');
const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');

// ---- 1. a typed address keeps the page's scheme --------------------------
const schemeRule = /pageIsHttps\s*\?\s*'https:\/\/'\s*:\s*'http:\/\/'/;
ok(schemeRule.test(admin), 'Admin.html: bare host uses the page scheme');
ok(schemeRule.test(mini), 'mini-admin.html: bare host uses the page scheme');
ok(schemeRule.test(client), 'client.html: bare host uses the page scheme');
ok(!/candidate = 'https:\/\/' \+ candidate;/.test(admin + mini + client),
   'no file still forces https:// onto a schemeless address');

// The guess must actually be the documented one: run it as the pages do.
function guess(value, pageProtocol) {
  let candidate = value;
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(candidate)) {
    candidate = (pageProtocol === 'https:' ? 'https://' : 'http://') + candidate;
  }
  return new URL(candidate).origin;
}
assert.equal(guess('192.0.2.10:8446', 'http:'), 'http://192.0.2.10:8446');
assert.equal(guess('192.0.2.10:8446', 'https:'), 'https://192.0.2.10:8446');
assert.equal(guess('https://panel.example.com', 'http:'), 'https://panel.example.com');
checks += 3;

// ---- 2. the admin panel can recover -------------------------------------
ok(/const PAGE_ORIGIN = /.test(admin), 'Admin.html knows the origin serving it');
ok(/function tryPageOriginFallback\(\)/.test(admin),
   'Admin.html can fall back to that origin');
ok(/if \(reconnectAttempts >= 2 && tryPageOriginFallback\(\)\)/.test(admin),
   'the fallback runs after two failed WS attempts');
ok(/async function probeBackend\(\)/.test(admin),
   'Admin.html probes the saved backend before login');
ok(admin.includes("pingServer(SERVER_API_BASE)") && admin.includes("pingServer(serverApiBase(PAGE_ORIGIN))"),
   'the probe only switches when the page origin really answers');
ok(/localStorage\.setItem\('lpv_server_endpoint', SERVER_ACTIVE\.origin\)/.test(admin),
   'the address that worked is remembered');
ok(/Connection error — ' \+ serverHost\(SERVER_ACTIVE\)/.test(admin),
   'the error message names the address that failed');
ok(/id="loginServerHost"/.test(admin) && /function initServerControl\(\)/.test(admin),
   'the login screen shows the backend and lets it be changed');
ok(/localStorage\.removeItem\('lpv_server_endpoint'\)/.test(admin),
   'the saved backend can be cleared');

// ---- 3. the viewer falls back too ---------------------------------------
ok(/function switchToPageOrigin\(\)/.test(client),
   'client.html can switch to the origin serving it');
ok(/let ACTIVE_SERVER = SERVER_OVERRIDE;/.test(client),
   'client.html keeps the backend mutable');
ok(/if \(value\.slice\(0, 2\) === '\/\/'\) return ACTIVE_SERVER\.protocol \+ value;/.test(client),
   'protocol-relative URLs follow the active backend');

// Behavioural: extract switchToPageOrigin + its helpers and run them.
function extract(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `missing function ${name}`);
  let depth = 0;
  for (let i = source.indexOf('{', start); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced function ${name}`);
}

const ctx = {
  URL,
  ACTIVE_SERVER: new URL('https://stale.example:8446'),
  PAGE_ORIGIN_URL: new URL('http://192.0.2.10:8446'),
  serverFallbackUsed: false,
  API_ORIGIN: 'https://stale.example:8446',
  API_PATH_PREFIX: '',
  CONFIG: { wsUrl: 'wss://stale.example:8446/ws', apiUrl: 'https://stale.example:8446' },
  serverApiBase: null,
  serverWsBase: null,
  dbg: () => {},
};
vm.createContext(ctx);
vm.runInContext(extract(client, 'serverApiBase'), ctx);
vm.runInContext(extract(client, 'serverWsBase'), ctx);
vm.runInContext(extract(client, 'switchToPageOrigin'), ctx);

ok(ctx.switchToPageOrigin() === true, 'switchToPageOrigin switches once');
assert.equal(ctx.CONFIG.wsUrl, 'ws://192.0.2.10:8446/ws');
assert.equal(ctx.CONFIG.apiUrl, 'http://192.0.2.10:8446');
assert.equal(ctx.API_ORIGIN, 'http://192.0.2.10:8446');
checks += 3;
ok(ctx.switchToPageOrigin() === false, 'it never switches twice (no ping-pong)');
ok(ctx.serverFallbackUsed === true, 'the fallback is recorded as used');

// Same origin already -> nothing to do (fresh state).
const same = vm.createContext({
  URL,
  ACTIVE_SERVER: new URL('http://192.0.2.10:8446'),
  PAGE_ORIGIN_URL: new URL('http://192.0.2.10:8446'),
  serverFallbackUsed: false,
  API_ORIGIN: 'http://192.0.2.10:8446',
  API_PATH_PREFIX: '',
  CONFIG: { wsUrl: 'ws://192.0.2.10:8446/ws', apiUrl: 'http://192.0.2.10:8446' },
  dbg: () => {},
});
vm.runInContext(extract(client, 'serverApiBase'), same);
vm.runInContext(extract(client, 'serverWsBase'), same);
vm.runInContext(extract(client, 'switchToPageOrigin'), same);
ok(same.switchToPageOrigin() === false,
   'no switch when the page origin is the backend in use');

// ---- 4. mini-admin has the same control ---------------------------------
ok(/BACKEND_KEY/.test(mini) && /setServerUrl\(/.test(mini),
   'mini-admin keeps its editable backend field');
ok(/localStorage\.removeItem\(BACKEND_KEY\)/.test(mini),
   'mini-admin can clear a stale saved backend');

console.log('backend address checks passed');
console.log(`${checks} checks passed`);
