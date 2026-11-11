"""Reverse resource proxy for the DOM Streaming Engine.

Provides Browser B with an authorized same-origin route for loading original
website resources when direct loading is unsuitable.  Direct resource URLs
remain the default; the proxy is strictly a fallback.

Modules:
  config        - Configuration (env-overridable)
  security      - SSRF protection, destination allowlisting, header filtering
  resolver      - URL resolution (relative, protocol-relative, absolute)
  response      - Response metadata preservation
  cache         - HTTP-semantics caching (never mixes sessions)
  css_rewriter  - CSS URL rewriting (@import, url(), font-face, etc.)
  streaming     - Streaming, range requests, cancellation
  client        - Async HTTP client with connection pooling
  gateway       - Starlette application entry point
"""

from . import config, security, resolver, response, cache, css_rewriter, streaming, client, gateway

__all__ = [
    "config",
    "security",
    "resolver",
    "response",
    "cache",
    "css_rewriter",
    "streaming",
    "client",
    "gateway",
]
