"""CSS URL rewriting for the reverse resource proxy.

Rewrites relative URLs in CSS (@import, url(), font-face src, background-image,
etc.) against the stylesheet's actual URL so they resolve through the
appropriate permitted route.  Implements Part 2, Section 11 (css_rewriter.py)
and Section 13 (URL rewriting for CSS).

Does NOT rewrite unrelated CSS content.
"""

from __future__ import annotations

import re
from typing import Optional

from .resolver import resolve_url, get_origin


# url(...) — matches url("..."), url('...'), url(...)
_URL_RE = re.compile(
    r'url\(\s*(["\']?)(.*?)\1\s*\)',
    re.IGNORECASE,
)

# @import url("...") or @import "..." or @import ...;
_IMPORT_RE = re.compile(
    r'@import\s+(?:url\(\s*)?(["\'])(.*?)\1(?:\s*\))?[^;]*;',
    re.IGNORECASE,
)


def rewrite_css(
    css_text: str,
    stylesheet_url: str,
    proxy_base: str,
) -> str:
    """Rewrite all resource URLs in ``css_text`` to use the proxy.

    Handles:
      * url(...) references (background images, fonts, etc.)
      * @import url(...) and @import "..."
      * @font-face src: url(...)

    ``proxy_base`` is the base path for proxy URLs (e.g. "/resource-proxy").
    The original URL is embedded in the proxy path so it can be recovered.

    Returns the rewritten CSS text.
    """
    if not css_text:
        return css_text

    origin = get_origin(stylesheet_url)

    def _rewrite_url(match: re.Match) -> str:
        quote_char = match.group(1)
        original_url = match.group(2).strip()
        if not original_url:
            return match.group(0)
        # Resolve against the stylesheet's origin.
        resolved = resolve_url(original_url, stylesheet_url)
        # Build the proxy URL.
        proxy_url = f"{proxy_base}/fetch?{_encode_proxy_params(resolved)}"
        return f"url({quote_char}{proxy_url}{quote_char})"

    def _rewrite_import(match: re.Match) -> str:
        quote_char = match.group(1)
        original_url = match.group(2).strip()
        if not original_url:
            return match.group(0)
        resolved = resolve_url(original_url, stylesheet_url)
        proxy_url = f"{proxy_base}/fetch?{_encode_proxy_params(resolved)}"
        return f'@import url({quote_char}{proxy_url}{quote_char});'

    css_text = _URL_RE.sub(_rewrite_url, css_text)
    css_text = _IMPORT_RE.sub(_rewrite_import, css_text)
    return css_text


def _encode_proxy_params(resolved_url: str) -> str:
    """Encode a resolved URL as proxy query parameters."""
    from urllib.parse import urlencode, urlsplit, quote

    parts = urlsplit(resolved_url)
    # Include scheme, netloc, path, and query so the original is fully recoverable.
    params = {
        "u": resolved_url,
    }
    return urlencode(params)


def extract_css_urls(css_text: str) -> list:
    """Extract all resource URLs referenced in CSS text (for prewarming).

    Returns a list of (original_url, resolved_url) tuples.
    """
    urls = []
    for match in _URL_RE.finditer(css_text):
        original = match.group(2).strip()
        if original:
            urls.append(original)
    for match in _IMPORT_RE.finditer(css_text):
        original = match.group(2).strip()
        if original:
            urls.append(original)
    return urls


__all__ = [
    "rewrite_css",
    "extract_css_urls",
]
