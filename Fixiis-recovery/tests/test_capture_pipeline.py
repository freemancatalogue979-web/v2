"""Asset-rewrite behaviour and speed: python tests/test_capture_pipeline.py

The rewrite phase is where a capture spends most of its wall clock after the
DOM has been serialized: every stylesheet, image, icon and font the page
mentions has to be fetched once and re-pointed. It used to walk stylesheets
one at a time, opening a fresh HTTP client per sheet, so a page with a dozen
of them paid a dozen sequential round trips — inside a 10 s budget.

These tests run the real ``_rewrite_assets_to_cache`` against a stubbed httpx
that records how many requests are in flight at once, so the concurrency (and
the correctness of the resulting HTML) is asserted, not assumed.
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dom_capture  # noqa: E402

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


class FakeResponse:
    def __init__(self, url, content, ctype):
        self.url = url
        self.status_code = 200
        self.content = content
        self.headers = {"content-type": ctype}


class FakeClient:
    """Records peak concurrency; every request takes `latency` seconds."""

    peak = 0
    peak_inner_css = 0     # concurrency of refs fetched *from inside* sheets
    _inner_now = 0
    requests = []
    routes = {}

    def __init__(self, *args, **kwargs):
        self._inflight = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, timeout=None):
        cls = type(self)
        cls._inflight_now = getattr(cls, "_inflight_now", 0) + 1
        cls.peak = max(cls.peak, cls._inflight_now)
        cls.requests.append(url)
        inner = url.endswith(("logo.png", "font.woff2"))
        if inner:
            cls._inner_now += 1
            cls.peak_inner_css = max(cls.peak_inner_css, cls._inner_now)
        try:
            await asyncio.sleep(FakeClient.latency)
            body, ctype = cls.routes.get(url, (None, None))
            if body is None:
                fake = FakeResponse(url, b"", "text/plain")
                fake.status_code = 404
                return fake
            return FakeResponse(url, body, ctype)
        finally:
            cls._inflight_now -= 1
            if inner:
                cls._inner_now -= 1


FakeClient.latency = 0.05


def install_fake_httpx():
    mod = types.ModuleType("httpx")
    mod.AsyncClient = FakeClient
    sys.modules["httpx"] = mod


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


install_fake_httpx()

BASE = "https://bank.example/"

CSS_BODY = (
    b".logo{background-image:url(logo.png)}"
    b"@font-face{font-family:X;src:url(font.woff2)}"
)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
FONT = b"wOF2" + b"0" * 64


def page_html(n_sheets=4, n_imgs=2):
    parts = ["<html><head>"]
    for i in range(n_sheets):
        parts.append('<link rel="stylesheet" href="/css/site%d.css">' % i)
    parts.append("</head><body>")
    for i in range(n_imgs):
        parts.append('<img src="/img/hero%d.png" alt="">' % i)
    parts.append("</body></html>")
    return "".join(parts)


def seed_routes(prefix):
    FakeClient.routes = {}
    FakeClient.requests = []
    FakeClient.peak = 0
    FakeClient.peak_inner_css = 0
    FakeClient._inner_now = 0
    FakeClient._inflight_now = 0
    for i in range(4):
        FakeClient.routes[prefix + "/css/site%d.css" % i] = (CSS_BODY, "text/css")
        FakeClient.routes[prefix + "/css/logo.png"] = (PNG, "image/png")
        FakeClient.routes[prefix + "/css/font.woff2"] = (FONT, "font/woff2")
    for i in range(2):
        FakeClient.routes[prefix + "/img/hero%d.png" % i] = (PNG, "image/png")


# ---------------------------------------------------------------------------
# 1. Stylesheets are rewritten concurrently (the speed regression this fixes)
# ---------------------------------------------------------------------------
prefix = "https://bank%d.example" % os.getpid()
seed_routes(prefix)
dom_capture._LAST_REWRITE_STATS.clear()
html, meta = run(dom_capture._rewrite_assets_to_cache(page_html(), prefix + "/"))

ok(FakeClient.peak >= 2,
   "asset fetches overlap instead of running one at a time (peak %d)" % FakeClient.peak)
ok(FakeClient.peak <= dom_capture.ASSET_FETCH_CONCURRENCY,
   "the fetch pool stays bounded by ASSET_FETCH_CONCURRENCY (peak %d)" % FakeClient.peak)
ok(dom_capture.ASSET_FETCH_CONCURRENCY >= 6, "the default pool is wider than the old six")
# The regression this pins: stylesheet *inner* refs (background images, fonts)
# used to be rewritten one sheet at a time, so at most one sheet's two refs
# could ever be in flight. Now every sheet is prepared in the same pass.
ok(FakeClient.peak_inner_css >= 4,
   "refs inside different stylesheets are fetched together (peak %d, was 2)"
   % FakeClient.peak_inner_css)

# ---------------------------------------------------------------------------
# 2. The rewritten HTML is still correct
# ---------------------------------------------------------------------------
ok("<style" in html, "small stylesheets still inline as <style> blocks")
ok("data-shfcss=" in html, "the inlined stylesheet records the URL it came from")
ok("url(logo.png)" not in html,
   "url() inside a stylesheet is rewritten (no dangling relative path)")
ok("data:image/png;base64," in html, "small CSS images are embedded as data URIs")
ok("font.woff2" not in html or "data:font/woff2" in html,
   "a font referenced from CSS is resolved, not left dangling")
ok(html.count("<img") == 2 and 'src="/img/hero0.png"' not in html,
   "page images are re-pointed at the asset cache or embedded")
ok(isinstance(meta, list), "assets metadata is returned for client pre-warming")

# ---------------------------------------------------------------------------
# 3. Instrumentation: the phase reports what it cost
# ---------------------------------------------------------------------------
stats = dom_capture._LAST_REWRITE_STATS
ok(stats.get("css_sheets") == 4, "every fresh stylesheet is counted (%r)" % (stats.get("css_sheets"),))
ok(stats.get("refs", 0) >= 6, "all refs are counted (%r)" % (stats.get("refs"),))
ok(isinstance(stats.get("ms"), float) and stats["ms"] >= 0, "the phase reports its duration")
ok(stats.get("cached", 0) + stats.get("embedded", 0) >= 4, "decisions are counted per ref")

# ---------------------------------------------------------------------------
# 4. A second capture of the same page is a memo hit — no network at all
# ---------------------------------------------------------------------------
FakeClient.requests = []
FakeClient.peak = 0
html2, _ = run(dom_capture._rewrite_assets_to_cache(page_html(), prefix + "/"))
ok(len(FakeClient.requests) == 0,
   "a recapture fetches nothing that was already cached (got %d)" % len(FakeClient.requests))
ok(html2.count("<style") >= 4, "the cached stylesheets still land inline")

# ---------------------------------------------------------------------------
# 5. Budget exhaustion degrades instead of failing
# ---------------------------------------------------------------------------
prefix2 = "https://slow%d.example" % os.getpid()
seed_routes(prefix2)
FakeClient.latency = 0.5
old_budget = dom_capture._REWRITE_GLOBAL_BUDGET_S
dom_capture._REWRITE_GLOBAL_BUDGET_S = 0.15
try:
    html3, _ = run(dom_capture._rewrite_assets_to_cache(page_html(), prefix2 + "/"))
    ok(isinstance(html3, str) and "<link rel=\"stylesheet\" href=\"/css/site0.css\">" in html3,
       "a blown budget leaves the authored markup instead of raising")
finally:
    dom_capture._REWRITE_GLOBAL_BUDGET_S = old_budget
    FakeClient.latency = 0.05

# ---------------------------------------------------------------------------
# 6. No refs at all is still a cheap no-op
# ---------------------------------------------------------------------------
dom_capture._LAST_REWRITE_STATS.clear()
plain = "<html><body><p>nothing external</p></body></html>"
out, meta0 = run(dom_capture._rewrite_assets_to_cache(plain, BASE))
ok(out == plain and meta0 == [], "a page with no external refs is untouched")
ok(dom_capture._LAST_REWRITE_STATS.get("refs") == 0, "and the stats say so")

# ---------------------------------------------------------------------------
# 7. Regression: an URL change must NEVER be swallowed by the seq no-op skip
#    (SPA pushState: same document, unchanged batch seq, new URL — the new
#    content has to go out as a full document, or the mirror stays stuck on
#    the old page).
# ---------------------------------------------------------------------------
class FakeWS:
    def __init__(self):
        self.frames = []
    async def send_text(self, frame):
        self.frames.append(frame)


class FakeNavPage:
    url = "https://new.example/page"
    async def title(self):
        return "new"
    async def evaluate(self, *a, **k):
        return None


def make_nav_session(ws):
    sess = dom_capture.DOMCaptureSession(FakeNavPage(), websocket=ws, client_id="nav")
    sess.has_initial_capture = True
    sess._delta_installed = True
    sess._last_capture_supports_delta = True
    sess._last_capture_supports_assets = False   # keep the rewrite out of the way
    sess._delta_active = True
    sess._delta_seq_seen = 42
    sess.last_sent_url = "https://old.example/"
    sess.last_sent_html = "<html>old</html>"

    async def _seq():
        return 42                                # unchanged batch sequence
    sess._read_delta_seq = _seq

    async def _capture(settle=None):
        return "<html><body>new page content</body></html>"
    sess.capture_page = _capture
    return sess


ws = FakeWS()
sess = make_nav_session(ws)
run(sess._send_full("https://new.example/page", reason="url_change"))
ok(any('"full_document"' in f and "new page content" in f for f in ws.frames),
   "url_change with an unchanged seq still sends the new document (got %r)"
   % ([f[:60] for f in ws.frames],))

# ...and the no-op skip still works for a genuine same-page no-op
ws2 = FakeWS()
sess2 = make_nav_session(ws2)
sess2.last_sent_url = "https://new.example/page"
sess2.page.url = "https://new.example/page"
run(sess2._send_full("https://new.example/page", reason="click"))
ok(not any('"full_document"' in f for f in ws2.frames),
   "a same-URL no-mutation capture is still skipped")

# ---------------------------------------------------------------------------
# 8. Regression: a capture with its own <base href> resolves relative refs
#    against THAT base, not against the page URL (Gmail-style
#    "images/cleardot.gif" must not become /mail/u/0/images/... and 404).
# ---------------------------------------------------------------------------
base_prefix = "https://static%d.example" % os.getpid()
FakeClient.routes = {
    base_prefix + "/images/cleardot.gif": (PNG, "image/gif"),
}
FakeClient.requests = []
gmailish = ('<html><head><base href="' + base_prefix + '/"></head><body>'
            '<img src="images/cleardot.gif" alt=""></body></html>')
page_url = "https://mail%d.example/mail/u/0/#inbox" % os.getpid()
out, _meta = run(dom_capture._rewrite_assets_to_cache(gmailish, page_url))
ok(base_prefix + "/images/cleardot.gif" in FakeClient.requests,
   "the img ref was resolved against the document <base>, not the page URL "
   "(got %r)" % (FakeClient.requests,))
ok("images/cleardot.gif" not in out and "data:image/gif" in out,
   "the tiny image was embedded instead of left dangling: %r" % (out[:160],))
ok('<base href="' + base_prefix + '/">' in dom_capture._inject_base_href(gmailish, page_url),
   "the injected <base href> keeps the document's own base")
plain2 = "<html><head></head><body><img src=\"pic.png\"></body></html>"
ok('<base href="' + page_url + '">' in dom_capture._inject_base_href(plain2, page_url),
   "a base-less capture still gets the page URL as <base href>")

# ---------------------------------------------------------------------------
# 9. Snapshot mode for heavy hosts (YouTube/TikTok-class pages)
# ---------------------------------------------------------------------------
ok(dom_capture.is_snapshot_host("https://www.youtube.com/watch?v=x"),
   "youtube is a snapshot host")
ok(dom_capture.is_snapshot_host("https://m.tiktok.com/video/7"),
   "tiktok is a snapshot host")
ok(not dom_capture.is_snapshot_host("https://youtube.com.evil.example/"),
   "a lookalike suffix is NOT a snapshot host")
ok(not dom_capture.is_snapshot_host("https://mail.google.com/mail/u/0/"),
   "a normal page is not a snapshot host")

# embed_all inlines even images above the normal EMBED_MAX_BYTES cap
big_prefix = "https://snap%d.example" % os.getpid()
BIG_PNG = b"\x89PNG\r\n\x1a\n" + b"1" * (1024 * 1024 + 10)   # > 1 MB default cap
FakeClient.routes = {big_prefix + "/big.png": (BIG_PNG, "image/png")}
FakeClient.requests = []
snap_html = "<html><head></head><body><img src=\"/big.png\"></body></html>"
out_snap, _ = run(dom_capture._rewrite_assets_to_cache(snap_html, big_prefix + "/", embed_all=True))
ok("data:image/png;base64," in out_snap and "/big.png" not in out_snap,
   "embed_all inlines an over-cap image as a data URI (snapshot is self-contained)")
out_norm, _ = run(dom_capture._rewrite_assets_to_cache(snap_html, big_prefix + "/", embed=True))
ok("/assets/" in out_norm and "data:image/png" not in out_norm,
   "the normal path still uses the /assets cache for over-cap images")

# ...but the total snapshot embed budget stops runaway pages
old_budget = dom_capture.SNAPSHOT_EMBED_TOTAL_MAX_BYTES
dom_capture.SNAPSHOT_EMBED_TOTAL_MAX_BYTES = 1024 * 1024 + 64   # room for one image only
try:
    two = ('<html><body><img src="/a.png"><img src="/big.png"></body></html>')
    FakeClient.routes[big_prefix + "/a.png"] = (PNG, "image/png")
    out_b, _ = run(dom_capture._rewrite_assets_to_cache(two, big_prefix + "/", embed_all=True))
    ok("data:image/png" in out_b and "/assets/" in out_b,
       "past the total budget the rest keeps /assets refs (frame stays sendable)")
finally:
    dom_capture.SNAPSHOT_EMBED_TOTAL_MAX_BYTES = old_budget

# a snapshot-host send: full document, NO delta flag, snapshot_only on, and
# a byte-identical recapture is skipped instead of re-sent
old_hosts = dom_capture.SNAPSHOT_HOSTS
dom_capture.SNAPSHOT_HOSTS = frozenset({"snap%d.example" % os.getpid()})
try:
    FakeClient.routes = {big_prefix + "/big.png": (BIG_PNG, "image/png")}
    ws3 = FakeWS()
    sess3 = make_nav_session(ws3)
    sess3.page.url = big_prefix + "/watch"
    sess3._delta_installed = False            # isolate from the seq/checksum skips
    sess3._last_capture_supports_assets = True
    async def _cap3(settle=None):
        return '<html><body><img src="/big.png"></body></html>'
    sess3.capture_page = _cap3
    run(sess3._send_full(big_prefix + "/watch", reason="navigation"))
    frames3 = [f for f in ws3.frames if '"full_document"' in f]
    ok(len(frames3) == 1, "snapshot host got exactly one full document")
    ok('"delta": true' not in frames3[0],
       "snapshot host sends NO delta flag — the client re-renders, never patches")
    ok("data:image/png;base64," in frames3[0],
       "the snapshot document carries the inlined image (self-contained)")
    ok(sess3.snapshot_only is True, "snapshot_only is armed for the host")
    run(sess3._send_full(big_prefix + "/watch", reason="click"))
    frames3b = [f for f in ws3.frames if '"full_document"' in f]
    ok(len(frames3b) == 1, "a byte-identical snapshot recapture is skipped")
finally:
    dom_capture.SNAPSHOT_HOSTS = old_hosts

print("capture pipeline: %d checks passed" % PASSED)
