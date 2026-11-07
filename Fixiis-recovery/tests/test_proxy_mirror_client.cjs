// node tests/test_proxy_mirror_client.cjs
// Pins the client side of the proxy mirror ("redirect from browser to
// client"): when the server advertises proxy_prefix, the iframe is redirected
// to /m/<url> on our origin and the live page drives the session itself — the
// capture mirror (full_document/dom_patch/resync) is bypassed, with a
// load-timeout fallback back to it.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
let checks = 0;
function ok(cond, label) { assert.ok(cond, label); checks++; }

const client = fs.readFileSync(path.join(__dirname, '..', 'client.html'), 'utf8');

// session_info adopts the advertised prefix
ok(/case 'session_info':[\s\S]{0,200}?message\.proxy_prefix/.test(client),
  'session_info adopts message.proxy_prefix');

// page_transition redirects the iframe instead of waiting for a capture
{
  const i = client.indexOf("case 'page_transition':");
  const body = client.slice(i, i + 700);
  ok(/state\.proxyPrefix && message\.url/.test(body) && /openProxiedPage\(message\.url\)/.test(body),
    'page_transition redirects the iframe to the proxied url in proxy mode');
}

// openProxiedPage builds the same-origin URL with the session id
{
  const i = client.indexOf('function openProxiedPage(url)');
  assert.ok(i > 0, 'openProxiedPage exists');
  const body = client.slice(i, i + 4600);
  ok(/state\.proxyPrefix \+ \(encSid \? encSid \+ '\/' : ''\) \+ encTarget/.test(body),
    'iframe src is /m/<sid>/<encoded-target> — sid in the PATH, not a query param');
  ok(/encodeURIComponent\(url\)/.test(body) && body.includes('%2F') && body.includes('%3A'),
    'target is URI-encoded with slashes/colons restored (matches server quote(safe="/:")) so its ?query&#fragment stays in the path and <base> resolves correctly');
  ok(/encodeURIComponent\(state\.sessionId/.test(body),
    'sid is encoded into the first path segment so it survives <base> relative resolution (keeps the persistent-context cookies on every request)');
  ok(/f\.src = proxied/.test(body), 'assigns the proxied src to the iframe');
  ok(/state\.proxyMode = true/.test(body), 'proxyMode armed');
  ok(/u\.searchParams\.set\('url', url\)/.test(body) && /history\.replaceState/.test(body),
    'the outer ?url= follows the redirect (refresh keeps the page)');
  ok(/proxy_load_timeout/.test(body) && /state\.proxyPrefix = null/.test(body),
    'if the proxied page never loads, fall back to the capture mirror');
}

// capture path is suppressed while the live proxied page owns the iframe
ok(/case 'dom_patch':\s*if \(state\.proxyMode\) break;/.test(client),
  'dom_patch is a no-op in proxy mode');
{
  const i = client.indexOf("case 'full_document':");
  const body = client.slice(i, i + 500);
  ok(/if \(state\.proxyMode\)/.test(body) && /acceptGeneration/.test(body),
    'full_document only does bookkeeping in proxy mode');
}
ok(/function requestResync\(currentGen, reason\) \{\s*if \(state\.proxyMode\) return;/.test(client),
  'no resync storms against a live proxied page');

// remote-browser sync: in-proxy navigations are reported so the server can
// put the persistent browser on the same page
{
  const i = client.indexOf('function handleProxyFrameLoad()');
  assert.ok(i > 0, 'handleProxyFrameLoad exists');
  const body = client.slice(i, i + 1600);
  ok(/loc\.pathname\.indexOf\(state\.proxyPrefix\)/.test(body), 'load listener reads the proxied iframe location');
  ok(/rest\.slice\(slash \+ 1\)/.test(body), 'sid segment is stripped when decoding the real target');
  ok(/send\(\{ type: 'proxy_sync', url: target \}\)/.test(body), 'sends proxy_sync so the remote browser follows the client navigation');
  ok(/target === state\.targetUrl/.test(body), 'same-URL loads are ignored (no loop with the server-side sync)');
  ok(/f\.addEventListener\('load', handleProxyFrameLoad\)/.test(client), 'load listener attached to the mirror frame');
}

// native input in proxy mode: the WS click/key relay must NOT bind to the live iframe
ok(/function setupIframeEventListeners\(d\) \{\s*[^]*?if \(state\.proxyMode\) return;/.test(client),
  'setupIframeEventListeners is a no-op in proxy mode (the live page handles input itself through /m/)');

// page_transition from the remote browser must not reload the iframe when the
// URL is unchanged (proxy_sync already put the remote browser there)
{
  const i = client.indexOf("case 'page_transition':");
  const body = client.slice(i, i + 700);
  ok(/state\.proxyPrefix && message\.url && message\.url !== state\.targetUrl/.test(body),
    'page_transition redirects only on a REAL url change');
}

// profile-bound links: client.html must honor ?user_id= so a generated link
// attaches to the persistent profile (cookies/storage) it was crafted from
ok(/urlParams\.get\('user_id'\)/.test(client), 'client reads ?user_id= from the link');
ok(/state\.userId = userIdParam/.test(client), 'user_id param becomes the session profile identity');

console.log('proxy mirror client: %d checks passed', checks);
