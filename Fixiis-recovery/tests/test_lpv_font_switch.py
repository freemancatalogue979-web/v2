#!/usr/bin/env python3
"""The "Set live pages in Space Mono" switch, from the panel to the page.

One setting, three surfaces, and one rule that must not break:

  * ``server_settings.lpv_mono_font`` — off by default, persisted, coerced
    from strings so a hand-edited file cannot turn a page's typography over
    with the word "off";
  * the served ``client.html`` carries the answer as a boot flag (the plain,
    unbranded copy too — that one is served straight from disk through the
    ETag path and never sees the branding blob);
  * ``/api/public/boot`` answers a copy hosted elsewhere;
  * ``lpv_branding`` pushes it to visitors who are already reading a page, and
    saving the switch is what triggers that push;
  * the Admin panel has the control, loads it, and saves it.

The behaviour on the page itself (inject / strip / never touch a streamed
mirror) is ``node tests/test_lpv_mono_font.cjs``; this file is the wiring.

    python3 tests/test_lpv_font_switch.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server_settings  # noqa: E402

try:
    import api  # noqa: E402
    from starlette.requests import Request  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - bare checkout
    print("lpv font switch: skipped (%s not installed)" % exc.name)
    raise SystemExit(0)

ADMIN_HTML = (ROOT / "Admin.html").read_text(encoding="utf-8", errors="replace")
CLIENT_HTML = (ROOT / "client.html").read_text(encoding="utf-8", errors="replace")


def request_for(query: str = "") -> Request:
    scope = {
        "type": "http", "method": "GET", "path": "/api/public/boot",
        "headers": [], "query_string": query.encode(),
        "scheme": "http", "server": ("testserver", 80),
    }
    return Request(scope)


class SettingTest(unittest.TestCase):
    def setUp(self):
        self._saved = server_settings.get_settings_dict()

    def tearDown(self):
        # Swap the live object back: rebuilding from __dict__ loses nothing
        # but keeps the rest of the suite's expectations intact.
        server_settings._settings = server_settings.ServerSettings.from_dict(self._saved)

    def test_it_is_off_by_default(self):
        self.assertIs(server_settings.ServerSettings().lpv_mono_font, False,
                      "a live page must keep its own fonts until somebody asks")

    def test_the_default_object_carries_it(self):
        self.assertIn("lpv_mono_font", server_settings.DEFAULT_SETTINGS.to_dict())

    def test_strings_are_coerced_both_ways(self):
        for value in ("true", "1", "on", "yes", "TRUE"):
            self.assertIs(server_settings.normalise_setting("lpv_mono_font", value), True, value)
        for value in ("false", "0", "off", "no", "", "nonsense"):
            self.assertIs(server_settings.normalise_setting("lpv_mono_font", value), False, value)
        self.assertIs(server_settings.normalise_setting("lpv_mono_font", True), True)
        self.assertIs(server_settings.normalise_setting("lpv_mono_font", 0), False)

    def test_the_other_switches_are_untouched(self):
        # public_pages_enabled defaults ON, so its coercion must stay inverted.
        self.assertIs(server_settings.normalise_setting("public_pages_enabled", "off"), False)
        self.assertIs(server_settings.normalise_setting("public_pages_enabled", "true"), True)

    def test_it_round_trips_through_a_settings_patch(self):
        asyncio.run(server_settings.set_settings({"lpv_mono_font": True}))
        self.assertIs(server_settings.get_settings().lpv_mono_font, True)
        self.assertIn("lpv_mono_font", server_settings.get_settings_dict())
        asyncio.run(server_settings.set_settings({"lpv_mono_font": "off"}))
        self.assertIs(server_settings.get_settings().lpv_mono_font, False)


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self._saved = server_settings.get_settings_dict()

    def tearDown(self):
        server_settings._settings = server_settings.ServerSettings.from_dict(self._saved)

    def test_the_boot_flag_says_no_by_default(self):
        flag = api._client_font_flag()
        self.assertIn('id="lpv-font-flag"', flag)
        self.assertIn('window.__LPV_BOOT_FONTS__ = {"mono": false}', flag)

    def test_the_boot_flag_says_yes_when_switched_on(self):
        server_settings._settings = server_settings.ServerSettings.from_dict(
            dict(self._saved, lpv_mono_font=True)
        )
        self.assertIn('{"mono": true}', api._client_font_flag())

    def test_the_public_boot_answer_carries_it(self):
        server_settings._settings = server_settings.ServerSettings.from_dict(
            dict(self._saved, lpv_mono_font=True)
        )
        response = asyncio.run(api.public_boot(request_for()))
        payload = json.loads(response.body)
        self.assertEqual(payload["fonts"], {"mono": True})

        server_settings._settings = server_settings.ServerSettings.from_dict(
            dict(self._saved, lpv_mono_font=False)
        )
        payload = json.loads(asyncio.run(api.public_boot(request_for())).body)
        self.assertEqual(payload["fonts"], {"mono": False})

    def test_the_served_copy_injects_the_flag_on_both_paths(self):
        src = inspect.getsource(api._render_client_html)
        self.assertEqual(src.count("_client_font_flag()"), 2,
                         "the plain (ETag) copy and the branded copy both need it")

    def test_the_render_cache_key_includes_the_switch(self):
        self.assertIn('"mono" if mono_font else "own-fonts"',
                      inspect.getsource(api._render_client_html),
                      "flipping the switch must rebuild the served copy")

    def test_the_live_push_carries_it(self):
        seen = []

        class FakeWS:
            async def send_json(self, payload):
                seen.append(payload)

        async def fake_push():
            return None

        saved_ws = dict(api._lpv_only_ws)
        saved_device = api._client_device
        try:
            api._lpv_only_ws.clear()
            api._lpv_only_ws["client-1"] = FakeWS()

            async def device(_cid):
                return "mobile"

            api._client_device = device
            server_settings._settings = server_settings.ServerSettings.from_dict(
                dict(self._saved, lpv_mono_font=True, lpv_tab_title="Sign in")
            )
            asyncio.run(api._push_lpv_branding_to_clients())
        finally:
            api._lpv_only_ws.clear()
            api._lpv_only_ws.update(saved_ws)
            api._client_device = saved_device

        self.assertTrue(seen, "no branding message was pushed")
        self.assertEqual(seen[0].get("type"), "lpv_branding")
        self.assertIs(seen[0].get("mono_font"), True,
                      "the live message must carry the font switch")
        self.assertEqual(seen[0].get("tab_title"), "Sign in")

    def test_saving_the_switch_is_what_pushes_it(self):
        src = inspect.getsource(api.settings_update)
        self.assertIn("lpv_mono_font", src,
                      "a save that does not list the key never re-sets live pages")


class PanelTest(unittest.TestCase):
    def test_the_control_exists_in_live_page_branding(self):
        self.assertIn('id="settings-lpv-mono-font"', ADMIN_HTML)
        group = ADMIN_HTML[ADMIN_HTML.index("Live page branding"):
                           ADMIN_HTML.index("<!-- ============== Live chat")]
        self.assertIn("settings-lpv-mono-font", group,
                      "the switch belongs with the other live-page branding settings")

    def test_the_control_is_loaded(self):
        self.assertIn("$('settings-lpv-mono-font').checked     = !!s.lpv_mono_font", ADMIN_HTML)

    def test_the_control_is_saved(self):
        self.assertIn("lpv_mono_font:               !!document.getElementById('settings-lpv-mono-font').checked",
                      ADMIN_HTML)

    def test_it_says_what_off_means(self):
        at = ADMIN_HTML.index("Set live pages in Space Mono")
        hint = ADMIN_HTML[at:at + 900]
        self.assertIn("OFF (default)", hint)
        self.assertIn("its own", hint)
        self.assertIn("icons", hint)


class ViewerTest(unittest.TestCase):
    def test_off_is_the_fallback_inside_the_viewer(self):
        self.assertIn("return false;\n            }\n\n            function stripLpvMonoFont",
                      CLIENT_HTML.replace("\r\n", "\n"),
                      "no flag anywhere must mean the page's own fonts")

    def test_the_mono_face_is_space_mono_not_the_shell_font(self):
        at = CLIENT_HTML.index("const LPV_MONO_HREF")
        self.assertIn("Space+Mono", CLIENT_HTML[at:at + 200])
        self.assertNotIn("Space+Grotesk", CLIENT_HTML[at:at + 200])
        # The shell face is a system font now, so this switch is the ONLY
        # thing in the client that ever asks a font CDN for a file.
        self.assertEqual(CLIENT_HTML.count("fonts.googleapis.com"), 1)

    def test_it_is_never_applied_to_the_shell(self):
        # The only selectors it writes are html/body/body * inside the pushed
        # document; nothing here reaches out of the frame.
        at = CLIENT_HTML.index("function applyLpvMonoFont")
        body = CLIENT_HTML[at:at + 2200]
        self.assertIn("if (!isLpvPageDocument(doc) || !lpvMonoFontEnabled())", body)
        self.assertNotIn("document.body", body)
        self.assertNotIn("window.document", body)

    def test_it_is_stripped_when_switched_off(self):
        at = CLIENT_HTML.index("function setLpvMonoFont")
        self.assertIn("stripLpvMonoFont(d)", CLIENT_HTML[at:at + 900])


if __name__ == "__main__":
    unittest.main(verbosity=1)
