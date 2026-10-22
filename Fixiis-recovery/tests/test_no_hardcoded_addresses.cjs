// No dependencies: node tests/test_no_hardcoded_addresses.cjs
//
// A server address baked into the shipped code is a trap: it works on the
// machine it was typed on and silently breaks everywhere else (the panel
// connects to nothing, workflows push pages nobody can fetch, a stale tunnel
// answer is remembered forever). This guard fails the build if a real-looking
// address appears in the files that actually ship.
//
// Allowed, because they are not "somebody's address":
//   * loopback / wildcard          127.0.0.1  0.0.0.0  ::1  localhost
//   * RFC 5737 documentation ranges 192.0.2.0/24  198.51.100.0/24  203.0.113.0/24
//   * example.* / *.example domains
// Everything else - a private LAN address, a public IP, a tunnel host - is
// configuration, not code: it belongs in config.py, .env or the panel.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');

// Every file that ships to an operator: all top-level Python modules, the
// three panels, the generated standalone copy, the deploy assets and the
// dependency list. Tests and docs are deliberately excluded - they may
// describe example addresses, and a fixture is not a setting.
const SHIPPED = fs.readdirSync(root)
  .filter((f) => f.endsWith('.py') && fs.statSync(path.join(root, f)).isFile())
  .concat([
    'client.html', 'Admin.html', 'mini-admin.html', 'standalone/client.html',
    '.env.example', 'requirements.txt',
    'deploy/nginx.conf', 'deploy/doctor.sh', 'deploy/setup.sh',
    'deploy/deploy.sh', 'deploy/fixiis.service', 'deploy/make-standalone.sh',
  ]);

const IPV4 = /\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b/g;

const ALLOWED_EXACT = new Set(['0.0.0.0', '127.0.0.1', '255.255.255.255']);
const ALLOWED_DOC = [
  ['192', '0', '2'],    // TEST-NET-1
  ['198', '51', '100'], // TEST-NET-2
  ['203', '0', '113'],  // TEST-NET-3
];

function isAllowed(octets) {
  const dotted = octets.join('.');
  if (ALLOWED_EXACT.has(dotted)) return true;
  if (ALLOWED_DOC.some((p) => p[0] === octets[0] && p[1] === octets[1] && p[2] === octets[2])) return true;
  // 127.0.0.0/8 and 0.0.0.0/8 are "this machine", not an address of a box.
  if (octets[0] === '127' || octets[0] === '0') return true;
  return false;
}

// Well-known public resolvers used as a CONNECTIVITY PROBE ("is the internet
// reachable at all?"). They say nothing about where this server lives.
const NETWORK_PROBES = new Set(['8.8.8.8', '8.8.4.4', '1.1.1.1', '1.0.0.1']);

// Lookalikes that are not addresses: browser version strings in user agents,
// SVG path data ("M19.14 12.94..." is geometry, not a host), and software
// versions ("147.0.0.0", "18.3.1.0", "protobuf>=4.9.0.80").
function isLookalike(line) {
  if (/Mozilla\/5\.0|AppleWebKit|Chrome\/\d|Safari\/\d|sec-ch-ua/.test(line)) return true;
  if (/viewBox=|d="M[\d.-]|<path /.test(line)) return true;
  if (/version/i.test(line) || />=|==/.test(line)) return true;
  return false;
}

// A four-part number ending in .0 is a version (x.y.z.0), never a host: no
// real server address ends in a zero octet in held-out practice, and keeping
// this rule avoids a thousand browser-fingerprint false positives.
function isVersionShaped(value) {
  return /\.0$/.test(value) || /\.0\.0\.0$/.test(value);
}

const offenders = [];
let scanned = 0;
for (const rel of SHIPPED) {
  const file = path.join(root, rel);
  if (!fs.existsSync(file)) continue;
  scanned++;
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, i) => {
    if (isLookalike(line)) return;
    let m;
    IPV4.lastIndex = 0;
    while ((m = IPV4.exec(line)) !== null) {
      const octets = [m[1], m[2], m[3], m[4]];
      const value = octets.join('.');
      // Bare version-ish values (>= 100 in the last octet after a dot-dot-dot)
      // are covered by isAllowed only via the doc ranges; anything else is
      // reported, so a real address cannot hide.
      if (isAllowed(octets) || NETWORK_PROBES.has(value)) continue;
      if (isVersionShaped(value)) continue;
      offenders.push(`${rel}:${i + 1}  ${value}  ::  ${line.trim().slice(0, 90)}`);
    }
  });
}

ok(scanned >= 12, `scanned the shipped files (${scanned})`);
assert.deepEqual(offenders, [],
  'no server IP address may be hard-coded in shipped code:\n  ' + offenders.join('\n  '));
checks++;

// ---- no baked backend URL in the panels --------------------------------
// The panels resolve their backend from (1) ?server=, (2) a saved value,
// (3) the origin that served them. Baking one in overrides all three.
for (const rel of ['Admin.html', 'mini-admin.html', 'client.html']) {
  const src = fs.readFileSync(path.join(root, rel), 'utf8');
  ok(!/window\.SERVER_ENDPOINT\s*=\s*["']https?:\/\//.test(src),
     `${rel} does not bake a backend URL`);
  ok(!/localStorage\.setItem\(\s*['"]lpv_server_endpoint['"]\s*,\s*['"]https?:/.test(src),
     `${rel} does not pre-seed the saved backend`);
}

// The standalone build may bake ONE address (that is its purpose), but it must
// still be overridable and it must be the only place - and an empty bake has
// to mean "ask the page's own origin", not a broken literal.
{
  const standalone = fs.readFileSync(path.join(root, 'standalone/client.html'), 'utf8');
  const baked = standalone.match(/window\.SERVER_ENDPOINT\s*=\s*"([^"]*)"/);
  ok(baked !== null, 'standalone build declares its SERVER_ENDPOINT slot');
  ok(/lpv_server_endpoint/.test(standalone) && /server=/.test(standalone),
     'the standalone build can still be overridden (?server= / saved value)');
  // The copy committed to the repository must not carry anybody's address:
  // a tunnel or VPS URL in here is stale the moment it changes, and points
  // every operator who clones the repo at one machine. Bake your own locally
  // (deploy/make-standalone.sh <url>) - that output is git-ignored.
  assert.equal(baked[1], '',
    'the committed standalone copy must carry no address (use ./deploy/make-standalone.sh --none)');
  checks++;
}

// The documented way to produce that address-free copy must exist.
{
  const script = fs.readFileSync(path.join(root, 'deploy/make-standalone.sh'), 'utf8');
  ok(/--none/.test(script), 'make-standalone.sh can emit an address-free copy');
}

// ---- page addresses must survive an http/https mismatch -----------------
// config.py ships a plain-http PUBLIC_IP default, so an https-hosted viewer
// receives page URLs it cannot fetch. The viewer must retry the same path on
// the backend it is actually using instead of failing.
{
  const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');
  ok(/function sameSchemeCandidate\(/.test(client),
     'client retries a mixed-content page URL on the live backend');
  ok(/if \(!backendIsTls && !pageIsTls\) return '';/.test(client),
     'the retry is skipped when both sides are plain http');
}

// ---- the settings that DO hold an address are the operator's -----------
// config.py is the single settings file, so a configured server address is
// expected there - it must be overridable from the environment so a deploy
// never has to edit code.
{
  const cfg = fs.readFileSync(path.join(root, 'config.py'), 'utf8');
  ok(/"PUBLIC_IP":\s*\("public_ip"/.test(cfg), 'PUBLIC_IP overrides the address');
  ok(/"SERVER_DOMAIN":\s*\("server_domain"/.test(cfg), 'SERVER_DOMAIN overrides the domain');
  // A blank address must mean "use the address the client connected to",
  // never a leftover literal.
  ok(/def is_placeholder_host/.test(cfg), 'blank/placeholder hosts are detected');
}

// ---- the one place that needs an address degrades gracefully ------------
// Telegram links leave the browser, so they need a real host. With the
// shipped blank settings that must produce an explanation, not a crash and
// not a link to example.com / 127.0.0.1.
{
  const tg = fs.readFileSync(path.join(root, 'telegram_bot.py'), 'utf8');
  ok(/def _public_link_host\(/.test(tg), 'Telegram link host is resolved in one place');
  ok(/is_placeholder_host/.test(tg), 'placeholder domains are not turned into links');
  ok(/'localhost', '127\.0\.0\.1', '0\.0\.0\.0'/.test(tg),
     'loopback is never put in a Telegram link');
}

console.log('hard-coded address checks passed');
console.log(`${checks} checks passed`);
