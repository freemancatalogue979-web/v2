// No dependencies beyond jsdom: node tests/test_mirror_sanitize.cjs
//
// The mirror iframe is same-origin with the client shell, so code that
// executes inside it can reach this document.  Captures never carry
// <script> tags, but an authored inline event handler is not a <script>
// tag — this suite proves the sanitizer kills the whole class:
//
//   1. on* attributes are removed from every element (incl. SVG/foreignObject);
//   2. handlers nested inside shadow roots are removed (querySelectorAll
//      alone cannot see them);
//   3. javascript:/vbscript: URLs in href/src/action/formaction/... are removed
//      (case-insensitive, leading C0 controls/whitespace tolerated);
//   4. srcdoc iframes are removed (they would run same-origin JS);
//   5. <meta http-equiv=refresh> is removed (a mirror must never navigate);
//   6. benign markup (class, https href, data: images, about:blank) survives;
//   7. dropMirrorAttr treats an attribute REMOVAL (null value) as safe.
const fs = require('node:fs');
const path = require('node:path');

let checks = 0;
const failures = [];
function ok(cond, label) {
  if (cond) { checks++; return; }
  failures.push(label);
}

const root = path.join(__dirname, '..');
const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');
const A = client.indexOf('// ---- Visual-only mirror sanitizer');
const B = client.indexOf('// Legacy delta-capture handler');
if (A < 0 || B < 0 || B <= A) {
  console.error('FAIL sanitize: sanitizer block not found in client.html');
  process.exit(1);
}
const block = client.slice(A, B)
  + '\nreturn { MIRROR_URL_ATTRS, dropMirrorAttr, sanitizeMirrorElement, walkMirrorElements, stripDeadMirrorNodes };\n';

let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/jsd/node_modules/jsdom', '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}
if (!jsdom) { console.log('skipped: jsdom not installed'); process.exit(0); }
const { JSDOM } = jsdom;

const HTML = `<!doctype html><html><head><title>t</title></head><body>
  <img id="i1" src="https://cdn.example/a.png" onerror="fetch('https://evil/log')">
  <a id="a1" href="javascript:alert(1)" class="nav">js url</a>
  <a id="a2" href="  JaVaScRiPt:alert(2)">padded js url</a>
  <a id="a3" href="vbscript:msgbox(3)">vb url</a>
  <a id="ok1" href="https://example.com/page">fine</a>
  <a id="ok2" href="about:blank">fine about</a>
  <a id="ok3" href="data:text/plain,hello">fine data</a>
  <iframe id="f1" srcdoc="<script>parent.document.body.innerHTML='x'</script>"></iframe>
  <details id="d1" ontoggle="steal()"><summary id="s1">more</summary></details>
  <svg id="svg1" onload="steal()"><circle r="5" onclick="steal()"></circle></svg>
  <form id="f2" action="javascript:steal()"><input id="f3" formaction="javascript:steal()"></form>
  <input id="f4" src="javascript:steal()">
  <div id="host"></div>
  <meta http-equiv="refresh" content="0;url=https://evil.example/">
  <div id="keep" class="card" data-state="open">keep me</div>
</body></html>`;

const dom = new JSDOM(HTML, { url: 'https://x.test/', runScripts: 'outside-only' });
const w = dom.window;
const d = w.document;

const api = new w.Function(block)();

// shadow root with its own handler — must be walked too
const host = d.getElementById('host');
const shadow = host.attachShadow({ mode: 'open' });
shadow.innerHTML = '<button id="inShadow" onclick="steal()">in shadow</button><img id="shadowImg" onerror="steal()">';

api.stripDeadMirrorNodes(d);

ok(!d.getElementById('i1').hasAttribute('onerror'), 'img onerror removed');
ok(d.getElementById('i1').getAttribute('src') === 'https://cdn.example/a.png', 'img src kept');
ok(!d.getElementById('a1').hasAttribute('href'), 'javascript: href removed');
ok(d.getElementById('a1').className === 'nav', 'javascript: href removal keeps class');
ok(!d.getElementById('a2').hasAttribute('href'), 'padded/case-mixed javascript: href removed');
ok(!d.getElementById('a3').hasAttribute('href'), 'vbscript: href removed');
ok(d.getElementById('ok1').getAttribute('href') === 'https://example.com/page', 'https href kept');
ok(d.getElementById('ok2').getAttribute('href') === 'about:blank', 'about:blank kept');
ok(d.getElementById('ok3').getAttribute('href') === 'data:text/plain,hello', 'data: kept');
ok(!d.getElementById('f1').hasAttribute('srcdoc'), 'srcdoc removed');
ok(!d.getElementById('d1').hasAttribute('ontoggle'), 'details ontoggle removed');
ok(!d.getElementById('svg1').hasAttribute('onload'), 'svg onload removed');
ok(!d.querySelector('#svg1 circle').hasAttribute('onclick'), 'svg child onclick removed');
ok(!d.getElementById('f2').hasAttribute('action'), 'form action javascript: removed');
ok(!d.getElementById('f3').hasAttribute('formaction'), 'input formaction javascript: removed');
ok(!d.getElementById('f4').hasAttribute('src'), 'input src javascript: removed');
ok(d.querySelectorAll('meta[http-equiv="refresh"]').length === 0, 'meta refresh removed');
ok(d.getElementById('keep').className === 'card' && d.getElementById('keep').getAttribute('data-state') === 'open', 'benign markup untouched');
ok(!shadow.getElementById('inShadow').hasAttribute('onclick'), 'shadow root handler removed');
ok(!shadow.getElementById('shadowImg').hasAttribute('onerror'), 'shadow root handler removed (attr)');
ok(d.getElementById('host') !== null, 'shadow host still present');

// dropMirrorAttr contract: value === null (removal) is always safe to apply
ok(api.dropMirrorAttr('onclick', null) === true, 'dropMirrorAttr still flags on* for removal ops?? (must be true)');
ok(api.dropMirrorAttr('class', 'x') === false, 'class allowed');
ok(api.dropMirrorAttr('href', 'javascript:x') === true, 'javascript: flagged');
ok(api.dropMirrorAttr('href', 'https://x') === false, 'https allowed');
ok(api.dropMirrorAttr('data-foo', 'javascript:x') === false, 'javascript: in a data-* attribute allowed (never compiled)');

if (failures.length) {
  console.error('FAIL sanitize: ' + checks + ' passed, ' + failures.length + ' failed');
  for (const f of failures) console.error('  - ' + f);
  process.exit(1);
}
console.log('PASS sanitize: ' + checks + ' checks passed');
process.exit(0);
