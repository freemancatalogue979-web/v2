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

print("capture pipeline: %d checks passed" % PASSED)
