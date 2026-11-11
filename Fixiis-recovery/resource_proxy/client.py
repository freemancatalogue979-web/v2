"""Asynchronous HTTP client for the reverse resource proxy.

Implements connection pooling, persistent connections, concurrent fetching,
redirect handling, timeouts, appropriate request headers, and safe destination
validation.  Implements Part 2, Section 11 (client.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Dict, Any, Tuple

import httpx

from . import config
from .security import validate_destination, strip_forward_blacklist
from .response import ProxyResponse

logger = logging.getLogger(__name__)


class ProxyHttpClient:
    """Async HTTP client with connection pooling for the resource proxy.

    Wraps httpx.AsyncClient with proxy-specific configuration: connection
    pooling, timeouts, redirect handling, safe headers, and destination
    validation.
    """

    def __init__(
        self,
        timeout_s: float = config.FETCH_TIMEOUT_S,
        max_connections: int = config.MAX_CONCURRENT_FETCHES,
        max_keepalive: int = 20,
    ) -> None:
        self.timeout_s = timeout_s
        self.max_connections = max_connections
        self.max_keepalive = max_keepalive
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazily create and return the shared httpx client."""
        if self._client is None or self._client.is_closed:
            async with self._lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(self.timeout_s),
                        limits=httpx.Limits(
                            max_connections=self.max_connections,
                            max_keepalive_connections=self.max_keepalive,
                        ),
                        follow_redirects=True,
                        http2=True,
                    )
        return self._client

    async def fetch(
        self,
        url: str,
        session_id: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[Optional[ProxyResponse], Optional[str]]:
        """Fetch a resource from the origin.

        Returns (ProxyResponse, None) on success or (None, error_reason)
        on failure.  The destination is validated before any network call.
        """
        # --- Destination validation (SSRF protection) ---
        allowed, reason = validate_destination(url)
        if not allowed:
            logger.warning("Proxy fetch blocked: %s (%s)", url, reason)
            return None, reason

        client = await self._get_client()

        # --- Build safe request headers ---
        # Strip client secrets before forwarding.
        req_headers: Dict[str, str] = {
            "User-Agent": "Fixiis-ResourceProxy/1.0",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        if headers:
            req_headers.update(strip_forward_blacklist(headers))

        try:
            resp = await client.get(url, headers=req_headers)
        except httpx.TimeoutException as exc:
            logger.warning("Proxy fetch timeout: %s", url)
            return None, f"timeout: {exc}"
        except httpx.TooManyRedirects as exc:
            logger.warning("Proxy fetch too many redirects: %s", url)
            return None, f"too_many_redirects: {exc}"
        except httpx.RequestError as exc:
            logger.warning("Proxy fetch failed: %s — %s", url, exc)
            return None, f"request_error: {exc}"

        # --- Build ProxyResponse ---
        body = resp.content
        proxy_resp = ProxyResponse(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            body=body,
            url=str(resp.url),
        )

        logger.debug(
            "Proxy fetch %s -> %d %d bytes %s",
            url, resp.status_code, size := len(body),
            proxy_resp.content_type or "?",
        )
        return proxy_resp, None

    async def stream(
        self,
        url: str,
        session_id: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Stream a large resource from the origin without buffering.

        Returns (httpx.Response, None) on success or (None, error_reason)
        on failure.  The caller is responsible for reading/closing the
        response to release connections.
        """
        allowed, reason = validate_destination(url)
        if not allowed:
            logger.warning("Proxy stream blocked: %s (%s)", url, reason)
            return None, reason

        client = await self._get_client()

        req_headers: Dict[str, str] = {
            "User-Agent": "Fixiis-ResourceProxy/1.0",
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
        if headers:
            req_headers.update(strip_forward_blacklist(headers))

        try:
            resp = await client.stream("GET", url, headers=req_headers)
        except httpx.RequestError as exc:
            logger.warning("Proxy stream failed: %s — %s", url, exc)
            return None, f"request_error: {exc}"

        return resp, None

    async def close(self) -> None:
        """Close the underlying HTTP client and release connections."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# Global client instance.
_global_client: Optional[ProxyHttpClient] = None


def get_global_client() -> ProxyHttpClient:
    global _global_client
    if _global_client is None:
        _global_client = ProxyHttpClient()
    return _global_client


__all__ = [
    "ProxyHttpClient",
    "get_global_client",
]
