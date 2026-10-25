// node tests/test_loading_handover.cjs
//
// A loading screen is a loading screen: the moment the mirrored page is up it
// has to be gone, and it must never paint itself back over the page.
//
// The bug this file pins down was a race, and it is invisible in the source:
// when a push named a site whose loading screen was NOT the copy embedded in
// the document, the runtime downloaded that screen and revealed it when the
// download finished — even if the page it was covering had landed a moment
// earlier. The visitor then sat on a spinner with the page behind it, which
// reads as "the custom loading screen never switches to the mirrored content".
//
// So this runs the real client.html under jsdom, dispatches the same websocket
// messages a visitor receives, and looks at what is actually on screen:
//
//   * boot copy (the embedded loading page) gone once a page commits;
//   * the runtime's own loading-page frame hidden, even when its download
//     finishes AFTER the page;
//   * the mirrored frame present, the built-in ring hidden.
//
// jsdom is optional; without it the source checks still run.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');
const client = fs.readFileSync(path.join(root, 'client.html'), 'utf8');

let jsdom = null;
for (const where of ['jsdom', path.join(process.cwd(), 'node_modules/jsdom'),
                     '/tmp/domtest/node_modules/jsdom']) {
  try { jsdom = require(where); break; } catch (e) { /* keep looking */ }
}

// ---------------------------------------------------------------------------
// 1. The hand-over is in the source, in the right places
// ---------------------------------------------------------------------------
{
  ok(/_navEpoch: 0,/.test(client),
    'the runtime counts how many times the screen has moved on');

  const showRedirect = client.slice(client.indexOf('_showRedirect(show) {'));
  ok(/if \(!show\) \{\s*this\._navEpoch = \(this\._navEpoch \|\| 0\) \+ 1;/.test(showRedirect),
    'dismissing the loading page counts as moving on');

  ok(/const epoch = this\._navEpoch = \(this\._navEpoch \|\| 0\) \+ 1;/.test(showRedirect),
    'a loading page remembers which moment it belongs to');

  ok(/if \(self\._navEpoch === epoch\) \{\s*frame\.classList\.remove\('hidden'\);/.test(showRedirect),
    'and only reveals itself if nothing has moved on since');

  ok(/loading page finished after the page did; staying hidden/.test(showRedirect),
    'a late download is dropped instead of drawn');

  const commit = client.slice(client.indexOf("// The pushed page is up: the embedded custom spinner"));
  ok(/this\._navEpoch = \(this\._navEpoch \|\| 0\) \+ 1;/.test(commit.slice(0, 600)),
    'committing a mirrored page moves the screen on');

  ok(/try \{ lpvDropBootLoading\(\); \} catch \(e\) \{\}/.test(commit.slice(0, 400)),
    'and the embedded copy is removed at the same moment');
}

// ---------------------------------------------------------------------------
// 2. What a visitor actually ends up looking at (real document, jsdom)
// ---------------------------------------------------------------------------
const PAGE_HTML = '<!doctype html><html><body><h1>MIRRORED-CONTENT</h1></body></html>';
const SPINNER_HTML = '<!doctype html><html><body><h1>OTHER-SPINNER</h1></body></html>';

// The served document carries the embedded copy; put one in so the runtime has
// the same starting point a visitor gets.
function documentWithBootCopy() {
  const tag =
    '<div id="lpvBootLoading" data-lpv-url="/api/lpv/domains/mirror.example.test/spinner.html">' +
    '<iframe id="lpvBootLoadingFrame"></iframe></div>';
  const withStyle = client.includes('id="lpv-boot-loading-style"') ? client
    : client.replace('</head>', '<style id="lpv-boot-loading-style">' +
        '#lpvBootLoading{position:fixed;inset:0;z-index:2147483646;background:#fff}' +
        '.lpv-boot-loading-frame{position:absolute;inset:0;width:100%;height:100%;border:0}' +
        '</style></head>');
  const at = withStyle.indexOf('<body');
  const end = withStyle.indexOf('>', at) + 1;
  return withStyle.slice(0, end) + tag + withStyle.slice(end);
}

function runCase(label, { spinnerUrl, slowSpinner }) {
  return new Promise((resolve) => {
    const errors = [];
    const messages = [];
    const vc = new jsdom.VirtualConsole();
    // jsdom cannot parse a couple of the newer CSS constructs in the page;
    // that is noise about styling, not about the hand-over.
    vc.on('jsdomError', (e) => {
      const text = String(e.message || e);
      if (text.indexOf('Could not parse CSS') >= 0) return;
      errors.push(text.slice(0, 200));
    });

    class FakeWS {
      constructor(url) {
        this.url = url;
        this.readyState = 1;
        FakeWS.last = this;
        setTimeout(() => this.onopen && this.onopen({}), 1);
      }
      send() {}
      close() { this.readyState = 3; }
    }
    FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSED = 3;

    const dom = new jsdom.JSDOM(documentWithBootCopy(), {
      url: 'http://localhost/client.html',
      runScripts: 'dangerously',
      pretendToBeVisual: true,
      virtualConsole: vc,
      beforeParse(win) {
        win.WebSocket = FakeWS;
        win.fetch = (url) => {
          const u = String(url && url.url ? url.url : url);
          const reply = (text) => ({ ok: true, status: 200, url: u,
                                     text: async () => text, json: async () => ({}) });
          if (u.indexOf('/spinner.html') >= 0) {
            // The race: the loading page's own download is slower than the
            // page it is supposed to cover.
            return slowSpinner
              ? new Promise((done) => setTimeout(() => done(reply(SPINNER_HTML)), 350))
              : Promise.resolve(reply(SPINNER_HTML));
          }
          if (u.indexOf('/api/lpv/') >= 0) return Promise.resolve(reply(PAGE_HTML));
          return Promise.resolve(reply('{}'));
        };
        win.URL.createObjectURL = () => 'blob:fake';
        win.URL.revokeObjectURL = () => {};
      },
    });

    setTimeout(() => {
      const win = dom.window;
      const doc = win.document;
      const sock = FakeWS.last;
      const bootBefore = doc.getElementById('lpvBootLoading');
      try {
        sock.onmessage({ data: JSON.stringify({
          type: 'lpv_show_page',
          page_id: 'p1',
          page_url: '/api/lpv/archive/p1/html',
          page_name: 'Pushed page',
          guard: null,
          actions: [],
          spinner_url: spinnerUrl,
        }) });
      } catch (e) { errors.push('dispatch: ' + e.message); }

      setTimeout(() => {
        const boot = doc.getElementById('lpvBootLoading');
        const redirect = doc.querySelector('.lpv-redirect-frame');
        const frame = doc.querySelector('iframe[data-lpv-frame]');
        const ring = doc.getElementById('lpv-spinner');
        const loadingScreen = doc.getElementById('loadingScreen');
        const display = redirect ? win.getComputedStyle(redirect).display : '';
        resolve({
          label,
          errors,
          hadBoot: !!bootBefore,
          bootGone: !boot,
          mirroredFrame: !!frame,
          spinnerHidden: ring ? ring.classList.contains('hidden') : null,
          loadingScreen: loadingScreen ? loadingScreen.style.display : '(missing)',
          redirectState: redirect ? display : null,
          redirectHidden: redirect ? (display === 'none' || redirect.classList.contains('hidden')) : true,
          messages,
        });
      }, 700);
    }, 900);
  });
}

(async () => {
  if (!jsdom) {
    ok(true, 'jsdom not installed: the hand-over half is skipped');
  } else {
    // (a) the push names the same loading screen the document already shows
    const same = await runCase('same', {
      spinnerUrl: '/api/lpv/domains/mirror.example.test/spinner.html',
    });
    ok(same.errors.length === 0, 'no runtime errors on a normal push');
    ok(same.hadBoot, 'the visitor starts on the embedded loading page');
    ok(same.bootGone, 'the embedded loading page is removed once the page commits');
    ok(same.mirroredFrame, 'the mirrored page is on screen');
    ok(same.spinnerHidden === true, 'the built-in ring stays hidden');
    ok(same.loadingScreen === 'none', 'and the branded loading screen never comes back');

    // (b) the push names a DIFFERENT site, whose loading screen finishes
    //     downloading after the page already landed — the stuck case
    const late = await runCase('late', {
      spinnerUrl: '/api/lpv/domains/other.example.test/spinner.html',
      slowSpinner: true,
    });
    ok(late.errors.length === 0, 'no runtime errors when the download is slow');
    ok(late.bootGone, 'the embedded loading page is gone');
    ok(late.mirroredFrame, 'the mirrored page is on screen');
    ok(late.redirectHidden,
      'a loading page that finishes after the page does NOT paint over it');
    ok(late.spinnerHidden === true, 'and the ring does not flash either');
  }

  console.log('loading hand-over: ' + checks + ' checks passed');
  // The page keeps websocket/reconnect timers alive; nothing else to do here.
  process.exit(0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
