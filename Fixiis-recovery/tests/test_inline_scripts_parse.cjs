// No dependencies: node tests/test_inline_scripts_parse.cjs
//
// The panels ship as ONE file each, so every script is inline. A quoting slip
// inside one of those scripts does not produce a warning, a broken style or a
// console note the operator would notice: it kills the whole block silently.
// That is exactly what happened while the product font was changed — a CSS
// family name written with quotes inside a single-quoted JS string
// (`'… font-family:'Helvetica Neue' …'`) made Admin.html's main script
// unparseable, and the Settings tab's archive picker simply stayed empty.
//
// So this test parses every inline script in every shipped page with the real
// JavaScript parser, and pins down the two shapes that caused it: a style
// attribute or JS string that opens a quote it never closes.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let checks = 0;
const failures = [];
function ok(cond, label) {
  if (cond) { checks++; return; }
  failures.push(label);
}

const root = path.join(__dirname, '..');
const PAGES = ['Admin.html', 'client.html', 'mini-admin.html', 'index.html'];

// ---------------------------------------------------------------------------
// 1. Every inline script parses
// ---------------------------------------------------------------------------
for (const page of PAGES) {
  const file = path.join(root, page);
  if (!fs.existsSync(file)) continue;
  const src = fs.readFileSync(file, 'utf8');
  const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
  let m, count = 0, broken = [];
  while ((m = re.exec(src)) !== null) {
    if (/\bsrc\s*=/.test(m[1])) continue;      // external: nothing inline to parse
    if (/type\s*=\s*["'](?!text\/javascript|module|application\/javascript)/i.test(m[1])) continue;
    count++;
    const line = src.slice(0, m.index).split('\n').length;
    try {
      // vm.Script is the real compiler; new Function() would happily accept a
      // body that a <script> rejects in strict-mode edge cases.
      new vm.Script(m[2], { filename: page + ':' + line });
    } catch (e) {
      broken.push('script at line ' + line + ': ' + e.message);
    }
  }
  ok(count > 0, page + ': has at least one inline script');
  ok(broken.length === 0, page + ': every inline script parses' +
     (broken.length ? '\n      ' + broken.join('\n      ') : ''));
}

// ---------------------------------------------------------------------------
// 2. No style attribute or JS string quotes a CSS family name with the same
//    quote character that delimits it
// ---------------------------------------------------------------------------
// `style="… font-family:"Helvetica Neue" …"` ends the attribute early, and
// `'… font-family:'Helvetica Neue' …'` ends the JS string early. Both are the
// same mistake: a nested quote of the same kind.
for (const page of PAGES) {
  const file = path.join(root, page);
  if (!fs.existsSync(file)) continue;
  const src = fs.readFileSync(file, 'utf8');
  // The quote must be followed by the family itself to count: client.html
  // legitimately ends a string with `font-family:` and concatenates the stack
  // (`'html,body{font-family:' + MIRROR_FONT_STACK + '…'`).
  const badAttr = src.match(/style="[^"\n]*font-family:"(?=[A-Za-z#])/);
  ok(!badAttr, page + ': no style attribute broken by a quoted font family' +
     (badAttr ? ' — ' + badAttr[0].slice(-70) : ''));
  const badJs = src.match(/'[^'\n]*font-family:'(?=[A-Za-z#])/);
  ok(!badJs, page + ': no single-quoted JS string broken by a quoted font family' +
     (badJs ? ' — ' + badJs[0].slice(-70) : ''));
}

// ---------------------------------------------------------------------------
// 3. The font itself: Helvetica Neue, and no webfont request anywhere
// ---------------------------------------------------------------------------
{
  const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');
  const admin = fs.readFileSync(path.join(root, 'Admin.html'), 'utf8');
  const mini = fs.readFileSync(path.join(root, 'mini-admin.html'), 'utf8');

  for (const [name, src] of [['client.html', client], ['Admin.html', admin], ['mini-admin.html', mini]]) {
    ok(/Helvetica Neue/.test(src), name + ' is set in Helvetica Neue');
    ok(!/Space\+Grotesk/.test(src) && !/Space Grotesk/.test(src),
       name + ' asks for Space Grotesk nowhere');
  }
  // The shell and the panels must not fetch a font at all; the one exception in
  // the whole product is the optional "live pages in mono" switch.
  ok(!/fonts\.googleapis/.test(admin), 'Admin.html fetches no webfont');
  ok(!/fonts\.googleapis/.test(mini), 'mini-admin.html fetches no webfont');
  ok((client.match(/fonts\.googleapis\.com/g) || []).length === 1,
     'client.html reaches a font CDN exactly once — the mono switch');
}

if (failures.length) {
  console.log('FAIL  inline scripts: ' + checks + ' passed, ' + failures.length + ' failed');
  failures.forEach((f) => console.log('  - ' + f));
  process.exit(1);
}
console.log('PASS  inline scripts: ' + checks + ' checks, every shipped page parses');
