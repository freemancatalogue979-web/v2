#!/usr/bin/env python3
"""The operator's custom spinner, embedded in the page they hand out.

A workflow can carry its own loading page ("custom spinner"): HTML uploaded in
the workflow editor, or an archived page. client.html used to fetch it only
AFTER the WebSocket boot, so a visitor opening the link saw the built-in loader
first (black, plain ring) and the custom spinner a round trip later.

The server now embeds that page in the served document, in an iframe with a
`srcdoc` attribute: the browser paints it while parsing, so the spinner is on
screen the moment the page opens.

Pinned here:

  * a visit with a custom loading page gets the markup, the stylesheet and the
    document flag, in the right places (markup is the first thing in <body>)
  * a visit without one is untouched (no markup, no stylesheet, no flag)
  * both ways of setting it work: uploaded HTML and an archived page
  * LPV-only mode embeds the landing page the operator chose, and nothing when
    the landing page is the built-in `_default.html` sentinel
  * the content is escaped, so quotes / <script> / </iframe> in somebody's
    spinner cannot break out of the attribute
  * changing the loading page rebuilds the copy instead of serving the old one
  * the page is sanitized like every other stored page

Needs fastapi; skips on a bare checkout.

    python3 tests/test_boot_loading_embed.py
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
    from starlette.requests import Request  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - bare checkout
    print("boot loading embed: skipped (%s not installed)" % exc.name)
    raise SystemExit(0)

MARK = '<div id="lpvBootLoading"'
STYLE = "lpv-boot-loading-style"
FLAG = 'data-lpv-boot-loading="1"'

SPINNER = (
    "<!doctype html><html><head><style>"
    "body{margin:0;background:#101820;color:#fff}"
    ".ring{width:54px;height:54px;border:4px solid rgba(255,255,255,.2);"
    "border-top-color:#a1b2c3;border-radius:50%}"
    "</style></head><body><div class=\"ring\"></div></body></html>"
)


def request_for(query: str = "") -> Request:
    scope = {
        "type": "http", "method": "GET", "path": "/client.html",
        "headers": [], "query_string": query.encode(),
        "scheme": "http", "server": ("testserver", 80),
    }
    return Request(scope)


def served(query: str = "") -> str:
    return bytes(api._render_client_html(request_for(query)).body).decode("utf-8")


def embedded_html(body: str) -> str:
    """The spinner markup as the browser would hand it to the frame."""
    if 'srcdoc="' not in body:
        return ""
    start = body.index('srcdoc="') + len('srcdoc="')
    end = body.index('"', start)
    return (body[start:end]
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&amp;", "&"))


class _WorkflowCase(unittest.TestCase):
    """Creates real workflows/pages, and removes them again."""

    def setUp(self):
        api._CLIENT_HTML_CACHE.pop("page", None)
        self._wf_ids = []
        self._page_ids = []
        self._mode = api.srv_settings.get_settings().lpv_only_mode
        self._default = api.srv_settings.get_settings().lpv_default_page

    def tearDown(self):
        for wf_id in self._wf_ids:
            try:
                lpv_store.delete_workflow(wf_id)
            except Exception:
                pass
        for page_id in self._page_ids:
            try:
                lpv_store.delete_page(page_id)
            except Exception:
                pass
        api.srv_settings._settings.lpv_only_mode = self._mode
        api.srv_settings._settings.lpv_default_page = self._default
        api._CLIENT_HTML_CACHE.pop("page", None)

    def workflow(self, **kw):
        wf = lpv_store.save_workflow(None, kw.pop("name", "Flow"), "", [], **kw)
        self._wf_ids.append(wf["id"])
        return wf

    def page(self, html=SPINNER, name="Spinner Page"):
        page = lpv_store.save_page(name=name, html=html)
        self._page_ids.append(page["id"])
        return page


class UploadedHtmlCase(_WorkflowCase):
    def test_the_spinner_is_in_the_document_before_the_first_paint(self):
        wf = self.workflow(redirect_html=SPINNER)
        body = served("workflow=" + wf["id"])
        self.assertIn(MARK, body, "the custom loading page is embedded")
        self.assertIn(STYLE, body, "with the stylesheet that retires the black loader")
        self.assertIn(FLAG, body, "and the document flag")
        self.assertIn('srcdoc="', body)

        # It must be the FIRST thing in <body>: anything before it would paint
        # first, which is the whole bug.
        body_open = body.index("<body")
        body_end = body.index(">", body_open) + 1
        self.assertTrue(body[body_end:body_end + len(MARK)] == MARK,
                        "the embedded loader is the first element in <body>")
        self.assertLess(body.index(STYLE), body_open,
                        "the stylesheet is in the head, so it applies to the first paint")

    def test_the_embedded_page_is_the_operators_html(self):
        wf = self.workflow(redirect_html=SPINNER)
        embedded = embedded_html(served("workflow=" + wf["id"]))
        self.assertIn('class="ring"', embedded[:4000])
        self.assertIn("#a1b2c3", embedded[:4000])

    def test_it_points_at_the_url_the_runtime_will_use(self):
        wf = self.workflow(redirect_html=SPINNER)
        body = served("workflow=" + wf["id"])
        self.assertIn(f'data-lpv-url="/api/lpv/workflows/{wf["id"]}/redirect.html"', body)

    def test_quotes_and_tags_cannot_break_out(self):
        nasty = ('<html><body><p title="a &quot;b&quot;">'
                 'x</p><script>window.__nasty = 1;</script></body></html>')
        wf = self.workflow(redirect_html=nasty)
        body = served("workflow=" + wf["id"])
        start = body.index(MARK)
        element = body[start:body.index("</div>", start)]
        self.assertEqual(element.count("srcdoc="), 1, "exactly one srcdoc attribute")
        self.assertNotIn('</script>', element)
        # The only closing tag in there is the frame's own: nothing in the
        # spinner can end the element early and escape into the page.
        self.assertEqual(element.count("</iframe>"), 1)

    def test_a_broken_loading_page_does_not_break_the_page(self):
        wf = self.workflow(redirect_html="<html><body><div>unclosed")
        body = served("workflow=" + wf["id"])
        self.assertTrue("<title" in body)
        self.assertIn(MARK, body)


class ArchivedPageCase(_WorkflowCase):
    def test_an_archived_page_works_too(self):
        page = self.page()
        wf = self.workflow(redirect_page_id=page["id"])
        body = served("workflow=" + wf["id"])
        self.assertIn(MARK, body)
        self.assertIn(f'data-lpv-url="/api/lpv/archive/{page["id"]}/html"', body)
        self.assertIn('class="ring"', embedded_html(body)[:4000])
        # Sanitised on the way in, like every other stored page.
        self.assertNotIn("<base", embedded_html(body).lower())

    def test_a_deleted_loading_page_is_not_embedded(self):
        page = self.page()
        wf = self.workflow(redirect_page_id=page["id"])
        lpv_store.delete_page(page["id"])
        self._page_ids.remove(page["id"])
        api._CLIENT_HTML_CACHE.pop("page", None)
        body = served("workflow=" + wf["id"])
        self.assertNotIn(MARK, body, "a missing loading page falls back to the built-in ring")


class LpvOnlyModeCase(_WorkflowCase):
    def test_the_landing_page_is_embedded_in_lpv_mode(self):
        page = self.page()
        api.srv_settings._settings.lpv_only_mode = True
        api.srv_settings._settings.lpv_default_page = page["id"]
        body = served("")
        self.assertIn(MARK, body)
        self.assertIn(f'/api/lpv/archive/{page["id"]}/html', body)

    def test_the_built_in_default_is_not_embedded(self):
        api.srv_settings._settings.lpv_only_mode = True
        api.srv_settings._settings.lpv_default_page = "_default.html"
        body = served("")
        self.assertNotIn(MARK, body, "the built-in landing page has no HTML to embed")

    def test_a_workflow_loading_page_wins_in_lpv_mode(self):
        page = self.page()
        wf = self.workflow(redirect_html=SPINNER)
        api.srv_settings._settings.lpv_only_mode = True
        api.srv_settings._settings.lpv_default_page = page["id"]
        body = served("workflow=" + wf["id"])
        self.assertIn(f'/api/lpv/workflows/{wf["id"]}/redirect.html', body)


class UntouchedVisitsCase(_WorkflowCase):
    def test_a_plain_visit_is_not_changed(self):
        body = served("")
        self.assertNotIn(MARK, body)
        self.assertNotIn(STYLE, body)
        self.assertNotIn(FLAG, body)

    def test_a_browser_link_is_not_changed(self):
        body = served("auth=abc123&url=login.yahoo.com")
        self.assertNotIn(MARK, body, "a remote-browser session keeps the black loader")

    def test_a_workflow_without_a_custom_page_is_not_changed(self):
        wf = self.workflow()
        body = served("workflow=" + wf["id"])
        self.assertNotIn(MARK, body)


class HandoverCase(unittest.TestCase):
    """The embedded copy must get out of the way once the real page is up."""

    @classmethod
    def setUpClass(cls):
        cls.client = (ROOT / "client.html").read_text(encoding="utf-8", errors="replace")

    def test_it_fades_itself_out_and_is_removed(self):
        self.assertIn("function lpvDropBootLoading()", self.client)
        self.assertIn("lpv-boot-loading-out", self.client)
        self.assertIn("removeChild(el)", self.client)
        self.assertIn("window.__LPV_DROP_BOOT_LOADING__ = lpvDropBootLoading", self.client)

    def test_the_first_pushed_page_hands_over(self):
        fn = self.client.index("async showPage(")
        body = self.client[fn:fn + 60000]
        self.assertIn("lpvDropBootLoading()", body)

    def test_the_runtimes_own_loading_page_hands_over(self):
        fn = self.client.index("_showRedirect(show) {")
        body = self.client[fn:fn + 9000]
        self.assertGreaterEqual(body.count("lpvDropBootLoading()"), 2)

    def test_clicks_in_the_embedded_page_still_advance_a_redirect_step(self):
        self.assertIn("function lpvBindBootLoadingCapture()", self.client)
        self.assertIn("'lpv_spinner'", self.client)
        self.assertIn("lpvBindBootLoadingCapture()", self.client[self.client.index("async start() {"):][:2000])

    def test_the_loader_is_only_hidden_while_the_embedded_copy_is_up(self):
        # Otherwise a failed hand-over would leave a blank white screen.
        embedded = api._embed_loading_page(
            "<html><head></head><body>hi</body></html>", "<html>x</html>", "/x")
        self.assertIn(":has(#lpvBootLoading) .loading-screen", embedded)
        self.assertNotIn("] .loading-screen{display:none", embedded)


class CacheCase(_WorkflowCase):
    def test_editing_the_spinner_rebuilds_the_copy(self):
        wf = self.workflow(redirect_html=SPINNER)
        first = served("workflow=" + wf["id"])
        self.assertIn("#a1b2c3", first)

        changed = SPINNER.replace("#a1b2c3", "#ff0055")
        lpv_store.save_workflow(wf["id"], "Flow", "", [], redirect_html=changed)
        second = served("workflow=" + wf["id"])
        self.assertIn("#ff0055", second, "the new spinner is served")
        self.assertNotIn("#a1b2c3", second)

    def test_the_variants_do_not_share_a_cache_entry(self):
        wf = self.workflow(redirect_html=SPINNER)
        with_loader = served("workflow=" + wf["id"])
        without = served("")
        self.assertIn(MARK, with_loader)
        self.assertNotIn(MARK, without)


if __name__ == "__main__":
    unittest.main(verbosity=2)
