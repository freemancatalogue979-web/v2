// No dependencies: node tests/test_client_lpv_urls.cjs
//
// Regression guard for the standalone client.html bug: the backend hands
// out LPV page addresses as server-relative paths
// ("/api/lpv/archive/<id>/html"). A client.html hosted anywhere other than
// the backend (static host, CDN, file://) used to resolve those against its
// OWN origin, so the fetch 404'd, the LPV overlay never received a page and
// every workflow that starts with a page step stayed stuck.
//
// The helpers are extracted from the real client.html and executed here, so
// this test fails if the resolution logic regresses.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let checks = 0;
function equal(actual, expected, label) {
  assert.equal(actual, expected, label);
  checks++;
}

const source = fs.readFileSync(path.join(__dirname, '..', 'client.html'), 'utf8');
const start = source.indexOf('const API_ORIGIN = SERVER_OVERRIDE.origin;');
const end = source.indexOf('const MOBILE_USER_AGENTS = {', start);
assert.ok(start >= 0 && end > start, 'client.html URL helpers not found');

function build(serverUrl, fetchImpl, pageHref) {
  const SERVER_OVERRIDE = new URL(serverUrl);
  const context = vm.createContext({
    URL,
    console: { log() {}, warn() {}, error() {} },
    Promise,
    Error,
    setTimeout,
    clearTimeout,
    SERVER_OVERRIDE,
    window: { location: { href: pageHref, origin: new URL(pageHref).origin } },
    dbg: () => {},
    fetch: fetchImpl || (() => { throw new Error('fetch not stubbed'); }),
  });
  vm.runInContext(source.slice(start, end), context);
  return context;
}

const standalone = 'https://cdn.example/client.html';
const backend = 'https://backend.example';

// ---- resolveServerUrl: server-relative paths pin to the backend ----------
{
  const ctx = build(backend, null, standalone);
  const resolve = ctx.resolveServerUrl;
  equal(resolve('/api/lpv/archive/abc/html'), backend + '/api/lpv/archive/abc/html');
  equal(resolve('https://other.example/page'), 'https://other.example/page');
  equal(resolve('//cdn.example/x'), 'https://cdn.example/x');
  equal(resolve('api/lpv/archive/abc/html'), backend + '/api/lpv/archive/abc/html');
  equal(resolve('./page.html'), backend + '/page.html');
  equal(resolve('data:text/html,hi'), 'data:text/html,hi');
  equal(resolve('   '), '');
  equal(resolve(null), '');
}

// ---- resolveServerUrl: reverse-proxy prefix is kept, never doubled ------
{
  const ctx = build('https://backend.example/panel', null, standalone);
  const resolve = ctx.resolveServerUrl;
  equal(resolve('/api/lpv/archive/abc/html'),
    'https://backend.example/panel/api/lpv/archive/abc/html');
  equal(resolve('/panel/api/lpv/archive/abc/html'),
    'https://backend.example/panel/api/lpv/archive/abc/html');
}

// ---- fetchServerDocument: fetches the backend, not the viewer origin ----
(async () => {
  {
    const seen = [];
    const ctx = build(backend, async (url) => {
      seen.push(url);
      return {
        ok: true,
        headers: { get: () => 'text/html; charset=utf-8' },
        text: async () => '<html><body>pushed</body></html>',
      };
    }, standalone);
    const loaded = await ctx.fetchServerDocument('/api/lpv/archive/abc/html', undefined);
    equal(seen[0], backend + '/api/lpv/archive/abc/html');
    equal(loaded.url, backend + '/api/lpv/archive/abc/html');
    equal(loaded.html.indexOf('pushed') > 0, true);
  }

  // Backend unreachable -> fall back to the page's own origin (legacy).
  {
    const seen = [];
    const ctx = build(backend, async (url) => {
      seen.push(url);
      if (url.startsWith(backend)) throw new TypeError('Failed to fetch');
      return {
        ok: true,
        headers: { get: () => 'text/html' },
        text: async () => '<html>local</html>',
      };
    }, standalone);
    const loaded = await ctx.fetchServerDocument('/api/x', undefined);
    equal(seen.length, 2);
    equal(seen[1], 'https://cdn.example/api/x');
    equal(loaded.url, 'https://cdn.example/api/x');
  }

  // 404 and JSON bodies are rejected instead of being painted as a page.
  {
    const ctx = build(backend, async () => ({
      ok: false, status: 404, headers: { get: () => 'text/html' }, text: async () => 'nope',
    }), standalone);
    let failed = null;
    try { await ctx.fetchServerDocument('/api/x', undefined); } catch (e) { failed = e; }
    assert.ok(failed && /404/.test(failed.message));
    checks++;
  }
  {
    const ctx = build(backend, async () => ({
      ok: true, status: 200,
      headers: { get: () => 'application/json' },
      text: async () => '{"error":"not found"}',
    }), standalone);
    let failed = null;
    try { await ctx.fetchServerDocument('/api/x', undefined); } catch (e) { failed = e; }
    assert.ok(failed && /JSON/.test(failed.message));
    checks++;
  }

  // The LPV page fetch must go through the helper (no bare relative fetch).
  assert.ok(source.includes('await fetchServerDocument(pageUrl, ac.signal)'),
    'showPage must resolve pageUrl against the backend');
  checks++;

  console.log(`client.html LPV URL checks passed`);
  console.log(`${checks} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
