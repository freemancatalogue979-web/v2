#!/usr/bin/env python3
"""Who boots straight onto the custom spinner?

`client.html` used to paint the black remote-browser loading screen first and
only then become the branded spinner. The page can only start on the custom
one if the *document* is marked before the first paint, so the server marks it
when it already knows the visit is LPV.

These tests pin that decision — deliberately narrow, because a wrong "yes"
gives a remote-browser visitor a loading screen that does not match what they
are waiting for:

    ?workflow= / ?workflow_id=      a workflow link            -> LPV
    ?page= / ?lpv=1                 explicit                   -> LPV
    lpv_only_mode is on             every visitor is LPV       -> LPV
    ?auth= alone                    may be a browser session   -> NOT LPV
    ?lpv=0                          explicit no                -> NOT LPV
    plain /                         not LPV (unless the switch) -> NOT LPV

It also checks what the marked copy carries: the attribute on <html>, the
preboot stylesheet with the brand colour, and a workflow logo that survives the
"hide the empty logo box" rule.

Needs fastapi; skips on a bare checkout.

    python3 tests/test_lpv_first_paint.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import api  # noqa: E402
    from starlette.requests import Request  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - bare checkout
    print("LPV first paint: skipped (%s not installed)" % exc.name)
    raise SystemExit(0)


def request_for(query: str = "") -> Request:
    scope = {
        "type": "http", "method": "GET", "path": "/client.html",
        "headers": [], "query_string": query.encode(),
        "scheme": "http", "server": ("testserver", 80),
    }
    return Request(scope)


def is_marked(body: str) -> bool:
    return 'data-lpv-boot="1"' in body[body.index("<html"):body.index("<html") + 120]


class BootDecisionCase(unittest.TestCase):
    def setUp(self):
        self._mode = api.srv_settings.get_settings().lpv_only_mode
        api.srv_settings._settings.lpv_only_mode = False

    def tearDown(self):
        api.srv_settings._settings.lpv_only_mode = self._mode

    def lpv(self, query: str) -> bool:
        return api._lpv_boot_for(request_for(query), "")

    def test_workflow_links_are_lpv(self):
        # The caller resolves the workflow id (from ?workflow=, ?auth= or the
        # auto-workflow setting) and asks with it. A link that names a
        # workflow is an LPV visit even if that workflow has since been
        # deleted: the visitor is still going to a pushed page.
        for query in ("workflow=w1", "workflow_id=w1"):
            self.assertTrue(api._lpv_boot_for(request_for(query), "w1"), query)

    def test_a_workflow_link_is_marked_end_to_end(self):
        body = bytes(api._render_client_html(request_for("workflow=deleted-workflow")).body).decode()
        self.assertTrue(is_marked(body),
                        "a workflow link boots on the custom spinner even when "
                        "the workflow itself is gone")

    def test_explicit_markers(self):
        self.assertTrue(self.lpv("lpv=1"))
        self.assertTrue(self.lpv("page=abc123"))
        self.assertTrue(self.lpv("page=abc123&lpv=true"))

    def test_a_bare_auth_link_is_not(self):
        # ?auth= links can open a real browser session; branding that one as
        # LPV would swap a streaming loader for a page spinner.
        self.assertFalse(self.lpv("auth=abc"))
        self.assertFalse(self.lpv("auth=abc&url=login.yahoo.com"))

    def test_explicit_no(self):
        self.assertFalse(self.lpv("lpv=0"))
        self.assertFalse(self.lpv("lpv=false"))

    def test_plain_visit_follows_the_switch(self):
        self.assertFalse(self.lpv(""))
        api.srv_settings._settings.lpv_only_mode = True
        self.assertTrue(self.lpv(""), "with LPV-only on, every visitor boots on the custom spinner")

    def test_a_broken_settings_read_does_not_mark(self):
        real = api.srv_settings.get_settings

        def boom():
            raise RuntimeError("settings unavailable")

        api.srv_settings.get_settings = boom
        try:
            self.assertFalse(self.lpv(""))
        finally:
            api.srv_settings.get_settings = real


class MarkedCopyCase(unittest.TestCase):
    def setUp(self):
        self._mode = api.srv_settings.get_settings().lpv_only_mode
        api.srv_settings._settings.lpv_only_mode = False
        api._CLIENT_HTML_CACHE.pop("page", None)

    def tearDown(self):
        api.srv_settings._settings.lpv_only_mode = self._mode
        api._CLIENT_HTML_CACHE.pop("page", None)

    def body(self, query: str) -> str:
        return bytes(api._render_client_html(request_for(query)).body).decode("utf-8")

    def test_an_lpv_copy_carries_the_mark_and_the_style(self):
        body = self.body("page=abc123")
        self.assertTrue(is_marked(body), "the <html> tag is marked before the body paints")
        self.assertIn("lpv-boot-style", body)
        self.assertIn("--lpv-brand: #4285F4", body)
        self.assertIn('html[data-lpv-boot="1"] .loading-screen', body)

    def test_a_browser_copy_is_untouched(self):
        body = self.body("auth=abc&url=login.yahoo.com")
        self.assertFalse(is_marked(body))
        self.assertNotIn("lpv-boot-style", body)

    def test_a_plain_copy_is_untouched(self):
        body = self.body("")
        self.assertFalse(is_marked(body))
        self.assertNotIn("lpv-boot-style", body)

    def test_lpv_only_mode_marks_every_visitor(self):
        api.srv_settings._settings.lpv_only_mode = True
        self.assertTrue(is_marked(self.body("")))

    def test_the_two_variants_do_not_share_a_cache_entry(self):
        # The cache keeps one branded copy; if the LPV flag were not part of
        # the key, a browser visitor could be served the LPV loader or the
        # other way round.
        plain = self.body("")
        marked = self.body("page=abc123")
        self.assertFalse(is_marked(plain))
        self.assertTrue(is_marked(marked))
        self.assertFalse(is_marked(self.body("")), "and it flips back correctly")

    def test_a_workflow_logo_survives_the_hidden_logo_rule(self):
        # The stylesheet hides an empty logo box on an LPV boot; an operator
        # logo must therefore carry has-logo or it would vanish.
        source = (ROOT / "client.html").read_text(encoding="utf-8")
        self.assertIn('.loading-logo.has-logo', source)
        html = (ROOT / "api.py").read_text(encoding="utf-8")
        self.assertIn('class="loading-logo has-logo"', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
