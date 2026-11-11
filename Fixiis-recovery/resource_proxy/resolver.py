"""URL resolution for the reverse resource proxy.

Resolves relative, protocol-relative, and absolute URLs against a base URL,
respecting the original page's base URL and handling redirects.  Implements
Part 2, Sections 11 (resolver.py) and 13 (URL rewriting).
"""

from __future__ import annotations

from typing import Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit, quote


def resolve_url(
    ref: str,
    base_url: str,
) -> str:
    """Resolve ``ref`` against ``base_url``.

    Handles:
      * Relative URLs: ``/assets/image.webp``, ``./theme.css``, ``../fonts/x.woff2``
      * Protocol-relative URLs: ``//cdn.example.com/file.js``
      * Absolute URLs: ``https://example.com/assets/image.webp``

    Returns the absolute resolved URL.
    """
    if not ref:
        return base_url
    # urljoin handles relative, protocol-relative, and absolute URLs.
    resolved = urljoin(base_url, ref)
    return resolved


def split_url(url: str) -> Tuple[str, str, str, str, str]:
    """Split a URL into (scheme, netloc, path, query, fragment)."""
    parts = urlsplit(url)
    return (parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment)


def join_url(
    scheme: str,
    netloc: str,
    path: str,
    query: str = "",
    fragment: str = "",
) -> str:
    """Join URL components back into a URL string."""
    return urlunsplit((scheme, netloc, path, query, fragment))


def get_origin(url: str) -> str:
    """Return the origin (scheme://netloc) of a URL."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def get_host(url: str) -> str:
    """Return the hostname of a URL."""
    return urlsplit(url).hostname or ""


def get_path(url: str) -> str:
    """Return the path component of a URL."""
    return urlsplit(url).path


def get_query(url: str) -> str:
    """Return the query string of a URL."""
    return urlsplit(url).query


def same_origin(url_a: str, url_b: str) -> bool:
    """Return True when both URLs share the same scheme and host."""
    pa = urlsplit(url_a)
    pb = urlsplit(url_b)
    return pa.scheme == pb.scheme and pa.netloc == pb.netloc


def encode_path(path: str) -> str:
    """Percent-encode a URL path while preserving reserved delimiters."""
    return quote(path, safe="/:@!$&'()*+,;=-._~")


__all__ = [
    "resolve_url",
    "split_url",
    "join_url",
    "get_origin",
    "get_host",
    "get_path",
    "get_query",
    "same_origin",
    "encode_path",
]
