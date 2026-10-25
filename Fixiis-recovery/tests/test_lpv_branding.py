#!/usr/bin/env python3
"""The LPV tab name and favicon, on the page that the visitor opens.

Two things were wrong, and both read as "it shows only sometimes":

  * the head's branding block ran BEFORE the <link rel=icon> and the <title>
    existed, so an identity that came from the link itself (or from the boot
    data) was silently dropped — `querySelector('link[rel*=icon]')` answered
    null;
  * the operator's own tab name/icon (Settings -> Live pages) only ever
    arrived over the socket, so the tab said the site's name first and then
    changed its mind a second later — or never, if the boot message was missed.

An LPV visit is now branded in the SERVED DOCUMENT: name and icon are in the
bytes the browser receives, so the first painted frame already carries them.
A copy hosted away from the backend (static host, CDN, file://) gets the same
identity from the link's query string instantly, and `/api/public/boot`
answers the rest — including whether visitor pages are open at all (the 404)
and which custom loading page belongs to the visit.

    python3 tests/test_lpv_branding.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server_settings  # noqa: E402
import lpv_store  # noqa: E402

try:
    import api  # noqa: E402
    from starlette.requests import Request  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - bare checkout
    print("lpv branding: skipped (%s not installed)" % exc.name)
    raise SystemExit(0)


def request_for(query: str = "", path: str = "/client.html") -> Request:
    scope = {
        "type": "http", "method": "GET", "path": path,
        "headers": [], "query_string": query.encode(),
        "scheme": "http", "server": ("testserver", 80),
    }
    return Request(scope)


def has(body: str, needle: str, message: str = "") -> bool:
    """assertIn with a SHORT failure message (the page is ~600 KB)."""
    if needle in body:
        return True
    raise AssertionError(message or ("missing from the served page: %r" % (needle,)))


def served(query: str = "") -> str:
    api._CLIENT_HTML_CACHE.pop("page", None)
    return bytes(api._render_client_html(request_for(query)).body).decode("utf-8")


def boot(query: str = ""):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(
        api.public_boot(request_for(query, "/api/public/boot")))


def boot_json(query: str = ""):
    response = boot(query)
    return response.status_code, json.loads(bytes(response.body).decode("utf-8"))


class _Base(unittest.TestCase):
    def setUp(self):
        api._CLIENT_HTML_CACHE.pop("page", None)
        self._wf_ids = []
        self._saved = server_settings._settings
        server_settings._settings = type(self._saved)(**{
            **self._saved.__dict__,
            "lpv_tab_title": "", "lpv_favicon_url": "",
        })

    def tearDown(self):
        for wf_id in self._wf_ids:
            try:
                lpv_store.delete_workflow(wf_id)
            except Exception:
                pass
        server_settings._settings = self._saved
        api._CLIENT_HTML_CACHE.pop("page", None)

    def workflow(self, **kw):
        wf = lpv_store.save_workflow(None, kw.pop("name", "Flow"), "", [], **kw)
        self._wf_ids.append(wf["id"])
        return wf

    def tab_branding(self, title="", icon=""):
        server_settings._settings = type(self._saved)(**{
            **self._saved.__dict__,
            "lpv_tab_title": title, "lpv_favicon_url": icon,
        })
        api._CLIENT_HTML_CACHE.pop("page", None)


# ---------------------------------------------------------------------------
# 1. The served document already carries the identity
# ---------------------------------------------------------------------------
class ServedDocumentCase(_Base):
    def test_the_workflow_name_is_the_title_in_the_first_byte(self):
        wf = self.workflow(name="Moonlight Trust")
        body = served("workflow=" + wf["id"])
        self.assertIn('<title id="pageTitle">Moonlight Trust</title>', body,
                      "the tab is named before any script runs")
        self.assertNotIn('<title id="pageTitle">Loading...</title>', body)

    def test_the_workflow_logo_replaces_the_icon_link(self):
        wf = self.workflow(name="Flow", brand_logo_url="https://cdn.example/logo.png")
        body = served("workflow=" + wf["id"])
        self.assertIn('href="https://cdn.example/logo.png"', body)
        self.assertIn('id="shf-lpv-brand-icon"', body,
                      "with the marker the mirror's icon sync stands down for")
        self.assertNotIn("cdn.brandfetch.io/idaZJ2tIZc", body.split("<title")[0],
                         "the brand mark is replaced, not merely joined")

    def test_the_boot_payload_names_every_field_the_head_reads(self):
        wf = self.workflow(name="Flow", brand_color="#123456",
                           brand_logo_url="https://cdn.example/l.png")
        body = served("workflow=" + wf["id"])
        self.assertIn('id="lpv-preboot-data"', body)
        start = body.index('id="lpv-preboot-data"')
        script = body[start:body.index("</script>", start)]
        for field in ('"workflowName"', '"tabTitle"', '"color"', '"logoUrl"',
                      '"tabIcon"', '"title"', '"icon"', '"visit"'):
            self.assertIn(field, script, field)

    def test_the_operators_tab_settings_win(self):
        wf = self.workflow(name="Workflow Name", brand_logo_url="https://cdn.example/wf.png")
        self.tab_branding("Operator Tab", "https://cdn.example/tab.png")
        body = served("workflow=" + wf["id"])
        self.assertIn("Operator Tab", body)
        self.assertIn("https://cdn.example/tab.png", body)
        self.assertNotIn('<title id="pageTitle">Workflow Name</title>', body)

    def test_a_tab_setting_alone_brands_an_lpv_visit(self):
        # No workflow at all: the operator's Settings still describe the tab.
        self.tab_branding("Just The Tab", "https://cdn.example/t.png")
        live = server_settings._settings
        try:
            server_settings._settings = type(live)(**{
                **live.__dict__, "lpv_only_mode": True})
            body = served("")
            has(body, "Just The Tab", "the tab name is in the served document")
            has(body, "https://cdn.example/t.png", "and so is its icon")
        finally:
            server_settings._settings = live

    def test_a_changed_tab_setting_rebuilds_the_copy(self):
        wf = self.workflow(name="Flow")
        self.assertNotIn("Before Change", served("workflow=" + wf["id"]))
        self.tab_branding("Before Change", "")
        self.assertIn("Before Change", served("workflow=" + wf["id"]))
        self.tab_branding("After Change", "")
        body = served("workflow=" + wf["id"])
        self.assertIn("After Change", body)
        self.assertNotIn("Before Change", body,
                         "the cached copy is keyed on the tab branding too")

    def test_a_streamed_visit_is_still_not_branded(self):
        self.workflow(name="LPV ONLY NAME")
        try:
            server_settings._settings = type(self._saved)(**{
                **self._saved.__dict__,
                "auto_workflow_id": self._wf_ids[0],
                "lpv_tab_title": "TAB NAME", "lpv_favicon_url": "https://cdn.example/x.png",
            })
            body = served("auth=abc123&url=login.yahoo.com")
            self.assertNotIn('id="lpv-preboot-data"', body)
            self.assertNotIn("LPV ONLY NAME", body)
            self.assertNotIn("TAB NAME", body)
            self.assertIn('<title id="pageTitle">Loading...</title>', body)
        finally:
            server_settings._settings = type(self._saved)(**{
                **self._saved.__dict__, "auto_workflow_id": ""})


# ---------------------------------------------------------------------------
# 2. The link carries the identity, for copies the backend never renders
# ---------------------------------------------------------------------------
class LinkQueryCase(_Base):
    def test_a_workflow_link_carries_its_branding(self):
        wf = self.workflow(name="Shared Flow", brand_color="#00a1b2",
                           brand_logo_url="https://cdn.example/s.png")
        q = api._lpv_brand_query(wf)
        self.assertIn("workflow_name=Shared+Flow", q)
        self.assertIn("brand_color=%2300a1b2", q)
        self.assertIn("brand_logo=https%3A%2F%2Fcdn.example%2Fs.png", q)

    def test_the_operator_tab_settings_are_carried_and_come_first(self):
        wf = self.workflow(name="Shared Flow", brand_logo_url="https://cdn.example/s.png")
        self.tab_branding("Op Tab", "https://cdn.example/op.png")
        q = api._lpv_brand_query(wf)
        self.assertLess(q.index("tab_name=Op+Tab"), q.index("workflow_name="),
                        "the head reads the first value it finds")
        self.assertLess(q.index("tab_icon="), q.index("brand_logo="))

    def test_a_plain_workflow_adds_nothing(self):
        self.tab_branding("", "")
        self.assertEqual(api._lpv_brand_query({}), "",
                         "no branding set: the link stays a plain link")


# ---------------------------------------------------------------------------
# 3. What a copy hosted away from the backend asks for
# ---------------------------------------------------------------------------
class PublicBootCase(_Base):
    def test_it_answers_while_pages_are_open(self):
        status, data = boot_json("")
        self.assertEqual(status, 200)
        self.assertTrue(data["open"])
        self.assertIn("branding", data)
        self.assertIn("loading", data)

    def test_it_carries_the_branding_a_static_copy_needs(self):
        wf = self.workflow(name="Boot Flow", brand_color="#abcdef")
        self.tab_branding("Boot Tab", "https://cdn.example/b.png")
        status, data = boot_json("workflow=" + wf["id"])
        self.assertEqual(status, 200)
        self.assertEqual(data["branding"]["title"], "Boot Tab",
                         "the operator's setting wins, exactly as on the page")
        self.assertEqual(data["branding"]["icon"], "https://cdn.example/b.png")
        self.assertEqual(data["branding"]["color"], "#abcdef")
        self.assertTrue(data["lpv_visit"])

    def test_it_carries_the_custom_loading_page(self):
        wf = self.workflow(name="Flow",
                           redirect_html="<!doctype html><html><body>WAITING</body></html>")
        status, data = boot_json("workflow=" + wf["id"])
        self.assertEqual(status, 200)
        self.assertIn("WAITING", data["loading"]["html"])
        self.assertTrue(data["loading"]["url"])

    def test_it_404s_when_the_operator_closed_visitor_pages(self):
        server_settings._settings = type(self._saved)(**{
            **self._saved.__dict__, "public_pages_enabled": False})
        response = boot("")
        self.assertEqual(response.status_code, 404)
        body = bytes(response.body).decode("utf-8")
        self.assertEqual(body, api._NOT_FOUND_HTML,
                         "the same bytes a closed /client.html answers with")
        self.assertIn("no-store", response.headers.get("cache-control", ""))

    def test_it_is_never_cached(self):
        response = boot("")
        self.assertIn("no-store", response.headers.get("cache-control", ""))
        self.assertEqual(response.headers.get("x-robots-tag"), "noindex")

    def test_it_never_exposes_anything_but_the_visit(self):
        wf = self.workflow(name="Boot Flow")
        _, data = boot_json("workflow=" + wf["id"])
        self.assertEqual(set(data.keys()),
                         {"open", "lpv_only", "lpv_visit", "workflow_id",
                          "branding", "loading"})
        self.assertEqual(set(data["branding"].keys()), {"title", "icon", "color", "logo"})


# ---------------------------------------------------------------------------
# 4. The document itself: the block runs where the elements exist
# ---------------------------------------------------------------------------
class HeadOrderCase(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "client.html").read_text(encoding="utf-8", errors="replace")

    def test_the_branding_block_comes_after_the_title_and_the_icon(self):
        icon = self.src.index('<link rel="icon" id="shf-brand-favicon"')
        title = self.src.index('<title id="pageTitle">')
        block = self.src.index('<script id="lpv-preboot-branding">')
        self.assertLess(icon, title)
        self.assertLess(title, block,
                        "the old copy ran before both existed and found nothing")

    def test_it_reads_the_boot_data_and_the_link(self):
        block = self.src[self.src.index('<script id="lpv-preboot-branding">'):
                         self.src.index("</script>", self.src.index('<script id="lpv-preboot-branding">'))]
        self.assertIn("__LPV_BOOT_BRANDING__", block)
        for key in ("tab_name", "workflow_name", "brand_logo", "brand_color", "tab_icon"):
            self.assertIn(key, block, key)

    def test_it_replaces_the_icon_element_instead_of_editing_it(self):
        block = self.src[self.src.index('<script id="lpv-preboot-branding">'):
                         self.src.index("</script>", self.src.index('<script id="lpv-preboot-branding">'))]
        self.assertIn("createElement('link')", block,
                      "Chrome caches a favicon per <link>: a new element always re-reads")
        self.assertIn("removeChild", block)

    def test_the_runtime_keeps_what_the_head_applied(self):
        self.assertIn("window.__LPV_NAME_PREBOOT__", self.src)
        self.assertIn("if (window.__LPV_NAME_PREBOOT__) {", self.src,
                      "init() must not rename the tab back to the site name")


# ---------------------------------------------------------------------------
# 5. The standalone boot (gate, branding, loading page) is really wired
# ---------------------------------------------------------------------------
class StandaloneBootCase(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "client.html").read_text(encoding="utf-8", errors="replace")

    def test_it_asks_once_and_only_from_a_copy_the_backend_never_rendered(self):
        self.assertIn("async function lpvStandaloneBoot()", self.src)
        self.assertIn("function lpvBootIsServerServed()", self.src)
        self.assertIn("if (lpvBootIsServerServed()) return;", self.src)
        self.assertIn("resolveServerUrl('/api/public/boot') + qs", self.src)
        self.assertIn("try { lpvStandaloneBoot(); } catch (e) {}", self.src,
                      "it is fired from init(), on the boot path")

    def test_a_closed_site_paints_the_404_and_stops(self):
        self.assertIn("if (res.status === 404) {", self.src)
        self.assertIn("lpvPaint404(await res.text()", self.src)
        self.assertIn("state.reconnectDisabled = true;", self.src,
                      "no reconnect loop behind a 404")
        self.assertIn("document.write(body)", self.src,
                      "the browser is left showing the server's own 404 bytes")

    def test_it_fails_open(self):
        at = self.src.index("async function lpvStandaloneBoot()")
        body = self.src[at:at + 4200]
        self.assertIn("catch (e) {", body)
        self.assertIn("Unreachable backend: fail open", body)

    def test_it_mounts_a_custom_loading_page_and_lets_the_hand_over_drop_it(self):
        self.assertIn("function lpvMountBootLoading(html, url)", self.src)
        self.assertIn("frame.srcdoc = String(html);", self.src)
        self.assertIn("wrap.id = 'lpvBootLoading';", self.src)
        at = self.src.index("function lpvDropBootLoading()")
        self.assertIn("getElementById('lpvBootLoading')", self.src[at:at + 260])

    def test_bring_your_own_loading_page(self):
        self.assertIn("new URLSearchParams(qs).get('loading')", self.src)
        self.assertIn("lpvMountBootLoading(await r.text(), own)", self.src)

    def test_the_gate_wait_is_bounded(self):
        self.assertIn("const LPV_BOOT_FETCH_MS = 2500;", self.src)
        self.assertIn("ctrl.abort()", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
