"""Reverse resource proxy gateway — main entry point.

Receives Browser B resource requests, validates destinations, identifies
resource types, forwards permitted requests, and returns responses with
preserved metadata.  Implements Part 2, Section 11 (gateway.py) and
Section 12 (resource routing rules).

The proxy is a fallback — direct resource URLs remain the default wherever
they work.  Only allowed destinations may be proxied (SSRF protection).
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from typing import Optional, Tuple, Dict, Any
from urllib.parse import parse_qs, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse, JSONResponse
from starlette.routing import Route, Mount

from . import config
from .security import validate_destination, filter_response_headers, strip_forward_blacklist
from .resolver import get_origin, get_host, same_origin
from .client import ProxyHttpClient, get_global_client
from .response import ProxyResponse
from .cache import get_global_cache, ResourceCache
from .css_rewriter import rewrite_css
from .streaming import StreamedResponse, RangeRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resource type detection
# ---------------------------------------------------------------------------

RESOURCE_TYPE_MAP: Dict[str, str] = {
    # Images
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".svg": "image", ".ico": "image", ".avif": "image",
    ".bmp": "image", ".tiff": "image",
    # Stylesheets
    ".css": "css",
    # Fonts
    ".woff": "font", ".woff2": "font", ".ttf": "font", ".otf": "font",
    ".eot": "font",
    # Video/audio
    ".mp4": "video", ".webm": "video", ".ogg": "video", ".mov": "video",
    ".avi": "video", ".mp3": "audio", ".wav": "audio", ".oga": "audio",
    # JavaScript
    ".js": "javascript", ".mjs": "javascript",
    # HTML
    ".html": "html", ".htm": "html",
}


def detect_resource_type(url: str, content_type: Optional[str] = None) -> str:
    """Detect the resource type from URL extension or Content-Type."""
    path = urlsplit(url).path.lower()
    ext = os.path.splitext(path)[1]
    if ext in RESOURCE_TYPE_MAP:
        return RESOURCE_TYPE_MAP[ext]
    if content_type:
        ct = content_type.lower()
        if ct.startswith("image/"):
            return "image"
        if ct.startswith("text/css"):
            return "css"
        if ct.startswith("font/") or "font" in ct:
            return "font"
        if ct.startswith("video/") or ct.startswith("audio/"):
            return "media"
        if "javascript" in ct or "ecmascript" in ct:
            return "javascript"
        if "html" in ct:
            return "html"
    return "other"


def should_proxy(resource_type: str, url: str) -> bool:
    """Decide whether a resource should be fetched through the proxy.

    Direct-first: most resources are loaded directly by the browser.
    The proxy is only used when the destination is allowed and the
    resource type is in FORCE_PROXY_TYPES, or when direct loading is
    known to be unsuitable (operator configuration).
    """
    if resource_type in config.FORCE_PROXY_TYPES:
        return True
    return False


# ---------------------------------------------------------------------------
# Proxy URL parsing
# ---------------------------------------------------------------------------

def parse_proxy_path(path: str, base_path: str = "") -> Optional[str]:
    """Parse a proxy request path and return the target URL, or None.

    The ``base_path`` prefix is handled by Starlette route mounting; this
    function simply extracts the ``u`` query parameter from the request.
    Expected format: /fetch?u=<encoded-target-url>
    """
    # Extract the 'u' query parameter from the path.
    query_start = path.find("?")
    if query_start == -1:
        return None
    query = path[query_start + 1:]
    params = parse_qs(query)
    target_list = params.get("u")
    if not target_list:
        return None
    return target_list[0]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _fetch_handler(request: Request) -> Response:
    """Handle a proxied resource fetch request.

    Query: ?u=<target-url>
    Headers: optional Range, Accept-Encoding, etc.
    """
    target_url = parse_proxy_path(str(request.url))
    if not target_url:
        return Response("Missing or invalid proxy URL", status_code=400)

    allowed, reason = validate_destination(target_url)
    if not allowed:
        return Response(f"Destination not allowed: {reason}", status_code=403)

    # Detect resource type for logging/metrics.
    resource_type = detect_resource_type(target_url)

    client = get_global_client()
    cache = get_global_cache()

    # --- Check cache first (session-scoped via client host) ---
    session_id = request.client.host if request.client else None
    # Parse optional range request.
    range_header = request.headers.get("range")
    range_req = RangeRequest.parse(range_header)

    # If range request, we can't serve from cache directly (would need
    # to slice cached body).  Skip cache for range requests.
    cached = None if range_req else cache.get(session_id, target_url)

    if cached:
        logger.debug("Cache hit: %s (%s)", target_url, resource_type)
        return _build_response(cached, session_id, range_req)

    # --- Fetch from origin ---
    proxy_resp, error = await client.fetch(
        target_url,
        session_id=session_id,
        headers=dict(request.headers),
    )
    if error or proxy_resp is None:
        return Response(f"Upstream fetch failed: {error}", status_code=502)

    # --- Rewrite CSS if needed ---
    body = proxy_resp.body or b""
    if resource_type == "css" and proxy_resp.content_type and "css" in proxy_resp.content_type.lower():
        try:
            body = rewrite_css(
                body.decode("utf-8", errors="replace"),
                proxy_resp.url or target_url,
                base_path,
            ).encode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("CSS rewrite failed for %s: %s", target_url, exc)

    # --- Handle range requests ---
    if range_req and proxy_resp.accept_ranges and "bytes" in proxy_resp.accept_ranges.lower():
        # Re-fetch with Range header for the specific portion.
        range_headers = dict(request.headers)
        range_req.apply_to_headers(range_headers)
        proxy_resp, error = await client.fetch(
            target_url,
            session_id=session_id,
            headers=range_headers,
        )
        if error or proxy_resp is None:
            return Response(f"Upstream range fetch failed: {error}", status_code=502)
        body = proxy_resp.body or b""
        status_code = 206
        extra_headers = {"Content-Range": range_req.content_range_header(len(body))}
    else:
        status_code = proxy_resp.status_code
        extra_headers = {}

    # --- Store in cache ---
    proxy_resp.body = body
    cache.put(session_id, target_url, proxy_resp)

    return _build_response(proxy_resp, session_id, range_req, extra_headers, status_code)


def _build_response(
    proxy_resp: ProxyResponse,
    session_id: Optional[str],
    range_req: Optional[RangeRequest] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    status_code: Optional[int] = None,
) -> Response:
    """Build a Starlette Response from a ProxyResponse."""
    headers = dict(proxy_resp.headers)
    if extra_headers:
        headers.update(extra_headers)

    # Remove content-length when body may have changed (CSS rewrite, range).
    if range_req or status_code == 206:
        headers.pop("content-length", None)

    body = proxy_resp.body or b""
    sc = status_code or proxy_resp.status_code

    return Response(
        content=body,
        status_code=sc,
        headers=headers,
        media_type=proxy_resp.content_type or None,
    )


async def _stream_handler(request: Request) -> StreamingResponse:
    """Handle a streaming resource request (video, large files).

    Streams the origin response directly to the client without buffering
    the full body in memory.
    """
    target_url = parse_proxy_path(str(request.url))
    if not target_url:
        return Response("Missing or invalid proxy URL", status_code=400)

    allowed, reason = validate_destination(target_url)
    if not allowed:
        return Response(f"Destination not allowed: {reason}", status_code=403)

    session_id = request.client.host if request.client else None
    client = get_global_client()

    origin_resp, error = await client.stream(
        target_url,
        session_id=session_id,
        headers=dict(request.headers),
    )
    if error or origin_resp is None:
        return Response(f"Upstream stream failed: {error}", status_code=502)

    async def stream_generator():
        try:
            async for chunk in origin_resp.aiter_bytes():
                yield chunk
        finally:
            await origin_resp.aclose()

    headers = filter_response_headers(dict(origin_resp.headers))
    return StreamingResponse(
        stream_generator(),
        status_code=origin_resp.status_code,
        headers=headers,
        media_type=origin_resp.headers.get("content-type"),
    )


async def _health_handler(request: Request) -> JSONResponse:
    """Health check endpoint."""
    cache = get_global_cache()
    return JSONResponse(
        content={
            "status": "ok",
            "proxy_base": config.PROXY_BASE_PATH,
            "allowed_hosts": sorted(config.ALLOWED_HOSTS),
            "cache": cache.stats(),
        },
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> Starlette:
    """Create the Starlette application for the resource proxy."""
    routes = [
        Route("/fetch", _fetch_handler, methods=["GET", "HEAD"]),
        Route("/stream", _stream_handler, methods=["GET"]),
        Route("/health", _health_handler, methods=["GET"]),
    ]
    app = Starlette(
        routes=routes,
    )
    return app


app = create_app()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def startup() -> None:
    """Initialize the proxy on startup."""
    logger.info(
        "Resource proxy starting on %s:%d base=%s allowed_hosts=%s",
        config.PROXY_HOST, config.PROXY_PORT, config.PROXY_BASE_PATH,
        sorted(config.ALLOWED_HOSTS),
    )


async def shutdown() -> None:
    """Release resources on shutdown."""
    client = get_global_client()
    await client.close()
    logger.info("Resource proxy shut down")


__all__ = [
    "create_app",
    "app",
    "startup",
    "shutdown",
    "_fetch_handler",
    "_stream_handler",
    "validate_destination",
    "detect_resource_type",
    "should_proxy",
]
