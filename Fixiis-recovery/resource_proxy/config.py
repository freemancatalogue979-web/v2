"""Configuration for the Python reverse resource proxy.

All settings are overridable via environment variables.  The proxy is an
independent service that Browser B uses to load original website resources
when direct loading is unsuitable.  Direct resource URLs remain the default
wherever they work; the proxy is strictly a fallback.
"""

from __future__ import annotations

import os
from typing import Set


# ---------------------------------------------------------------------------
# Operational
# ---------------------------------------------------------------------------

PROXY_HOST: str = os.environ.get("RESOURCE_PROXY_HOST", "127.0.0.1")
PROXY_PORT: int = int(os.environ.get("RESOURCE_PROXY_PORT", "8765"))
PROXY_BASE_PATH: str = os.environ.get("RESOURCE_PROXY_BASE_PATH", "/resource-proxy")

# Maximum concurrent fetches across all clients.
MAX_CONCURRENT_FETCHES: int = int(os.environ.get("RESOURCE_PROXY_MAX_CONCURRENT", "64"))

# Per-fetch timeout in seconds.
FETCH_TIMEOUT_S: float = float(os.environ.get("RESOURCE_PROXY_FETCH_TIMEOUT_S", "30"))

# Maximum body size to buffer in memory (bytes).  Larger bodies are streamed.
def _parse_int_env(name: str, default: int) -> int:
    """Parse an int env var; the default may be a Python expression string."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


MAX_BUFFER_BYTES: int = _parse_int_env("RESOURCE_PROXY_MAX_BUFFER_BYTES", 4 * 1024 * 1024)
MAX_CACHE_BYTES: int = _parse_int_env("RESOURCE_PROXY_MAX_CACHE_BYTES", 512 * 1024 * 1024)

# Default cache TTL in seconds when origin sends no explicit directive.
DEFAULT_CACHE_TTL_S: int = int(os.environ.get("RESOURCE_PROXY_DEFAULT_CACHE_TTL_S", "3600"))

# Maximum cache size in bytes.
MAX_CACHE_BYTES: int = _parse_int_env("RESOURCE_PROXY_MAX_CACHE_BYTES", 512 * 1024 * 1024)

# Maximum number of cached entries.
MAX_CACHE_ENTRIES: int = int(os.environ.get("RESOURCE_PROXY_MAX_CACHE_ENTRIES", "50000"))


# ---------------------------------------------------------------------------
# Destination allowlisting (SSRF protection)
# ---------------------------------------------------------------------------
#
# The proxy must NOT become a way to bypass access controls or perform SSRF.
# Only destinations explicitly allowed by the operator may be proxied.
# By default nothing is allowed; the operator must populate this list.
# ---------------------------------------------------------------------------

def _parse_allowed_hosts(raw: str) -> Set[str]:
    """Parse a comma-separated list of allowed hostnames (case-insensitive)."""
    hosts = set()
    for h in raw.split(","):
        h = h.strip().lower()
        if h:
            hosts.add(h)
    return hosts


ALLOWED_HOSTS: Set[str] = _parse_allowed_hosts(
    os.environ.get("RESOURCE_PROXY_ALLOWED_HOSTS", "")
)

# Allow subdomains of allowed hosts (e.g. allowing "example.com" also allows
# "assets.example.com").
ALLOW_SUBDOMAINS: bool = (
    os.environ.get("RESOURCE_PROXY_ALLOW_SUBDOMAINS", "0").strip().lower()
    in ("1", "true", "yes", "on")
)

# Allow private/local IP ranges (127.0.0.1, 10.x, 172.16-31.x, 192.168.x).
# DISABLED by default to prevent SSRF against internal services.
ALLOW_PRIVATE_IPS: bool = (
    os.environ.get("RESOURCE_PROXY_ALLOW_PRIVATE_IPS", "0").strip().lower()
    in ("1", "true", "yes", "on")
)

# Allow localhost.
ALLOW_LOCALHOST: bool = (
    os.environ.get("RESOURCE_PROXY_ALLOW_LOCALHOST", "0").strip().lower()
    in ("1", "true", "yes", "on")
)


# ---------------------------------------------------------------------------
# Header filtering
# ---------------------------------------------------------------------------
#
# Hop-by-hop headers that must NOT be forwarded to the origin or returned
# to the client, per RFC 7230.
# ---------------------------------------------------------------------------

HOP_BY_HOP_HEADERS: Set[str] = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})

# Origin-specific security headers that are invalid for a proxy response and
# must be stripped before returning to the client.
ORIGIN_SECURITY_HEADERS: Set[str] = frozenset({
    "strict-transport-security",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
    "x-content-type-options",
    "x-xss-protection",
    "referrer-policy",
    "expect-CT",
    "feature-policy",
    "permissions-policy",
})

# Headers that must never be forwarded to the origin (client secrets).
FORWARD_BLACKLIST_HEADERS: Set[str] = frozenset({
    "authorization",
    "cookie",
    "x-api-key",
    "x-auth-token",
    "proxy-authorization",
    "websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-accept",
})


# ---------------------------------------------------------------------------
# Resource routing defaults
# ---------------------------------------------------------------------------

# Resource types that are always fetched through the proxy when the origin
# is allowed (no direct-first preference).  Kept small to avoid becoming a
# full proxy by default.
FORCE_PROXY_TYPES: Set[str] = frozenset({
    # e.g. "pdf" if the operator wants all PDFs proxied
})

# MIME types that should never be cached (dynamic content).
NO_CACHE_MIME_PREFIXES: tuple = (
    "text/event-stream",
    "text/html",
    "application/json",
)


__all__ = [
    "PROXY_HOST", "PROXY_PORT", "PROXY_BASE_PATH",
    "MAX_CONCURRENT_FETCHES", "FETCH_TIMEOUT_S", "MAX_BUFFER_BYTES",
    "DEFAULT_CACHE_TTL_S", "MAX_CACHE_BYTES", "MAX_CACHE_ENTRIES",
    "ALLOWED_HOSTS", "ALLOW_SUBDOMAINS", "ALLOW_PRIVATE_IPS", "ALLOW_LOCALHOST",
    "HOP_BY_HOP_HEADERS", "ORIGIN_SECURITY_HEADERS", "FORWARD_BLACKLIST_HEADERS",
    "FORCE_PROXY_TYPES", "NO_CACHE_MIME_PREFIXES",
]
