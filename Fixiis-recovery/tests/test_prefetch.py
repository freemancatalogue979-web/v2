#!/usr/bin/env python3
"""Intent prefetch: the next page's assets, fetched before the click.

Every other optimisation in this pipeline shortens a wait the viewer is
already in.  This one removes the wait: the cursor resting on a link is
enough to know which document comes next, so that document is fetched and its
assets are warmed into the fetch-once memo while the viewer is still reading.

When the navigation then arrives, the capture finds nothing missing and sends
ONE complete document — no `assets_pending`, no `asset_map`, nothing to fill
in.  That is the whole claim, and it is what the first test measures.

What else is pinned, because a prefetch is a background fetch on the operator's
bandwidth and must never be a liability:

  * same-origin only — a mirror must not become a proxy for arbitrary URLs;
  * never the page you are already on, never a fragment, never a non-HTML body;
  * a hard budget per page, one at a time, and a revisit cooldown;
  * a kill switch, and total silence when the network fails.

    PYTHONPATH=. python3 tests/test_prefetch.py
"""
import asyncio
import json
import sys
import types

sys.path.insert(0, "/home/user/v2/Fixiis-recovery")

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
FONT = b"wOF2" + b"0" * 40
CSS = b".h{background:url(/img/hero.png)}"

NEXT_HTML = """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="/css/next.css">
<style>@font-face{font-family:N;src:url("/f/next.woff2")}</style>
</head><body><img src="/img/next.png"><p>next page</p></body></html>"""

REQUEST_LOG = []


class Response:
    def __init__(self, url, body=b"", ctype="text/html", status=200):
        self.url = url
        self.status_code = status
        self.content = body
        self.headers = {"content-type": ctype}


class Client:
    """Serves the destination document and its assets."""

    def __init__(self, *a, **k):
        self.headers = (k or {}).get("headers") or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def get(self, url, timeout=None):
        REQUEST_LOG.append((url, dict(self.headers)))
        if url.endswith("/missing"):
            return Response(url, b"", "text/plain", status=404)
        if url.endswith("/plain.txt"):
            return Response(url, b"hello", "text/plain")
        if url.rsplit("/", 1)[-1] in ("next", "a", "b", "c"):
            return Response(url, NEXT_HTML.encode(), "text/html; charset=utf-8")
        if url.endswith(".woff2"):
            return Response(url, FONT, "font/woff2")
        if url.endswith(".css"):
            return Response(url, CSS, "text/css")
        if url.endswith(".png"):
            return Response(url, PNG, "image/png")
        return Response(url, b"", "text/plain", status=404)


def install_fake_httpx():
    mod = types.ModuleType("httpx")
    mod.AsyncClient = Client

    class Limits:
        def __init__(self, *a, **k):
            pass

    mod.Limits = Limits
    sys.modules["httpx"] = mod


install_fake_httpx()

import dom_capture  # noqa: E402

CURRENT = "https://site.example/login"
NEXT = "https://site.example/next"


class CookieJar:
    async def cookies(self, url):
        return [{"name": "sid", "value": "abc123"}]


class Page:
    def __init__(self):
        self.url = CURRENT
        self.context = CookieJar()

    async def title(self):
        return "Login"

    async def evaluate(self, *a, **k):
        return "1:1:1"


class WS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def of(self, kind):
        return [m for m in self.sent if m["type"] == kind]

    def types(self):
        return [m["type"] for m in self.sent]


def make_session(ws=None):
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    dom_capture._ASSET_GATE = None
    ws = ws or WS()
    session = dom_capture.DOMCaptureSession(Page(), websocket=ws, client_id="pf")
    session._last_capture_supports_assets = True
    session._last_capture_supports_delta = True
    session._delta_installed = False
    return session, ws


def run(fn):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(fn())
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 1. THE CLAIM: the page after a prefetch needs no upgrade at all
# ---------------------------------------------------------------------------
def test_a_prefetched_page_arrives_complete():
    session, ws = make_session()
    warmed = run(lambda: session.prefetch_assets(NEXT))
    ok(warmed is True, "the prefetch did not warm anything")

    # Now the viewer clicks.  The capture must find every ref already known.
    async def capture(settle=None):
        return NEXT_HTML

    session.capture_page = capture
    session.page.url = NEXT
    ws.sent = []
    run(lambda: session._send_full(NEXT, reason="navigation"))

    ok(ws.types() == ["full_document"],
       "a prefetched navigation still needed an upgrade: %r" % (ws.types(),))
    doc = ws.of("full_document")[0]
    ok("assets_pending" not in doc,
       "the document said assets were pending after a prefetch: %r"
       % doc.get("assets_pending"))
    ok("data:font/woff2" in doc["html"],
       "the font was not already embedded — the prefetch did not warm it")
    ok("/f/next.woff2" not in doc["html"],
       "an unresolved font ref survived a prefetched navigation")
    ok("data:" in doc["html"], "nothing was embedded at all")


def test_the_prefetch_fetches_the_document_and_its_assets():
    REQUEST_LOG.clear()
    session, ws = make_session()
    run(lambda: session.prefetch_assets(NEXT))
    urls = [u for u, _h in REQUEST_LOG]
    ok(any(u == NEXT for u in urls), "the destination document was not fetched")
    ok(any(u.endswith(".woff2") for u in urls), "the destination's font was not fetched")
    ok(any(u.endswith(".css") for u in urls), "the destination's stylesheet was not fetched")
    ok(any(u.endswith("next.png") for u in urls), "the destination's image was not fetched")


def test_the_prefetch_carries_the_session_cookies():
    REQUEST_LOG.clear()
    session, ws = make_session()
    run(lambda: session.prefetch_assets(NEXT))
    html_req = [h for u, h in REQUEST_LOG if u == NEXT]
    ok(html_req, "the document was never requested")
    ok("Cookie" in html_req[0] and "sid=abc123" in html_req[0]["Cookie"],
       "the prefetch went out without the session cookie, so a logged-in page "
       "would have been prefetched as an anonymous one: %r" % html_req[0])


# ---------------------------------------------------------------------------
# 2. it is not a proxy
# ---------------------------------------------------------------------------
def test_cross_origin_is_refused():
    REQUEST_LOG.clear()
    session, ws = make_session()
    ok(run(lambda: session.prefetch_assets("https://evil.example/steal")) is False,
       "a cross-origin prefetch was allowed")
    ok(not REQUEST_LOG, "a cross-origin URL was actually fetched: %r" % (REQUEST_LOG,))
    ok(run(lambda: session.prefetch_assets("file:///etc/passwd")) is False,
       "a non-http scheme was allowed")
    ok(not REQUEST_LOG, "a file:// prefetch was fetched")


def test_the_current_page_is_not_prefetched():
    REQUEST_LOG.clear()
    session, ws = make_session()
    ok(run(lambda: session.prefetch_assets(CURRENT + "#section")) is False,
       "prefetching the page already on screen is pure waste")
    ok(not REQUEST_LOG, "the current page was re-fetched")


def test_a_non_html_body_is_refused():
    REQUEST_LOG.clear()
    session, ws = make_session()
    ok(run(lambda: session.prefetch_assets("https://site.example/plain.txt")) is False,
       "a text/plain body was treated as a page")
    ok(not any(u.endswith(".woff2") for u, _h in REQUEST_LOG),
       "assets were fetched for a non-HTML destination")


def test_a_404_destination_is_refused():
    session, ws = make_session()
    ok(run(lambda: session.prefetch_assets("https://site.example/missing")) is False,
       "a 404 destination warmed nothing (it must return False)")


# ---------------------------------------------------------------------------
# 3. budgets and cooldowns
# ---------------------------------------------------------------------------
def test_the_per_page_budget_holds():
    session, ws = make_session()
    saved = dom_capture.PREFETCH_MAX_PER_PAGE
    dom_capture.PREFETCH_MAX_PER_PAGE = 2
    try:
        first = run(lambda: session.prefetch_assets("https://site.example/a"))
        second = run(lambda: session.prefetch_assets("https://site.example/b"))
        third = run(lambda: session.prefetch_assets("https://site.example/c"))
    finally:
        dom_capture.PREFETCH_MAX_PER_PAGE = saved
    ok(first or second, "neither of the first two prefetches ran")
    ok(third is False, "the per-page budget was exceeded")


def test_the_same_url_is_not_warmed_twice():
    session, ws = make_session()
    run(lambda: session.prefetch_assets(NEXT))
    ok(run(lambda: session.prefetch_assets(NEXT)) is False,
       "the same destination was prefetched again inside the cooldown")


def test_only_one_prefetch_runs_at_a_time():
    session, ws = make_session()
    session._prefetch_running = True
    try:
        ok(run(lambda: session.prefetch_assets(NEXT)) is False,
           "a second prefetch started while one was in flight")
    finally:
        session._prefetch_running = False


def test_a_navigation_resets_the_budget():
    session, ws = make_session()

    async def capture(settle=None):
        return NEXT_HTML

    session.capture_page = capture
    session._prefetch_count = dom_capture.PREFETCH_MAX_PER_PAGE
    run(lambda: session._send_full(NEXT, reason="navigation"))
    session.page.url = NEXT
    ok(session._prefetch_count == 0,
       "the prefetch budget did not reset on a new page: %r" % session._prefetch_count)


# ---------------------------------------------------------------------------
# 4. switches and failure
# ---------------------------------------------------------------------------
def test_the_kill_switch():
    session, ws = make_session()
    saved = dom_capture.PREFETCH_ENABLED
    dom_capture.PREFETCH_ENABLED = False
    try:
        REQUEST_LOG.clear()
        ok(run(lambda: session.prefetch_assets(NEXT)) is False,
           "the prefetch kill switch did nothing")
        ok(not REQUEST_LOG, "the kill switch still fetched: %r" % (REQUEST_LOG,))
    finally:
        dom_capture.PREFETCH_ENABLED = saved


def test_a_dead_network_is_silent():
    class Dead(Client):
        async def get(self, url, timeout=None):
            raise RuntimeError("network gone")

    mod = sys.modules["httpx"]
    real = mod.AsyncClient
    mod.AsyncClient = Dead
    try:
        session, ws = make_session()
        ok(run(lambda: session.prefetch_assets(NEXT)) is False,
           "a failing prefetch must return False, not raise")
    finally:
        mod.AsyncClient = real
    ok(session._prefetch_running is False,
       "a failed prefetch left the in-flight flag stuck")


def test_a_prefetch_never_touches_the_page():
    """It must not navigate, click, evaluate or capture the live page."""
    calls = []

    class Watched(Page):
        async def evaluate(self, *a, **k):
            calls.append("evaluate")
            return "1:1:1"

        async def goto(self, *a, **k):     # pragma: no cover - must not happen
            calls.append("goto")

        async def click(self, *a, **k):    # pragma: no cover - must not happen
            calls.append("click")

    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    dom_capture._ASSET_GATE = None
    session = dom_capture.DOMCaptureSession(Watched(), websocket=WS(), client_id="pf2")
    run(lambda: session.prefetch_assets(NEXT))
    ok(not calls, "the prefetch drove the live page: %r" % calls)


# ---------------------------------------------------------------------------
# 5. the plumbing that connects it
# ---------------------------------------------------------------------------
def test_the_message_is_dispatched_off_the_hot_path():
    api = open("/home/user/v2/Fixiis-recovery/api.py", encoding="utf-8",
               errors="replace").read()
    ok("msg_type == 'prefetch'" in api, "api.py has no prefetch branch")
    ok("asyncio.create_task(session.handle_prefetch" in api,
       "the prefetch is awaited on the message loop — the next click would "
       "queue behind a background fetch")
    session_py = open("/home/user/v2/Fixiis-recovery/session.py", encoding="utf-8",
                      errors="replace").read()
    ok("async def handle_prefetch" in session_py, "session.py does not forward it")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("intent prefetch: %d checks passed" % PASSED)
