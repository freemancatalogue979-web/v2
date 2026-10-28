#!/usr/bin/env python3
"""How long a viewer waits before the page is ON SCREEN, and when it is whole.

The asset phase is the slow half of a capture, and it used to hold the document
hostage: nothing went out until the last font came back.  This measures both
timelines on the same stub, so the difference is the feature:

    python3 tests/bench_time_to_first_paint.py
    python3 tests/bench_time_to_first_paint.py --latency-ms 120 --assets 40

  first_paint  when the full_document reached the socket
  complete     when the last asset edit (asset_map) reached the socket

With progressive assets on, first_paint is the capture+serialize time only;
with `--no-progressive` (the old behaviour) it is complete.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types

sys.path.insert(0, "/home/user/v2/Fixiis-recovery")


def build_page(n_assets: int) -> str:
    head = ['<style>']
    for i in range(max(1, n_assets // 2)):
        head.append('@font-face{font-family:F%d;src:url("/f/f%d.woff2")}' % (i, i))
    head.append('</style></head><body>')
    body = ['<img src="/img/i%d.png">' % i for i in range(n_assets // 2)]
    body.append('<p>content</p></body></html>')
    return '<!DOCTYPE html><html><head>' + ''.join(head) + ''.join(body)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--latency-ms", type=float, default=120.0,
                    help="per asset round trip (a real page, not localhost)")
    ap.add_argument("--assets", type=int, default=30)
    ap.add_argument("--no-progressive", action="store_true",
                    help="measure the pre-progressive behaviour")
    args = ap.parse_args()

    # In a running server `api` is already loaded (it imports this module), so
    # the `_site_icon_url` lookup on the send path is a dict hit.  Import it
    # here as well, otherwise this bench times the import of api instead of
    # the capture.
    try:
        import api  # noqa: F401
    except Exception:
        pass

    import dom_capture

    page_html = build_page(args.assets)
    latency = args.latency_ms / 1000.0

    class Resp:
        def __init__(self, url):
            self.url = url
            self.status_code = 200
            if "woff" in url:
                self.content, self.headers = b"wOF2" + b"0" * 200, {"content-type": "font/woff2"}
            else:
                self.content, self.headers = b"\x89PNG\r\n\x1a\n" + b"0" * 200, {"content-type": "image/png"}

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, url, timeout=None):
            await asyncio.sleep(latency)
            return Resp(url)

    mod = types.ModuleType("httpx")
    mod.AsyncClient = Client
    sys.modules["httpx"] = mod

    class Page:
        url = "https://site.example/page"

        async def title(self):
            return "Example"

        async def evaluate(self, *a, **k):
            return "1:1:1"

    class WS:
        def __init__(self):
            self.t0 = asyncio.get_event_loop().time()
            self.events = []

        async def send_text(self, text):
            msg = json.loads(text)
            self.events.append((asyncio.get_event_loop().time() - self.t0, msg["type"]))

        def at(self, kind):
            for t, k in self.events:
                if k == kind:
                    return t
            return None

    dom_capture.PROGRESSIVE_ASSETS = not args.no_progressive
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    ws = WS()
    session = dom_capture.DOMCaptureSession(Page(), websocket=ws, client_id="bench")
    session._last_capture_supports_assets = True
    session._last_capture_supports_delta = True

    async def capture(settle=None):
        return page_html

    session.capture_page = capture

    async def go():
        ws.t0 = asyncio.get_event_loop().time()
        await session._send_full(Page.url, reason="navigation")

    asyncio.new_event_loop().run_until_complete(go())
    first = ws.at("full_document")
    complete = ws.at("asset_map") or first
    out = {
        "progressive": dom_capture.PROGRESSIVE_ASSETS,
        "assets": args.assets,
        "latency_ms": args.latency_ms,
        "first_paint_ms": round((first or 0) * 1000.0),
        "complete_ms": round((complete or 0) * 1000.0),
        "messages": [k for _t, k in ws.events],
    }
    if args.no_progressive:
        out["first_paint_ms"] = out["complete_ms"]
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
