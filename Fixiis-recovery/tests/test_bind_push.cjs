// No dependencies: node tests/test_bind_push.cjs
//
// Page bindings with values at push time. The rule for the whole feature:
//
//   * a binding is saved ONCE on the page (name + what to find + default)
//   * when the page is pushed, BOTH panels ask for the value
//   * the visitor gets the page with that value in it
//   * the stored HTML is never edited
//
// This checks the wiring end to end at the source level (the rewriting engine
// itself is covered by tests/test_page_bindings.py).
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
const mini = read('mini-admin.html');
const admin = read('Admin.html');
const client = read('client.html');
const store = read('lpv_store.py');
const engine = read('page_bindings.py');

// ---- the engine exists and is what the store uses -------------------------
ok(/def apply_bindings\(/.test(engine), 'engine exposes apply_bindings()');
ok(/def fields_for\(/.test(engine), 'engine exposes fields_for()');
ok(/MATCH_KINDS = \("text", "selector", "regex"\)/.test(engine), 'three matchers, text first');
ok(/import page_bindings/.test(store) || /from page_bindings import/.test(store),
   'lpv_store validates through the engine');

// ---- the stored page is never modified ------------------------------------
// Substitution must happen on the bytes being served, never at save time.
ok(/def read_page_html/.test(store), 'pages are read from disk for serving');
ok(!/def set_page_bindings[\s\S]{0,600}?write_page_html/.test(store),
   'saving bindings never rewrites the stored HTML');

// ---- the push URL carries the values --------------------------------------
ok(/def _lpv_page_url\(page_id: str, bind_token: Optional\[str\] = None\)/.test(api),
   '_lpv_page_url takes a binding token');
const urlFn = api.slice(api.indexOf('def _lpv_page_url('), api.indexOf('async def _send_to_client('));
ok(/\?b=\{bind_token\}/.test(urlFn), 'the token is encoded into the URL');
ok((urlFn.match(/query/g) || []).length >= 3, 'every URL shape appends the token');
ok(/return relative_url \+ query/.test(urlFn), 'the relative (same-origin) URL carries it too');

// Token storage is scoped per page, so a token for page A cannot rewrite B.
ok(/def _bind_token_for\(/.test(api), 'tokens are minted');
ok(/"page_id": page_id/.test(api.slice(api.indexOf('_BIND_RENDERS'), api.indexOf('def _bind_values_for'))),
   'a token remembers which page it belongs to');
ok(/def _bind_values_for\(page_id/.test(api), 'tokens are resolved per page');

// ---- the html route applies them -----------------------------------------
ok(/async def lpv_archive_html\([^)]*b: Optional\[str\]/.test(api),
   'the served-HTML route accepts ?b=');
ok(/_bind_values_for\(page_id, b\)/.test(api), 'the route looks the values up');
ok(/_apply_page_bindings\(page, html, _bind_values_for\(page_id, b\)\)/.test(api),
   'the route renders with those values');

// ---- both push paths carry values ----------------------------------------
ok(/"bindings": sorted\(bind_values\)/.test(api), 'the push ack reports what was applied');
ok(/bind_values = _clean_bind_values\(data\.get\("bindings"\)\)/.test(api),
   'the websocket push reads binding values');
ok(/bindings=bindings or None/.test(api), 'workflow launches carry values');
ok(/_bind_values_for_page\(page_id, bindings\)/.test(api),
   'a workflow page only receives the values it declares');

// ---- mini panel: asks for the value where the page is chosen -------------
ok(/id="drBind"/.test(mini), 'the drawer has a place for the fields');
ok(/function paintBindFields/.test(mini), 'choosing a page paints its fields');
ok(/function readBindFields/.test(mini), 'typed values are collected');
ok(/msg\.bindings = values/.test(mini), 'the typed values are sent with the push');
ok(/data-bind="\$\{esc\(b\.name\)\}"/.test(mini), 'one input per named binding');
ok(/\.bind-inp\{font-size:14px\}/.test(mini), 'inputs are thumb-sized on a phone');
// Fields appear only for pages that have them — no empty boxes on every push.
ok(/if\(!fields\.length\)\{ box\.hidden = true/.test(mini), 'pages without fields stay unchanged');

// ---- full panel: binding editor understands name / match / value ---------
ok(/class="lpv-bind-name"/.test(admin), 'each saved binding has a name');
ok(/<option value="selector">element<\/option>/.test(admin), 'the matcher is selectable, "text" first');
ok(/lpv-bind-match\">\s*' \+\s*$/m.test(admin) || /value="text">text<\/option>/.test(admin),
   'text is the default matcher');
ok(/function lpvBindPreview/.test(admin), 'there is a preview before pushing');

// ---- full panel: push asks for the value --------------------------------
ok(/function lpvAskValues/.test(admin), 'pushing a page with fields asks for values');
ok(/const values = await lpvAskValues\(page\)/.test(admin), 'the push waits for the answer');
ok(/msg\.bindings = values/.test(admin), 'the answer is sent with the push');
// Cancel must not push.
ok(/if \(values === null\) return;/.test(admin), 'cancelling the dialog sends nothing');

// ---- the visitor's side already follows page_url ------------------------
ok(/page_url/.test(client), 'client.html loads the URL it is given (token included)');

// ---- no secrets / addresses introduced ----------------------------------
ok(!/209\.74\.83\.91/.test(engine + mini + admin), 'no production address in the feature files');

console.log('page bindings + push values: ' + checks + ' checks passed');
