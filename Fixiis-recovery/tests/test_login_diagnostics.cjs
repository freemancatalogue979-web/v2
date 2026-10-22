// No dependencies: node tests/test_login_diagnostics.cjs
//
// The panel used to answer every rejected sign-in with the same sentence,
// "Invalid credentials", while unlocking itself optimistically first. Both
// made a *wrong password* look like a broken app: people went hunting for
// network problems and never learned that one command resets the password.
//
// These checks pin the behaviour that replaced it:
//   * the server says WHY a sign-in was rejected and how to reset it;
//   * the panel waits for the server before unlocking;
//   * 401 / 429 / unreachable are three different messages;
//   * server warnings are never shown before login (they used to leak
//     "Failed login attempt for user: admin" onto the login screen).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');
const read = (f) => fs.readFileSync(path.join(root, f), 'utf8');

const api = read('api.py');
const admin = read('Admin.html');
const mini = read('mini-admin.html');

// ---- server: the rejection explains itself --------------------------------
ok(/reason, message = 'bad_password'/.test(api), 'wrong password is named as such');
ok(/reason, message = 'unknown_user'/.test(api), 'unknown user is named as such');
ok(/reason = 'hash_unusable'/.test(api), 'an unusable hash is named as such');
ok(/"hint": hint/.test(api), 'the 401 carries a hint');
ok(/def client_host\(request/.test(api), 'failures are counted per client address');
ok(/_LOGIN_FAILURES\.pop\(client_host\(request\), None\)/.test(api),
   'a successful sign-in clears the counter');
ok(/failures >= 2/.test(api), 'the reset command is offered from the second rejection');
ok(/python main\.py --set-password/.test(api), 'the hint names the reset command');

// 429 must be JSON: the panels parse JSON, so a plain-text 429 showed up
// as "Invalid credentials" and people retyped a correct password.
ok(/@app\.exception_handler\(RateLimitExceeded\)/.test(api), 'the rate limit has a JSON handler');
ok(/reason": "rate_limited"/.test(api), 'the 429 names the reason');
ok(/'Retry-After': '60'|"Retry-After": "60"/.test(api), 'the 429 says how long to wait');

// A broken hash has to be announced at startup, not only on a failed login.
ok(/def _hash_problem\(/.test(api), 'the hash is validated');
ok(/ADMIN_PASSWORD_HASH from %s is unusable \(%s\) - no /.test(api),
   'a broken hash is logged at startup');

// ---- bootstrap: --check and the banner state the login --------------------
const boot = read('bootstrap.py');
ok(/def is_usable_password_hash\(/.test(boot), 'hash validity has one definition');
ok(/def admin_login_summary\(/.test(boot), 'the login summary has one definition');
ok(/"admin login"/.test(boot), '--check prints an admin login row');
ok(/default password: admin/.test(boot), '--check names the built-in default');
ok(/--reset-password/.test(boot), '--check offers going back to the built-in login');
ok(/"login"|'login'/.test(boot) && /lines\.append\(f"    \{'login'/.test(boot),
   'the startup banner shows the login line');
ok(/--set-password/.test(boot), 'the summary points at the reset command');

// ---- Admin.html: ask first, then unlock -----------------------------------
ok(!/localStorage\.setItem\('admin_auth', 'authenticated'\);\s*\n\s*isLoggedIn = true;\s*\n\s*if \(loginOverlay\) loginOverlay\.classList\.add\('hidden'\)/.test(admin),
   'the panel no longer unlocks before the server answers');
ok(/if \(r\.ok && data && data\.token\) \{/.test(admin), 'unlock happens on a token');
ok(/r\.status === 401/.test(admin), '401 is its own path');
ok(/r\.status === 429/.test(admin), '429 is its own path');
ok(/const data = await r\.json\(\)\.catch\(\(\) => \(\{\}\)\);/.test(admin)
   && /data\.hint/.test(admin), 'the hint from the server is displayed');
ok(/Cannot reach the server at /.test(admin), 'an unreachable server says so, with the address');
ok(/function fail\(message, hint\)/.test(admin), 'rejections share one failure path');
ok(/Signing in\.\.\./.test(admin), 'the button shows progress');
ok(/login-hint/.test(admin), 'the hint line has its own styling');
ok(/overflow-wrap: anywhere/.test(admin), 'the hint wraps on a phone');

// warnings are admin-only now
ok(/\?token=' \+ encodeURIComponent\(jwt\)/.test(admin), 'the admin socket carries the token');
ok(/if \(!isLoggedIn\) return;/.test(admin), 'warnings are not toasted before login');

// ---- mini-admin.html: same three outcomes --------------------------------
ok(/j\.hint/.test(mini), 'mini panel shows the hint too');
ok(/Too many attempts/.test(mini), 'mini panel distinguishes throttling');
ok(/Cannot reach ' \+ \(SERVER_URL \? SERVER_URL\.host/.test(mini), 'mini panel names the host');
ok(/function withToken\(path\)/.test(mini), 'mini panel adds the token to its sockets');

// ---- the socket only streams warnings to an authenticated caller ---------
ok(/token = websocket\.query_params\.get\("token", ""\)/.test(api), 'the socket reads the token');
ok(/if authorised:/.test(api), 'the warnings frame is behind the check');

console.log('login diagnostics checks passed');
console.log(`${checks} checks passed`);
