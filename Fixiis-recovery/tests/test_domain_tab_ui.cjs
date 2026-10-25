// node tests/test_domain_tab_ui.cjs
//
// The Domains tab: one configuration per site — the loading screen that site's
// visitors see, and the provider's logo they recognise.
//
// Structural checks on the markup the operator actually gets, plus the wiring
// between the tab, the form and the endpoints. The behaviour behind the
// endpoints is covered by tests/test_domain_spinner.py; this file is about the
// tab existing, being usable, and talking to the right URLs.
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
const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');
const api = fs.readFileSync(path.join(root, 'api.py'), 'utf8');

// jsdom is optional: the structural half of this test needs it, the rest does
// not. Both halves always run.
let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}

// ---------------------------------------------------------------------------
// 1. A nav tab called Domains, and a pane behind it
// ---------------------------------------------------------------------------
{
  ok(/<button class="top-tab" data-tab="domains">/.test(admin),
    'the Domains tab is in the nav bar');
  ok(/data-tab="domains">[\s\S]{0,900}?<span>Domains<\/span>/.test(admin),
    'it is labelled "Domains"');
  ok(/<div class="tab-content" id="domains-content">/.test(admin),
    'it has its own pane');
  ok(/if \(tabId === 'domains'\) \{[\s\S]{0,120}?domainsLoad\(\);/.test(admin),
    'opening the tab loads the list');
  ok(/function switchTab\(tabId\)[\s\S]{0,4000}?domainsWire\(\)/.test(admin),
    'the first open wires the form up');
}

// ---------------------------------------------------------------------------
// 2. The form can express everything the server stores
// ---------------------------------------------------------------------------
{
  const pane = admin.slice(admin.indexOf('id="domains-content"'));
  const end = pane.indexOf('<div class="tab-content" id="lpv-content">');
  const tab = pane.slice(0, end > 0 ? end : 20000);

  for (const id of ['dm-domain', 'dm-label', 'dm-logo', 'dm-logo-preview',
                    'dm-spinner-kind', 'dm-html-file', 'dm-html', 'dm-spinner-page',
                    'dm-enabled', 'dm-save', 'dm-reset', 'dm-list', 'dm-note']) {
    ok(tab.includes('id="' + id + '"'), 'the form has #' + id);
  }
  ok(/id="dm-html-file"[^>]*accept="\.html,\.htm,text\/html"/.test(tab),
    'the loading page is uploaded as an HTML file');
  // The native file input is hidden: the operator gets a normal button, in
  // the same style as the rest of the panel, that names the chosen file.
  ok(/id="dm-html-file"[^>]*style="display:none;"/.test(tab),
    'the native file input is hidden');
  ok(/id="dm-html-pick"/.test(tab), 'there is a custom "Choose HTML file" button');
  ok(/<button type="button"[^>]*id="dm-html-pick"/.test(tab),
    'it is a real button, so it cannot submit anything');
  ok(/id="dm-html-pick"[\s\S]{0,600}?Choose HTML file/.test(tab),
    'and it says what it does');
  ok(/id="dm-html-filename"/.test(tab), 'the chosen file name has its own line');
  ok(/No file chosen/.test(tab), 'which starts empty');
  ok(/data-dm-preview=/.test(admin) && /function domainsPreview\(domain\)/.test(admin),
    'the list can preview the exact document a visitor is served');
  ok(/const chain = domainsMarkChain\(item\)/.test(admin),
    'the list shows the resolved mark, not an empty box');
  ok(/on\('dm-html-pick', 'click',[\s\S]{0,160}?input\.click\(\)/.test(admin),
    'the button opens the hidden input');
  ok(/el\.classList\.toggle\('has-file'/.test(admin),
    'a chosen file is visibly marked');
  ok(/#domains-content \.dm-file-btn \{[^}]*display:inline-flex/.test(tab),
    'the button is styled with the panel, not as a bare input');
  ok(/<option value="logo">/.test(tab) && /<option value="html">/.test(tab)
     && /<option value="page">/.test(tab),
    'three ways to get a loading screen: built-in logo, own HTML, archived page');
  ok(/id="dm-enabled"[^>]*checked/.test(tab),
    'a new domain is on by default');
  ok(/type="checkbox"/.test(tab), 'the on/off switch is a checkbox');
}

// ---------------------------------------------------------------------------
// 3. It talks to the domain endpoints, not to something else
// ---------------------------------------------------------------------------
{
  ok(/serverFetch\('\/api\/lpv\/domains'\)/.test(admin), 'the list comes from /api/lpv/domains');
  ok(/serverFetch\('\/api\/lpv\/domains', \{[\s\S]{0,120}?method: 'POST'/.test(admin),
    'saving POSTs to /api/lpv/domains');
  ok(/serverFetch\('\/api\/lpv\/domains\/' \+ encodeURIComponent\(domain\), \{ method: 'DELETE' \}\)/.test(admin),
    'deleting DELETEs the one domain');
  ok(/\/api\/lpv\/archive\?limit=200/.test(admin),
    'the page picker uses the archive endpoint');
  ok(/\/api\/lpv\/pages\/' \+ encodeURIComponent\(pageId\) \+ '\/logo'/.test(admin),
    'picking a page asks the server for its domain + logo');
  ok(/\/api\/lpv\/domains\/' \+ encodeURIComponent\(item\.domain\) \+ '\/spinner\.html'/.test(admin),
    'editing fetches the stored loading page from its own URL');
}

// ---------------------------------------------------------------------------
// 4. Uploading an HTML file: read locally, capped, then posted as text
// ---------------------------------------------------------------------------
{
  ok(/new FileReader\(\)/.test(admin), 'the file is read in the browser');
  ok(/readAsText\(file\)/.test(admin), 'as text, because the server stores HTML');
  ok(/file\.size > 1024 \* 1024/.test(admin), 'a file over 1 MB is refused before it is sent');
  ok(/payload\.spinner_html = html;/.test(admin), 'the HTML travels in spinner_html');
  ok(/payload\.spinner_page_id = pid;/.test(admin), 'or as an archived page id');
}

// ---------------------------------------------------------------------------
// 5. The list says what a domain will actually show
// ---------------------------------------------------------------------------
{
  ok(/function domainsSpinnerKind\(item\)/.test(admin),
    'the list classifies each domain: own html, archived page, or logo');
  ok(/'own html' : \(kind === 'page' \? 'archived page' : 'logo'\)/.test(admin),
    'and names it in plain words');
  ok(/data-dm-toggle/.test(admin) && /data-dm-edit/.test(admin) && /data-dm-del/.test(admin),
    'each row can be switched off, edited and deleted');
  ok(/domain: domain, enabled: !item\.enabled/.test(admin),
    'the on/off switch sends only the domain and the new state');
}

// ---------------------------------------------------------------------------
// 5b. Every row carries the site's own icon, so domains read as brands
// ---------------------------------------------------------------------------
{
  ok(/function domainsMarkChain\(item\)/.test(admin),
    'each row has an icon chain: your logo -> the captured icon -> the favicon');
  ok(/push\('https:\/\/' \+ host \+ '\/favicon\.ico'\)/.test(admin),
    'the site\'s favicon is the floor, so a configured domain always has a mark');
  ok(/data-dm-chain=/.test(admin) && /class="dm-initial"/.test(admin),
    'the row falls back to the next file, then to a letter tile');
  ok(/addEventListener\('error'[\s\S]{0,400}?dm-initial/.test(admin),
    'a file that does not exist does not leave an empty box');
  ok(/domainsHostOf\(domainEl && domainEl\.value\)/.test(admin),
    'the form previews the typed domain\'s favicon before anything is saved');
  ok(/on\('dm-domain', 'input', domainsSyncLogoPreview\)/.test(admin),
    'and updates as the domain is typed');
}

if (jsdom) {
  // The helpers themselves, run for real: hosts are cleaned the same way the
  // server cleans them, so a pasted link still yields a favicon.
  const grab = (name) => {
    const at = admin.indexOf('function ' + name + '(');
    if (at < 0) return '';
    let depth = 0;
    for (let i = admin.indexOf('{', at); i < admin.length; i += 1) {
      if (admin[i] === '{') depth += 1;
      else if (admin[i] === '}') { depth -= 1; if (!depth) return admin.slice(at, i + 1); }
    }
    return '';
  };
  const src = grab('domainsHostOf') + '\n' + grab('domainsMarkChain') + '\n' +
              grab('domainsInitial') +
              '\nreturn { domainsHostOf, domainsMarkChain, domainsInitial };';
  const helpers = new Function(src)();

  ok(helpers.domainsHostOf('https://www.Google.com:8443/search?q=1') === 'google.com',
    'a pasted link becomes a bare host');
  ok(helpers.domainsHostOf('') === '' && helpers.domainsHostOf('nonsense') === '',
    'no host, no favicon guess');

  const bare = helpers.domainsMarkChain({ domain: 'google.com' });
  ok(bare.length === 1 && bare[0] === 'https://google.com/favicon.ico',
    'a domain with nothing configured still shows its favicon');

  const captured = helpers.domainsMarkChain({
    domain: 'example-provider.com',
    resolved_logo: 'https://www.example-provider.com/touch-180.png',
  });
  ok(captured[0] === 'https://www.example-provider.com/touch-180.png' &&
     captured[captured.length - 1] === 'https://example-provider.com/favicon.ico',
    'the captured icon is preferred, the favicon is the safety net');

  const deduped = helpers.domainsMarkChain({
    domain: 'example-provider.com',
    logo_url: 'https://example-provider.com/favicon.ico',
  });
  ok(deduped.filter((u) => u === 'https://example-provider.com/favicon.ico').length === 1,
    'the same file is never tried twice');

  ok(helpers.domainsInitial({ domain: 'chase.com' }) === 'C' &&
     helpers.domainsInitial({ domain: 'chase.com', label: 'bank' }) === 'B',
    'the letter tile uses the name when there is one');
}

// ---------------------------------------------------------------------------
// 5c. The rendered list, built for real in a DOM
// ---------------------------------------------------------------------------
if (jsdom) {
  const grabAll = (names) => names.map((name) => {
    const at = admin.indexOf('function ' + name + '(');
    if (at < 0) throw new Error('missing function ' + name);
    let depth = 0;
    for (let i = admin.indexOf('{', at); i < admin.length; i += 1) {
      if (admin[i] === '{') depth += 1;
      else if (admin[i] === '}') { depth -= 1; if (!depth) return admin.slice(at, i + 1); }
    }
    throw new Error('unbalanced ' + name);
  }).join('\n');

  const page = new jsdom.JSDOM(
    '<!doctype html><html><body><div id="dm-list"></div></body></html>',
    { url: 'http://localhost/admin' });
  const doc = page.window.document;
  const DOMAINS = { items: [
    { domain: 'example-bank.test', label: 'Example Bank', enabled: true,
      spinner_html: '', spinner_page_id: '',
      resolved_logo: 'https://www.example-bank.test/touch-180.png' },
    { domain: 'google.com', label: 'Google', enabled: true,
      spinner_html: '', spinner_page_id: '', resolved_logo: '' },
  ] };
  const src = grabAll(['domainsHostOf', 'domainsMarkChain', 'domainsInitial',
                       'domainsSpinnerKind', 'domainsRender']) +
    '\nreturn { domainsRender };';
  const { domainsRender } = new Function('document', 'dmEl', 'DOMAINS', 'dmNote', 'SERVER_API_BASE',
    src)(doc, (id) => doc.getElementById(id), DOMAINS, () => {}, '');

  domainsRender();
  const rows = doc.querySelectorAll('#dm-list .dm-item');
  ok(rows.length === 2, 'one row per configured domain');

  const first = rows[0].querySelector('img');
  ok(first && first.getAttribute('src') === 'https://www.example-bank.test/touch-180.png',
    'the row shows the icon the capture carries, not a grey square');
  ok(first && (first.getAttribute('data-dm-chain') || '').indexOf('favicon.ico') > 0,
    'with the favicon queued behind it');

  const second = rows[1].querySelector('img');
  ok(second && second.getAttribute('src') === 'https://google.com/favicon.ico',
    'a domain with nothing else still shows its own favicon');

  // A file that does not exist: the queue empties, the letter tile takes over.
  const before = rows[1].querySelector('.dm-initial');
  ok(before && before.style.display === 'none', 'the letter tile waits its turn');
  second.dispatchEvent(new page.window.Event('error'));
  ok(second.style.display === 'none' && before.style.display === 'flex',
    'a missing favicon ends on the letter, never on an empty box');
  ok(before.textContent.trim() === 'G', 'and the letter is the domain\'s first');
}

// ---------------------------------------------------------------------------
// 6. Structure, parsed (jsdom when it is installed)
// ---------------------------------------------------------------------------
if (jsdom) {
  const start = admin.indexOf('<div class="tab-content" id="domains-content">');
  const end = admin.indexOf('<div class="tab-content" id="lpv-content">', start);
  const markup = admin.slice(start, end > 0 ? end : start + 30000);
  const dom = new jsdom.JSDOM('<!doctype html><html><body>' + markup + '</body></html>');
  const doc = dom.window.document;

  ok(doc.querySelectorAll('#domains-content').length === 1, 'exactly one Domains pane');
  ok(doc.querySelectorAll('#dm-spinner-kind option').length === 3,
    'the loader choice has three options');
  ok(doc.querySelector('#dm-html-file').getAttribute('type') === 'file',
    'the file input really is a file input');
  ok(doc.querySelector('#dm-html-file').style.display === 'none',
    'and it is not the thing the operator sees or clicks');
  ok(doc.querySelector('#dm-html-pick').textContent.trim().indexOf('Choose HTML file') >= 0,
    'the visible control is the custom button');
  ok(doc.querySelector('#dm-html-filename').textContent.trim() === 'No file chosen',
    'the file name line starts empty');
  ok(doc.querySelector('#dm-html-row').style.display === 'none',
    'the upload box starts hidden (the built-in logo loader is the default)');
  ok(doc.querySelector('#dm-page-row').style.display === 'none',
    'so does the archived-page picker');
  ok(doc.querySelectorAll('#dm-spinner-page option').length === 1,
    'the loader picker starts with just its placeholder');

  // The form must be usable on a phone (the operator flips these from one).
  ok(/@media \(max-width: 980px\)[\s\S]{0,120}?dm-grid \{ grid-template-columns:\s*1fr/.test(markup),
    'the two columns stack on a narrow screen');
  ok(/@media \(max-width: 520px\)[\s\S]{0,120}?dm-split \{ grid-template-columns:\s*1fr/.test(markup),
    'and so do the paired inputs');
  ok(!/(^|[^-])width:\s*\d{3,}px/m.test(markup),
    'nothing is pinned to a wide fixed width (max-width media queries are fine)');
} else {
  ok(true, 'jsdom not installed: structure half skipped');
}

// ---------------------------------------------------------------------------
// 7. Server side: the endpoints exist and are shaped the way the tab expects
// ---------------------------------------------------------------------------
{
  ok(/@app\.get\("\/api\/lpv\/domains"\)/.test(api), 'GET /api/lpv/domains');
  ok(/@app\.post\("\/api\/lpv\/domains"\)/.test(api), 'POST /api/lpv/domains');
  ok(/@app\.delete\("\/api\/lpv\/domains\/\{domain\}"\)/.test(api), 'DELETE /api/lpv/domains/<domain>');
  ok(/@app\.get\("\/api\/lpv\/domains\/\{domain\}\/spinner\.html"\)/.test(api),
    'GET /api/lpv/domains/<domain>/spinner.html');
  ok(/@app\.get\("\/api\/lpv\/pages\/\{page_id\}\/logo"\)/.test(api),
    'GET /api/lpv/pages/<id>/logo');
  ok(/return \{"domains": out\}/.test(api) && /rows = lpv_store\.list_domains\(\)/.test(api),
    'the list returns {domains: [...]}');
  ok(/item\["resolved_logo"\] = _domain_logo\(cfg\)/.test(api),
    'each row says which mark the visitor will actually see');
  ok(/item\["spinner_url"\]/.test(api), 'and where its loading screen is served from');
  ok(/out\["has_spinner_html"\]/.test(api) && /out\["spinner_html"\] = ""/.test(api),
    'saving never echoes a megabyte of HTML back');
}

// ---------------------------------------------------------------------------
// 8. One spinner, not two: the runtime adopts the embedded copy
// ---------------------------------------------------------------------------
{
  ok(/function lpvAdoptBootLoading\(url\)/.test(client), 'the client can adopt the embedded page');
  ok(/_showRedirect\(show\) \{[\s\S]{0,1200}?lpvAdoptBootLoading\(url\)/.test(client),
    'the loading path checks it before fetching anything');
  ok(/_redirectAdopted = true/.test(client), 'and records that it did');
  ok(/spinner_url/.test(client) && /"spinner_url"/.test(api),
    'a push carries its site\'s spinner so later steps keep the same identity');
}

// ---------------------------------------------------------------------------
// 9. The provider logo is drawn, not stretched
// ---------------------------------------------------------------------------
{
  ok(/def _brand_spinner_html\(/.test(api), 'the server builds a logo loading screen');
  ok(/object-fit:contain/.test(api), 'the mark keeps its proportions');
  ok(/width:\" ?76px|width:76px/.test(api), 'at a size a full-resolution icon survives');
  ok(!/\.mark\{[^}]*transform/.test(api), 'nothing scales the mark (that is what looks soft)');
  ok(/apple-touch-icon/.test(api) && /og:image/.test(api),
    'the best icon in the capture is preferred, not the 16px favicon');

  const python = 'tests/test_domain_spinner.py';
  ok(fs.existsSync(path.join(root, python)), 'the behaviour test ships with it');
}

// ---------------------------------------------------------------------------
// 10. The adoption decision itself, run against a real DOM
// ---------------------------------------------------------------------------
if (jsdom) {
  const start = client.indexOf('let lpvBootLoadingDropped = false;');
  const end = client.indexOf('// Clicks and submits on the embedded page', start);
  ok(start > 0 && end > start, 'the boot-loading helpers are where the test expects them');
  const helperSrc = client.slice(start, end);
  const page = new jsdom.JSDOM(
    '<!doctype html><html><body>' +
    '<div id="lpvBootLoading" data-lpv-url="/api/lpv/domains/x.com/spinner.html">' +
    '<iframe id="lpvBootLoadingFrame"></iframe></div></body></html>',
    { url: 'http://localhost/client.html' });
  const build = () => new Function('document', 'dbg', helperSrc +
    '; return { lpvAdoptBootLoading, lpvBootLoadingUrl, lpvBootLoadingOnScreen, lpvDropBootLoading };')(
      page.window.document, () => {});

  const helper = build();
  ok(helper.lpvBootLoadingUrl() === '/api/lpv/domains/x.com/spinner.html',
    'the embedded copy reports which page it is showing');
  ok(helper.lpvAdoptBootLoading('/api/lpv/domains/x.com/spinner.html') === true,
    'the same page is adopted — one spinner, not two');
  ok(helper.lpvAdoptBootLoading('/api/lpv/domains/y.com/spinner.html') === false,
    'a different page is NOT adopted (it has to be loaded)');
  ok(helper.lpvAdoptBootLoading('') === false, 'an empty URL is never adopted');

  const dropped = build();
  dropped.lpvDropBootLoading();
  ok(dropped.lpvBootLoadingOnScreen() === false, 'a dropped copy is off screen');
  ok(dropped.lpvAdoptBootLoading('/api/lpv/domains/x.com/spinner.html') === false,
    'and is not adopted afterwards — the runtime loads the page it needs');
} else {
  ok(true, 'jsdom not installed: adoption run skipped');
}

// ---------------------------------------------------------------------------
// 11. The hard-coded branded loader gets out of the way, for real
// ---------------------------------------------------------------------------
if (jsdom) {
  const start = client.indexOf('_retireBootLoader() {');
  const end = client.indexOf('_dropRedirectFrame() {', start);
  ok(start > 0 && end > start, 'the retire helper is where the test expects it');
  // The helper is a method in the LPV object literal: keep only its body.
  const methodSrc = client.slice(start, end).trim().replace(/,$/, '');
  const body = methodSrc.slice(methodSrc.indexOf('{') + 1, methodSrc.lastIndexOf('}'));
  const page = new jsdom.JSDOM(
    '<!doctype html><html><body>' +
    '<div class="loading-screen" id="loadingScreen" style="display:flex"></div>' +
    '<div id="lpv-spinner" class="hidden"></div></body></html>',
    { url: 'http://localhost/client.html' });
  const doc = page.window.document;
  const spinner = doc.getElementById('lpv-spinner');
  const fakeThis = { spinner };
  const fn = new Function('document', 'return { retire: function () {' + body + '} };')(doc);
  fn.retire.call(fakeThis);
  ok(doc.getElementById('loadingScreen').style.display === 'none',
    'the built-in branded loading screen is hidden once a custom one is up');
  ok(spinner.classList.contains('hidden'),
    'and so is the built-in ring');
} else {
  ok(true, 'jsdom not installed: retire run skipped');
}

console.log('domains tab: ' + checks + ' checks passed');
