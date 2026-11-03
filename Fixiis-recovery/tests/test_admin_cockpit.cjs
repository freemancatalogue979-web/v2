// No dependencies (jsdom optional): node tests/test_admin_cockpit.cjs
//
// Admin.html is drawn as a mission-control console: a near-black ops room
// with a hairline grid and scanlines, amber-lit console keys, ice-cyan
// instruments, a tab rail where every nav entry is a slanted key, every modal
// a corner-bracketed console dialog, a radar scope behind the live view, and
// Helvetica (oblique) across the chrome. None of that is allowed to cost
// anybody the panel they already had, so this file guards three things:
//
//   1. THE CONTRACT. Every id and class the panel's own scripts reach for is
//      still in the markup, and every tab still exists. Captured before the
//      reskin (tests/fixtures/admin_shell_contract.json); a rename is a bug,
//      not a redesign.
//   2. THE DESIGN. The skin is present, last in the cascade, and built from
//      the console's palette and shapes - not "roughly dark".
//   3. THE RULES IT MUST NOT BREAK. No network fetch of any kind for the look,
//      machine output stays upright and monospace, the art is decoration only
//      (aria-hidden, no pointer events), and motion is respectful.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');
const admin = fs.readFileSync(path.join(root, 'Admin.html'), 'utf8');
const contract = JSON.parse(fs.readFileSync(path.join(root, 'tests', 'fixtures', 'admin_shell_contract.json'), 'utf8'));

// The skin block, and the CSS text inside it.
const skinStart = admin.indexOf('<style id="shf-reactor-shell">');
ok(skinStart > 0, 'the console skin is a named block, not scattered edits');
const cssStart = admin.indexOf('>', skinStart) + 1;
const cssEnd = admin.indexOf('</style>', cssStart);
const css = admin.slice(cssStart, cssEnd);
const skin = admin.slice(skinStart, cssEnd);

// ---------------------------------------------------------------------------
// 1. The contract with the panel's own scripts
// ---------------------------------------------------------------------------
{
  const ids = new Set(
    Array.from(admin.matchAll(/\bid="([A-Za-z][\w.:-]*)"/g)).map((m) => m[1])
  );
  const gone = contract.ids.filter((id) => !ids.has(id) && id !== 'shf-font-face');
  ok(gone.length === 0, 'every id the panel had is still there (missing: ' + gone.join(', ') + ')');

  const classes = new Set();
  for (const m of admin.matchAll(/\bclass="([^"]+)"/g)) m[1].split(/\s+/).forEach((c) => c && classes.add(c));
  const goneClasses = contract.classes.filter((c) => !classes.has(c));
  ok(goneClasses.length === 0, 'no class was removed either (missing: ' + goneClasses.slice(0, 6).join(', ') + ')');
  const goneScript = contract.script_classes.filter((c) => !classes.has(c));
  ok(goneScript.length === 0,
    'including every class a script queries (missing: ' + goneScript.join(', ') + ')');

  for (const tab of contract.data_tabs) {
    ok(admin.includes('data-tab="' + tab + '"'), 'the ' + tab + ' tab still exists');
  }
  for (const sub of contract.data_display_sub) {
    ok(admin.includes('data-display-sub="' + sub + '"'), 'the ' + sub + ' sub-tab still exists');
  }
  ok(contract.data_tabs.length >= 13, 'all thirteen nav tabs are declared (' + contract.data_tabs.length + ')');
}

// ---------------------------------------------------------------------------
// 2. It is the console's design, not an approximation of "dark"
// ---------------------------------------------------------------------------
{
  // Last thing in the head, so it wins the cascade: that is what makes this a
  // reskin rather than a suggestion. (The panel's scripts carry their own
  // '</head>' strings, so the invariant is checked from the skin forward.)
  const headEnd = admin.indexOf('</head>', cssEnd);
  ok(headEnd > cssEnd, 'the head closes after the skin');
  const between = admin.slice(cssEnd + '</style>'.length, headEnd);
  ok(between.trim() === '',
    'nothing follows the skin but the end of the head (found: ' + JSON.stringify(between.trim().slice(0, 60)) + ')');
  const stylesBefore = Array.from(admin.slice(0, skinStart).matchAll(/<style\b/g)).length;
  ok(stylesBefore > 0, 'and it comes after the panel\'s own stylesheets (' + stylesBefore + ')');

  // Palette: amber console light, ice instruments, live green - in dark and
  // in light.
  for (const token of ['--mc-void', '--mc-amber', '--mc-ice', '--mc-green', '--mc-text']) {
    ok(css.includes(token + ':'), 'the palette declares ' + token);
  }
  ok(/:root\s*\{[\s\S]*?--mc-amber:\s*#ffb02e/.test(css), 'the lit key is the console amber');
  ok(/:root\s*\{[\s\S]*?--mc-ice:\s*#59dcff/.test(css), 'the instrument accent is ice cyan');
  ok(/\[data-theme="light"\]\s*\{[\s\S]*?--mc-amber:/.test(css),
    'the light theme is a real theme, not a broken dark one');
  ok(!/--mc-amber:\s*#ffb02e/.test(css.slice(css.indexOf('[data-theme="light"]'))),
    'and it does not reuse the dark amber');

  // Bevel-cut panels and slanted tab keys: the console's two strongest shapes.
  ok(/--mc-cut:\s*polygon\(/.test(css), 'a bevel cut is defined once as a token');
  ok(css.includes('var(--mc-cut)'), 'and the dialogs use it');
  ok(/--mc-tab-cut:\s*polygon\(/.test(css), 'a slanted tab key is defined once as a token');
  ok(/\.top-tab\s*\{[\s\S]{0,1200}clip-path:\s*var\(--mc-tab-cut\)/.test(css), 'and every nav tab is cut with it');

  // The tab rail: uppercase oblique keys, the active one lit amber.
  ok(/\.top-tab\s*\{[\s\S]{0,1200}text-transform:\s*uppercase/.test(css), 'tabs are labelled like console keys');
  ok(/\.top-tab\.active\s*\{[\s\S]{0,1200}#e08f00/.test(css), 'the active tab is the lit amber key');
  ok(/\.top-tab\.active\s*\{[\s\S]{0,1200}box-shadow/.test(css), 'and it glows');
  ok(/\.display-subtab\.active[\s\S]{0,800}--mc-amber/.test(css), 'the segmented sub-tabs light up too');

  // Every modal is a console dialog: bevel cut, corner brackets, header lamp,
  // lit primary key.
  ok(/\.lpv-vals-modal\s*\{[\s\S]{0,1200}clip-path:\s*var\(--mc-cut\)/.test(css) && css.includes('.pcm-modal,'), 'the capture dialog is bevel cut');
  ok(/\.pcm-modal::before[\s\S]{0,1200}border-top:\s*1px solid var\(--mc-amber\)/.test(css), 'dialogs carry corner brackets');
  ok(/\.pcm-modal-title::before[\s\S]{0,800}animation:\s*mc-blink/.test(css), 'the dialog header lamp blinks');
  ok(/\.pcm-modal-btn\.primary[\s\S]{0,800}#e08f00/.test(css), 'the primary action is the lit amber key');
  for (const sel of ['.login-modal', '.klg-modal', '.lpv-modal', '.lpv-bind-modal', '.profile-modal']) {
    ok(css.includes(sel + ','), 'the dialog language covers ' + sel);
  }
  ok(/\.login-overlay[\s\S]{0,1200}backdrop-filter/.test(css), 'overlays are scrimmed and blurred');

  // The instruments the console shows.
  ok(/\.logo-icon[\s\S]{0,1200}border-radius:\s*50%/.test(css), 'the header mark is a round reticle');
  ok(/\.placeholder-icon[\s\S]{0,1200}border-radius:\s*50%/.test(css), 'the empty state is an instrument ring');
  ok(/\.placeholder-icon::before[\s\S]{0,700}border:\s*1px dashed/.test(css), 'with a dashed inner ring');
  ok(/#screenshot-status[\s\S]{0,800}border-radius:\s*999px/.test(css),
    'the stream status is the reference pill');
  ok(/\.screenshot-panel::before[\s\S]{0,1200}border:/.test(css), 'the stage has corner brackets');
  ok(/\.screenshot-meta[\s\S]{0,1200}clip-path:\s*polygon/.test(css),
    'the readout strip is bevel cut like the reference');
  ok(/@keyframes mc-spin/.test(css) && /@keyframes mc-blink/.test(css) && /@keyframes mc-sweep/.test(css),
    'the rings turn, the lamps blink and the radar sweeps');

  // The stage: dot field, scanlines, and an inline radar scope.
  ok(/\.lx-stage[\s\S]{0,1200}radial-gradient\(rgba\(89, 220, 255/.test(css),
    'the stage carries the dot field');
  ok(/\.lx-stage::before[\s\S]{0,800}repeating-linear-gradient\(0deg, rgba\(255, 176, 46/.test(css),
    'and the scanlines');
  ok(/class="mc-radar"/.test(admin) && /viewBox="0 0 600 600"/.test(admin),
    'the radar scope is drawn inline');
  ok(/preserveAspectRatio="xMidYMid slice"/.test(admin), 'and it fills the stage');
  ok(/\.mc-radar \.mc-sweep[\s\S]{0,700}animation:\s*mc-sweep/.test(css), 'the sweep is animated in CSS');
  const radar = admin.slice(admin.indexOf('<svg class="mc-radar"'),
                            admin.indexOf('</svg>', admin.indexOf('<svg class="mc-radar"')) + 6);
  const radarShapes = (radar.match(/<(?:circle|path)\b/g) || []).length;
  ok(radarShapes >= 15, 'the scope is real artwork (' + radarShapes + ' shapes)');
  const grads = (radar.match(/<(?:linear|radial)Gradient\b/g) || []).length;
  ok(grads >= 2, 'with a glow and a sweep gradient (' + grads + ')');
}

// ---------------------------------------------------------------------------
// 3. Helvetica, oblique - and where it must not be
// ---------------------------------------------------------------------------
{
  ok(/font-style:\s*italic/.test(css), 'the chrome is set oblique');
  ok(!/'Helvetica Neue'|"Helvetica Neue"/.test(css.slice(0, css.indexOf('font-style'))),
    'the family is spelled unquoted (a quoted family in a JS string has broken this panel before)');
  ok(/font-family:\s*var\(--mc-font\)/.test(css), 'one family token');

  // Machine output: upright, monospace, and it has to out-specify the panel's
  // own `* { font-family: … !important }` reset.
  ok(/textarea, pre, code[\s\S]{0,1200}font-family:\s*var\(--mc-mono\) !important/.test(css),
    'the terminal, the logs and typed prose stay monospace');
  ok(/font-style:\s*normal !important/.test(css), 'and upright');

  ok(!/Roboto Slab/.test(admin), 'the panel asks for no other house face');
  ok(!/@font-face\s*\{/.test(admin), 'and carries no embedded font payload');
}

// ---------------------------------------------------------------------------
// 4. The look costs nothing at runtime
// ---------------------------------------------------------------------------
{
  // No image, font, or stylesheet fetched for the design.
  const urls = Array.from(skin.matchAll(/url\((['"]?)([^)'"]*)\1\)/g)).map((m) => m[2]);
  ok(urls.length === 0, 'the skin fetches nothing (found: ' + urls.slice(0, 3).join(', ') + ')');
  ok(!/https?:\/\//.test(skin), 'no absolute URL anywhere in the skin');
  const radar = admin.slice(admin.indexOf('<svg class="mc-radar"'),
                            admin.indexOf('</svg>', admin.indexOf('<svg class="mc-radar"')) + 6);
  ok(!/<img\b/i.test(radar), 'the scope is drawn, not loaded');
  ok(!/<image\b/i.test(radar), 'and it embeds no raster either');
  ok(/class="mc-radar"[^>]*aria-hidden="true"/.test(admin), 'the scope is hidden from assistive tech');
  ok(/pointer-events:\s*none/.test(css), 'and the art never takes a pointer event');
  ok(/class="lx-stage" aria-hidden="true"/.test(admin), 'the stage backdrop is hidden too');

  // The art's gradient ids live in the same document as 400-odd panel ids, so
  // they are namespaced and unique - a duplicate id silently steals the fill.
  const artIds = Array.from(radar.matchAll(/id="([^"]+)"/g)).map(m => m[1]);
  ok(artIds.length > 0, 'the art names its gradients');
  ok(artIds.every(id => id.startsWith('mc')), 'and namespaces every one of them');
  ok(new Set(artIds).size === artIds.length, 'none of them repeats');
  const docIds = Array.from(admin.matchAll(/\sid="([^"]+)"/g)).map((m) => m[1]);
  const stolen = artIds.filter(id => docIds.filter(d => d === id).length > 1);
  ok(stolen.length === 0, 'no id is defined twice in the page (found: ' + stolen.join(', ') + ')');

  // Motion.
  ok(/@media \(prefers-reduced-motion: reduce\)/.test(css), 'reduced motion is honoured');
  const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
  ok(/\.logo-icon/.test(reduced) && /animation:\s*none !important/.test(reduced),
    'the reticle stops turning');
  ok(/\.placeholder-icon::after/.test(reduced), 'the instrument ring stops too');
  ok(/\.mc-radar \.mc-sweep/.test(reduced), 'and the radar stops sweeping');

  // Responsive: the scope is the first thing to go on a small screen.
  const small = css.slice(css.indexOf('@media (max-width: 1100px)'));
  ok(/\.mc-radar\s*\{\s*display:\s*none/.test(small), 'the scope is dropped on narrow screens');
  ok(/@media \(max-width: 760px\)/.test(css), 'and phones get their own pass');
}

// ---------------------------------------------------------------------------
// 5. Paint order: the rails sit below the content, the art below everything
// ---------------------------------------------------------------------------
{
  ok(/\.sidebar\s*\{[\s\S]{0,800}z-index:\s*1/.test(css), 'the left rail is a layer below the content');
  ok(/\.right-sidebar\s*\{[\s\S]{0,800}z-index:\s*1/.test(css), 'and so is the right rail');
  ok(/\.sidebar \.client-list\s*\{\s*position:\s*relative;\s*z-index:\s*3/.test(css),
    'but the session cards stay readable on top');
  ok(/\.mc-radar\s*\{[\s\S]{0,800}pointer-events:\s*none/.test(css), 'the scope never intercepts a click');
  ok(/\.lx-stage\s*\{[\s\S]{0,800}pointer-events:\s*none/.test(css), 'and neither does the backdrop');
}

// ---------------------------------------------------------------------------
// 6. The stylesheet actually parses (needs jsdom; skipped otherwise)
// ---------------------------------------------------------------------------
{
  let JSDOM = null;
  try { JSDOM = require('jsdom').JSDOM; } catch (e) { /* optional */ }
  if (!JSDOM) {
    try { JSDOM = require('/tmp/jsd/node_modules/jsdom').JSDOM; } catch (e2) { /* optional */ }
  }
  if (JSDOM) {
    const dom = new JSDOM(admin, { pretendToBeVisual: true });
    const doc = dom.window.document;
    const sheet = doc.getElementById('shf-reactor-shell').sheet;
    ok(sheet && sheet.cssRules.length > 100,
      'the whole skin parses (' + (sheet ? sheet.cssRules.length : 0) + ' rules)');
    ok(/@media/.test(Array.from(sheet.cssRules).map((r) => r.cssText).join('').slice(0, 40000)),
      'including its media queries');

    const style = (sel, prop) => {
      const el = doc.querySelector(sel);
      return el ? dom.window.getComputedStyle(el)[prop] : null;
    };
    ok(style('body', 'fontStyle') === 'italic', 'the panel computes as oblique');
    ok(style('.logo-text', 'fontStyle') === 'italic', 'the title is oblique');
    ok(style('textarea', 'fontStyle') === 'normal', 'a textarea is upright');
    ok(style('.top-tab', 'fontStyle') === 'italic', 'the nav is oblique');
    ok(/Helvetica/.test(style('body', 'fontFamily') || ''), 'and everything is Helvetica');
    ok(!!doc.querySelector('.main-app'), 'the shell is intact');
    ok(!!doc.querySelector('svg.mc-radar'), 'the radar scope is in the stage');
  } else {
    console.log('  (jsdom missing: cascade checks skipped)');
  }
}

console.log('admin console: ' + checks + ' checks passed');
