#!/usr/bin/env python3
"""The asset rewrite over REAL HTTP, end to end.

``tests/test_capture_inline_assets.py`` stubs httpx so it can assert exactly
what was requested. That is the right tool for behaviour, but it also means
the stubbed layer could agree with a bug (a wrong content type, a URL joined
against the wrong base, a fragment sent to the server). This one starts a real
HTTP server on an ephemeral port, serves a page's assets from it, and runs the
real rewrite with the real httpx client.

The page is shaped like the ones that were losing content: a font declared in
the page's own <style> (CORS-blocked in the mirror), a background image in a
style attribute, a sprite icon, a video poster, and a stylesheet whose own
@font-face has to be rewritten too.

    PYTHONPATH=. python3 tests/test_capture_live_assets.py
"""
import asyncio
import http.server
import socketserver
import sys
import threading

sys.path.insert(0, "/home/user/v2/Fixiis-recovery")

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
FONT = b"wOF2" + b"0" * 512
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><symbol id="star"/></svg>'
SITE_CSS = b".brand{font-family:Brand}@font-face{font-family:Brand;src:url(/fonts/brand.woff2)}"

PAGE = """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="/css/site.css">
<style>
  @font-face { font-family: Head; src: url("/fonts/head.woff2") format("woff2"); }
  h1 { font-family: Head; }
  .hero { background-image: url(/img/hero.png); }
</style>
</head><body>
<h1>Hello</h1>
<div class="hero" style="background-image:url('/img/hero.png')">hero</div>
<svg width="16" height="16"><use href="/sprite.svg#star"></use></svg>
<video poster="/img/hero.png" src="/v.mp4"></video>
<img src="/img/hero.png">
</body></html>"""

FILES = {
    "/css/site.css": (SITE_CSS, "text/css"),
    "/fonts/brand.woff2": (FONT, "font/woff2"),
    "/fonts/head.woff2": (FONT, "font/woff2"),
    "/img/hero.png": (PNG, "image/png"),
    "/sprite.svg": (SVG, "image/svg+xml"),
}


class Handler(http.server.BaseHTTPRequestHandler):
    served = []

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        type(self).served.append(path)
        body, ctype = FILES.get(path, (None, None))
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Server(socketserver.TCPServer):
    allow_reuse_address = True


def main():
    import dom_capture

    httpd = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://127.0.0.1:%d/page" % httpd.server_address[1]
        dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
        out, meta = asyncio.new_event_loop().run_until_complete(
            dom_capture._rewrite_assets_to_cache(PAGE, base)
        )
    finally:
        httpd.shutdown()
        httpd.server_close()

    # -- everything the browser would have had to fetch from the site -----
    ok("/fonts/head.woff2" in Handler.served, "the inline @font-face was never requested")
    ok("/fonts/brand.woff2" in Handler.served, "the font inside the external sheet")
    ok("/img/hero.png" in Handler.served, "the hero image")
    ok("/sprite.svg" in Handler.served, "the sprite")
    ok("/css/site.css" in Handler.served, "the stylesheet")

    # -- and nothing left pointing at the site ----------------------------
    ok("/fonts/head.woff2" not in out, out)
    ok("/fonts/brand.woff2" not in out, out)
    ok("/img/hero.png" not in out, out)
    ok("url(/img/hero.png)" not in out, out)

    # -- real content types decide how each ref is carried ----------------
    ok("data:font/woff2;base64," in out, "a font must arrive with its own MIME type")
    ok("data:image/png;base64," in out, "an image must arrive as image/png")
    ok("/assets/" in out and "#star" in out, "the sprite is served, fragment intact")

    # -- the page's own structure survived -------------------------------
    ok('format("woff2")' in out, "the format() hint was lost")
    ok('src="/v.mp4"' in out, "an unrelated URL was rewritten")
    ok(len(meta) >= 1, "the client prewarm list is empty")

    # -- a second pass over the same (already rewritten) page is a no-op --
    served_before = list(Handler.served)
    httpd2 = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd2.serve_forever, daemon=True).start()
    try:
        Handler.served = []
        base2 = "http://127.0.0.1:%d/page" % httpd2.server_address[1]
        dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
        twice, _m2 = asyncio.new_event_loop().run_until_complete(
            dom_capture._rewrite_assets_to_cache(out, base2)
        )
    finally:
        httpd2.shutdown()
        httpd2.server_close()
    ok(twice == out, "re-running the rewrite changed an already-rewritten page")
    ok(Handler.served == [], "re-running the rewrite re-fetched something: %r" % Handler.served)

    print("capture live assets: %d checks passed" % PASSED)


if __name__ == "__main__":
    main()
