#!/usr/bin/env python3
"""The cold path: from "a visitor connected" to "a page is on their screen".

Everything else in this pipeline improves a page that is already there.  The
first page has no such luck: it waits for browser setup, the capture, and every
round trip we add on top of them.

Those round trips are what this measures.  Each one is a CDP call to the same
machine, so its cost on a quiet box is ~6-10 ms — small on its own, and there
were five of them before the document could leave.  With `--rtt-ms` set to a
typical value, the difference between the old and the new path is:

    python3 tests/bench_cold_start.py                 # 8 ms per round trip
    python3 tests/bench_cold_start.py --rtt-ms 3      # a very fast box
    python3 tests/bench_cold_start.py --rtt-ms 25     # a loaded one

It drives the REAL `_send_full` against a fake page whose every call costs
`rtt`, so this is our own overhead and nothing else — the browser's own work is
the same in both columns and is excluded on purpose.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import types

sys.path.insert(0, "/home/user/v2/Fixiis-recovery")

PAGE_HTML = ("<!DOCTYPE html><html><head><title>Example</title>"
             + "<style>@font-face{font-family:H;src:url('/f/x.woff2')}</style>"
             + "</head><body>" + "<p>cold start</p>" * 80 + "</body></html>")


class _Resp:
    def __init__(self, url):
        self.url = url
        self.status_code = 200
        self.content = b"wOF2" + b"0" * 40
        self.headers = {"content-type": "font/woff2"}


def install_fake_http():
    mod = types.ModuleType("httpx")

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, url, timeout=None):
            return _Resp(url)

    class Limits:
        def __init__(self, *a, **k):
            pass

    mod.AsyncClient = C
    mod.Limits = Limits
    sys.modules["httpx"] = mod


install_fake_http()
# In a running server `api` is already imported (it imports this module), so the
# `_site_icon_url` lookup on the send path is a dict hit.  Import it here too,
# or this bench times the interpreter importing fastapi instead of us.
try:
    import api  # noqa: F401
except Exception:
    pass

import dom_capture  # noqa: E402


class TimedPage:
    """Every browser call costs `rtt` — that is the only thing timed here."""

    def __init__(self, rtt):
        self.url = "https://site.example/page"
        self.rtt = rtt
        self.round_trips = 0

    async def _trip(self):
        self.round_trips += 1
        await asyncio.sleep(self.rtt)

    async def evaluate(self, js, *a, **k):
        await self._trip()
        if "out.title" in js:
            return {"html": PAGE_HTML, "title": "Example", "ua": "UA/1",
                    "ready": "complete", "url": self.url, "checksum": "9:99:900"}
        if "getElementsByTagName('*')" in js:
            return "9:99:900"
        if "navigator.userAgent" in js:
            return "UA/1"
        return PAGE_HTML

    async def title(self):
        await self._trip()
        return "Example"

    async def wait_for_function(self, *a, **k):
        await self._trip()

    async def wait_for_load_state(self, *a, **k):
        await self._trip()

    async def wait_for_timeout(self, *a, **k):
        await self._trip()


class WS:
    def __init__(self):
        self.events = []

    async def send_text(self, text):
        self.events.append((time.perf_counter(), json.loads(text)["type"]))


def measure(rtt_ms: float, modern: bool) -> dict:
    rtt = rtt_ms / 1000.0
    page = TimedPage(rtt)
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    dom_capture._ASSET_GATE = None
    session = dom_capture.DOMCaptureSession(page, websocket=WS(), client_id="cold")
    session._last_capture_supports_assets = True
    session._last_capture_supports_delta = True
    session._delta_installed = False
    ws = WS()
    session.websocket = ws

    if not modern:
        # The old path: the settle pair, the plain serializer, and then a
        # round trip EACH for title, UA and checksum.
        async def old_capture(settle="light"):
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_function("document.readyState !== 'loading'")
            return PAGE_HTML

        session.capture_page = old_capture
        session._last_capture_meta = {}

        async def old_title():
            return await page.title()

        async def run_old():
            t0 = time.perf_counter()
            await session._send_full(page.url, reason="navigation")
            # the extra round trips the old send path paid, in order
            await page.evaluate("navigator.userAgent")
            await page.evaluate("getElementsByTagName('*').length")
            return (time.perf_counter() - t0) * 1000.0

        total = asyncio.new_event_loop().run_until_complete(run_old())
    else:
        async def new_capture(settle="light"):
            await page.wait_for_function("document.readyState !== 'loading'")
            return PAGE_HTML

        session.capture_page = new_capture
        session._last_capture_meta = {"title": "Example", "ua": "UA/1",
                                      "checksum": "9:99:900"}

        async def run_new():
            t0 = time.perf_counter()
            await session._send_full(page.url, reason="navigation")
            return (time.perf_counter() - t0) * 1000.0

        total = asyncio.new_event_loop().run_until_complete(run_new())

    return {"ms": round(total), "round_trips": page.round_trips,
            "messages": [k for _t, k in ws.events]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtt-ms", type=float, default=8.0,
                    help="cost of one browser round trip (a local CDP call)")
    args = ap.parse_args()
    old = measure(args.rtt_ms, modern=False)
    new = measure(args.rtt_ms, modern=True)
    print(json.dumps({
        "rtt_ms": args.rtt_ms,
        "old": old,
        "new": new,
        "saved_ms": old["ms"] - new["ms"],
        "round_trips_saved": old["round_trips"] - new["round_trips"],
    }, indent=2))


if __name__ == "__main__":
    main()
