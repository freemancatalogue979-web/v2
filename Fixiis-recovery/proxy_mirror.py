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


def proxied_path(target_url: str, sid: str = "") -> str:
    """Encode a full target URL into a single /m/ path (slashes stay raw).

    The session id is embedded as the FIRST PATH SEGMENT — /m/<sid>/<url> —
    NOT as a ?sid= query param: a query on the injected <base> is dropped by
    RFC 3986 relative resolution, so every runtime-built relative URL in page
    JS would lose the session and hit /m/ without the persistent context's
    cookies.  With the sid in the path, relative resolution keeps it:
    base /m/S/https://a.com/x/y + "z.png" -> /m/S/https://a.com/x/z.png.
    """
    enc = quote(target_url or "", safe=_PROXY_SAFE)
    if sid:
        return PROXY_PREFIX + quote(sid, safe="") + "/" + enc
    return PROXY_PREFIX + enc


def parse_proxy_path(target: str):
    """Split a /m/ path back into (sid, target_url).  sid may be empty.

    Accepts both /m/<sid>/<url> (preferred) and /m/<url> (sid then comes from
    the ?sid= query param at the route).
    """
    target = target or ""
    m = re.match(r"^(?:([^/]+)/)?(https?://.+)$", target, re.I | re.S)
    if m:
        return (m.group(1) or ""), m.group(2)
    if re.match(r"^https?://", target, re.I):
        return "", target
    return "", "https://" + target


def proxied_page_url(target_url: str, sid: str = "") -> str:
    """The same-origin URL the client iframe loads for ``target_url``."""
    return proxied_path(target_url, sid)


def _target_origin(target_url: str) -> str:
    p = urlsplit(target_url)
    return f"{p.scheme}://{p.netloc}"


_SKIP_PREFIXES = ("data:", "blob:", "javascript:", "mailto:", "tel:", "about:", "#")


def rewrite_ref(ref: Optional[str], base_target: str, sid: str = "") -> Optional[str]:
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
    if ref.startswith(("/m/", "/api/", "/assets/")):
        return None  # already on our origin — never double-proxy
    if low.startswith("http://") or low.startswith("https://"):
        return proxied_path(ref, sid)
    if ref.startswith("//"):
        return proxied_path("https:" + ref, sid)
    if ref.startswith("/"):
        # Root-absolute: against the TARGET's origin, not ours.
        return proxied_path(_target_origin(base_target) + ref, sid)
    return None  # relative — the injected <base> handles it (sid stays in path)


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
# <meta http-equiv="refresh" content="0; url=..."> — the url must stay proxied
# or the iframe navigates straight to the real site, breaking out of /m/.
_META_REFRESH_RE = re.compile(
    r'(<meta\b[^>]*http-equiv\s*=\s*["\']?refresh["\']?[^>]*content\s*=\s*["\'])([^"\']*)(["\'])',
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


def rewrite_css_for_proxy(css: str, base_target: str, sid: str = "") -> str:
    def _url_sub(m: "re.Match[str]") -> str:
        new = rewrite_ref(m.group(2), base_target, sid)
        if not new:
            return m.group(0)
        return m.group(0).replace(m.group(2), new, 1)

    def _import_sub(m: "re.Match[str]") -> str:
        new = rewrite_ref(m.group(2), base_target, sid)
        if not new:
            return m.group(0)
        return m.group(0).replace(m.group(2), new, 1)

    css = _CSS_URL_RE.sub(_url_sub, css)
    css = _CSS_IMPORT_RE.sub(_import_sub, css)
    return css


def build_router_script(sid: str, target_url: str) -> str:
    """JS injected at the very top of <head>, BEFORE any page script.

    The HTML rewriter only fixes static attributes; page JS builds URLs at
    runtime (fetch/XHR/img.src=/window.open/…).  On an insecure (http) origin we
    cannot use a service worker, so this script patches the URL entry points
    in-page to route through /m/<sid>/… instead.  It also bridges
    localStorage/sessionStorage with the persistent remote context
    (GET/POST /api/proxy/store) so sites that keep login tokens in storage
    behave as if they were the remote browser.
    """
    import json as _json
    p = _json.dumps({"prefix": PROXY_PREFIX, "sid": sid or "", "origin": _target_origin(target_url)})
    return (
        "(function(){var P=" + p + ";window.__PROXY__=P;"
        "function enc(u){return encodeURIComponent(u).replace(/%2F/g,'/').replace(/%3A/g,':');}"
        "function rw(u){if(u==null)return u;u=String(u);if(!u)return u;"
        "if(/^(data:|blob:|javascript:|mailto:|tel:|about:|#)/i.test(u))return u;"
        "if(/^\/(m|api|assets)\//.test(u))return u;"
        "if(/^https?:\/\//i.test(u))return P.prefix+(P.sid?P.sid+'/':'')+enc(u);"
        "if(u.indexOf('//')===0)return P.prefix+(P.sid?P.sid+'/':'')+enc('https:'+u);"
        "if(u.charAt(0)==='/')return P.prefix+(P.sid?P.sid+'/':'')+enc(P.origin+u);"
        "return u;}"
        "window.__proxyRw=rw;"
        "try{var of=window.fetch;if(of)window.fetch=function(inp,init){if(typeof inp==='string')inp=rw(inp);"
        "else if(inp&&inp.url){try{inp=new Request(rw(inp.url),inp);}catch(e){}}return of.call(this,inp,init);};}catch(e){}"
        "try{var oo=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(){var a=Array.prototype.slice.call(arguments);"
        "if(a.length>1)a[1]=rw(a[1]);return oo.apply(this,a);};}catch(e){}"
        "try{var sb=navigator.sendBeacon&&navigator.sendBeacon.bind(navigator);if(sb)navigator.sendBeacon=function(u,d){return sb(rw(u),d);};}catch(e){}"
        "try{var OE=window.EventSource;if(OE)window.EventSource=function(u,c){return new OE(rw(u),c);};}catch(e){}"
        "try{var ow=window.open;if(ow)window.open=function(u,t,f){return ow.call(window,rw(u),t,f);};}catch(e){}"
        "function pp(proto,prop){try{var d=Object.getOwnPropertyDescriptor(proto,prop);if(!d||!d.set)return;var st=d.set;"
        "Object.defineProperty(proto,prop,{configurable:true,get:d.get,set:function(v){try{v=rw(v);}catch(e){}return st.call(this,v);}});}catch(e){}}"
        "try{['HTMLImageElement','HTMLScriptElement','HTMLLinkElement','HTMLMediaElement','HTMLSourceElement','HTMLTrackElement','HTMLIFrameElement','HTMLEmbedElement'].forEach(function(n){var q=window[n]&&window[n].prototype;if(q)pp(q,'src');});"
        "['HTMLAnchorElement','HTMLAreaElement','HTMLBaseElement'].forEach(function(n){var q=window[n]&&window[n].prototype;if(q)pp(q,'href');});"
        "var fp=window.HTMLFormElement&&window.HTMLFormElement.prototype;if(fp)pp(fp,'action');"
        "var op2=window.HTMLObjectElement&&window.HTMLObjectElement.prototype;if(op2)pp(op2,'data');}catch(e){}"
        "try{var done=false;"
        "function api(qs,body){return fetch('/api/proxy/store'+qs,{method:body?'POST':'GET',"
        "headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined,credentials:'include'});}"
        "function seed(){if(done)return;done=true;"
        "try{api('?sid='+encodeURIComponent(P.sid)+'&origin='+encodeURIComponent(P.origin)).then(function(r){return r.ok?r.json():null;}).then(function(d){if(!d)return;"
        "try{var ls=d.localStorage||{};for(var k in ls){try{localStorage.setItem(k,ls[k]);}catch(e){}}}catch(e){}"
        "try{var ss=d.sessionStorage||{};for(var k in ss){try{sessionStorage.setItem(k,ss[k]);}catch(e){}}}catch(e){}"
        "}).catch(function(){});}catch(e){}}"
        "if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',seed);else seed();"
        "var osi=Storage.prototype.setItem,ori=Storage.prototype.removeItem,ocl=Storage.prototype.clear;"
        "function push(op,k,v){try{api('?sid='+encodeURIComponent(P.sid)+'&origin='+encodeURIComponent(P.origin),"
        "{op:op,key:k,value:v,which:(this===localStorage?'localStorage':'sessionStorage')});}catch(e){}}"
        "Storage.prototype.setItem=function(k,v){push('set',String(k),String(v));return osi.call(this,k,v);};"
        "Storage.prototype.removeItem=function(k){push('remove',String(k));return ori.call(this,k);};"
        "Storage.prototype.clear=function(){push('clear');return ocl.call(this);};"
        "}catch(e){}"
        "})();"
    )


# Valid JS: try { ... } catch(e) { } with balanced braces — a malformed inject
# here shows up in the client console as "Missing catch or finally after try".
_SW_NEUTER_JS = (
    "(function(){try{if(navigator.serviceWorker){"
    "navigator.serviceWorker.register=function(){return Promise.resolve(null)}"
    "}}catch(e){}})();"
)


def rewrite_html_for_proxy(html: str, target_url: str, sid: str = "") -> str:
    """Rewrite a fetched document so every reference routes through /m/.

    Relative refs need no rewrite: the injected ``<base href="/m/<target>">``
    sends them back through the proxy.  Root-absolute and cross-origin absolute
    refs are rewritten explicitly.  Meta CSP tags are stripped (header CSP is
    stripped at the route), and a tiny script neuters the target's service
    worker so it cannot hijack our origin.  <script> bodies are NEVER touched.
    """
    if not html:
        return html
    base = proxied_page_url(target_url, sid)

    def _attr_sub(m: "re.Match[str]") -> str:
        new = rewrite_ref(m.group(3), target_url, sid)
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
            new = rewrite_ref(bits[0], target_url, sid)
            parts.append((new if new else bits[0]) + ((" " + bits[1]) if len(bits) > 1 else ""))
        if not parts:
            return m.group(0)
        return m.group(1) + m.group(2) + ", ".join(parts) + m.group(2)

    def _rewrite_segment(seg: str) -> str:
        seg = _URL_ATTR_RE.sub(_attr_sub, seg)
        seg = _SRCSET_ATTR_RE.sub(_srcset_sub, seg)
        seg = _STYLE_BLOCK_RE.sub(
            lambda m: m.group(1) + rewrite_css_for_proxy(m.group(2), target_url, sid) + m.group(3),
            seg,
        )
        seg = _STYLE_ATTR_RE.sub(
            lambda m: m.group(1) + m.group(2) + rewrite_css_for_proxy(m.group(3), target_url, sid) + m.group(2),
            seg,
        )
        # Meta CSP cannot be removed via response headers — drop the tag.
        seg = _META_CSP_RE.sub("", seg)
        # Meta refresh: rewrite the url= target so timed redirects stay in /m/.
        def _refresh_sub(m: "re.Match[str]") -> str:
            content = m.group(2)
            mm = re.search(r"url\s*=\s*(.+)$", content, re.IGNORECASE)
            if not mm:
                return m.group(0)
            urlpart = mm.group(1).strip().strip("'\"")
            new = rewrite_ref(urlpart, target_url, sid)
            if not new:
                return m.group(0)
            new_content = content[: mm.start(1)] + new + content[mm.end(1):]
            return m.group(1) + new_content + m.group(3)
        seg = _META_REFRESH_RE.sub(_refresh_sub, seg)
        return seg

    html = _map_outside_scripts(html, _rewrite_segment)

    # Inject <base> + SW-neuter at the very top of <head> so they win over any
    # page script.  The base makes every relative ref resolve to /m/<target>.
    router = "<script>" + build_router_script(sid, target_url) + "</script>"
    inject = f'<base href="{base}">' + router + "<script>" + _SW_NEUTER_JS + "</script>"
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


def rewrite_location(location: str, base_target: str, sid: str = "") -> str:
    """Rewrite a redirect Location header to its proxied form (sid kept in path)."""
    if not location:
        return location
    absu = urljoin(base_target, location)
    new = rewrite_ref(absu, base_target, sid)
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


def merge_cookie_headers(context_header: str, browser_header: str) -> str:
    """Merge the persistent context's jar with the cookies the browser attached
    to the /m/ request.

    The route mirrors every upstream Set-Cookie onto OUR origin (Path=/), so
    document.cookie in the proxied page works and the browser auto-sends those
    on /m/ requests.  Browser-sent pairs win for names present in both, because
    they also carry document.cookie writes made by page JS.
    """
    out: Dict[str, str] = {}
    order: List[str] = []
    for hdr in (context_header, browser_header):
        for part in (hdr or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name = part.split("=", 1)[0].strip()
            if not name:
                continue
            if name not in out:
                order.append(name)
            out[name] = part
    return "; ".join(out[n] for n in order)


def cookie_records_from_header(header: str, target_url: str) -> List[Dict[str, Any]]:
    """Parse a Cookie REQUEST header into context.add_cookies() records — used
    to sync document.cookie writes made in the proxied page back into the
    persistent remote context."""
    out: List[Dict[str, Any]] = []
    for part in (header or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            out.append({"name": name, "value": value.strip(), "url": target_url})
    return out


def mirror_set_cookie_for_our_origin(raw: str) -> str:
    """Turn an upstream Set-Cookie into one for OUR origin (Path=/, no Domain)
    so document.cookie inside the proxied iframe sees the site's cookies."""
    first = (raw or "").split(";", 1)[0].strip()
    if not first or "=" not in first:
        return ""
    return first + "; Path=/"


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
    "range",
}
