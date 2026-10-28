#!/usr/bin/env python3
"""How long the asset-rewrite phase takes — and what the CSS/SVG change costs.

Not a pass/fail test: a measurement, so a change to this pipeline can be
compared with the one before it instead of guessed at.

    # the tree you are in
    python3 tests/bench_asset_rewrite.py

    # the same benchmark against another copy of dom_capture.py
    python3 tests/bench_asset_rewrite.py --module-dir /tmp/before

It serves a page's assets from a local HTTP server (so the numbers are the
pipeline, not someone's uplink), then measures:

  * cold  — first capture of a page: nothing in the fetch-once memo yet
  * warm  — the same page captured again: the normal recapture path
  * plain — a ~1 MB document with no inline CSS/SVG at all, to show what the
            new pass costs a page that cannot use it

Latency is injected per asset (--latency-ms) because on a real network the
fetch count matters more than the CPU does.
"""
from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import os
import socketserver
import statistics
import sys
import threading
import time


def build_server(latency_ms: float):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 900
    font = b"wOF2" + b"0" * 900
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><symbol id="i"/></svg>'
    css_tail = b"@font-face{font-family:S;src:url(inner-font.woff2)}.x{background:url(inner-bg.png)}"

    class Handler(http.server.BaseHTTPRequestHandler):
        served: list = []

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if latency_ms:
                time.sleep(latency_ms / 1000.0)
            if path.startswith("/css/"):
                body, ctype = css_tail, "text/css"
            elif path.startswith(("/img/", "/bg/")):
                body, ctype = png, "image/png"
            elif path.startswith("/fonts/"):
                body, ctype = font, "font/woff2"
            elif path.startswith("/sprite"):
                body, ctype = svg, "image/svg+xml"
            else:
                self.send_response(404)
                self.end_headers()
                return
            type(self).served.append(path)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True
        # Default backlog is 5; the rewrite connects from a pool of 10, so the
        # kernel would drop SYNs and retry them a second later — which measures
        # this server's queue, not the pipeline.
        request_queue_size = 256

    return Server(("127.0.0.1", 0), Handler), Handler


def mixed_page(n_bg=25, n_img=20, n_faces=3, n_sprite=8, n_sheets=2, filler=2500):
    head = ['<link rel="stylesheet" href="/css/site%d.css">' % i for i in range(n_sheets)]
    head.append("<style>")
    head += ['@font-face{font-family:F%d;src:url("/fonts/f%d.woff2") format("woff2")}' % (i, i)
             for i in range(n_faces)]
    head += [".bg%d{background-image:url(/bg/bg%d.png)}" % (i, i) for i in range(n_bg)]
    head.append("</style>")
    body = ['<img src="/img/img%d.png" alt="">' % i for i in range(n_img)]
    body += ['<div class="bg%d" style="background-image:url(/bg/bg%d.png)">x</div>' % (i, i)
             for i in range(n_bg)]
    body += ['<svg width="16" height="16"><use href="/sprite%d.svg#i"></use></svg>' % i
             for i in range(n_sprite)]
    body.append('<video poster="/img/img0.png" src="/v.mp4"></video>')
    body += ['<p class="p%d">paragraph %d with some text in it</p>' % (i, i) for i in range(filler)]
    return ("<!DOCTYPE html><html><head>%s</head><body>%s</body></html>"
            % ("".join(head), "".join(body)))


def plain_page(kb=900):
    parts = ["<html><head><title>t</title></head><body>", '<img src="/img/a.png">']
    parts += ['<p class="row">some ordinary paragraph text of typical length</p>'
              for _ in range(int(kb * 1024 / 60))]
    parts.append("</body></html>")
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--module-dir", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="directory holding the dom_capture.py to measure")
    ap.add_argument("--latency-ms", type=float, default=0.0)
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    sys.path.insert(0, args.module_dir)
    import dom_capture  # noqa: E402

    server, handler = build_server(args.latency_ms)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/page" % server.server_address[1]
    loop = asyncio.new_event_loop()

    def timed(doc, fresh: bool):
        if fresh:
            dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
        handler.served = []
        t = time.perf_counter()
        html, meta = loop.run_until_complete(dom_capture._rewrite_assets_to_cache(doc, base))
        ms = (time.perf_counter() - t) * 1000.0
        return ms, html, meta, list(handler.served)

    out = {"module": dom_capture.__file__, "latency_ms": args.latency_ms}

    page = mixed_page()
    cold_ms, html, _meta, served = timed(page, fresh=True)
    out["cold_ms"] = round(cold_ms, 1)
    out["cold_requests"] = len(served)
    out["page_kb"] = round(len(page) / 1024.0, 1)

    warm = []
    for _ in range(args.runs):
        ms, _h, _m, _s = timed(page, fresh=False)
        warm.append(ms)
    out["warm_ms_median"] = round(statistics.median(warm), 2)

    out["leftover_site_refs"] = len([
        ln for ln in html.splitlines()
        if ("/fonts/" in ln or "/bg/" in ln or "/img/" in ln or "/sprite" in ln)
        and "data:" not in ln
    ])
    out["embedded"] = html.count("data:")
    out["cached_refs"] = html.count("/assets/")

    plain = plain_page()
    plain_runs = []
    timed(plain, fresh=True)
    for _ in range(args.runs):
        ms, _h, _m, _s = timed(plain, fresh=False)
        plain_runs.append(ms)
    out["plain_1mb_warm_ms"] = round(statistics.median(plain_runs), 2)

    if hasattr(dom_capture, "_doc_ref_spans"):
        t = time.perf_counter()
        spans = dom_capture._doc_ref_spans(page)
        out["collect_ms"] = round((time.perf_counter() - t) * 1000.0, 2)
        out["collect_spans"] = len(spans)

    server.shutdown()
    server.server_close()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
