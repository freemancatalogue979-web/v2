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
  ok(/state\.proxyPrefix \+ encTarget/.test(body), 'iframe src is proxyPrefix + encoded target');
  ok(/encodeURIComponent\(url\)\s*\.replace\(%2F/.test(body.replace(/\s/g,' ')) || /encodeURIComponent\(url\)/.test(body),
    'target is URI-encoded so its own ?query&#fragment stays inside the /m/ path (no upstream 400)');
  ok(body.includes('%2F') && body.includes('%3A'), 'slashes and colons restored (matches server quote(safe="/:")) so <base> resolves correctly');
  ok(/'\?sid=' \+ encodeURIComponent\(state\.sessionId/.test(body), 'carries sid so the proxy uses the persistent context cookies');
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

console.log('proxy mirror client: %d checks passed', checks);
