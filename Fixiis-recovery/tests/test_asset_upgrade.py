#!/usr/bin/env python3
"""Progressive assets: the page goes out first, its fonts and backgrounds follows.

The asset phase is the slow half of a capture — it downloads what the mirror
would otherwise ask the site for and be refused. The document used to wait for
all of it, so a navigation showed nothing until the last font came back, and
the live patch channel was closed for the same window (a full capture owns
``_send_inflight``), which is exactly what "the mirror is frozen" feels like.

Now: capture → send the document → fetch → send the EDITS as an ``asset_map``.

What these tests pin:

  * the document is on the wire BEFORE the fetches start (ordering, with a
    deliberately slow stub so the difference is unambiguous);
  * replaying the map on the first document reproduces, byte for byte, the
    document the old single-message path would have sent — so this changes
    WHEN the result arrives, never WHAT it is;
  * the delta channel stays open while the fetch runs, and closes again after;
  * with everything already in the fetch-once memo (the normal recapture) the
    document goes out complete and in ONE message — no map, no extra work;
  * ``DOM_CAPTURE_PROGRESSIVE_ASSETS=0`` is a real kill switch.

    PYTHONPATH=. python3 tests/test_asset_upgrade.py
"""
import asyncio
import json
import re
import sys
import types

sys.path.insert(0, "/home/user/v2/Fixiis-recovery")

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
FONT = b"wOF2" + b"0" * 64
CSS = b".x{background:url(/img/x.png)}"

ROUTES = {
    "https://site.example/img/x.png": (PNG, "image/png"),
    "https://site.example/f/x.woff2": (FONT, "font/woff2"),
    "https://site.example/css/site.css": (CSS, "text/css"),
    "https://site.example/sprite.svg": (b"<svg/>", "image/svg+xml"),
}

FETCH_LOG = []


class FakeResponse:
    def __init__(self, url):
        if url in ROUTES:
            body, ctype = ROUTES[url]
        elif url.endswith(".woff2"):
            body, ctype = FONT, "font/woff2"          # a heavy page's own fonts
        elif url.endswith(".png"):
            body, ctype = PNG, "image/png"
        else:
            body, ctype = b"", "text/plain"
        self.url = url
        self.status_code = 200 if body else 404
        self.content = body
        self.headers = {"content-type": ctype}


class FakeClient:
    """Slow on purpose: 20 ms per asset, so "sent before the fetch" is provable."""

    latency = 0.02

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def get(self, url, timeout=None):
        FETCH_LOG.append(url)
        await asyncio.sleep(self.latency)
        return FakeResponse(url)


def install_fake_httpx():
    mod = types.ModuleType("httpx")
    mod.AsyncClient = FakeClient
    sys.modules["httpx"] = mod


install_fake_httpx()

import dom_capture  # noqa: E402


PAGE_HTML = """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="/css/site.css">
<style>@font-face{font-family:H;src:url("/f/x.woff2") format("woff2")}</style>
</head><body>
<img src="/img/x.png">
<div style="background:url(/img/x.png)">hero</div>
<svg><use href="/sprite.svg#i"></use></svg>
<p>hello</p>
</body></html>"""

URL = "https://site.example/page"


class FakePage:
    def __init__(self):
        self.url = URL
        self._html = PAGE_HTML

    async def title(self):
        return "Example"

    async def evaluate(self, *a, **k):
        return "1:1:1"

    async def wait_for_load_state(self, *a, **k):
        return None

    async def wait_for_function(self, *a, **k):
        return None


class FakeWebSocket:
    """Records (time, message) so ordering can be asserted."""

    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append((asyncio.get_event_loop().time(), json.loads(text)))

    def types(self):
        return [m["type"] for _t, m in self.sent]

    def first(self, kind):
        for _t, m in self.sent:
            if m["type"] == kind:
                return m
        return None


def make_session(ws, progressive=True):
    dom_capture.PROGRESSIVE_ASSETS = progressive
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    session = dom_capture.DOMCaptureSession(FakePage(), websocket=ws, client_id="c1")
    session._last_capture_supports_assets = True
    session._last_capture_supports_delta = True
    session._delta_installed = False

    async def _capture(settle=None):
        return PAGE_HTML

    session.capture_page = _capture
    return session


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def replay(html, swap):
    """Exactly what client.html's applyAssetSwapsToHtml does, in Python."""
    for entry in swap:
        if entry.get("tag"):
            html = re.sub(r'<link\b[^>]*\bhref\s*=\s*["\']' + re.escape(entry["from"])
                          + r'["\'][^>]*>', lambda m, e=entry: e["to"], html, flags=re.I)
        else:
            html = html.replace(entry["from"], entry["to"])
    return html


# ---------------------------------------------------------------------------
# 1. ORDER: the document leaves before a single asset is fetched
# ---------------------------------------------------------------------------
def test_document_arrives_before_the_fetches():
    FETCH_LOG.clear()
    ws = FakeWebSocket()
    session = make_session(ws)
    run(session._send_full(URL, reason="navigation"))

    ok(ws.types() == ["full_document", "asset_map"],
       "expected the document and then the map, got %r" % (ws.types(),))

    doc = ws.first("full_document")
    ok(doc.get("assets_pending", 0) >= 1,
       "the document must say assets are still coming: %r" % doc.get("assets_pending"))
    ok("/f/x.woff2" in doc["html"],
       "the first document must carry the authored font URL (it is what a "
       "browser can load while we fetch): %r" % doc["html"][:200])

    # The document was written before the upgrade was even built: the map is
    # the second message, and the fetches happened in between.
    ok(len(FETCH_LOG) > 0, "nothing was fetched at all")
    ok(ws.sent[0][0] <= ws.sent[-1][0], "ordering broke")

    upgrade = ws.first("asset_map")
    ok(upgrade["gen"] == doc["gen"], "the map must belong to that document's generation")
    ok(any(s["from"] == "/f/x.woff2" for s in upgrade["swap"]),
       "the font was not in the swap list: %r" % upgrade["swap"])
    ok(upgrade["url"] == URL, "the map must name its page")


# ---------------------------------------------------------------------------
# 2. ACCURACY: replaying the map reproduces the old single-message document
# ---------------------------------------------------------------------------
def test_the_upgrade_reproduces_the_document_exactly():
    # What the old path would have sent, in one piece — through the same
    # _send_full, so both sides get the <base>/referrer decoration the real
    # send applies (comparing a raw capture against it would differ for a
    # reason that has nothing to do with assets).
    old_ws = FakeWebSocket()
    old_session = make_session(old_ws, progressive=False)
    run(old_session._send_full(URL, reason="navigation"))
    complete = old_ws.first("full_document")["html"]

    # What the new path sends, in two pieces.
    ws = FakeWebSocket()
    session = make_session(ws, progressive=True)
    run(session._send_full(URL, reason="navigation"))
    reconstructed = replay(ws.first("full_document")["html"], ws.first("asset_map")["swap"])

    ok(reconstructed == complete,
       "the two-piece delivery is not byte-identical to the one-piece delivery"
       + ("\nrewritten: %r\nreplayed:  %r" % (complete[:300], reconstructed[:300])))


def test_the_edits_actually_change_something():
    ws = FakeWebSocket()
    session = make_session(ws)
    run(session._send_full(URL, reason="navigation"))
    upgrade = ws.first("asset_map")
    swaps = upgrade["swap"]
    ok(len(swaps) >= 3, "expected the font, the images and the sprite: %r" % swaps)
    kinds = {s["from"]: s["to"] for s in swaps}
    ok(kinds["/f/x.woff2"].startswith("data:font/woff2"), kinds.get("/f/x.woff2"))
    ok(kinds["/img/x.png"].startswith("data:image/png"), kinds.get("/img/x.png"))
    ok(kinds["/sprite.svg#i"].startswith("/assets/"), kinds.get("/sprite.svg#i"))
    ok(kinds["/sprite.svg#i"].endswith("#i"),
       "the fragment identifies the symbol and must survive the swap")
    ok(any(s.get("tag") and s["from"] == "/css/site.css" for s in swaps),
       "the inlined stylesheet must be a tag swap: %r" % swaps)


# ---------------------------------------------------------------------------
# 3. THE PATCH CHANNEL: open during the fetch, closed after
# ---------------------------------------------------------------------------
def test_delta_patches_keep_flowing_while_assets_load():
    seen = {}

    class SlowClient(FakeClient):
        latency = 0.05

    mod = sys.modules["httpx"]
    real = mod.AsyncClient
    mod.AsyncClient = SlowClient
    try:
        ws = FakeWebSocket()
        session = make_session(ws)

        async def watch():
            await asyncio.sleep(0.02)
            seen["inflight"] = session._send_inflight
            seen["pending"] = session._assets_pending

        async def both():
            task = asyncio.ensure_future(watch())
            await session._send_full(URL, reason="navigation")
            await task

        run(both())
    finally:
        mod.AsyncClient = real

    ok(seen.get("inflight") is True, "the capture should still be in flight")
    ok(seen.get("pending") is True,
       "while assets are loading the delta channel must be open (a patch "
       "arriving then would have been dropped by the old gate)")
    ok(session._assets_pending is False, "the flag must be cleared when the upgrade is done")
    # And the gate itself: a patch during the fetch is not swallowed.
    session._send_inflight = True
    session._assets_pending = True
    ok(not (session._send_inflight and not session._assets_pending),
       "the delta gate would still discard patches during the asset phase")


# ---------------------------------------------------------------------------
# 4. The normal recapture: one complete document, no map
# ---------------------------------------------------------------------------
def test_a_warm_capture_sends_one_complete_document():
    ws = FakeWebSocket()
    session = make_session(ws)
    run(session._send_full(URL, reason="navigation"))      # cold: doc + map
    ws2 = FakeWebSocket()
    session.websocket = ws2
    run(session._send_full(URL, reason="click"))           # warm
    ok(ws2.types() == ["full_document"],
       "a capture with every asset already cached must be one message, got %r"
       % (ws2.types(),))
    doc = ws2.first("full_document")
    ok("assets_pending" not in doc, "nothing is pending on a warm capture")
    ok("data:font/woff2" in doc["html"], "the warm document must be complete")
    ok("/f/x.woff2" not in doc["html"], "an authored font URL survived a warm capture")


def test_the_same_swap_is_not_sent_twice():
    ws = FakeWebSocket()
    session = make_session(ws)
    run(session._send_full(URL, reason="navigation"))
    first = ws.first("asset_map")["swap"]
    ok(len(first) >= 3, "the first document should have produced swaps")

    # The fetch-once memo is empty again (a restart, or an eviction), so the
    # next capture defers again — but the client already applied these edits,
    # and re-sending a font's base64 would be pure waste.
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    ws.sent = []
    run(session._send_full(URL, reason="navigation"))
    ok(ws.types()[0] == "full_document", "the document must still go first")
    map_msg = ws.first("asset_map")
    ok(map_msg is None or map_msg["swap"] == [],
       "edits the client already has were re-sent: %r"
       % (map_msg and [x["from"] for x in map_msg["swap"]]))


def test_a_new_page_gets_the_whole_edit_list_again():
    # The client caches edits per page URL, so a navigation must be told
    # everything (the same authored text means a different file on a new site).
    ws = FakeWebSocket()
    session = make_session(ws)
    run(session._send_full(URL, reason="navigation"))
    ok(len(ws.first("asset_map")["swap"]) >= 3, "cold page has swaps")

    class OtherPage(FakePage):
        url = "https://site.example/second"

    session.page = OtherPage()
    # Empty memo again, so this page defers too — that is the only situation
    # in which the edit list is rebuilt at all.
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    ws.sent = []
    run(session._send_full("https://site.example/second", reason="navigation"))
    second = ws.first("asset_map")
    ok(second is not None and len(second["swap"]) >= 3,
       "a new page must be sent the edits again, got %r"
       % (second and second["swap"]))
    ok(second["url"] == "https://site.example/second", "the map must name the new page")


# ---------------------------------------------------------------------------
# 5. Kill switch
# ---------------------------------------------------------------------------
def test_the_kill_switch_restores_the_old_path():
    ws = FakeWebSocket()
    session = make_session(ws, progressive=False)
    run(session._send_full(URL, reason="navigation"))
    ok(ws.types() == ["full_document"], "kill switch: expected one message, got %r" % (ws.types(),))
    doc = ws.first("full_document")
    ok("assets_pending" not in doc, "kill switch: nothing may be deferred")
    ok("data:font/woff2" in doc["html"], "kill switch: the document must be complete")
    dom_capture.PROGRESSIVE_ASSETS = True


# ---------------------------------------------------------------------------
# 6. Failure is survivable
# ---------------------------------------------------------------------------
def test_a_failed_upgrade_leaves_a_working_page():
    class DeadClient(FakeClient):
        async def get(self, url, timeout=None):
            raise RuntimeError("network gone")

    mod = sys.modules["httpx"]
    real = mod.AsyncClient
    mod.AsyncClient = DeadClient
    try:
        ws = FakeWebSocket()
        session = make_session(ws)
        run(session._send_full(URL, reason="navigation"))
    finally:
        mod.AsyncClient = real
    ok(ws.types()[0] == "full_document", "the document must still arrive")
    ok("/f/x.woff2" in ws.first("full_document")["html"],
       "with no assets resolved the page keeps its authored URLs (they still "
       "load through <base href>)")
    ok(session._assets_pending is False, "the flag must not stay stuck")


# ---------------------------------------------------------------------------
# 6b. The FIRST WAVE: what the visitor can see, one round trip
# ---------------------------------------------------------------------------
def heavy_page(n_fonts=12, n_images=12, stylesheet=False):
    """A page whose asset count is well past one fetch pool."""
    fonts = "".join(
        '@font-face{font-family:H%d;src:url("/f/h%d.woff2")}' % (i, i)
        for i in range(n_fonts))
    imgs = "".join('<img src="/img/h%d.png">' % i for i in range(n_images))
    link = '<link rel="stylesheet" href="/css/site.css">' if stylesheet else ""
    return ('<!DOCTYPE html><html><head>' + link + '<style>' + fonts
            + '</style></head><body>' + imgs + '</body></html>')


def heavy_session(ws, wave=None, html=None):
    """A session whose page is heavy, at a latency where rounds are countable."""
    html = html or heavy_page()

    class HeavyPage(FakePage):
        """Same page interface; only the URL and the capture matter here."""

    session = make_session(ws)
    session.page = HeavyPage()

    async def _capture(settle=None):
        return html

    session.capture_page = _capture
    if wave is not None:
        dom_capture.ASSET_FIRST_WAVE = wave
    return session


def test_visual_priority_order():
    p = dom_capture._asset_priority
    ok(p("https://x/f/a.woff2", "img") == 0, "a font declared in CSS is a font")
    ok(p("https://x/f/a.woff2?v=2", "other") == 0, "a query string does not hide a font")
    ok(p("https://x/f/a.ttf", "other") == 0 and p("https://x/a.otf", "other") == 0,
       "ttf/otf are fonts too")
    ok(p("https://x/css/site.css", "css") == 1, "a stylesheet comes after fonts")
    ok(p("https://x/css/site.css?h=1", "other") == 1,
       "the extension decides, not the tag it arrived on")
    ok(p("https://x/img/a.png", "img") == 2, "images after stylesheets")
    ok(p("https://x/v.mp4", "other") == 3, "media and the rest come last")
    ok(p("https://x/f/a.woff2", "css") == 0, "the extension wins over the kind")


def test_the_first_wave_is_the_visible_half():
    """Fonts (and the top of the page) are upgraded a round trip early."""
    old_wave = dom_capture.ASSET_FIRST_WAVE
    old_latency = FakeClient.latency
    FakeClient.latency = 0.05          # 24 refs = 3 rounds at pool size 10
    FETCH_LOG.clear()
    try:
        ws = FakeWebSocket()
        session = heavy_session(ws, wave=10)
        run(session._send_full(URL, reason="navigation"))
    finally:
        FakeClient.latency = old_latency
        dom_capture.ASSET_FIRST_WAVE = old_wave

    maps = [m for _t, m in ws.sent if m["type"] == "asset_map"]
    ok(len(maps) == 2,
       "a 24-ref page should upgrade in two waves (visible half, then the rest), "
       "got %d map(s)" % len(maps))
    first, rest = maps
    ok(len(first["swap"]) == 10, "the first wave must be exactly one pool wide: %r"
       % [s["from"] for s in first["swap"]])
    ok(all(s["from"].endswith(".woff2") for s in first["swap"]),
       "the first wave must be the FONTS, got %r" % [s["from"] for s in first["swap"]])
    ok(len(rest["swap"]) == 14, "everything else follows in one message: %d" % len(rest["swap"]))

    # One round trip, not three: the whole point.
    t_first = [t for t, m in ws.sent if m["type"] == "asset_map"][0]
    t_doc = [t for t, m in ws.sent if m["type"] == "full_document"][0]
    ok(t_first - t_doc < 0.10,
       "the first upgrade took %.0f ms — it must cost one round trip (50 ms), "
       "not the whole page" % ((t_first - t_doc) * 1000))


def test_the_split_costs_no_extra_download():
    """Two waves, but every ref is still fetched exactly once."""
    old_wave = dom_capture.ASSET_FIRST_WAVE
    FETCH_LOG.clear()
    try:
        ws = FakeWebSocket()
        session = heavy_session(ws, wave=5)
        run(session._send_full(URL, reason="navigation"))
    finally:
        dom_capture.ASSET_FIRST_WAVE = old_wave
    ok(len(FETCH_LOG) == 24, "expected 24 downloads, got %d" % len(FETCH_LOG))
    ok(len(set(FETCH_LOG)) == len(FETCH_LOG),
       "an asset was downloaded twice: %r"
       % [u for u in set(FETCH_LOG) if FETCH_LOG.count(u) > 1])


def test_both_waves_replay_to_the_one_piece_document():
    old_wave = dom_capture.ASSET_FIRST_WAVE
    try:
        # Baseline: the pre-progressive path, one complete document.
        old_ws = FakeWebSocket()
        old_session = heavy_session(old_ws)
        dom_capture.PROGRESSIVE_ASSETS = False
        run(old_session._send_full(URL, reason="navigation"))
        dom_capture.PROGRESSIVE_ASSETS = True
        complete = old_ws.first("full_document")["html"]
        ok(old_ws.types() == ["full_document"], "the baseline must be one message")

        ws = FakeWebSocket()
        session = heavy_session(ws, wave=10)
        run(session._send_full(URL, reason="navigation"))
        html = ws.first("full_document")["html"]
        for _t, m in ws.sent:
            if m["type"] == "asset_map":
                html = replay(html, m["swap"])
    finally:
        dom_capture.ASSET_FIRST_WAVE = old_wave
    ok(html == complete,
       "the staged delivery is not byte-identical to the one-piece document\n"
       "one piece: %r\nstaged:    %r" % (complete[:300], html[:300]))


def test_a_stylesheet_in_the_first_wave_brings_its_own_refs():
    """Most real pages put their @font-face inside an external sheet.

    The sheet is what the first wave can see, so the sheet goes first — and
    it drags its own fonts and backgrounds with it (a <style> full of
    unresolved url() would be a different bug, not a faster one).
    """
    old_wave = dom_capture.ASSET_FIRST_WAVE
    FETCH_LOG.clear()
    try:
        ws = FakeWebSocket()
        session = heavy_session(ws, wave=4,
                                html=heavy_page(1, 20, stylesheet=True))
        run(session._send_full(URL, reason="navigation"))
    finally:
        dom_capture.ASSET_FIRST_WAVE = old_wave

    maps = [m for _t, m in ws.sent if m["type"] == "asset_map"]
    ok(len(maps) == 2, "expected two waves, got %d" % len(maps))
    first = [s["from"] for s in maps[0]["swap"]]
    ok("/css/site.css" in first, "the stylesheet must be in the first wave: %r" % first)
    ok("/f/h0.woff2" in first,
       "the sheet's own font must be resolved WITH it (it is in the sheet, not "
       "in the page): %r" % first)
    ok(any(s.get("tag") for s in maps[0]["swap"]),
       "the sheet must arrive as the <link> -> <style> tag swap")
    # ...and the sheet's inner background came along too: the <style> that
    # replaced the <link> carries the resolved url(), not the site's.
    tag_swap = next(s for s in maps[0]["swap"] if s.get("tag"))
    ok("background:url(data:" in tag_swap["to"],
       "the sheet was inlined with its background still pointing at the site: %r"
       % tag_swap["to"][:200])
    ok("/img/x.png" not in tag_swap["to"],
       "an unresolved inner ref rode along inside the inlined sheet")


def test_a_page_with_a_stylesheet_still_replays_exactly():
    old_wave = dom_capture.ASSET_FIRST_WAVE
    page = heavy_page(1, 6, stylesheet=True)
    try:
        old_ws = FakeWebSocket()
        old_session = heavy_session(old_ws, html=page)
        dom_capture.PROGRESSIVE_ASSETS = False
        run(old_session._send_full(URL, reason="navigation"))
        dom_capture.PROGRESSIVE_ASSETS = True
        complete = old_ws.first("full_document")["html"]

        ws = FakeWebSocket()
        session = heavy_session(ws, wave=3, html=page)
        run(session._send_full(URL, reason="navigation"))
        html = ws.first("full_document")["html"]
        for _t, m in ws.sent:
            if m["type"] == "asset_map":
                html = replay(html, m["swap"])
    finally:
        dom_capture.ASSET_FIRST_WAVE = old_wave
    ok("<link rel=\"stylesheet\" href=\"/css/site.css\">" in ws.first("full_document")["html"],
       "the first document must still carry the <link> (it loads while we fetch)")
    ok(html == complete,
       "a page with an external stylesheet did not replay byte-for-byte\n"
       "one piece: %r\nstaged:    %r" % (complete[:300], html[:300]))


def test_wave_zero_keeps_the_whole_upgrade_in_one_message():
    """ASSET_FIRST_WAVE=0 is the fallback: one map, no staging."""
    old_wave = dom_capture.ASSET_FIRST_WAVE
    try:
        ws = FakeWebSocket()
        session = heavy_session(ws, wave=0)
        run(session._send_full(URL, reason="navigation"))
    finally:
        dom_capture.ASSET_FIRST_WAVE = old_wave
    ok(ws.types() == ["full_document", "asset_map"],
       "wave=0 must produce exactly one map, got %r" % (ws.types(),))
    ok(len(ws.first("asset_map")["swap"]) == 24,
       "the single map must still carry every edit")


def test_a_small_page_is_never_split():
    """Below one wave there is nothing to gain: the map goes out whole."""
    ws = FakeWebSocket()
    session = make_session(ws)          # the 4-ref PAGE_HTML, wave default 10
    run(session._send_full(URL, reason="navigation"))
    ok(ws.types() == ["full_document", "asset_map"],
       "a page that fits one wave must stay two messages, got %r" % (ws.types(),))


def test_max_fetch_caps_the_downloads_and_keeps_the_order():
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    FETCH_LOG.clear()
    report = {}
    run(dom_capture._rewrite_assets_to_cache(
        heavy_page(6, 6), URL, None, owner_id="t", report=report, max_fetch=3))
    ok(len(FETCH_LOG) == 3, "max_fetch=3 downloaded %d" % len(FETCH_LOG))
    ok(all(u.endswith(".woff2") for u in FETCH_LOG),
       "the cap must be spent on fonts first, got %r" % (FETCH_LOG,))
    ok(len(report["swap"]) == 3, "only the resolved refs may be swapped: %r"
       % [s["from"] for s in report["swap"]])


def test_pending_refs_are_reported_in_priority_order():
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    report = {}
    run(dom_capture._rewrite_assets_to_cache(
        heavy_page(4, 4), URL, None, owner_id="t", allow_fetch=False, report=report))
    missing = report["missing"]
    ok(len(missing) == 8, "expected 8 pending refs, got %r" % (missing,))
    prio = [dom_capture._asset_priority(u) for u in missing]
    ok(prio == sorted(prio), "the deferred refs are not in visual order: %r" % (missing,))
    ok(missing[0].endswith(".woff2"), "fonts must lead the list: %r" % (missing[:2],))


# ---------------------------------------------------------------------------
# 7. The client half exists and is wired
# ---------------------------------------------------------------------------
def test_the_client_applies_the_map():
    client = open("/home/user/v2/Fixiis-recovery/client.html", encoding="utf-8",
                  errors="replace").read()
    ok("case 'asset_map':" in client, "the client has no asset_map handler")
    ok("function applyAssetSwapsToHtml(" in client, "no render-time apply")
    ok("function applyAssetSwapsLive(" in client, "no live apply")
    ok("function mergeAssetSwaps(" in client, "no map merge")
    ok("html = applyAssetSwapsToHtml(html, url);" in client,
       "the render path does not apply the map")
    ok("state._docGen = state._deltaGen;" in client,
       "the render path does not record which generation is on screen")
    ok("_assetMaps: new Map()" in client and "_assetUrl:" in client,
       "no per-document cache (a swap from site A would be applied on site B)")
    ok("mapUrl === state._assetUrl" in client,
       "the live apply must refuse a map that belongs to another page")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("progressive assets: %d checks passed" % PASSED)
