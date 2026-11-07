#!/usr/bin/env python3
"""Proxy-mirror ("redirect from browser to client") helpers — pure functions.

The idea (see PROXY_MIRROR_DESIGN.md): instead of copy-pasting the DOM out of
the persistent remote browser and re-rendering it in the client (slow, lossy),
the CLIENT loads the site through OUR origin at ``/m/<full-target-url>``.  The
server fetches the target with the persistent context's cookies, rewrites the
document, and serves it same-origin — so the site's own JavaScript runs in the
client iframe and drives the session natively (clicks, forms, XHR).  The
remote browser stays the source of truth for data (cookies are synced back
into its context), and the client controls the session directly through the
proxy instead of waiting on capture round-trips.

URL encoding note: a proxied URL is ``/m/`` + ``quote(target, safe="/:")``.
Slashes stay raw so that an injected ``<base href="/m/<target>">`` still
resolves relative refs segment-by-segment exactly like the original URL, while
``?``, ``&``, ``#``, ``%`` ... are percent-encoded so a target that itself has
a query string (``/signin/identifier?flowName=...``) survives intact inside
the path instead of being split off into the proxy request's own query (which
is how upstream 400 "malformed" responses happen).  ASGI percent-decodes the
path before routing, so the route receives the full original URL back.

Everything here is pure string/cookie logic so it can be unit-tested without
FastAPI: the HTTP route in api.py is a thin wrapper around these.
"""
import os
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit

PROXY_PREFIX = "/m/"

# Characters kept raw inside a proxied path: "/" (so <base> relative
# resolution keeps working) and ":" (readability).  Everything structurally
# dangerous in a URL — ?, &, #, %, space, quotes — is percent-encoded.
_PROXY_SAFE = "/:"


def is_proxy_enabled() -> bool:
    # Default ON: the proxy mirror is the primary live path ("real site in the
    # iframe"). Set MIRROR_PROXY=0 to fall back to the capture mirror only.
    return os.environ.get("MIRROR_PROXY", "1").strip().lower() not in ("0", "false", "no", "off")


def proxied_path(target_url: str) -> str:
    """Encode a full target URL into a single /m/ path (slashes stay raw)."""
    return PROXY_PREFIX + quote(target_url or "", safe=_PROXY_SAFE)


def proxied_page_url(target_url: str, sid: str = "") -> str:
    """The same-origin URL the client iframe loads for ``target_url``."""
    u = proxied_path(target_url)
    if sid:
        u += "?sid=" + quote(sid, safe="")
    return u


def _target_origin(target_url: str) -> str:
    p = urlsplit(target_url)
    return f"{p.scheme}://{p.netloc}"


_SKIP_PREFIXES = ("data:", "blob:", "javascript:", "mailto:", "tel:", "about:", "#")


def rewrite_ref(ref: Optional[str], base_target: str) -> Optional[str]:
    """Rewrite one URL reference for proxy serving.

    Returns the rewritten (encoded) /m/ path, or None to leave the ref alone
    (relative refs are resolved by the injected <base>, and data:/js:/# refs
    must not move).
    """
    if not ref:
        return None
    ref = ref.strip()
    if not ref or ref.startswith(_SKIP_PREFIXES):
        return None
    low = ref.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return proxied_path(ref)
    if ref.startswith("//"):
        return proxied_path("https:" + ref)
    if ref.startswith("/"):
        # Root-absolute: against the TARGET's origin, not ours.
        return proxied_path(_target_origin(base_target) + ref)
    return None  # relative — the injected <base> handles it


# Attributes whose value is a single URL.  These are applied ONLY outside
# <script> blocks — page JS routinely contains strings like src="..." and
# rewriting those would corrupt the script (SyntaxError in the client).
_URL_ATTR_RE = re.compile(
    r'(\b(?:src|href|action|poster|formaction|data-src|data-href|cite)\s*=\s*)(["\'])(.*?)\2',
    re.IGNORECASE | re.DOTALL,
)
# srcset is a comma-separated list of "url descriptor" tokens.
_SRCSET_ATTR_RE = re.compile(
    r'(\bsrcset\s*=\s*)(["\'])(.*?)\2', re.IGNORECASE | re.DOTALL
)
_CSS_URL_RE = re.compile(r'url\(\s*(["\']?)([^"\')\s][^"\')]*?)\1\s*\)', re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(
    r'@import\s+(?:url\(\s*)?(["\'])([^"\']+)\1', re.IGNORECASE
)
_STYLE_BLOCK_RE = re.compile(r'(<style\b[^>]*>)(.*?)(</style\s*>)', re.IGNORECASE | re.DOTALL)
_STYLE_ATTR_RE = re.compile(r'(<[^>]*?\bstyle\s*=\s*)(["\'])(.*?)\2', re.IGNORECASE | re.DOTALL)
_META_CSP_RE = re.compile(
    r'<meta\b[^>]*http-equiv\s*=\s*["\']?content-security-policy["\']?[^>]*>',
    re.IGNORECASE,
)
_BASE_TAG_RE = re.compile(r'<base\b[^>]*>', re.IGNORECASE)
_SCRIPT_BLOCK_RE = re.compile(r'(<script\b[^>]*>)(.*?)(</script\s*>)', re.IGNORECASE | re.DOTALL)


def _map_outside_scripts(html: str, fn: Callable[[str], str]) -> str:
    """Apply ``fn`` to every part of ``html`` except <script>...</script> bodies."""
    if "<script" not in html.lower():
        return fn(html)
    parts = _SCRIPT_BLOCK_RE.split(html)
    # With 3 capture groups, split() yields [text, open, body, close, text, ...]
    out = []
    for i, part in enumerate(parts):
        out.append(part if i % 4 != 0 else fn(part))
    return "".join(out)


def rewrite_css_for_proxy(css: str, base_target: str) -> str:
    def _url_sub(m: "re.Match[str]") -> str:
        new = rewrite_ref(m.group(2), base_target)
        if not new:
            return m.group(0)
        return m.group(0).replace(m.group(2), new, 1)

    def _import_sub(m: "re.Match[str]") -> str:
        new = rewrite_ref(m.group(2), base_target)
        if not new:
            return m.group(0)
        return m.group(0).replace(m.group(2), new, 1)

    css = _CSS_URL_RE.sub(_url_sub, css)
    css = _CSS_IMPORT_RE.sub(_import_sub, css)
    return css


# Valid JS: try { ... } catch(e) { } with balanced braces — a malformed inject
# here shows up in the client console as "Missing catch or finally after try".
_SW_NEUTER_JS = (
    "(function(){try{if(navigator.serviceWorker){"
    "navigator.serviceWorker.register=function(){return Promise.resolve(null)}"
    "}}catch(e){}})();"
)


def rewrite_html_for_proxy(html: str, target_url: str) -> str:
    """Rewrite a fetched document so every reference routes through /m/.

    Relative refs need no rewrite: the injected ``<base href="/m/<target>">``
    sends them back through the proxy.  Root-absolute and cross-origin absolute
    refs are rewritten explicitly.  Meta CSP tags are stripped (header CSP is
    stripped at the route), and a tiny script neuters the target's service
    worker so it cannot hijack our origin.  <script> bodies are NEVER touched.
    """
    if not html:
        return html
    base = proxied_page_url(target_url)

    def _attr_sub(m: "re.Match[str]") -> str:
        new = rewrite_ref(m.group(3), target_url)
        if not new:
            return m.group(0)
        return m.group(1) + m.group(2) + new + m.group(2)

    def _srcset_sub(m: "re.Match[str]") -> str:
        parts = []
        for tok in m.group(3).split(","):
            tok_stripped = tok.strip()
            if not tok_stripped:
                continue
            bits = tok_stripped.split(None, 1)
            new = rewrite_ref(bits[0], target_url)
            parts.append((new if new else bits[0]) + ((" " + bits[1]) if len(bits) > 1 else ""))
        if not parts:
            return m.group(0)
        return m.group(1) + m.group(2) + ", ".join(parts) + m.group(2)

    def _rewrite_segment(seg: str) -> str:
        seg = _URL_ATTR_RE.sub(_attr_sub, seg)
        seg = _SRCSET_ATTR_RE.sub(_srcset_sub, seg)
        seg = _STYLE_BLOCK_RE.sub(
            lambda m: m.group(1) + rewrite_css_for_proxy(m.group(2), target_url) + m.group(3),
            seg,
        )
        seg = _STYLE_ATTR_RE.sub(
            lambda m: m.group(1) + m.group(2) + rewrite_css_for_proxy(m.group(3), target_url) + m.group(2),
            seg,
        )
        # Meta CSP cannot be removed via response headers — drop the tag.
        seg = _META_CSP_RE.sub("", seg)
        return seg

    html = _map_outside_scripts(html, _rewrite_segment)

    # Inject <base> + SW-neuter at the very top of <head> so they win over any
    # page script.  The base makes every relative ref resolve to /m/<target>.
    inject = f'<base href="{base}">' + "<script>" + _SW_NEUTER_JS + "</script>"
    if _BASE_TAG_RE.search(html):
        html = _map_outside_scripts(
            html, lambda seg: _BASE_TAG_RE.sub(inject, seg, count=1)
        )
    else:
        m = re.search(r"<head\b[^>]*>", html, re.IGNORECASE)
        if m:
            html = html[: m.end()] + inject + html[m.end():]
        else:
            m = re.search(r"<html\b[^>]*>", html, re.IGNORECASE)
            if m:
                html = html[: m.end()] + "<head>" + inject + "</head>" + html[m.end():]
            else:
                html = inject + html
    return html


def rewrite_location(location: str, base_target: str) -> str:
    """Rewrite a redirect Location header to its proxied form."""
    if not location:
        return location
    absu = urljoin(base_target, location)
    new = rewrite_ref(absu, base_target)
    return new if new else location


# ---------------------------------------------------------------------------
# Cookies — the bridge between the persistent remote context and the proxy
# ---------------------------------------------------------------------------

def cookie_header_from_records(records: List[Dict[str, Any]]) -> str:
    """Build a Cookie request header from Playwright-style cookie dicts."""
    parts: List[str] = []
    seen = set()
    for c in records or []:
        name = str(c.get("name") or "").strip()
        value = c.get("value")
        if not name or value is None:
            continue
        if name in seen:
            continue
        seen.add(name)
        # Cookie values must not carry CR/LF or separators that would make the
        # upstream server reject the whole request as malformed (400).
        value = str(value).replace("\r", "").replace("\n", "").replace(";", "%3B")
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def set_cookie_records(set_cookie_values: List[str], target_url: str) -> List[Dict[str, Any]]:
    """Parse raw Set-Cookie header values into context.add_cookies() records.

    Domain/Path attributes are dropped — the cookie is pinned to the target
    URL's origin, which is what the persistent context should hold.
    """
    out: List[Dict[str, Any]] = []
    for raw in set_cookie_values or []:
        if not raw:
            continue
        first = raw.split(";", 1)[0]
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        name = name.strip()
        if not name:
            continue
        out.append({"name": name, "value": value.strip(), "url": target_url})
    return out


# Headers that must never pass from the upstream response to the client iframe.
STRIP_RESPONSE_HEADERS = {
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
    "strict-transport-security",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
    "permissions-policy",
    "report-to",
    "nel",
}

# Request headers worth forwarding upstream (rewritten where needed).
FORWARD_REQUEST_HEADERS = {
    "user-agent",
    "accept",
    "accept-language",
    "accept-encoding",
    "content-type",
    "referer",
    "origin",
    "cookie",
}
