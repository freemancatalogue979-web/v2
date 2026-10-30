#!/usr/bin/env python3
"""How many times a capture has to talk to the browser.

A navigation used to cost a round trip for the document, then one EACH for the
title, the DOM checksum and the user agent — three trips to the same page,
milliseconds apart, to read things the serializer was already walking past.
None of them is big; together they are tens of milliseconds on every
navigation, and they are pure overhead on a path we are trying to make feel
instant.

Now the capture call returns all of it at once.  These tests count the round
trips on a page object that records them, so "one call" is a measured fact and
not a code reading.

    PYTHONPATH=. python3 tests/test_capture_roundtrips.py
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


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32

PAGE_HTML = ("<!DOCTYPE html><html><head><title>Real Title</title>"
             "<style>@font-face{font-family:H;src:url('/f/x.woff2')}</style>"
             "</head><body>" + "<p>content</p>" * 60 + "</body></html>")


class Response:
    def __init__(self, url=""):
        self.url = url
        self.status_code = 200
        self.content = b"wOF2" + b"0" * 32 if url.endswith(".woff2") else PNG
        self.headers = {"content-type": "font/woff2" if url.endswith(".woff2")
                        else "image/png"}


class Client:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def get(self, url, timeout=None):
        return Response(url)


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

URL = "https://site.example/page"


class CountingPage:
    """Fake browser page that records every round trip by kind."""

    def __init__(self, combined=True, meta_fails=False):
        self.url = URL
        self.calls = []          # ("kind", detail)
        self.combined = combined
        self.meta_fails = meta_fails

    # -- round trips -----------------------------------------------------
    async def evaluate(self, js, *a, **k):
        if "out.title" in js:
            self.calls.append(("evaluate", "combined"))
            if self.meta_fails:
                raise RuntimeError("page went away")
            if not self.combined:
                # an old page that only understands the plain serializer
                return PAGE_HTML
            return {"html": PAGE_HTML, "title": "Real Title",
                    "ua": "FakeUA/1.0", "ready": "complete", "url": URL,
                    "checksum": "777:1234:900"}
        if "getElementsByTagName('*').length" in js:
            self.calls.append(("evaluate", "checksum"))
            return "777:1234:900"
        if "navigator.userAgent" in js:
            self.calls.append(("evaluate", "userAgent"))
            return "FakeUA/1.0"
        self.calls.append(("evaluate", "serialize"))
        return PAGE_HTML

    async def title(self):
        self.calls.append(("title", ""))
        return "Real Title"

    async def wait_for_function(self, *a, **k):
        self.calls.append(("wait_for_function", ""))

    async def wait_for_load_state(self, *a, **k):
        self.calls.append(("wait_for_load_state", ""))

    # -- helpers ---------------------------------------------------------
    def kinds(self):
        return [k for k, _d in self.calls]

    def count(self, kind, detail=None):
        return sum(1 for k, d in self.calls if k == kind and (detail is None or d == detail))


class WS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def of(self, kind):
        return [m for m in self.sent if m["type"] == kind]


def make_session(page, ws=None):
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    dom_capture._ASSET_GATE = None
    ws = ws or WS()
    session = dom_capture.DOMCaptureSession(page, websocket=ws, client_id="rt")
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
# 1. one capture = one evaluate
# ---------------------------------------------------------------------------
def test_a_navigation_costs_one_evaluate():
    page = CountingPage()
    session, ws = make_session(page)

    async def go():
        return await session._send_full(URL, reason="navigation")

    run(go)
    ok(page.count("evaluate", "combined") == 1,
       "the capture did not use the combined call: %r" % page.calls)
    ok(page.count("evaluate", "checksum") == 0,
       "the DOM checksum was asked for separately: %r" % page.calls)
    ok(page.count("evaluate", "serialize") == 0,
       "the document was serialized twice: %r" % page.calls)
    ok(page.count("title") == 0,
       "the title cost its own round trip: %r" % page.calls)
    ok(page.count("wait_for_load_state") == 0,
       "domcontentloaded and readyState were both awaited: %r" % page.calls)
    ok(page.count("wait_for_function") == 1,
       "expected exactly one settle wait, got %r" % page.calls)
    ok(len(ws.of("full_document")) == 1, "the document never arrived")
    total = len(page.calls)
    ok(total <= 2,
       "a navigation cost %d browser round trips (%r) — the whole point is "
       "that it is one wait and one capture" % (total, page.calls))


# ---------------------------------------------------------------------------
# 2. the things it returns are actually used
# ---------------------------------------------------------------------------
def test_the_title_comes_from_the_capture():
    page = CountingPage()
    session, ws = make_session(page)
    run(lambda: session._send_full(URL, reason="navigation"))
    doc = ws.of("full_document")[0]
    ok(doc.get("title") == "Real Title",
       "the title did not reach the client: %r" % doc.get("title"))
    ok(page.count("title") == 0, "the title was fetched twice")


def test_the_checksum_comes_from_the_capture():
    page = CountingPage()
    session, ws = make_session(page)
    run(lambda: session._send_full(URL, reason="navigation"))
    ok(session._last_checksum == "777:1234:900",
       "the checksum was not adopted: %r" % session._last_checksum)
    ok(page.count("evaluate", "checksum") == 0,
       "the checksum cost its own round trip anyway")


def test_the_user_agent_is_seeded_for_the_asset_pass():
    page = CountingPage()
    session, ws = make_session(page)
    run(lambda: session._send_full(URL, reason="navigation"))
    ok(page.count("evaluate", "userAgent") == 0,
       "the asset pass asked the browser for a UA the capture already had")
    ok(getattr(page, "_cached_ua", None) == "FakeUA/1.0",
       "the UA was not memoised on the page: %r" % getattr(page, "_cached_ua", None))
    # ...and the font really was resolved, so the UA was not skipped by luck.
    payload = "".join(m["html"] for m in ws.of("full_document"))
    ok("/f/x.woff2" in payload, "the test page lost its font ref")


# ---------------------------------------------------------------------------
# 3. it degrades exactly like the old path
# ---------------------------------------------------------------------------
def test_an_old_page_falls_back_to_the_plain_serializer():
    page = CountingPage(combined=False)      # returns a bare string
    session, ws = make_session(page)
    run(lambda: session._send_full(URL, reason="navigation"))
    ok(len(ws.of("full_document")) == 1, "the document was lost on fallback")
    ok(page.count("evaluate", "combined") == 1 and page.count("evaluate", "serialize") == 1,
       "the fallback did not re-run the plain serializer: %r" % page.calls)
    ok(page.count("title") == 1,
       "with no title from the capture the old path must still get one: %r" % page.calls)


def test_a_raising_combined_call_still_produces_a_document():
    page = CountingPage(meta_fails=True)     # combined call raises
    session, ws = make_session(page)
    run(lambda: session._send_full(URL, reason="navigation"))
    ok(len(ws.of("full_document")) == 1,
       "a failing combined call cost the page: no document was sent")
    ok(page.count("evaluate", "serialize") == 1,
       "the serializer was not retried after the combined call failed")


def test_a_page_without_a_title_field_still_gets_one():
    """meta present but partial: the fallback must fill the gaps only."""
    page = CountingPage()

    async def partial(js, *a, **k):
        if "out.title" in js:
            page.calls.append(("evaluate", "combined"))
            return {"html": PAGE_HTML}       # no title, no checksum, no UA
        return await CountingPage.evaluate(page, js, *a, **k)

    page.evaluate = partial
    session, ws = make_session(page)
    run(lambda: session._send_full(URL, reason="navigation"))
    doc = ws.of("full_document")[0]
    ok(doc.get("title") == "Real Title",
       "the fallback title did not reach the client: %r" % doc.get("title"))
    ok(page.count("title") == 1, "the fallback asked for the title once")
    ok(page.count("evaluate", "checksum") <= 1, "checksum fallback ran twice")


# ---------------------------------------------------------------------------
# 4. the wiring in the page itself
# ---------------------------------------------------------------------------
def test_the_widened_call_is_one_expression():
    js = dom_capture._FAST_CAPTURE_CALL_JS
    ok("out.title" in js and "out.checksum" in js and "out.ua" in js,
       "the combined call does not carry the metadata")
    ok(js.count("(() => {") == 1 and js.rstrip().endswith("})();"),
       "the combined call is not one self-contained expression")
    ok("try {" in js,
       "the metadata reads are not guarded — one missing field would cost the "
       "whole capture")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("capture round trips: %d checks passed" % PASSED)
