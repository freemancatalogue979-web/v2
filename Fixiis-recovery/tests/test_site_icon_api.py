"""The site icon API: the domain the page is in answers for its own icon.

"The favicon — what it should do is use an API of the domain the page is in and
fetch it." The visitor's tab asks `/api/site/icon?domain=<host>`; this module
checks the contract around that route without touching the network:

  * the route exists and is served from our origin (so a hotlink block, a CORS
    refusal or a 200-HTML soft-404 on the site cannot leave the tab blank);
  * only a real public hostname is ever fetched (never localhost, an IP
    literal, a port or a path — this endpoint must not become an SSRF hole);
  * the answer is only ever an image, with its real media type (a soft-404 HTML
    page is not an icon and must be rejected, not served);
  * the icon is cached, so a page load costs a dict lookup;
  * no third-party favicon service is involved.

Run: PYTHONPATH=. python tests/test_site_icon_api.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import api
from fastapi import HTTPException

CHECKS = 0


def ok(cond, label):
    global CHECKS
    assert cond, label
    CHECKS += 1


# ---------------------------------------------------------------------------
# 1. The route is registered, on our own origin
# ---------------------------------------------------------------------------
paths = {getattr(r, "path", "") for r in api.app.routes}
ok("/api/site/icon" in paths, "the site icon API route exists")

src = Path(api.__file__).read_text(encoding="utf-8")
ok('@app.get("/api/site/icon")' in src, "it is a GET on the app")
client = (Path(__file__).resolve().parent.parent / "client.html").read_text(encoding="utf-8")
ok("function siteIconApiFor(" in client, "the client resolves the icon through the api")
ok("'/api/site/icon?domain=' + encodeURIComponent(host)" in client,
   "it names the domain the page is in")
ok("typeof API_ORIGIN === 'string'" in client,
   "a standalone client.html asks the configured backend, not its own origin")
ok("function mirrorIconCandidates(" in client
   and "siteIconApiFor(base)" in client,
   "the tab's icon chain uses it")
ok("function updateFavicon(" in client
   and client.count("siteIconApiFor(base)") >= 2,
   "and so does the first paint, before any page has arrived")

# No third-party favicon service anywhere (it answers with its own globe).
for name in ("api.py", "client.html"):
    body = (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")
    ok("google.com/s2/favicons" not in body,
       "%s never asks a third-party favicon service" % name)

# ---------------------------------------------------------------------------
# 2. Only a real public hostname is fetched
# ---------------------------------------------------------------------------
safe = api._icon_host_is_safe
ok(safe("bank.example"), "a plain domain is fetched")
ok(safe("www.bank.co.uk"), "a subdomain is fetched")
ok(safe("Bank.Example."), "a trailing dot and capitals are normalised")
for bad in ("", "localhost", "localhost:8080", "127.0.0.1", "10.0.0.5",
            "intranet", "/etc/passwd", "bank.example/path", "user@bank.example",
            "http://bank.example", ".local", "printer.local", "[::1]"):
    ok(not safe(bad), "%r is refused" % bad)

# ---------------------------------------------------------------------------
# 3. Only an image is ever served, with its real type
# ---------------------------------------------------------------------------
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
ICO = b"\x00\x00\x01\x00" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 8

for raw, media in ((PNG, "image/png"), (GIF, "image/gif"), (ICO, "image/x-icon"),
                   (JPG, "image/jpeg"), (SVG, "image/svg+xml"), (WEBP, "image/webp")):
    got, got_media = api._icon_bytes(raw)
    ok(got == raw and got_media == media, "%s bytes are served as %s" % (media, media))

for name, raw in (
    ("an HTML soft-404", b"<!doctype html><html><body>Not found</body></html>"),
    ("a JSON error", b'{"error":"forbidden"}'),
    ("an empty body", b""),
    ("an oversized body", PNG + b"\x00" * (api._SITE_ICON_MAX_BYTES + 1)),
):
    ok(api._icon_bytes(raw) == (b"", ""), "%s is not an icon" % name)

# ---------------------------------------------------------------------------
# 4. Where a domain's icon is looked for
# ---------------------------------------------------------------------------
candidates, page = api._icon_url_candidates("bank.example", "bank.example/login")
ok(candidates[0] == "https://bank.example/apple-touch-icon.png",
   "the sharp 180px touch icon is asked first")
ok("https://bank.example/favicon.ico" in candidates,
   "the root favicon.ico is the floor")
ok(page == "https://bank.example/login",
   "a bare host path is turned into a real URL for the page scan (got %r)" % page)
_, page2 = api._icon_url_candidates("bank.example", "https://bank.example/x")
ok(page2 == "https://bank.example/x", "an absolute URL is left alone")

# ---------------------------------------------------------------------------
# 5. The route's own behaviour
# ---------------------------------------------------------------------------
async def _route(domain, url=""):
    return await api.site_icon(domain=domain, url=url)


try:
    try:
        asyncio.run(_route("localhost"))
        ok(False, "a non-public host is refused")
    except HTTPException as exc:
        ok(exc.status_code == 400, "a non-public host is refused with 400")

    try:
        asyncio.run(_route(""))
        ok(False, "an empty domain is refused")
    except HTTPException as exc:
        ok(exc.status_code == 400, "an empty domain is refused with 400")

    real_payload = api._site_icon_payload
    asked = {}

    async def fake_payload(host, page_url=""):
        asked["host"] = host
        asked["page"] = page_url
        return PNG, "image/png", "domain"

    api._site_icon_payload = fake_payload
    try:
        resp = asyncio.run(_route("https://www.Bank.Example/login?x=1"))
        ok(asked["host"] == "bank.example",
           "the host is normalised for the fetch (got %r)" % asked["host"])
        ok(asked["page"].lower().startswith("https://www.bank.example/login"),
           "the page URL is passed through, so its declared icons can win (got %r)"
           % asked["page"])
        ok(resp.body == PNG, "the icon bytes are returned unchanged")
        ok(resp.media_type == "image/png", "with the right media type")
        ok(resp.headers["cache-control"].startswith("public"),
           "and a cache header, so the tab does not re-ask on every page")
        ok(resp.headers["x-icon-source"] == "domain",
           "the source is reported for debugging")

        async def no_icon(host, page_url=""):
            return b"", "", ""

        api._site_icon_payload = no_icon
        try:
            asyncio.run(_route("bank.example"))
            ok(False, "an unknown icon is a 404, never an empty image")
        except HTTPException as exc:
            ok(exc.status_code == 404, "an unknown icon is a 404, never an empty image")
    finally:
        api._site_icon_payload = real_payload
finally:
    api._SITE_ICON_BYTES.clear()

# ---------------------------------------------------------------------------
# 6. The cache is bounded, and the fetch is on a short leash
# ---------------------------------------------------------------------------
ok(0 < api._SITE_ICON_BYTES_TTL <= 24 * 3600,
   "an icon is cached for a bounded time, so a site can rebrand")
ok(0 < api._ICON_FETCH_TIMEOUT <= 6,
   "the fetch is short: an icon is a nicety on a loading path, never a wait")
ok(api._SITE_ICON_MAX_BYTES <= 2 * 1024 * 1024,
   "an oversized body is never read into memory")

api._SITE_ICON_BYTES.clear()
fetches = {"n": 0}
real_fetch = api._fetch_icon_bytes
real_declared = api._fetch_declared_icons


async def counting_fetch(candidates):
    fetches["n"] += 1
    return PNG, "image/png"


async def no_declared(page_url):
    return []


api._fetch_icon_bytes = counting_fetch
api._fetch_declared_icons = no_declared
try:
    first = asyncio.run(_route("bank.example"))
    second = asyncio.run(_route("bank.example"))
    ok(first.body == PNG and second.body == PNG, "the icon is served both times")
    ok(fetches["n"] == 1,
       "the second page load costs a dict lookup, not another fetch (got %d)" % fetches["n"])
finally:
    api._fetch_icon_bytes = real_fetch
    api._fetch_declared_icons = real_declared
    api._SITE_ICON_BYTES.clear()

# ---------------------------------------------------------------------------
# 7. When the domain itself refuses, the copy the server keeps answers
# ---------------------------------------------------------------------------
import tempfile

import config as _config

real_base = _config.CONFIG.profile_base_path
try:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "u1" / "favicons").mkdir(parents=True)
        (base / "u1" / "favicons" / "bank_com.png").write_bytes(PNG)
        _config.CONFIG.profile_base_path = str(base)

        data, media = api._stored_icon_bytes("/api/profiles/favicons/bank_com.png")
        ok(data == PNG and media == "image/png",
           "the server's own copy of the icon is read from the profile")
        ok(api._stored_icon_bytes("/api/profiles/favicons/missing.png") == (b"", ""),
           "a missing file is not an icon")
        ok(api._stored_icon_bytes("/api/profiles/favicons/../../etc/passwd") == (b"", ""),
           "a path through the name is refused")
        ok(api._stored_icon_bytes("https://cdn.example/logo.png") == (b"", ""),
           "and an off-origin URL is not read from disk")

        async def nothing_here():
            return []

        async def no_bytes(candidates):
            return b"", ""

        api._SITE_ICON_BYTES.clear()
        real_fetch = api._fetch_icon_bytes
        real_declared = api._fetch_declared_icons
        real_site_icon_url = api._site_icon_url
        api._fetch_icon_bytes = no_bytes
        api._fetch_declared_icons = lambda page_url: nothing_here()
        api._site_icon_url = lambda user: "/api/profiles/favicons/bank_com.png"
        try:
            data, media, source = asyncio.run(api._site_icon_payload("bank.example"))
            ok(data == PNG and source == "stored",
               "a domain that refuses still gets its icon from the server's copy")
        finally:
            api._fetch_icon_bytes = real_fetch
            api._fetch_declared_icons = real_declared
            api._site_icon_url = real_site_icon_url
            api._SITE_ICON_BYTES.clear()
finally:
    _config.CONFIG.profile_base_path = real_base

# ---------------------------------------------------------------------------
# 7b. One fetch per host: concurrent visitors share it, a miss is remembered
# ---------------------------------------------------------------------------
api._SITE_ICON_BYTES.clear()
api._SITE_ICON_MISS.clear()
api._SITE_ICON_LOCKS.clear()
calls = {"n": 0}
real_fetch = api._fetch_icon_bytes
real_declared = api._fetch_declared_icons
real_stored = api._stored_icon_bytes


async def slow_fetch(candidates):
    calls["n"] += 1
    await asyncio.sleep(0.05)
    return PNG, "image/png"


async def no_declared(page_url):
    return []


api._fetch_icon_bytes = slow_fetch
api._fetch_declared_icons = no_declared
try:
    async def burst():
        return await asyncio.gather(*[api._site_icon_payload("bank.example")
                                      for _ in range(8)])

    results = asyncio.run(burst())
    ok(all(len(r[0]) == len(PNG) for r in results), "every caller gets the icon bytes")
    ok(calls["n"] == 1,
       "eight simultaneous visitors cost ONE fetch of the site (got %d)" % calls["n"])
    ok(len(api._SITE_ICON_LOCKS) == 1, "the lock is per host")

    # A site with no icon at all: one fetch, then a quiet 404 for a few minutes.
    calls["n"] = 0
    api._SITE_ICON_BYTES.clear()
    api._SITE_ICON_MISS.clear()

    async def nothing(candidates):
        if candidates:                 # an empty list is not a fetch
            calls["n"] += 1
        return b"", ""

    api._fetch_icon_bytes = nothing
    api._stored_icon_bytes = lambda url: (b"", "")
    try:
        first = asyncio.run(api._site_icon_payload("noicon.example"))
        second = asyncio.run(api._site_icon_payload("noicon.example"))
        ok(first[0] == b"" and second[0] == b"", "a host with no icon resolves to nothing")
        ok(calls["n"] == 1,
           "and the miss is remembered instead of hammering the site (got %d)" % calls["n"])
        api._SITE_ICON_MISS.clear()
    finally:
        api._stored_icon_bytes = real_stored
finally:
    api._fetch_icon_bytes = real_fetch
    api._fetch_declared_icons = real_declared
    api._SITE_ICON_BYTES.clear()
    api._SITE_ICON_MISS.clear()
    api._SITE_ICON_LOCKS.clear()

# The per-host dictionaries cannot grow without bound.
for i in range(api._SITE_ICON_MAX_HOSTS + 40):
    api._SITE_ICON_BYTES["h%d.example" % i] = (api.time.time(), PNG, "image/png", "domain")
api._site_icon_prune(api.time.time())
ok(len(api._SITE_ICON_BYTES) <= api._SITE_ICON_MAX_HOSTS,
   "the icon cache is bounded (%d entries)" % len(api._SITE_ICON_BYTES))
api._SITE_ICON_BYTES.clear()

# ---------------------------------------------------------------------------
# 8. A machine that cannot verify certificates still gets icons
# ---------------------------------------------------------------------------
# A TLS-intercepting proxy whose CA is not in Python's store makes every
# handshake fail. An icon is a public picture, not a secret: the fetch retries
# once unverified and remembers that, instead of turning the whole endpoint into
# a 404 on that machine.
import ssl


class _FakeCertError(Exception):
    pass


ok(api._icon_cert_error(_FakeCertError(
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get "
    "local issuer certificate (_ssl.c:992)")),
   "a failed certificate verification is recognised")
ok(api._icon_cert_error(_FakeCertError("certificate verify failed")),
   "the shorter OpenSSL wording is recognised too")
ok(api._icon_cert_error(ssl.SSLError("handshake failure")),
   "a bare SSLError is recognised")
ok(not api._icon_cert_error(TimeoutError("timed out")),
   "a timeout is not a certificate problem (it is never retried insecurely)")

ok(api._ICON_TLS_STATE["verify"] is True,
   "verification is ON by default — the insecure retry is a fallback, never the mode")
src_api = Path(api.__file__).read_text(encoding="utf-8")
ok('_ICON_TLS_STATE["verify"] = False' in src_api,
   "a certificate failure flips it once and is remembered")
ok("for verify in ((True, False)" in src_api,
   "verification is tried FIRST and the unverified attempt is the second one")
ok("verify=verify," in src_api,
   "the flag is per-request (an AsyncClient's own setting, not a global disable)")

# The retry is per-request and bounded: two attempts at most.
real_http_get = api._icon_http_get
api._ICON_TLS_STATE["verify"] = False

async def _insecure_used(url, accept):
    return None

api._icon_http_get = _insecure_used
try:
    data, media = asyncio.run(api._fetch_icon_bytes(["https://bank.example/favicon.ico"]))
    ok((data, media) == (b"", ""), "no response is no icon, never an exception")
finally:
    api._icon_http_get = real_http_get
    api._ICON_TLS_STATE["verify"] = True

print("site icon api: %d checks passed" % CHECKS)
