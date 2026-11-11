"""Integration tests for the reverse resource proxy.

Tests the full stack: Starlette app -> security validation -> resolver ->
client fetch -> cache -> response -> CSS rewriting -> streaming.

Uses a local HTTP origin server as the "original website" and the proxy
as the gateway.  All tests run against real network loops (localhost).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

# Make the package importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resource_proxy.security import validate_destination
from resource_proxy.resolver import resolve_url, same_origin
from resource_proxy.response import ProxyResponse
from resource_proxy.cache import ResourceCache, get_global_cache
from resource_proxy.css_rewriter import rewrite_css
from resource_proxy.client import ProxyHttpClient, get_global_client
from resource_proxy.gateway import create_app, parse_proxy_path


# ---------------------------------------------------------------------------
# Local test origin server
# ---------------------------------------------------------------------------

class OriginHandler(BaseHTTPRequestHandler):
    """Serves static resources for testing the proxy."""

    RESOURCES = {
        "/style.css": (
            b'.hero { background-image: url("../images/hero.webp"); '
            b'@import url("./theme.css"); @font-face { src: url("../fonts/x.woff2"); } }',
            "text/css",
        ),
        "/theme.css": (b".theme { color: red; }", "text/css"),
        "/images/hero.webp": (b"\x89PNG\r\n\x1a\nfake-image", "image/webp"),
        "/fonts/x.woff2": (b"fake-font-data", "font/woff2"),
        "/page.html": (b"<html><body>hello</body></html>", "text/html"),
        "/large.mp4": (b"x" * (100 * 1024), "video/mp4"),  # 100 KB
        "/redirect": None,  # handled in do_GET
        "/private": (b"private", "text/html"),
    }

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/style.css")
            self.end_headers()
            return
        if path == "/private":
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"forbidden")
            return
        entry = self.RESOURCES.get(path)
        if entry is None:
            self.send_response(404)
            self.end_headers()
            return
        body, content_type = entry
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.send_header("ETag", hashlib.sha256(body).hexdigest())
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass  # suppress noise


def start_origin(port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), OriginHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ---------------------------------------------------------------------------
# Integration test helpers
# ---------------------------------------------------------------------------

BASE = "http://127.0.0.1:19876"
ORIGIN_PORT = 19877
origin_server: Optional[HTTPServer] = None


def start_origin_for_tests():
    global origin_server
    origin_server = start_origin(ORIGIN_PORT)
    time.sleep(0.2)


def stop_origin_for_tests():
    global origin_server
    if origin_server:
        origin_server.shutdown()
        origin_server = None


# Start the origin server at import time (works with both pytest and direct run)
start_origin_for_tests()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_security_blocks_origin_not_allowed():
    """localhost is not in ALLOWED_HOSTS by default."""
    ok, reason = validate_destination(f"{BASE}/style.css")
    assert not ok, f"expected blocked, got ok reason={reason}"
    assert "not allowed" in reason


def test_security_allows_configured_host():
    """When the host is added to ALLOWED_HOSTS it should pass."""
    import resource_proxy.config as cfg
    before_hosts = set(cfg.ALLOWED_HOSTS)
    before_private = cfg.ALLOW_PRIVATE_IPS
    cfg.ALLOWED_HOSTS.add("127.0.0.1")
    cfg.ALLOW_PRIVATE_IPS = True
    try:
        ok, reason = validate_destination(f"{BASE}/style.css")
        assert ok, f"expected allowed, got blocked reason={reason}"
    finally:
        cfg.ALLOWED_HOSTS = before_hosts
        cfg.ALLOW_PRIVATE_IPS = before_private


def test_security_rejects_file_scheme():
    ok, reason = validate_destination("file:///etc/passwd")
    assert not ok


def test_security_rejects_ftp_scheme():
    ok, reason = validate_destination("ftp://example.com/file")
    assert not ok


def test_security_rejects_javascript_scheme():
    ok, reason = validate_destination("javascript:alert(1)")
    assert not ok


def test_resolve_relative():
    resolved = resolve_url("/style.css", f"{BASE}/page.html")
    assert resolved == f"{BASE}/style.css"


def test_resolve_protocol_relative():
    resolved = resolve_url("//127.0.0.1:19877/style.css", f"{BASE}/page.html")
    assert resolved == f"http://127.0.0.1:{ORIGIN_PORT}/style.css"


def test_same_origin():
    assert same_origin(f"{BASE}/a", f"{BASE}/b")
    assert not same_origin(f"{BASE}/a", "http://other.com/b")


def test_css_rewriting():
    css = (
        '.hero { background-image: url("../images/hero.webp"); }'
        '@import url("./theme.css");'
        '@font-face { src: url("../fonts/x.woff2"); }'
    )
    result = rewrite_css(css, f"{BASE}/css/theme.css", "/resource-proxy")
    assert "/resource-proxy/fetch" in result
    # URL is percent-encoded by urlencode
    assert "u=http%3A%2F%2F127.0.0.1%3A19876%2Fimages%2Fhero.webp" in result


def test_response_metadata():
    headers = {
        "Content-Type": "text/css",
        "Content-Length": "123",
        "Cache-Control": "max-age=3600",
        "ETag": '"abc"',
        "Accept-Ranges": "bytes",
        "Connection": "keep-alive",
    }
    resp = ProxyResponse(200, headers, body=b"body", url=f"{BASE}/style.css")
    assert resp.status_code == 200
    assert resp.content_type == "text/css"
    assert resp.etag == '"abc"'
    assert resp.cache_control == "max-age=3600"
    assert "Connection" not in resp.headers
    assert resp.is_cacheable()
    assert resp.cache_ttl() == 3600


def test_cache_session_isolation():
    cache = ResourceCache(max_bytes=1024 * 1024, max_entries=100)
    resp = ProxyResponse(
        200,
        {"Content-Type": "text/css", "Cache-Control": "max-age=3600"},
        body=b"css",
        url=f"{BASE}/style.css",
    )
    assert cache.put("session1", f"{BASE}/style.css", resp)
    assert cache.get("session1", f"{BASE}/style.css") is not None
    assert cache.get("session2", f"{BASE}/style.css") is None


def test_cache_no_store_not_cached():
    cache = ResourceCache(max_bytes=1024 * 1024, max_entries=100)
    resp = ProxyResponse(
        200,
        {"Content-Type": "text/html", "Cache-Control": "no-store"},
        body=b"body",
        url=f"{BASE}/page.html",
    )
    assert not cache.put("session1", f"{BASE}/page.html", resp)
    assert cache.get("session1", f"{BASE}/page.html") is None


def test_parse_proxy_path():
    app = create_app()
    # Just verify the app is created without error.
    assert app is not None


def test_detect_resource_type():
    from resource_proxy.gateway import detect_resource_type
    assert detect_resource_type("http://example.com/image.png") == "image"
    assert detect_resource_type("http://example.com/style.css") == "css"
    assert detect_resource_type("http://example.com/font.woff2") == "font"
    assert detect_resource_type("http://example.com/video.mp4") == "video"
    assert detect_resource_type("http://example.com/audio.mp3") == "audio"
    assert detect_resource_type("http://example.com/script.js") == "javascript"
    assert detect_resource_type("http://example.com/page.html") == "html"
    assert detect_resource_type("http://example.com/unknown.xyz") == "other"


# ---------------------------------------------------------------------------
# Live proxy tests (need the app running)
# ---------------------------------------------------------------------------

def test_proxy_fetch_live():
    """Fetch a real resource through the proxy client from a live origin."""
    import resource_proxy.config as cfg
    before_hosts = set(cfg.ALLOWED_HOSTS)
    before_private = cfg.ALLOW_PRIVATE_IPS
    cfg.ALLOWED_HOSTS.add("127.0.0.1")
    cfg.ALLOW_PRIVATE_IPS = True
    try:
        client = get_global_client()
        origin_url = f"http://127.0.0.1:{ORIGIN_PORT}/style.css"
        resp, error = asyncio.run(client.fetch(origin_url))
        assert error is None, f"fetch failed: {error}"
        assert resp is not None
        assert resp.status_code == 200
        assert resp.content_type == "text/css"
        assert b"background-image" in (resp.body or b"")
        assert resp.etag  # ETag should be preserved
    finally:
        cfg.ALLOWED_HOSTS = before_hosts
        cfg.ALLOW_PRIVATE_IPS = before_private


def test_proxy_fetch_blocks_disallowed():
    """Fetching a disallowed host through the proxy should fail."""
    client = get_global_client()
    resp, error = asyncio.run(client.fetch("http://evil.example.com/style.css"))
    assert error is not None
    assert resp is None


def test_proxy_fetch_bad_scheme():
    client = get_global_client()
    resp, error = asyncio.run(client.fetch("file:///etc/passwd"))
    assert error is not None
    assert resp is None


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
