#!/usr/bin/env python3
"""A site's own loading screen, and the logo it shows.

Two things an operator asked for:

  1. a domain can carry its own loading page (uploaded HTML, or an archived
     page) plus the provider's logo, configured once and used for every page
     captured from that site — instead of the workflow's generic branding;
  2. the loading page is already in the document the visitor receives, so a
     workflow starting must not load a second copy on top of it.

Pinned here:

  * domain input is normalised (scheme, www, port, path gone) and matched by
    host, subdomains included, most specific parent first;
  * a domain with a logo and no uploaded page gets a generated loading screen
    that draws the logo at its natural size (crisp, never scaled up);
  * the boot resolution order: workflow page -> the site's page -> the one
    configured site (when the visit's landing page is not known yet) -> LPV
    landing page -> nothing;
  * a capture's host is not simply its first URL: <base>, rel=canonical and
    og:url are believed first, then URLs by how often they appear — so a
    gstatic/akamai asset at the top of the markup cannot stop a site's own
    loading screen from being found;
  * `/api/lpv/domains/<domain>/spinner.html` serves it, 404 when it is off;
  * page -> domain and page -> logo candidates come from the capture itself
    (apple-touch-icon before a 16px favicon);
  * pushes carry the site's spinner URL, and the client adopts the embedded
    copy instead of fetching it again.

Needs fastapi; skips on a bare checkout.

    python3 tests/test_domain_spinner.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import api  # noqa: E402
    import lpv_store  # noqa: E402
    import server_settings as srv_settings  # noqa: E402
    from starlette.requests import Request  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - bare checkout
    print("domain spinner: skipped (%s not installed)" % exc.name)
    raise SystemExit(0)

CLIENT = (ROOT / "client.html").read_text(encoding="utf-8", errors="replace")

SPINNER_HTML = (
    "<!doctype html><html><head><style>body{margin:0;background:#0b1020}"
    ".ring{width:56px;height:56px;border:4px solid rgba(255,255,255,.2);"
    "border-top-color:#a1b2c3;border-radius:50%}</style></head>"
    "<body><div class=\"ring\"></div></body></html>"
)

PAGE_HTML = """<!doctype html><html><head>
<base href="https://www.example-provider.com/">
<link rel="icon" href="/favicon.ico" sizes="16x16">
<link rel="apple-touch-icon" href="/assets/touch-180.png" sizes="180x180">
<meta property="og:image" content="https://www.example-provider.com/og.png">
<title>Sign in</title></head><body>page</body></html>"""


def request_for(query: str = "") -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/client.html",
        "headers": [], "query_string": query.encode(),
        "scheme": "http", "server": ("testserver", 80),
    })


def served(query: str = "") -> str:
    return bytes(api._render_client_html(request_for(query)).body).decode("utf-8")


def embedded_html(body: str) -> str:
    if 'srcdoc="' not in body:
        return ""
    start = body.index('srcdoc="') + len('srcdoc="')
    end = body.index('"', start)
    return (body[start:end].replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&amp;", "&"))


class _StoreMixin:
    """Shared fixtures: every test pages/domains are removed afterwards."""

    def setUp(self):
        self.pages = []
        self.domains = []
        # A domain configured for real (the developer's own store) must not
        # change what these tests see: "no site configured" has to mean none.
        self._parked = []
        for row in lpv_store.list_domains():
            if row.get("enabled"):
                self._parked.append(row["domain"])
                lpv_store.save_domain({"domain": row["domain"], "enabled": False})

    def tearDown(self):
        for pid in self.pages:
            lpv_store.delete_page(pid)
        for domain in self.domains:
            lpv_store.delete_domain(domain)
        for domain in getattr(self, "_parked", []):
            lpv_store.save_domain({"domain": domain, "enabled": True})

    def page(self, name="Page", html=PAGE_HTML):
        page = lpv_store.save_page(name=name, html=html)
        self.pages.append(page["id"])
        return page

    def domain(self, **kwargs):
        saved = lpv_store.save_domain(kwargs)
        self.domains.append(saved["domain"])
        return saved


class DomainStoreCase(_StoreMixin, unittest.TestCase):
    """Storing, normalising and matching domain configurations."""

    def test_a_link_becomes_a_bare_host(self):
        for raw, want in [
            ("https://www.chase.com/login?x=1", "chase.com"),
            ("HTTP://Chase.COM:8443/", "chase.com"),
            ("chase.com", "chase.com"),
            ("  www.login.example.co.uk  ", "login.example.co.uk"),
            ("chase.com.", "chase.com"),
            ("user:pass@bank.example.com/path", "bank.example.com"),
        ]:
            self.assertEqual(lpv_store.normalise_domain(raw), want, raw)

    def test_nonsense_is_rejected(self):
        for raw in ("", "   ", "localhost", "not a domain", "http://", "..", "-"):
            self.assertEqual(lpv_store.normalise_domain(raw), "", raw)
        with self.assertRaises(ValueError):
            lpv_store.save_domain({"domain": "localhost"})

    def test_subdomains_match_the_parent(self):
        self.domain(domain="chase.com")
        self.assertTrue(lpv_store.domain_matches("login.chase.com", "chase.com"))
        self.assertTrue(lpv_store.domain_matches("chase.com", "chase.com"))
        self.assertFalse(lpv_store.domain_matches("notchase.com", "chase.com"))
        self.assertEqual(
            (lpv_store.find_domain("secure.login.chase.com") or {}).get("domain"),
            "chase.com",
        )

    def test_the_most_specific_domain_wins(self):
        self.domain(domain="chase.com", label="Chase")
        self.domain(domain="login.chase.com", label="Chase sign-in")
        self.assertEqual(
            (lpv_store.find_domain("login.chase.com") or {}).get("label"),
            "Chase sign-in",
        )
        self.assertEqual(
            (lpv_store.find_domain("www.chase.com") or {}).get("label"),
            "Chase",
        )

    def test_a_switched_off_domain_does_not_apply(self):
        self.domain(domain="chase.com", enabled=False)
        self.assertIsNone(lpv_store.find_domain("chase.com"))

    def test_saving_keeps_what_was_not_resent(self):
        self.domain(domain="chase.com", label="Chase", spinner_html=SPINNER_HTML)
        lpv_store.save_domain({"domain": "chase.com", "enabled": False})
        reloaded = lpv_store.get_domain("chase.com")
        self.assertEqual(reloaded["label"], "Chase")
        self.assertIn("ring", reloaded["spinner_html"])
        self.assertFalse(reloaded["enabled"])

    def test_the_list_does_not_ship_the_html(self):
        self.domain(domain="chase.com", spinner_html=SPINNER_HTML)
        listed = [d for d in lpv_store.list_domains() if d["domain"] == "chase.com"][0]
        self.assertEqual(listed["spinner_html"], "")
        self.assertTrue(listed["has_spinner_html"])

    def test_an_oversized_loading_page_is_refused(self):
        with self.assertRaises(ValueError):
            lpv_store.save_domain({
                "domain": "chase.com",
                "spinner_html": "x" * (lpv_store.DOMAIN_SPINNER_MAX_BYTES + 10),
            })


class PageLookupCase(_StoreMixin, unittest.TestCase):
    def test_a_page_knows_its_site(self):
        page = self.page()
        self.assertEqual(api._page_domain(page["id"]), "www.example-provider.com")
        self.assertEqual(lpv_store.find_domain("www.example-provider.com"), None)

    def test_the_best_logo_comes_first(self):
        page = self.page()
        host, candidates = api._page_logo_candidates(page["id"])
        self.assertEqual(host, "www.example-provider.com")
        self.assertGreaterEqual(len(candidates), 3)
        self.assertEqual(candidates[0]["kind"], "apple-touch-icon")
        self.assertTrue(candidates[0]["url"].endswith("/assets/touch-180.png"))
        self.assertTrue(candidates[0]["url"].startswith("https://www.example-provider.com/"))
        kinds = [c["kind"] for c in candidates]
        self.assertLess(kinds.index("apple-touch-icon"), kinds.index("icon"),
                        "a 180px touch icon beats a 16px favicon")
        self.assertIn("og:image", kinds)
        # The site root is offered as a last resort.
        self.assertTrue(any(c["kind"] == "root-icon" for c in candidates))

    def test_a_page_without_any_host_is_harmless(self):
        page = self.page(html="<html><body>bare</body></html>")
        self.assertEqual(api._page_domain(page["id"]), "")
        self.assertEqual(api._page_logo_candidates(page["id"])[1], [])


class BootLoadingCase(_StoreMixin, unittest.TestCase):
    def workflow(self, **kwargs):
        steps = kwargs.pop("steps", [])
        wf = lpv_store.save_workflow(None, "Flow", "", steps, **kwargs)
        self.addCleanup(lpv_store.delete_workflow, wf["id"])
        return wf

    def test_the_site_page_is_embedded_for_its_workflow(self):
        page = self.page()
        self.domain(domain="example-provider.com", spinner_html=SPINNER_HTML)
        wf = self.workflow(steps=[{"kind": "page", "page_id": page["id"]}])
        body = served("workflow=" + wf["id"])
        self.assertIn('<div id="lpvBootLoading"', body)
        self.assertIn('#a1b2c3', embedded_html(body)[:5000])
        self.assertIn(f'data-lpv-url="/api/lpv/domains/example-provider.com/spinner.html"', body)

    def test_a_domain_with_no_logo_uses_its_favicon(self):
        # No capture of this site, so nothing to read an icon out of.
        self.domain(domain="example-provider.com", label="Example")
        self.assertEqual(
            api._domain_logo(lpv_store.get_domain("example-provider.com")),
            "https://example-provider.com/favicon.ico",
        )
        html = api._brand_spinner_html(lpv_store.get_domain("example-provider.com"))
        self.assertIn("https://example-provider.com/favicon.ico", html)

    def test_the_captures_icon_beats_the_guessed_favicon(self):
        page = self.page()
        self.domain(domain="example-provider.com", label="Example")
        api._DOMAIN_LOGO_CACHE.clear()
        self.assertEqual(
            api._domain_logo(lpv_store.get_domain("example-provider.com")),
            "https://www.example-provider.com/assets/touch-180.png",
        )

    def test_an_explicit_logo_still_wins(self):
        self.domain(domain="example-provider.com", logo_url="https://cdn.example.org/mark.svg")
        self.assertEqual(
            api._domain_logo(lpv_store.get_domain("example-provider.com")),
            "https://cdn.example.org/mark.svg",
        )

    def test_the_generated_screen_survives_a_missing_icon(self):
        cfg = {"domain": "example-provider.com", "label": "Example Provider"}
        html = api._brand_spinner_html(cfg)
        self.assertIn("onerror=", html, "a missing icon falls back instead of a broken box")
        self.assertIn('"https://example-provider.com/favicon.ico"', html)
        self.assertIn('id="dm-initial"', html, "and ends on a letter tile")
        self.assertIn(">E<", html, "the letter is the name's first letter")

    def test_the_icon_falls_back_from_a_touch_icon_to_a_favicon(self):
        # A bigger icon that is not really there must degrade gracefully, and
        # in the right order: touch icon (usually 180px) before a 16px favicon.
        cfg = {"domain": "example-provider.com", "label": "Example"}
        html = api._brand_spinner_html(cfg)
        self.assertIn('var q=["https://example-provider.com/apple-touch-icon.png", '
                      '"https://example-provider.com/favicon.ico"]', html)
        self.assertIn("q[i-1]", html, "the queue is walked one entry per error")
        self.assertIn("dm-initial", html, "and ends on the letter tile")

    def test_a_logo_only_domain_gets_a_generated_screen(self):
        page = self.page()
        self.domain(
            domain="example-provider.com",
            label="Example Provider",
            logo_url="https://www.example-provider.com/assets/touch-180.png",
        )
        wf = self.workflow(steps=[{"kind": "page", "page_id": page["id"]}])
        html = embedded_html(served("workflow=" + wf["id"]))[:6000]
        self.assertIn("Example Provider", html)
        self.assertIn("touch-180.png", html)
        self.assertIn("object-fit:contain", html, "the logo keeps its proportions")
        self.assertIn("width:76px", html, "drawn at its natural size, never scaled up")
        self.assertNotIn("scale(", html.split("</style>")[0].split(".mark")[1],
                         "no transform on the mark (that is what softens a logo)")

    def test_the_workflow_page_still_wins(self):
        page = self.page()
        self.domain(domain="example-provider.com", spinner_html=SPINNER_HTML)
        wf = self.workflow(
            steps=[{"kind": "page", "page_id": page["id"]}],
            redirect_html="<html><body><p id=\"wf-only\">workflow loader</p></body></html>",
        )
        body = served("workflow=" + wf["id"])
        self.assertIn("workflow loader", embedded_html(body)[:5000])
        self.assertNotIn("a1b2c3", embedded_html(body)[:5000])

    def test_no_domain_means_untouched(self):
        page = self.page()
        wf = self.workflow(steps=[{"kind": "page", "page_id": page["id"]}])
        body = served("workflow=" + wf["id"])
        self.assertNotIn('<div id="lpvBootLoading"', body)
        self.assertNotIn("data-lpv-boot-loading", body)

    def test_a_switched_off_domain_is_ignored(self):
        page = self.page()
        self.domain(domain="example-provider.com", spinner_html=SPINNER_HTML, enabled=False)
        wf = self.workflow(steps=[{"kind": "page", "page_id": page["id"]}])
        self.assertNotIn('<div id="lpvBootLoading"', served("workflow=" + wf["id"]))

    def test_an_archived_page_can_be_the_loader(self):
        loader = self.page(name="Loader", html=SPINNER_HTML)
        page = self.page(name="Target")
        self.domain(domain="example-provider.com", spinner_page_id=loader["id"])
        wf = self.workflow(steps=[{"kind": "page", "page_id": page["id"]}])
        body = served("workflow=" + wf["id"])
        self.assertIn("#a1b2c3", embedded_html(body)[:5000])

    def test_the_lpv_landing_page_still_works(self):
        landing = self.page(name="Landing", html="<html><body>landing</body></html>")
        original = srv_settings.get_settings().lpv_default_page
        srv_settings.get_settings().lpv_default_page = landing["id"]
        srv_settings.get_settings().lpv_only_mode = True
        try:
            self.assertIn("landing", served(""))
        finally:
            srv_settings.get_settings().lpv_default_page = original


CDN_FIRST_HTML = """<!doctype html><html><head>
<script src="https://cdn.example-assets.net/app.js"></script>
<link rel="canonical" href="https://www.example-provider.com/">
<link rel="apple-touch-icon" href="https://www.example-provider.com/touch-180.png">
</head><body><a href="https://www.example-provider.com/signin">Sign in</a></body></html>"""

MANY_PROVIDER_URLS_HTML = """<!doctype html><html><head>
<script src="https://cdn.example-assets.net/app.js"></script>
<link rel="apple-touch-icon" href="https://www.example-provider.com/touch-180.png">
</head><body>
<img src="https://www.example-provider.com/logo.png">
<a href="https://www.example-provider.com/one">1</a>
<a href="https://www.example-provider.com/two">2</a>
</body></html>"""


class CdnHostCase(_StoreMixin, unittest.TestCase):
    """A capture's first URL is often an asset host, not the site itself."""

    def test_the_declared_address_beats_a_leading_cdn_url(self):
        page = self.page(html=CDN_FIRST_HTML)
        self.assertEqual(api._page_domain(page["id"]), "www.example-provider.com")

    def test_the_most_mentioned_host_wins_without_a_canonical(self):
        page = self.page(html=MANY_PROVIDER_URLS_HTML)
        self.assertEqual(api._page_domain(page["id"]), "www.example-provider.com")

    def test_every_host_the_page_mentions_is_a_candidate(self):
        page = self.page(html=MANY_PROVIDER_URLS_HTML)
        hosts = api._page_hosts(page["id"])
        self.assertIn("cdn.example-assets.net", hosts)
        self.assertEqual(hosts[0], "www.example-provider.com")

    def test_the_config_is_found_behind_a_cdn_host(self):
        page = self.page(html=MANY_PROVIDER_URLS_HTML)
        self.domain(domain="example-provider.com", spinner_html=SPINNER_HTML)
        self.assertEqual(
            api._domain_spinner_url_for_page(page["id"]),
            "/api/lpv/domains/example-provider.com/spinner.html",
        )

    def test_the_capture_still_supplies_the_logo(self):
        self.page(html=CDN_FIRST_HTML)
        self.domain(domain="example-provider.com", label="Example")
        api._DOMAIN_LOGO_CACHE.clear()
        self.assertEqual(
            api._domain_logo(lpv_store.get_domain("example-provider.com")),
            "https://www.example-provider.com/touch-180.png",
        )


class PlainVisitCase(_StoreMixin, unittest.TestCase):
    """client.html opened before any page is known.

    This is the "send the visitor the client link and push a page by hand"
    path: there is no workflow and no landing page, so the server used to fall
    back to its own branded loader even though the site had a screen.
    """

    def test_a_plain_visit_shows_the_only_configured_site(self):
        self.domain(domain="example-provider.com", label="Example",
                    spinner_html=SPINNER_HTML)
        body = served("")
        self.assertIn('<div id="lpvBootLoading"', body)
        self.assertIn('data-lpv-url="/api/lpv/domains/example-provider.com/spinner.html"', body)
        self.assertIn("#0b1020", embedded_html(body)[:6000])

    def test_two_sites_do_not_guess(self):
        self.domain(domain="example-provider.com", spinner_html=SPINNER_HTML)
        self.domain(domain="example-bank.net",
                    spinner_html="<html><body id=\"bank\">bank</body></html>")
        body = served("")
        self.assertNotIn('<div id="lpvBootLoading"', body)

    def test_a_switched_off_site_does_not_take_a_plain_visit(self):
        self.domain(domain="example-provider.com", spinner_html=SPINNER_HTML,
                    enabled=False)
        self.assertNotIn('<div id="lpvBootLoading"', served(""))

    def test_a_site_with_only_a_logo_still_gets_a_screen(self):
        self.domain(domain="example-provider.com", label="Example")  # logo only
        # It still gets a generated screen: the mark is the site's own icon.
        html = embedded_html(served(""))[:6000]
        self.assertTrue(html, "a configured site always has something to show")
        self.assertIn("example-provider.com", html)


class SpinnerEndpointCase(_StoreMixin, unittest.TestCase):
    def test_it_serves_the_configured_document(self):
        import asyncio
        self.domain(domain="chase.com", spinner_html=SPINNER_HTML)
        response = asyncio.run(api.lpv_domain_spinner_html("chase.com"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"#a1b2c3", response.body)
        self.assertIn("no-cache", response.headers.get("cache-control", ""))

    def test_it_is_a_404_without_one(self):
        import asyncio
        response = asyncio.run(api.lpv_domain_spinner_html("nothing.example.com"))
        self.assertEqual(response.status_code, 404)

    def test_switched_off_serves_nothing(self):
        import asyncio
        self.domain(domain="chase.com", spinner_html=SPINNER_HTML, enabled=False)
        response = asyncio.run(api.lpv_domain_spinner_html("chase.com"))
        self.assertEqual(response.status_code, 404)

    def test_the_url_shipped_with_a_push(self):
        page = self.page()
        self.domain(domain="example-provider.com", spinner_html=SPINNER_HTML)
        self.assertEqual(
            api._domain_spinner_url_for_page(page["id"]),
            "/api/lpv/domains/example-provider.com/spinner.html",
        )
        self.assertEqual(api._domain_spinner_url_for_page(""), "")


class NoHardcodedLoaderCase(_StoreMixin, unittest.TestCase):
    """A domain that brings a loading screen must never show ours as well."""

    def test_every_success_path_retires_the_builtin_loader(self):
        start = CLIENT.index("_showRedirect(show) {")
        body = CLIENT[start:start + 9000]
        self.assertGreaterEqual(body.count("_retireBootLoader()"), 3,
                                "adopted, cached and freshly-fetched paths all retire it")
        self.assertIn("_retireBootLoader() {", CLIENT)
        self.assertIn("getElementById('loadingScreen')", CLIENT[CLIENT.index("_retireBootLoader() {"):][:400])

    def test_the_runtime_adopts_the_embedded_page_at_start(self):
        start = CLIENT.index("async start() {")
        body = CLIENT[start:start + 3000]
        self.assertIn("lpvBootLoadingUrl()", body)
        self.assertIn("this.redirectUrl = embedded;", body)
        self.assertIn("this._redirectLoaded = embedded;", body)
        self.assertLess(body.index("this.redirectUrl = embedded;"), body.index("lpvHoldLoadingScreen()"),
                        "the loading page is known before the loader is asked for")


class OneSpinnerCase(unittest.TestCase):
    """The document already carries the page: a workflow start must adopt it."""

    def test_the_client_adopts_the_embedded_copy(self):
        self.assertIn("function lpvAdoptBootLoading(url)", CLIENT)
        self.assertIn("function lpvBootLoadingOnScreen()", CLIENT)

    def test_show_redirect_checks_before_loading_again(self):
        start = CLIENT.index("_showRedirect(show) {")
        body = CLIENT[start:start + 9000]
        adopt = body.index("lpvAdoptBootLoading(url)")
        fetch = body.index("fetchServerDocument(")
        self.assertLess(adopt, fetch,
                        "the embedded copy is checked before anything is fetched")
        self.assertIn("this._redirectLoaded = url;", body[:adopt + 800])

    def test_a_push_can_bring_its_sites_spinner(self):
        self.assertIn("async showPage(pageId, pageUrl, pageName, guard, actions, spinnerUrl)", CLIENT)
        self.assertIn("message.spinner_url", CLIENT)
        self.assertIn("if (want && want !== this.redirectUrl)", CLIENT)

    def test_the_server_sends_it_with_every_push(self):
        api_source = (ROOT / "api.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(api_source.count('"spinner_url"'), 4,
                                "one per push path (workflow step, direct push, chat)")
        self.assertIn('"default_page_spinner_url"', api_source,
                      "and one for the LPV landing page")

    def test_the_domain_logo_replaces_the_workflow_branding(self):
        api_source = (ROOT / "api.py").read_text(encoding="utf-8")
        self.assertIn('brand_logo = ((domain_cfg or {}).get("logo_url") or "").strip()', api_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
