"""Response metadata preservation for the reverse resource proxy.

Preserves relevant response metadata (Content-Type, Cache-Control, ETag,
etc.) while stripping hop-by-hop and origin-specific security headers that
are invalid for a proxy response.  Implements Part 2, Section 11 (response.py).
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from . import config
from .security import filter_response_headers


class ProxyResponse:
    """A proxied HTTP response with preserved metadata."""

    __slots__ = (
        "status_code",
        "headers",
        "content_type",
        "content_length",
        "content_encoding",
        "cache_control",
        "etag",
        "last_modified",
        "accept_ranges",
        "content_range",
        "_headers_ci",
        "body",
        "stream",
        "url",
    )

    def __init__(
        self,
        status_code: int,
        headers: Dict[str, str],
        body: Optional[bytes] = None,
        stream=None,
        url: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        # Filter hop-by-hop and origin security headers before storing.
        self.headers: Dict[str, str] = filter_response_headers(headers)
        self.body = body
        self.stream = stream
        self.url = url

        # Build a case-insensitive header lookup (HTTP headers are case-insensitive).
        self._headers_ci: Dict[str, str] = {k.lower(): v for k, v in self.headers.items()}

        def _hdr(name: str) -> str:
            return self._headers_ci.get(name.lower(), "")

        # Extract key metadata for convenience.
        self.content_type = _hdr("content-type")
        self.content_length = _hdr("content-length")
        self.content_encoding = _hdr("content-encoding")
        self.cache_control = _hdr("cache-control")
        self.etag = _hdr("etag")
        self.last_modified = _hdr("last-modified")
        self.accept_ranges = _hdr("accept-ranges")
        self.content_range = _hdr("content-range")

    @classmethod
    def from_origin_response(
        cls,
        status_code: int,
        headers: Dict[str, str],
        body: Optional[bytes] = None,
        stream=None,
        url: Optional[str] = None,
    ) -> "ProxyResponse":
        """Build a ProxyResponse from an origin server response."""
        return cls(status_code, headers, body=body, stream=stream, url=url)

    def to_dict(self) -> Dict[str, object]:
        """Serialize to a plain dict (for logging/debugging)."""
        return {
            "status_code": self.status_code,
            "url": self.url,
            "content_type": self.content_type,
            "content_length": self.content_length,
            "content_encoding": self.content_encoding,
            "cache_control": self.cache_control,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "accept_ranges": self.accept_ranges,
            "content_range": self.content_range,
            "headers": self.headers,
        }

    def is_cacheable(self) -> bool:
        """Return True when the response should be cached per HTTP semantics."""
        cc = self.cache_control.lower()
        if "no-store" in cc or "private" in cc:
            return False
        if self.status_code not in (200, 203, 204, 206, 300, 301, 308, 404, 410, 414, 501):
            return False
        # No-Cache responses can still be cached but must be revalidated.
        return True

    def cache_ttl(self) -> Optional[int]:
        """Return the cache TTL in seconds, or None when not cacheable."""
        if not self.is_cacheable():
            return None
        cc = self.cache_control.lower()
        # max-age takes precedence.
        for token in cc.split(","):
            token = token.strip()
            if token.startswith("max-age="):
                try:
                    return int(token.split("=", 1)[1])
                except ValueError:
                    return None
        # Fall back to Expires header if present.
        expires = self._headers_ci.get("expires", "")
        if expires:
            try:
                from email.utils import parsedate_to_datetime
                import time as _time
                exp_dt = parsedate_to_datetime(expires)
                return max(0, int(exp_dt.timestamp() - _time.time()))
            except Exception:
                return None
        # Use the configured default.
        return config.DEFAULT_CACHE_TTL_S


__all__ = [
    "ProxyResponse",
]
