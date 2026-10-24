#!/usr/bin/env python3
"""The visitor-pages switch, server side.

One setting decides whether the pages a visitor opens exist: ``/``,
``/client.html`` and the pushed-page HTML all answer 404 while it is off, new
``/ws`` connections are told to stop, and the operator surfaces (``/admin``,
``/mini``, the API) keep working so the switch can be turned back on from a
phone.

What is pinned here:

  * OFF means 404 for visitors — the same answer a page that never existed
    gives, with no hint that somebody is running the site
  * the pushed-page route stops BEFORE it touches the archive (a closed site
    does not even look up the page)
  * a visitor socket is told ``site_disabled`` instead of silently hanging
  * the panels and the settings API are never blocked
  * an unreadable setting fails open: a broken JSON file must not take the
    site down
  * "false" / "0" / "off" typed into the settings file mean off, not truthy

Needs the app's dependencies (fastapi); skips on a bare checkout, like the
other api-level suites.

    python3 tests/test_public_pages.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server_settings  # noqa: E402

try:
    import api  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - bare checkout
    print("public pages: skipped (%s not installed)" % exc.name)
    raise SystemExit(0)


class _Switched(unittest.TestCase):
    """Flip the setting for one test, then put the real one back."""

    def setUp(self):
        self._saved = server_settings._settings

    def tearDown(self):
        server_settings._settings = self._saved

    def switch(self, value):
        server_settings._settings = dataclasses.replace(
            server_settings.get_settings(), public_pages_enabled=value
        )


class NormaliseCase(unittest.TestCase):
    def test_typed_values(self):
        for raw in ("false", "FALSE", " 0 ", "off", "no", 0, False):
            self.assertIs(server_settings.normalise_setting("public_pages_enabled", raw), False,
                          repr(raw))
        for raw in ("true", "1", "on", 1, True):
            self.assertIs(server_settings.normalise_setting("public_pages_enabled", raw), True,
                          repr(raw))

    def test_default_is_on(self):
        # A rejected sign-in on a fresh install must not 404 the whole site.
        self.assertIs(server_settings.ServerSettings().public_pages_enabled, True)

    def test_round_trips_through_the_settings_file(self):
        restored = server_settings.ServerSettings.from_dict(
            {"public_pages_enabled": False, "lpv_only_mode": True}
        )
        self.assertIs(restored.public_pages_enabled, False)
        self.assertIs(restored.lpv_only_mode, True)


class VisitorRoutesCase(_Switched):
    def test_off_answers_404_on_both_visitor_routes(self):
        self.switch(False)
        for name, handler in (("/", api.home), ("/client.html", api.client_page)):
            response = asyncio.run(handler(None))
            self.assertEqual(response.status_code, 404, name)

    def test_the_404_is_an_ordinary_404(self):
        self.switch(False)
        response = asyncio.run(api.client_page(None))
        body = bytes(response.body).decode("utf-8", "replace")
        self.assertIn("404", body)
        # A closed site must not advertise itself as somebody's panel.
        for word in ("mini admin", "switch", "settings", "neo browser", "admin"):
            self.assertNotIn(word, body.lower(), word)
        # ...and must not be cached, or turning it back on would not be seen.
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_on_serves_the_client_page(self):
        self.switch(True)
        calls = []
        real = api._render_client_html
        api._render_client_html = lambda request: calls.append(1) or api.HTMLResponse("client")
        try:
            self.assertEqual(asyncio.run(api.home(None)).status_code, 200)
            self.assertEqual(asyncio.run(api.client_page(None)).status_code, 200)
        finally:
            api._render_client_html = real
        self.assertEqual(len(calls), 2, "the page was rendered when it should be")

    def test_off_never_reads_the_client_page(self):
        self.switch(False)
        real = api._render_client_html

        def boom(request):
            raise AssertionError("client.html was rendered while the switch was off")

        api._render_client_html = boom
        try:
            self.assertEqual(asyncio.run(api.client_page(None)).status_code, 404)
        finally:
            api._render_client_html = real


class PushedPagesCase(_Switched):
    def test_off_stops_before_the_archive_lookup(self):
        # A closed site should not even reveal whether the page exists.
        self.switch(False)
        real = api.lpv_store.get_page

        def boom(page_id):
            raise AssertionError("the archive was read while the switch was off")

        api.lpv_store.get_page = boom
        try:
            response = asyncio.run(api.lpv_archive_html("deadbeef"))
        finally:
            api.lpv_store.get_page = real
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"not found", response.body)

    def test_off_and_missing_look_the_same(self):
        self.switch(False)
        closed = asyncio.run(api.lpv_archive_html("deadbeef"))
        self.switch(True)
        missing = asyncio.run(api.lpv_archive_html("deadbeef"))
        self.assertEqual(closed.status_code, missing.status_code)
        self.assertEqual(bytes(closed.body), bytes(missing.body))


class VisitorSocketCase(_Switched):
    class Socket:
        def __init__(self):
            self.accepted = False
            self.sent = []
            self.closed = None

        async def accept(self):
            self.accepted = True

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code=1000, reason=""):
            self.closed = (code, reason)

    def test_a_visitor_socket_is_told_to_stop(self):
        self.switch(False)
        socket = self.Socket()
        asyncio.run(api.websocket_endpoint(socket))
        self.assertTrue(socket.accepted)
        self.assertEqual(socket.sent, [{"type": "site_disabled"}],
                         "the viewer needs an explicit answer, not a silent hang")
        self.assertIsNotNone(socket.closed)
        self.assertEqual(socket.closed[0], 1008)

    def test_the_panels_are_never_blocked(self):
        # /mini and /admin must keep being served while the public site is
        # closed, or the switch could not be turned back on.
        for path in ("/mini", "/admin"):
            source = (ROOT / ("mini-admin.html" if path == "/mini" else "Admin.html"))
            self.assertTrue(source.exists(), path)
        source = (ROOT / "api.py").read_text(encoding="utf-8")
        mini = source[source.index('@app.get("/mini")'):]
        mini = mini[:mini.index("@app.")] if "@app." in mini else mini
        self.assertNotIn("_public_pages_open", mini,
                         "/mini must stay reachable while pages are off")
        admin = source[source.index('@app.get("/admin")'):]
        admin = admin[:admin.index("@app.")] if "@app." in admin else admin
        self.assertNotIn("_public_pages_open", admin,
                         "/admin must stay reachable while pages are off")


class FailOpenCase(unittest.TestCase):
    def test_a_broken_settings_read_keeps_the_site_up(self):
        real = server_settings.get_settings

        def boom():
            raise RuntimeError("settings file is being rewritten")

        server_settings.get_settings = boom
        try:
            # api holds the module, not the function, so patch through it.
            api.srv_settings.get_settings = boom
            self.assertTrue(api._public_pages_open())
        finally:
            api.srv_settings.get_settings = real
            server_settings.get_settings = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
