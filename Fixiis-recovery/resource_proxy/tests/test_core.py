"""Unit tests for core resource proxy modules."""

import os
import sys

# Ensure the parent package is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resource_proxy.security import validate_destination, is_allowed_host, strip_forward_blacklist, filter_response_headers
from resource_proxy.resolver import resolve_url, same_origin, get_origin
from resource_proxy.css_rewriter import rewrite_css, extract_css_urls
from resource_proxy.response import ProxyResponse
from resource_proxy.cache import ResourceCache


# ---------------------------------------------------------------------------
# Security tests
# ---------------------------------------------------------------------------

def test_validate_destination_rejects_unsupported_scheme():
    ok, reason = validate_destination("ftp://example.com/file")
    assert not ok
    assert "scheme" in reason


def test_validate_destination_rejects_empty_host():
    ok, reason = validate_destination("https:///path")
    assert not ok


def test_validate_destination_rejects_file_scheme():
    ok, reason = validate_destination("file:///etc/passwd")
    assert not ok


def test_validate_destination_rejects_javascript_scheme():
    ok, reason = validate_destination("javascript:alert(1)")
    assert not ok


def test_strip_forward_blacklist_removes_secrets():
    headers = {
        "Authorization": "Bearer secret",
        "Cookie": "session=abc",
        "X-Api-Key": "key123",
        "User-Agent": "test",
        "Accept": "*/*",
    }
    stripped = strip_forward_blacklist(headers)
    assert "Authorization" not in stripped
    assert "Cookie" not in stripped
    assert "X-Api-Key" not in stripped
    assert stripped["User-Agent"] == "test"
    assert stripped["Accept"] == "*/*"


def test_filter_response_headers_strips_hop_by_hop():
    headers = {
        "Content-Type": "text/html",
        "Connection": "keep-alive",
        "Keep-Alive": "timeout=5",
        "Transfer-Encoding": "chunked",
        "Cache-Control": "max-age=3600",
    }
    filtered = filter_response_headers(headers)
    assert "Connection" not in filtered
    assert "Keep-Alive" not in filtered
    assert "Transfer-Encoding" not in filtered
    assert filtered["Content-Type"] == "text/html"
    assert filtered["Cache-Control"] == "max-age=3600"


def test_filter_response_headers_strips_security_headers():
    headers = {
        "Content-Type": "text/html",
        "Strict-Transport-Security": "max-age=31536000",
        "Content-Security-Policy": "default-src 'self'",
        "X-Frame-Options": "DENY",
        "Cache-Control": "max-age=3600",
    }
    filtered = filter_response_headers(headers)
    assert "Strict-Transport-Security" not in filtered
    assert "Content-Security-Policy" not in filtered
    assert "X-Frame-Options" not in filtered
    assert filtered["Content-Type"] == "text/html"


# ---------------------------------------------------------------------------
# Resolver tests
# ---------------------------------------------------------------------------

def test_resolve_relative_url():
    resolved = resolve_url("/assets/image.webp", "https://example.com/page.html")
    assert resolved == "https://example.com/assets/image.webp"


def test_resolve_protocol_relative_url():
    resolved = resolve_url("//cdn.example.com/file.js", "https://example.com/page.html")
    assert resolved == "https://cdn.example.com/file.js"


def test_resolve_absolute_url():
    resolved = resolve_url("https://other.com/file.css", "https://example.com/page.html")
    assert resolved == "https://other.com/file.css"


def test_same_origin():
    assert same_origin("https://example.com/a", "https://example.com/b")
    assert not same_origin("https://example.com/a", "https://other.com/b")
    assert not same_origin("http://example.com/a", "https://example.com/b")


def test_get_origin():
    assert get_origin("https://example.com/path/to/file") == "https://example.com"


# ---------------------------------------------------------------------------
# CSS rewriter tests
# ---------------------------------------------------------------------------

def test_rewrite_css_url():
    css = '.hero { background-image: url("../images/hero.webp"); }'
    result = rewrite_css(css, "https://example.com/css/theme.css", "/resource-proxy")
    assert "/resource-proxy/fetch" in result
    assert "u=https://example.com/images/hero.webp" in result or "u=" in result


def test_rewrite_css_font_face():
    css = '@font-face { src: url("../fonts/example.woff2"); }'
    result = rewrite_css(css, "https://example.com/css/theme.css", "/resource-proxy")
    assert "/resource-proxy/fetch" in result


def test_rewrite_css_import():
    css = '@import url("./theme.css");'
    result = rewrite_css(css, "https://example.com/css/base.css", "/resource-proxy")
    assert "/resource-proxy/fetch" in result


def test_rewrite_css_preserves_unrelated():
    css = '.foo { color: red; } .bar { margin: 0; }'
    result = rewrite_css(css, "https://example.com/css/theme.css", "/resource-proxy")
    assert result == css


def test_extract_css_urls():
    css = 'url("../images/a.png"); url("./fonts/b.woff");'
    urls = extract_css_urls(css)
    assert len(urls) == 2


# ---------------------------------------------------------------------------
# Response tests
# ---------------------------------------------------------------------------

def test_proxy_response_extracts_metadata():
    headers = {
        "Content-Type": "text/css",
        "Content-Length": "1234",
        "Cache-Control": "max-age=3600",
        "ETag": "\"abc123\"",
        "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT",
        "Accept-Ranges": "bytes",
        "Connection": "keep-alive",
    }
    resp = ProxyResponse(200, headers, body=b"body", url="https://example.com/style.css")
    assert resp.status_code == 200
    assert resp.content_type == "text/css"
    assert resp.content_length == "1234"
    assert resp.etag == '"abc123"'
    assert resp.cache_control == "max-age=3600"
    assert "Connection" not in resp.headers


def test_proxy_response_is_cacheable():
    resp = ProxyResponse(
        200,
        {"Content-Type": "text/css", "Cache-Control": "max-age=3600"},
        body=b"body",
        url="https://example.com/style.css",
    )
    assert resp.is_cacheable()
    assert resp.cache_ttl() == 3600


def test_proxy_response_no_store_not_cacheable():
    resp = ProxyResponse(
        200,
        {"Content-Type": "text/html", "Cache-Control": "no-store"},
        body=b"body",
        url="https://example.com/page.html",
    )
    assert not resp.is_cacheable()


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------

def test_cache_put_and_get():
    cache = ResourceCache(max_bytes=1024 * 1024, max_entries=100)
    resp = ProxyResponse(
        200,
        {"Content-Type": "text/css", "Cache-Control": "max-age=3600"},
        body=b"css body",
        url="https://example.com/style.css",
    )
    assert cache.put("session1", "https://example.com/style.css", resp)
    cached = cache.get("session1", "https://example.com/style.css")
    assert cached is not None
    assert cached.body == b"css body"


def test_cache_session_isolation():
    cache = ResourceCache(max_bytes=1024 * 1024, max_entries=100)
    resp = ProxyResponse(
        200,
        {"Content-Type": "text/css", "Cache-Control": "max-age=3600"},
        body=b"session1 body",
        url="https://example.com/style.css",
    )
    cache.put("session1", "https://example.com/style.css", resp)
    # Different session should not see it.
    assert cache.get("session2", "https://example.com/style.css") is None


def test_cache_no_store_not_cached():
    cache = ResourceCache(max_bytes=1024 * 1024, max_entries=100)
    resp = ProxyResponse(
        200,
        {"Content-Type": "text/html", "Cache-Control": "no-store"},
        body=b"body",
        url="https://example.com/page.html",
    )
    assert not cache.put("session1", "https://example.com/page.html", resp)
    assert cache.get("session1", "https://example.com/page.html") is None


def test_cache_ttl_expiry():
    import time
    cache = ResourceCache(max_bytes=1024 * 1024, max_entries=100)
    resp = ProxyResponse(
        200,
        {"Content-Type": "text/css", "Cache-Control": "max-age=1"},
        body=b"body",
        url="https://example.com/style.css",
    )
    cache.put("session1", "https://example.com/style.css", resp)
    assert cache.get("session1", "https://example.com/style.css") is not None
    time.sleep(1.1)
    # After expiry, get returns None (expired entries are removed).
    assert cache.get("session1", "https://example.com/style.css") is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
