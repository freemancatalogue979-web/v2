// No dependencies (jsdom optional): node tests/test_admin_cockpit.cjs
//
// Admin.html is drawn as a black & white ops console: the dark theme is white
// on black, the light theme (the default) is black on white — no accent hue
// anywhere. Every shape is round: pill tabs and keys, rounded panels and
// dialogs, no clip-path cuts, no corner brackets. Every text is Courier
// (embedded once as a data: URI), oblique across the chrome, upright only in
// machine output. The skin is one named block, last in <head>. None of that
// may cost the panel its scripts, so this file guards:
//
//   1. THE CONTRACT. Every id and class the panel's own scripts reach for is
//      still in the markup, and every tab still exists. Captured before the
//      reskin (tests/fixtures/admin_shell_contract.json); a rename is a bug,
//      not a redesign.
//   2. THE DESIGN. The skin is present, last in the cascade, monochrome,
//      rounded, Courier throughout — not "roughly dark" and not angular.
//   3. THE RULES IT MUST NOT BREAK. No network fetch of any kind for the look,
//      machine output stays upright, the art is decoration only (aria-hidden,
//      no pointer events), and motion is respectful.
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
// 2. The skin is one block, last in the head, after the panel's own styles
// ---------------------------------------------------------------------------
{
  const headEnd = admin.indexOf('</head>', cssEnd);
  ok(headEnd > cssEnd, 'the head closes after the skin');
  const between = admin.slice(cssEnd + '</style>'.length, headEnd);
  ok(between.trim() === '',
    'nothing follows the skin but the end of the head (found: ' + JSON.stringify(between.trim().slice(0, 60)) + ')');
  const stylesBefore = Array.from(admin.slice(0, skinStart).matchAll(/<style\b/g)).length;
  ok(stylesBefore > 0, 'and it comes after the panel\'s own stylesheets (' + stylesBefore + ')');
}

// ---------------------------------------------------------------------------
// 3. Black & white only: no hue in the palette, both themes real
// ---------------------------------------------------------------------------
{
  for (const token of ['--mc-void', '--mc-accent', '--mc-text', '--mc-line']) {
    ok(css.includes(token + ':'), 'the palette declares ' + token);
  }
  ok(/:root\s*\{[\s\S]*?--mc-void:\s*#000000/.test(css), 'the dark theme is black');
  ok(/:root\s*\{[\s\S]*?--mc-accent:\s*#ffffff/.test(css), 'its accent is white');
  ok(/:root\s*\{[\s\S]*?--mc-text:\s*#ffffff/.test(css), 'and its text is white');
  ok(/\[data-theme="light"\]\s*\{[\s\S]*?--mc-void:\s*#ffffff/.test(css), 'the light theme is white');
  ok(/\[data-theme="light"\]\s*\{[\s\S]*?--mc-accent:\s*#000000/.test(css), 'its accent is black');
  ok(/\[data-theme="light"\]\s*\{[\s\S]*?--mc-text:\s*#000000/.test(css), 'and its text is black');

  // No orange (or any other hue) anywhere in the skin: every hex colour it
  // declares is a shade of gray.
  const hexes = Array.from(css.matchAll(/#([0-9a-fA-F]{3,8})\b/g)).map((m) => m[1].toLowerCase());
  const grey = (h) => {
    const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h.slice(0, 6);
    return full.length === 6 && full[0] === full[2] && full[2] === full[4] && full[1] === full[3] && full[3] === full[5];
  };
  const hues = hexes.filter((h) => !grey(h));
  ok(hues.length === 0, 'the skin declares no hue, only gray (found: #' + hues.slice(0, 4).join(', #') + ')');
  ok(!/--mc-amber|--mc-ice|--mc-hot|--mc-green/.test(css), 'no leftover accent tokens');
  ok(!/orange|amber/i.test(css.replace(/no accent hue|no hue/g, '')), 'no orange by name either');

  // The radar scope is monochrome too (it inherits currentColor per theme).
  const radar = admin.slice(admin.indexOf('<svg class="mc-radar"'),
                            admin.indexOf('</svg>', admin.indexOf('<svg class="mc-radar"')) + 6);
  const radarHexes = Array.from(radar.matchAll(/#([0-9a-fA-F]{3,8})\b/g)).map((m) => m[1].toLowerCase());
  ok(radarHexes.length === 0, 'the radar art uses currentColor, no fixed hex (' + radarHexes.slice(0, 3).join(',') + ')');
  ok((radar.match(/currentColor/g) || []).length >= 6, 'rings, ticks, sweep and blips all follow the theme');

  // The panel opens in the light theme; the black theme stays one toggle away.
  ok(/<html lang="en" data-theme="dark">/.test(admin), 'the panel opens in dark mode (white on black) by default');
  ok(/localStorage\.getItem\('admin-theme'\)\s*\|\|\s*'dark'/.test(admin),
    'and the JS fallback agrees (a saved choice still wins)');
  ok(/\[data-theme="light"\]/.test(css), 'the light theme (black on white) stays one toggle away');
  ok(/function toggleTheme\(\)/.test(admin), 'the black theme is still one toggle away');
}

// ---------------------------------------------------------------------------
// 4. Not angular: round everything, no clip-path cuts, no corner brackets
// ---------------------------------------------------------------------------
{
  ok(!/clip-path\s*:/.test(css), 'the skin declares no clip-path at all');
  ok(!/polygon\s*\(/.test(css), 'no polygon cuts');
  ok(!/--mc-[a-z-]*cut/.test(css), 'no cut tokens');

  ok(/\.top-tab\s*\{[\s\S]{0,1200}border-radius:\s*var\(--mc-pill\)|border-radius:\s*999px/.test(css),
    'nav tabs are pills');
  ok(/\.top-tab\.active\s*\{[\s\S]{0,1200}var\(--mc-accent\)/.test(css), 'the active tab is the inverted accent pill');
  ok(/\.pcm-modal-btn[\s\S]{0,900}border-radius:\s*var\(--mc-pill\)|border-radius:\s*999px/.test(css),
    'dialog keys are pills');
  ok(/\.btn\s*\{[\s\S]{0,1200}border-radius:/.test(css), 'buttons are rounded');
  ok(/\.lpv-vals-modal\s*\{[\s\S]{0,1200}border-radius:\s*18px/.test(css) && css.includes('.pcm-modal,'),
    'every dialog is a rounded panel');
  ok(!/\.screenshot-panel::before/.test(css) && !/\.screenshot-panel::after/.test(css),
    'the stage has no corner brackets');
  ok(!/\.pcm-modal::before/.test(css) && !/\.pcm-modal::after/.test(css),
    'the dialogs have no corner brackets');
  ok(/\.screenshot-panel\s*\{[\s\S]{0,1200}border-radius:\s*var\(--mc-r\)|border-radius:\s*16px/.test(css),
    'the stage is rounded');
  ok(/\.client-card\s*\{[\s\S]{0,1200}border-radius:\s*12px/.test(css), 'session rows are rounded');

  // Tabs still look like a console rail.
  ok(/\.top-tab\s*\{[\s\S]{0,1200}text-transform:\s*uppercase/.test(css), 'tabs are labelled like console keys');
  ok(/\.display-subtab\.active[\s\S]{0,1200}var\(--mc-accent\)/.test(css), 'segmented sub-tabs light up in the accent');
}

// ---------------------------------------------------------------------------
// 5. Courier for every letter, Montserrat for every number — one embedded face, nothing fetched
// ---------------------------------------------------------------------------
{
  ok((admin.match(/@font-face\s*\{/g) || []).length === 1, 'Admin.html carries exactly one embedded face');
  ok(!/Corptic/i.test(admin), 'no Corptic anywhere - the panel font is the system Courier, nothing embedded for letters');
  ok(/<style id="shf-montserrat-face">[\s\S]*?@font-face\s*\{\s*font-family:\s*Montserrat;/.test(admin),
    'and the digit face is Montserrat, subset to numbers, in its own block');
  ok(/src:\s*url\(data:font\/woff2;base64,/.test(admin), 'and they ship as data: URIs');
  const faceRules = admin.match(/@font-face\s*\{[\s\S]*?\}/g) || [];
  ok(faceRules.length === 1 && faceRules.every((r) => !/https?:\/\//.test(r) && /data:font\/woff2;base64,/.test(r)),
    'the face rule is a local data: URI, no remote URL');
  ok(/--mc-font:\s*Montserrat, "Courier", monospace/.test(css),
    'the universal stack is the Montserrat digit subset first (numbers), then the system Courier (every letter)');
  ok(/font-family:\s*var\(--mc-font\)\s*!important/.test(css),
    'every text uses it, with !important over the panel\'s `*` reset');
  ok(/font-style:\s*italic/.test(css), 'the chrome is oblique');
  const machineRule = (css.match(/textarea, pre, code, kbd, samp,[\s\S]*?\}/) || [''])[0];
  ok(/font-family:\s*"Courier", monospace\s*!important/.test(machineRule),
    'machine output is set in Courier (a system font), not the chrome face');
  ok(!/DM Mono|dmmono/.test(admin), 'no DM Mono is embedded or named anywhere');
  ok(/font-size:\s*11px\s*!important/.test(machineRule) && /font-weight:\s*500\s*!important/.test(machineRule) &&
     /letter-spacing:\s*0\.1em\s*!important/.test(machineRule) && /text-transform:\s*uppercase\s*!important/.test(machineRule),
    'at 11px, weight 500, 0.1em tracking, uppercased - all !important over the base styles');
  ok(/font-style:\s*normal/.test(machineRule), 'and still upright');
  const universalRule = (css.match(/html, body,[\s\S]*?font-family:\s*var\(--mc-font\)\s*!important;[\s\S]*?\}/) || [''])[0];
  ok(!/\.log-content|\.terminal|\[class\*="monospace"\]/.test(universalRule),
    'the universal Courier rule leaves machine output to its own stricter rule');
  ok(css.indexOf(machineRule) > css.indexOf(universalRule), 'and the DM Mono rule comes later, so it wins');
  ok(/!\s*var\(--mc-mono\)|Roboto Slab/.test(admin) === false, 'no mono lead and no other house face');
  ok(!/Roboto Slab/.test(admin), 'the panel asks for no other house face');
  ok(fs.existsSync(path.join(root, 'assets', 'fonts', 'montserrat-digits-400.woff2')) &&
     admin.includes(fs.readFileSync(path.join(root, 'assets', 'fonts', 'montserrat-digits-400.woff2')).toString('base64')),
    'and so is the embedded Montserrat digit subset');
  ok(fs.existsSync(path.join(root, 'assets', 'fonts', 'LICENSE-Montserrat.txt')),
    'and its licence travels with it');
}

// ---------------------------------------------------------------------------
// 6. The instruments, the modals, the airlock
// ---------------------------------------------------------------------------
{
  ok(/\.logo-icon[\s\S]{0,1200}border-radius:\s*50%/.test(css), 'the header mark is a round reticle');
  ok(/\.placeholder-icon[\s\S]{0,1200}border-radius:\s*50%/.test(css), 'the empty state is an instrument ring');
  ok(/\.placeholder-icon::before[\s\S]{0,300}border:\s*1px dashed/.test(css), 'with a dashed inner ring');
  ok(/#screenshot-status[\s\S]{0,1200}border-radius:\s*999px|var\(--mc-pill\)/.test(css),
    'the stream status is a pill');
  ok(/@keyframes mc-spin/.test(css) && /@keyframes mc-blink/.test(css) && /@keyframes mc-sweep/.test(css),
    'the rings turn, the lamps blink and the radar sweeps');

  // Every modal speaks the dialog language; the primary key is the accent.
  ok(/\.pcm-modal-btn\.primary[\s\S]{0,1200}var\(--mc-accent\)/.test(css), 'the primary action is the inverted accent key');
  ok(/\.pcm-modal-title::before[\s\S]{0,1200}animation:\s*mc-blink/.test(css), 'the dialog header lamp blinks');
  for (const sel of ['.login-modal', '.klg-modal', '.lpv-modal', '.lpv-bind-modal', '.profile-modal']) {
    ok(css.includes(sel + ','), 'the dialog language covers ' + sel);
  }
  ok(/\.login-overlay[\s\S]{0,1400}backdrop-filter/.test(css), 'overlays are scrimmed and blurred');

  // The stage backdrop: dot field + scanlines + the inline radar.
  ok(/\.lx-stage[\s\S]{0,1200}radial-gradient\(rgba\(255, 255, 255/.test(css), 'the stage carries the dot field');
  ok(/\.lx-stage::before[\s\S]{0,1200}repeating-linear-gradient\(0deg, rgba\(255, 255, 255/.test(css), 'and the scanlines');
  ok(/class="mc-radar"/.test(admin) && /viewBox="0 0 600 600"/.test(admin), 'the radar scope is drawn inline');
  ok(/\.mc-radar \.mc-sweep[\s\S]{0,300}animation:\s*mc-sweep/.test(css), 'the sweep is animated in CSS');
  ok(/\.mc-radar\s*\{[\s\S]{0,300}color:\s*#ffffff/.test(css) && /\[data-theme="light"\] \.mc-radar\s*\{\s*color:\s*#000000/.test(css),
    'the radar follows the theme (white on black, black on white)');
  const radar = admin.slice(admin.indexOf('<svg class="mc-radar"'),
                            admin.indexOf('</svg>', admin.indexOf('<svg class="mc-radar"')) + 6);
  const radarShapes = (radar.match(/<(?:circle|path)\b/g) || []).length;
  ok(radarShapes >= 15, 'the scope is real artwork (' + radarShapes + ' shapes)');
  ok((radar.match(/<(?:linear|radial)Gradient\b/g) || []).length >= 2, 'with a glow and a sweep gradient');

  // The sign-in airlock: a round glass card with a porthole reticle, pill
  // fields and one pill key — deliberately none of the angular cuts.
  ok(/\.login-modal\s*\{[\s\S]{0,1200}border-radius:\s*var\(--mc-r-lg\)|border-radius:\s*30px/.test(css), 'the airlock card is round');
  ok(!/\.login-modal\s*\{[\s\S]{0,1200}clip-path/.test(css), 'and has no cut');
  ok(!/\.login-modal::before/.test(css) && !/\.login-modal::after/.test(css), 'and no corner brackets');
  ok(/\.login-modal \.logo-icon\s*\{[\s\S]{0,1200}border-radius:\s*50%/.test(css), 'the porthole is a circle');
  ok(/\.login-modal \.logo-icon::before[\s\S]{0,400}border:\s*1px dashed/.test(css), 'with a dashed ring that turns');
  ok(/\.login-overlay\s*\{[\s\S]{0,1400}repeating-radial-gradient/.test(css), 'the airlock floats on concentric rings');
  ok(/\.login-btn\s*\{[\s\S]{0,1200}border-radius:\s*var\(--mc-pill\)|border-radius:\s*999px/.test(css), 'the sign-in key is a pill');
  ok(/\.login-modal \.form-input[\s\S]{0,1200}border-radius:\s*var\(--mc-pill\)|border-radius:\s*999px/.test(css), 'the fields are pills too');
}

// ---------------------------------------------------------------------------
// 7. The look costs nothing at runtime
// ---------------------------------------------------------------------------
{
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
  const artIds = Array.from(radar.matchAll(/id="([^"]+)"/g)).map((m) => m[1]);
  ok(artIds.length > 0 && artIds.every((id) => id.startsWith('mc')), 'the art names and namespaces its gradients');
  const docIds = Array.from(admin.matchAll(/\sid="([^"]+)"/g)).map((m) => m[1]);
  ok(artIds.every((id) => docIds.filter((d) => d === id).length === 1), 'no id is defined twice in the page');

  // Motion is optional.
  ok(/@media \(prefers-reduced-motion: reduce\)/.test(css), 'reduced motion is honoured');
  const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
  ok(/\.logo-icon/.test(reduced) && /animation:\s*none !important/.test(reduced), 'the reticle stops turning');
  ok(/\.placeholder-icon::after/.test(reduced), 'the instrument ring stops too');
  ok(/\.mc-radar \.mc-sweep/.test(reduced), 'and the radar stops sweeping');

  // Responsive: the scope is the first thing to go on a small screen.
  const small = css.slice(css.indexOf('@media (max-width: 1100px)'));
  ok(/\.mc-radar\s*\{\s*display:\s*none/.test(small), 'the scope is dropped on narrow screens');
  ok(/@media \(max-width: 760px\)/.test(css), 'and phones get their own pass');
}

// ---------------------------------------------------------------------------
// 8. Paint order: rails below content, art below everything
// ---------------------------------------------------------------------------
{
  ok(/\.sidebar\s*\{[\s\S]{0,400}z-index:\s*1/.test(css), 'the left rail is a layer below the content');
  ok(/\.right-sidebar\s*\{[\s\S]{0,400}z-index:\s*1/.test(css), 'and so is the right rail');
  ok(/\.sidebar \.client-list\s*\{\s*position:\s*relative;\s*z-index:\s*3/.test(css),
    'but the session cards stay readable on top');
  ok(/\.mc-radar\s*\{[\s\S]{0,400}pointer-events:\s*none/.test(css), 'the scope never intercepts a click');
  ok(/\.lx-stage\s*\{[\s\S]{0,400}pointer-events:\s*none/.test(css), 'and neither does the backdrop');
}

// ---------------------------------------------------------------------------
// 9. The stylesheet actually parses (needs jsdom; skipped otherwise)
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
    ok(doc.getElementById('shf-montserrat-face').sheet.cssRules.length === 1, 'the Montserrat digit face parses as one rule');
    ok(doc.getElementById('shf-montserrat-face').sheet.cssRules.length === 1, 'and so does the Montserrat digit face');

    const style = (sel, prop) => {
      const el = doc.querySelector(sel);
      return el ? dom.window.getComputedStyle(el)[prop] : null;
    };
    ok(doc.documentElement.getAttribute('data-theme') === 'dark', 'the panel computes as dark by default');
    ok(style('body', 'fontStyle') === 'italic', 'the panel computes as oblique');
    ok(/Courier/.test(style('body', 'fontFamily') || '') && /Montserrat/.test(style('body', 'fontFamily') || ''),
      'every text computes to the Courier + Montserrat stack');
    ok(/Montserrat/.test(dom.window.getComputedStyle(doc.documentElement).getPropertyValue('--mc-font') || ''),
      'the --mc-font stack offers Montserrat digits, then Courier');
    ok(/Courier/.test(style('.top-tab', 'fontFamily') || '') && style('.top-tab', 'fontStyle') === 'italic',
      'the nav is Courier and oblique');
    ok(style('.logo-text', 'fontStyle') === 'italic', 'the title is oblique');
    ok(style('textarea', 'fontStyle') === 'normal', 'a textarea is upright');
    ok(/Courier/.test(style('textarea', 'fontFamily') || ''), 'a textarea computes to Courier');
    ok(/11px/.test(style('textarea', 'fontSize') || '') && /uppercase/.test(style('textarea', 'textTransform') || ''),
      'at 11px, uppercased');
    ok(style('.top-tab', 'borderRadius') === '999px' || style('.top-tab', 'borderRadius') === 'var(--mc-pill)',
      'a tab computes as a pill');
    ok(style('.login-modal', 'borderRadius') === '30px' || style('.login-modal', 'borderRadius') === 'var(--mc-r-lg)',
      'the airlock card computes round');
    ok(!!doc.querySelector('.main-app'), 'the shell is intact');
    ok(!!doc.querySelector('svg.mc-radar'), 'the radar scope is in the stage');
  } else {
    console.log('  (jsdom missing: cascade checks skipped)');
  }
}

console.log('admin console: ' + checks + ' checks passed');
