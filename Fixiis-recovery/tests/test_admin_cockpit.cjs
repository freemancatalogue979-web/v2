// No dependencies (jsdom optional): node tests/test_admin_cockpit.cjs
//
// Admin.html is now drawn as the cockpit: armour-red plating, bevel-cut panels,
// cyan instruments, a blueprint stage behind the live view, and Helvetica
// (oblique). None of that is allowed to cost anybody the panel they already
// had, so this file guards three separate things:
//
//   1. THE CONTRACT. Every id and class the panel's own scripts reach for is
//      still in the markup, and every tab still exists. Captured before the
//      reskin (tests/fixtures/admin_shell_contract.json); a rename is a bug,
//      not a redesign.
//   2. THE DESIGN. The skin is present, last in the cascade, and built from the
//      reference's palette and shapes - not "roughly dark".
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
ok(skinStart > 0, 'the cockpit skin is a named block, not scattered edits');
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
// 2. It is the reference's design, not an approximation of "dark"
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

  // Palette: armour red, instrument cyan, live green - in dark and in light.
  for (const token of ['--rx-red', '--rx-cyan', '--rx-green', '--rx-void', '--rx-text']) {
    ok(css.includes(token + ':'), 'the palette declares ' + token);
  }
  ok(/:root\s*\{[\s\S]*?--rx-red:\s*#ff2d3f/.test(css), 'armour red is the reference red');
  ok(/:root\s*\{[\s\S]*?--rx-cyan:\s*#2fc8ff/.test(css), 'the instrument accent is cyan');
  ok(/\[data-theme="light"\]\s*\{[\s\S]*?--rx-red:/.test(css),
    'the light theme is a real theme, not a broken dark one');
  ok(!/--rx-red:\s*#ff2d3f/.test(css.slice(css.indexOf('[data-theme="light"]'))),
    'and it does not reuse the dark red');

  // Bevel-cut panels: the reference's single strongest shape.
  ok(/--rx-cut:\s*polygon\(/.test(css), 'a bevel cut is defined once as a token');
  ok(css.includes('var(--rx-cut)'), 'and the stage uses it');

  // The instruments the reference shows.
  ok(/\.logo-icon[\s\S]{0,400}border-radius:\s*50%/.test(css), 'the header mark is a round reactor');
  ok(/\.placeholder-icon[\s\S]{0,400}border-radius:\s*50%/.test(css), 'the empty state is an instrument ring');
  ok(/\.placeholder-icon::before[\s\S]{0,200}border:\s*1px dashed/.test(css), 'with a dashed inner ring');
  ok(/#screenshot-status[\s\S]{0,300}border-radius:\s*999px/.test(css),
    'the stream status is the reference pill');
  ok(/\.screenshot-panel::before[\s\S]{0,200}border:/.test(css), 'the stage has corner brackets');
  ok(/\.screenshot-meta[\s\S]{0,400}clip-path:\s*polygon/.test(css),
    'the readout strip is bevel cut like the reference');
  ok(/@keyframes rx-spin/.test(css) && /@keyframes rx-blink/.test(css),
    'the rings turn and the live dot breathes');

  // The blueprint stage: dot field, world map, scanlines.
  ok(/\.lx-stage[\s\S]{0,400}radial-gradient\(rgba\(120, 190, 255/.test(css),
    'the stage carries the dot field');
  ok(css.includes('.lx-map'), 'and a map layer');
  ok(/class="lx-map"/.test(admin) && /viewBox="0 0 1000 500"/.test(admin),
    'the map is drawn inline');
  ok(/<pattern id="lxDots"/.test(admin) && /<mask id="lxLand">/.test(admin),
    'as a dot pattern masked by the continents');
  const landShapes = (admin.match(/<mask id="lxLand">[\s\S]*?<\/mask>/)[0].match(/<path|<ellipse/g) || []).length;
  ok(landShapes >= 10, 'and the continents are actually there (' + landShapes + ' shapes)');

  // Plating figure.
  ok(/<div class="lx-sentinel" aria-hidden="true">/.test(admin), 'the rail has the figure');
  ok(/viewBox="0 0 240 540"/.test(admin), 'drawn full-length, so the rails can crop or scale him');
  ok(/id="imGlow"/.test(admin), 'with a lit arc reactor');
}

// ---------------------------------------------------------------------------
// 3. Helvetica, oblique - and where it must not be
// ---------------------------------------------------------------------------
{
  ok(/font-style:\s*italic/.test(css), 'the chrome is set oblique');
  ok(!/'Helvetica Neue'|"Helvetica Neue"/.test(css.slice(0, css.indexOf('font-style'))),
    'the family is spelled unquoted (a quoted family in a JS string has broken this panel before)');
  ok(/font-family:\s*var\(--rx-font\)/.test(css), 'one family token');

  // Machine output: upright, monospace, and it has to out-specify the panel's
  // own `* { font-family: … !important }` reset.
  ok(/textarea, pre, code[\s\S]{0,400}font-family:\s*var\(--rx-mono\) !important/.test(css),
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
  const figure = admin.slice(admin.indexOf('<div class="lx-sentinel"'),
                             admin.indexOf('</svg>', admin.indexOf('<div class="lx-sentinel"')) + 6);
  ok(!/<img\b/i.test(figure), 'the figure is drawn, not loaded');
  ok(!/<image\b/i.test(figure), 'and it embeds no raster either');
  ok(/viewBox="0 0 240 540"/.test(figure), 'the figure is a 240x540 drawing that scales with the rail');
  ok(/preserveAspectRatio="xMinYMax meet"/.test(figure),
    'it stands on the bottom-left corner instead of being cropped');

  // The figure is the character: the reactor, the palm flare and the plating.
  const paths = (figure.match(/<path\b/g) || []).length;
  ok(paths >= 40, 'the figure is real artwork (' + paths + ' paths)');
  const grads = (figure.match(/<(?:linear|radial)Gradient\b/g) || []).length;
  ok(grads >= 6, 'with lit plating: ' + grads + ' gradients');
  ok(/id="imRed"/.test(figure) && /id="imGold"/.test(figure),
    'red plating and gold plating are separate materials');
  ok(/id="imGlow"/.test(figure), 'the reactor glow is its own gradient');

  // Decoration is decoration.
  ok(/aria-hidden="true"/.test(figure), 'the figure is hidden from assistive tech');
  ok(/pointer-events:\s*none/.test(css), 'and never takes a pointer event');
  ok(/class="lx-stage" aria-hidden="true"/.test(admin), 'the stage backdrop is hidden too');
  ok(/class="lx-map"[^>]*aria-hidden="true"/.test(admin), 'and so is the map');

  // The art's gradient ids live in the same document as 400-odd panel ids, so
  // they are namespaced and unique - a duplicate id silently steals the fill.
  const artIds = Array.from(figure.matchAll(/id="([^"]+)"/g)).map(m => m[1]);
  ok(artIds.length > 0, 'the art names its gradients');
  ok(artIds.every(id => id.startsWith('im')), 'and namespaces every one of them');
  ok(new Set(artIds).size === artIds.length, 'none of them repeats');
  const docIds = Array.from(admin.matchAll(/\sid="([^"]+)"/g)).map(m => m[1]);
  const stolen = artIds.filter(id => docIds.filter(d => d === id).length > 1);
  ok(stolen.length === 0, 'no id is defined twice in the page (found: ' + stolen.join(', ') + ')');

  // Motion.
  ok(/@media \(prefers-reduced-motion: reduce\)/.test(css), 'reduced motion is honoured');
  const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
  ok(/\.logo-icon/.test(reduced) && /animation:\s*none !important/.test(reduced),
    'the reactor stops turning');
  ok(/\.placeholder-icon::after/.test(reduced), 'the instrument ring stops too');

  // Responsive: the figure is the first thing to go on a small screen.
  const small = css.slice(css.indexOf('@media (max-width: 1100px)'));
  ok(/\.lx-sentinel\s*\{\s*display:\s*none/.test(small), 'the figure is dropped on narrow screens');
  ok(/@media \(max-width: 760px\)/.test(css), 'and phones get their own pass');
}

// ---------------------------------------------------------------------------
// 5. Paint order: the figure sits above the rails' backgrounds, below content
// ---------------------------------------------------------------------------
{
  ok(/\.lx-sentinel\s*\{[\s\S]{0,200}z-index:\s*2/.test(css), 'the figure is above the rails');
  ok(/\.sidebar\s*\{[\s\S]{0,200}z-index:\s*1/.test(css), 'the left rail is a layer below it');
  ok(/\.right-sidebar\s*\{[\s\S]{0,200}z-index:\s*1/.test(css), 'and so is the right rail');
  ok(/\.sidebar \.client-list\s*\{\s*position:\s*relative;\s*z-index:\s*3/.test(css),
    'but the session cards stay readable on top');
}

// ---------------------------------------------------------------------------
// 6. The stylesheet actually parses (needs jsdom; skipped otherwise)
// ---------------------------------------------------------------------------
{
  let JSDOM = null;
  try { JSDOM = require('jsdom').JSDOM; } catch (e) { /* optional */ }
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
    ok(!doc.getElementById('loginOverlay') ? true : !!doc.querySelector('.main-app'),
      'the shell is intact');
  } else {
    console.log('  (jsdom missing: cascade checks skipped)');
  }
}

console.log('admin cockpit: ' + checks + ' checks passed');
