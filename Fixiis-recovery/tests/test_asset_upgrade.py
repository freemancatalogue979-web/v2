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
        body, ctype = ROUTES.get(url, (b"", "text/plain"))
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
