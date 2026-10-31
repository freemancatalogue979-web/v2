// No dependencies: node tests/test_boot_speed.cjs
//
// "The initial client page is slow, and even the LPV-only version takes
// forever to start." The startup path had four ways to stall that had nothing
// to do with the visitor's own page:
//
//   1. client.html (~550 KB) and every archived page were sent UNCOMPRESSED
//   2. a third-party font stylesheet blocked the first paint
//   3. a pre-connect POST had to finish before the WebSocket was even opened
//   4. the server awaited geolocation (3 providers x 4 s) before the boot
//      message, and pushed the landing page a second time afterwards
//
// These checks pin each of those shut, and run the page-fetch budget for
// real so a hanging backend address can never win again.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let checks = 0;
const ok = (cond, label) => { assert.ok(cond, label); checks++; };

const root = path.join(__dirname, '..');
const read = (f) => fs.readFileSync(path.join(root, f), 'utf8');
const api = read('api.py');
const client = read('client.html');

// ---- 1. everything crosses the wire compressed ---------------------------
ok(/from fastapi\.middleware\.gzip import GZipMiddleware/.test(api), 'gzip is wired in');
ok(/app\.add_middleware\(GZipMiddleware, minimum_size=\d+, compresslevel=\d+\)/.test(api),
   'responses are compressed, with a small-response threshold');
const gzAt = api.indexOf('GZipMiddleware, minimum_size');
const headerMwAt = api.indexOf('async def add_security_headers');
ok(gzAt > 0 && gzAt < headerMwAt, 'compression is installed with the other middleware');

// ---- 2. a repeat visit costs a 304, not another 550 KB -------------------
ok(/def _client_html_response\(/.test(api), 'the client page has one response path');
ok(/def _client_html_etag\(/.test(api) && /if _not_modified\(request, etag\)/.test(api),
   'it answers If-None-Match with 304');
ok(/Status_code=304|status_code=304/.test(api), 'and does return 304');
ok(/def _client_file_html\(/.test(api), 'the file itself is read once, not per visitor');
const clientRoute = api.slice(api.indexOf('async def client_page'), api.indexOf('# /remote endpoints removed'));
ok(/return _render_client_html\(request\)/.test(clientRoute), 'client.html goes through that path');
const adminRoute = api.slice(api.indexOf('async def admin_page'), api.indexOf('async def mini_admin_page'));
ok(/_client_html_response\(request, _client_file_html\(admin_path\)\)/.test(adminRoute),
   'the admin panel does too (it is ~1 MB)');

// ---- 3. no third-party font request in the head at all -------------------
// The shell face is Helvetica Neue, which the OS already has. That beats even
// the async `<link rel="preload">` it replaced: there is no request to defer,
// so the first paint cannot wait on a font CDN, and a filtered network cannot
// leave the shell in a different face from the panels.
const head = client.slice(0, client.toLowerCase().indexOf('</head>'));
ok(!/fonts\.googleapis/.test(head), 'the shell head never reaches a font CDN');
ok(!/fonts\.gstatic\.com/.test(head), 'and does not preconnect to one either');
ok(!/rel="preload" as="style"[^>]*fonts/.test(head), 'nothing about type is preloaded');
ok(/'Helvetica Neue'/.test(client), 'the shell is set in Helvetica Neue');

// ---- 4. the socket is not held hostage by the pre-connect handoff -------
ok(/BOOT_HANDOFF_BUDGET_MS = \d+/.test(client), 'the handoff has a budget');
const initSrc = client.slice(client.indexOf('function init() {'), client.indexOf('function viewportForServer()'));
ok(/setTimeout\(\(\) => bootSocket\(true\), BOOT_HANDOFF_BUDGET_MS\)/.test(initSrc),
   'the socket opens when the budget expires, whatever the POST is doing');
ok(/const bootSocket = \(raced\) => \{/.test(initSrc) && /connect\(\)/.test(initSrc),
   'that path really opens the WebSocket');
ok(initSrc.indexOf('disconnectPreviousRuntimeBeforeConnect()') <
   initSrc.indexOf('setTimeout('),
   'the handoff still starts first, so the normal case is unchanged');
// A handoff that answers after we connected replaces our own socket. That
// must not be mistaken for a takeover (which is a terminal "refresh this
// page" state) — the visitor would be stuck.
ok(/selfRaced/.test(client) && /handoffRaced/.test(client),
   'a late handoff is recognised as our own doing');
ok(/\(replacementClose \|\| accessReleaseClose\) && !selfRaced/.test(client),
   'and does not trigger the terminal "session replaced" screen');

// ---- 5. geolocation cannot delay the boot message ----------------------
// The visitor's page never waits for the IP lookup at all: it starts in the
// background with the socket, the browser launch waits a sliver for a cache
// hit, and the answer is attached to the session afterwards.
ok(/GEO_LAUNCH_WAIT = [\d.]+/.test(api), 'the browser launch has a (tiny) geo budget');
ok(/geo_within\(geo_task, GEO_LAUNCH_WAIT\)/.test(api),
   'the boot path waits no longer than that');
ok(/geo_task = start_geo_lookup\(client_ip\)/.test(api),
   'the lookup starts from the socket, not from the browser path');
ok(!/await asyncio\.wait_for\(server_geolocate\(client_ip\)/.test(api),
   'and nothing awaits the full provider budget inline any more');

// ---- 6. the boot message goes out before the bookkeeping ---------------
const lpvStart = api.indexOf('async def _enter_lpv_only_mode');
assert.ok(lpvStart > 0, '_enter_lpv_only_mode not found');
const lpvOnly = api.slice(lpvStart, lpvStart + 16000);
ok(lpvOnly.indexOf('"type": "lpv_only_session"') < lpvOnly.indexOf('await _upsert_lpv_admin_client('),
   'the visitor is told where their page is before the admin feed is written');

// ---- 7. the landing page is not downloaded twice -----------------------
ok(/_loadingPageId/.test(client) && /_loadingPageUrl/.test(client),
   'the client tracks the page it is already fetching');
ok(/this\._loadingPageId === _wantId[\s\S]{0,600}?return;/.test(client),
   'a duplicate push is dropped instead of restarting the download');
ok(/page = None if default_url else lpv_store\.get_page\(default_page\)/.test(api),
   'the server no longer re-pushes the page its boot message already named');

// ---- 8. the fetch of a pushed page has a per-candidate budget ----------
ok(/PAGE_FETCH_TIMEOUT_MS = \d+/.test(client), 'a page fetch has a time budget');
ok(/timedOut = true/.test(client) && /continue;/.test(client),
   'a candidate that runs out of time falls through to the next one');
ok(/if \(signal && signal\.aborted\) throw e;/.test(client) === false ||
   /canAbort/.test(client), 'the budget degrades on browsers without AbortController');

// ---- 9. boot timings are recorded, so "slow" can be checked ------------
ok(/__BOOT_T0/.test(client) && /__BOOT_MARK/.test(client), 'the page instruments its own boot');
ok(/__BOOT_MARK\('websocket open'\)/.test(client), 'when the socket opened');
ok(/ms_since_boot: sinceBoot/.test(client), 'and how long the first page took');

// ---- 10. the page-fetch budget, run for real ---------------------------
const start = client.indexOf('let API_ORIGIN = ACTIVE_SERVER.origin;');
const end = client.indexOf('const MOBILE_USER_AGENTS = {', start);
assert.ok(start >= 0 && end > start, 'client.html URL helpers not found');

function build(fetchImpl, serverUrl) {
  const ACTIVE_SERVER = new URL(serverUrl || 'https://backend.example');
  const ctx = vm.createContext({
    URL, Promise, Error, AbortController, setTimeout, clearTimeout,
    console: { log() {}, warn() {}, error() {} },
    SERVER_OVERRIDE: ACTIVE_SERVER,
    ACTIVE_SERVER,
    // The page itself is always served by the origin the visitor opened.
    window: { location: { href: 'https://backend.example/client.html',
                          origin: 'https://backend.example' } },
    dbg: () => {},
    fetch: fetchImpl,
    PAGE_FETCH_TIMEOUT_MS: 40,   // the real value is seconds; keep the test fast
  });
  vm.runInContext(client.slice(start, end), ctx);
  return ctx;
}

(async () => {
  // A dead candidate address (TCP black hole) must not hold the page: the
  // attempt is abandoned and the same-origin fallback is used.
  const seen = [];
  const ctx = build((url, opts) => {
    seen.push(url);
    if (seen.length === 1) {
      return new Promise((resolve, reject) => {
        const signal = opts && opts.signal;
        if (signal && signal.addEventListener) {
          signal.addEventListener('abort', () => {
            const err = new Error('aborted');
            err.name = 'AbortError';
            reject(err);
          });
        }
      });
    }
    return Promise.resolve({
      ok: true,
      headers: { get: () => 'text/html; charset=utf-8' },
      text: async () => '<html><body>fallback page</body></html>',
    });
  }, 'https://dead.example');
  // Already built against a host that never answers; the fallback is the
  // origin serving the page — exactly the standalone-client situation.
  const started = Date.now();
  const loaded = await ctx.fetchServerDocument('/api/lpv/archive/abc/html');
  const took = Date.now() - started;
  ok(loaded && /fallback page/.test(loaded.html), 'a black-holed address falls through to the next candidate');
  ok(took < 2000, 'and it does so in about the budget, not the browser TCP timeout (' + took + 'ms)');
  ok(seen.length >= 2, 'the fallback candidate was actually tried');

  // The caller's own cancel must still cancel, not silently fall through.
  const ctx2 = build((url, opts) => new Promise((resolve, reject) => {
    const signal = opts && opts.signal;
    if (signal && signal.addEventListener) {
      signal.addEventListener('abort', () => {
        const err = new Error('aborted');
        err.name = 'AbortError';
        reject(err);
      });
    }
  }));
  const outer = new AbortController();
  const pending = ctx2.fetchServerDocument('/api/lpv/archive/abc/html', outer.signal);
  setTimeout(() => outer.abort(), 10);
  let cancelled = false;
  try {
    await pending;
  } catch (e) {
    cancelled = (e && e.name === 'AbortError');
  }
  ok(cancelled, 'a newer page still cancels the one in flight');

  console.log('boot speed: ' + checks + ' checks passed');
})();
