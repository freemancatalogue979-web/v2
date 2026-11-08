#!/usr/bin/env python3
"""The server's clock, and the timezone the operator picked for it.

What was wrong: every timestamp the server wrote — the Telegram alerts, the
audit trail, the log lines — was the machine's local time. On a box in UTC
that is the wrong clock for the person reading the alerts, and the only fix
was to change the container's timezone and restart.

Now it is a setting (Settings -> Clock -> Timezone) and it takes effect the
moment it is saved:

  * ``app_clock`` owns the choice: an IANA name ("Africa/Lagos") that knows
    about DST, a fixed offset ("+01:00"), or "" for the server's own clock;
  * applying it sets ``TZ`` + ``tzset()`` for the process (so ``time.strftime``,
    ``time.localtime`` and the logging formatter move too) *and* keeps a
    tzinfo for platforms without ``tzset``;
  * ``api._tg_stamp`` formats through it, so an alert and the log line beside
    it read the same minute;
  * ``/api/admin/clock`` answers the applied clock (never the saved value) plus
    the zones this machine knows, and the panel shows a live preview.

    python3 tests/test_timezone.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app_clock  # noqa: E402
import server_settings  # noqa: E402

try:
    import api  # noqa: E402
    from starlette.requests import Request  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - bare checkout
    print("timezone: skipped (%s not installed)" % exc.name)
    raise SystemExit(0)

ADMIN_HTML = (ROOT / "Admin.html").read_text(encoding="utf-8", errors="replace")

# A fixed instant, so DST behaviour can be asserted without waiting a year.
WINTER = 1767225600.0      # 2026-01-01T00:00:00Z
SUMMER = 1782864000.0      # 2026-07-01T00:00:00Z


def request_for(method="GET", body=None, auth=True) -> Request:
    scope = {
        "type": "http", "method": method, "path": "/api/admin/clock",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("testserver", 80),
    }
    request = Request(scope)
    if body is not None:
        async def _json():
            return body
        request.json = _json  # type: ignore[assignment]
    return request


class ClockCase(unittest.TestCase):
    def setUp(self):
        self._saved_tz = app_clock.current_name()
        self._saved_env = os.environ.get("TZ")
        self._saved_settings = server_settings.get_settings_dict()

    def tearDown(self):
        app_clock.apply(self._saved_tz)
        if self._saved_env is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._saved_env
        try:
            time.tzset()
        except Exception:
            pass
        server_settings._settings = server_settings.ServerSettings.from_dict(self._saved_settings)


class ParsingTest(ClockCase):
    def test_an_iana_name_is_kept(self):
        self.assertEqual(app_clock.normalise("Africa/Lagos"), "Africa/Lagos")
        self.assertEqual(app_clock.normalise("  Asia/Tokyo  "), "Asia/Tokyo")
        self.assertEqual(app_clock.normalise("UTC"), "UTC")

    def test_a_name_is_matched_case_insensitively(self):
        self.assertEqual(app_clock.normalise("africa/lagos"), "Africa/Lagos")
        self.assertEqual(app_clock.normalise("ASIA/TOKYO"), "Asia/Tokyo")

    def test_offsets_are_accepted_in_the_ways_people_write_them(self):
        for raw, want in (
            ("+01:00", "+01:00"), ("-5", "-05:00"), ("GMT+2", "+02:00"),
            ("UTC-3:30", "-03:30"), ("+0530", "+05:30"), ("+00:00", "+00:00"),
        ):
            self.assertEqual(app_clock.normalise(raw), want, raw)

    def test_an_impossible_offset_is_refused(self):
        for raw in ("+15:00", "+05:99", "-99:00"):
            self.assertEqual(app_clock.normalise(raw), "", raw)

    def test_auto_words_mean_the_server_clock(self):
        for raw in ("", "  ", "auto", "server", "server default", "default", "local", "system"):
            self.assertEqual(app_clock.normalise(raw), "", raw)

    def test_junk_does_not_raise(self):
        for raw in ("nonsense", "Africa/Atlantis", "12:00", None, 42):
            self.assertEqual(app_clock.normalise(raw), "", repr(raw))

    def test_explain_says_why(self):
        good = app_clock.explain("Africa/Lagos")
        self.assertTrue(good["ok"])
        self.assertEqual(good["stored"], "Africa/Lagos")
        bad = app_clock.explain("nonsense")
        self.assertFalse(bad["ok"])
        self.assertIn("IANA", bad["message"])


class ApplyTest(ClockCase):
    def test_applying_moves_the_process_clock(self):
        app_clock.apply("Africa/Lagos")
        self.assertEqual(app_clock.current_name(), "Africa/Lagos")
        self.assertEqual(os.environ.get("TZ"), "Africa/Lagos")
        self.assertEqual(app_clock.offset_now(), timedelta(hours=1))
        # libc (what log lines and time.strftime use) agrees with our stamp.
        self.assertEqual(time.strftime("%H:%M"), app_clock.stamp()[11:16])

    def test_a_fixed_offset_moves_libc_by_the_same_amount(self):
        # POSIX inverts the sign; getting it wrong would put the logs an
        # offset away from the alerts.
        app_clock.apply("+05:30")
        self.assertEqual(app_clock.offset_now(), timedelta(hours=5, minutes=30))
        self.assertEqual(time.strftime("%H:%M"), app_clock.stamp()[11:16])
        app_clock.apply("-05:00")
        self.assertEqual(app_clock.offset_now(), timedelta(hours=-5))
        self.assertEqual(time.strftime("%H:%M"), app_clock.stamp()[11:16])

    def test_a_zone_knows_about_dst(self):
        app_clock.apply("America/New_York")
        self.assertEqual(app_clock.stamp(WINTER), "2025-12-31 19:00:00")
        self.assertEqual(app_clock.stamp(SUMMER), "2026-06-30 20:00:00")

    def test_server_default_hands_the_process_back(self):
        app_clock.apply("Asia/Tokyo")
        self.assertIn("TZ", os.environ)
        info = app_clock.apply("")
        self.assertEqual(info["source"], "server")
        self.assertEqual(info["name"], "")
        self.assertNotIn("TZ", os.environ, "the override must be removed, not left as Tokyo")

    def test_describe_is_what_the_panel_shows(self):
        app_clock.apply("Africa/Lagos")
        info = app_clock.describe()
        self.assertEqual(info["source"], "admin")
        self.assertEqual(info["offset"], "+01:00")
        self.assertEqual(info["tzname"], "WAT")
        self.assertRegex(info["now"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TelegramTest(ClockCase):
    def test_the_alert_timestamp_is_in_the_chosen_zone(self):
        app_clock.apply("Africa/Lagos")
        self.assertEqual(api._tg_stamp(WINTER), "2026-01-01 01:00:00")
        app_clock.apply("Asia/Tokyo")
        self.assertEqual(api._tg_stamp(WINTER), "2026-01-01 09:00:00")

    def test_the_status_reply_labels_the_zone_it_is_in(self):
        # "/status" used to hard-code "UTC" beside the time; once the operator
        # picks a zone, that label is a lie.
        import telegram_bot
        app_clock.apply("Africa/Lagos")
        self.assertTrue(telegram_bot._clock_stamp_labeled().endswith("WAT"))
        app_clock.apply("+05:30")
        self.assertTrue(telegram_bot._clock_stamp_labeled().endswith("UTC+05:30"))

    def test_a_freshly_built_alert_carries_that_clock(self):
        app_clock.apply("+02:00")
        message = api._tg_dispatch_message("OTP.html")
        self.assertIn(app_clock.stamp()[:16], message,
                      "the dispatch alert did not use the configured clock")


class SettingsTest(ClockCase):
    def test_it_is_a_setting_with_a_safe_default(self):
        self.assertIn("timezone", server_settings.ServerSettings().to_dict())
        self.assertEqual(server_settings.ServerSettings().timezone, "")

    def test_saving_it_applies_it_immediately(self):
        asyncio.run(server_settings.set_settings({"timezone": "Africa/Lagos"}))
        self.assertEqual(server_settings.get_settings().timezone, "Africa/Lagos")
        self.assertEqual(app_clock.current_name(), "Africa/Lagos")
        self.assertEqual(app_clock.offset_now(), timedelta(hours=1))

    def test_changing_it_twice_keeps_tracking(self):
        asyncio.run(server_settings.set_settings({"timezone": "Africa/Lagos"}))
        asyncio.run(server_settings.set_settings({"timezone": "Asia/Tokyo"}))
        self.assertEqual(app_clock.current_name(), "Asia/Tokyo")
        self.assertEqual(app_clock.offset_now(), timedelta(hours=9))
        # ...and back to the server's own clock.
        asyncio.run(server_settings.set_settings({"timezone": ""}))
        self.assertEqual(app_clock.current_name(), "")
        self.assertNotIn("TZ", os.environ)

    def test_junk_saves_as_server_default_instead_of_breaking(self):
        asyncio.run(server_settings.set_settings({"timezone": "Mars/Olympus"}))
        self.assertEqual(server_settings.get_settings().timezone, "")
        self.assertEqual(app_clock.current_name(), "")

    def test_the_broadcast_tells_the_other_panels(self):
        seen = []

        async def capture(payload):
            seen.append(payload)

        server_settings.subscribe(capture)
        try:
            asyncio.run(server_settings.update_setting("timezone", "Asia/Tokyo"))
        finally:
            server_settings.unsubscribe(capture)
        self.assertTrue(seen, "no broadcast")
        payload = seen[0]
        self.assertEqual(payload["settings"]["timezone"], "Asia/Tokyo")
        self.assertEqual(payload["clock"]["offset"], "+09:00",
                         "the payload must carry the clock that is now in force")


class EndpointTest(ClockCase):
    def setUp(self):
        super().setUp()
        self._saved_verify = api.verify_admin_token

        async def yes(_request):
            return True

        api.verify_admin_token = yes

    def tearDown(self):
        api.verify_admin_token = self._saved_verify
        super().tearDown()

    def test_get_answers_the_applied_clock_and_the_zones(self):
        asyncio.run(server_settings.set_settings({"timezone": "Africa/Lagos"}))
        response = asyncio.run(api.get_clock(request_for()))
        data = json.loads(response.body)
        self.assertEqual(data["name"], "Africa/Lagos")
        self.assertEqual(data["offset"], "+01:00")
        self.assertIn("now", data)
        self.assertIsInstance(data.get("zones"), list)
        self.assertIn("Africa/Lagos", data["zones"])

    def test_post_sets_it(self):
        response = asyncio.run(api.set_clock(request_for("POST", {"timezone": "Asia/Tokyo"})))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.body)
        self.assertTrue(data["success"])
        self.assertEqual(data["clock"]["offset"], "+09:00")
        self.assertEqual(server_settings.get_settings().timezone, "Asia/Tokyo")
        self.assertEqual(app_clock.current_name(), "Asia/Tokyo")

    def test_post_refuses_junk_with_a_reason(self):
        response = asyncio.run(api.set_clock(request_for("POST", {"timezone": "Mars/Olympus"})))
        self.assertEqual(response.status_code, 400)
        self.assertIn("IANA", json.loads(response.body)["error"])
        self.assertEqual(app_clock.current_name(), self._saved_tz,
                         "a rejected value must not change the clock")

    def test_post_accepts_the_server_default(self):
        asyncio.run(server_settings.set_settings({"timezone": "Africa/Lagos"}))
        response = asyncio.run(api.set_clock(request_for("POST", {"timezone": ""})))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["timezone"], "")
        self.assertEqual(server_settings.get_settings().timezone, "")

    def test_both_endpoints_need_an_admin_token(self):
        async def no(_request):
            return False

        api.verify_admin_token = no
        self.assertEqual(asyncio.run(api.get_clock(request_for())).status_code, 401)
        self.assertEqual(asyncio.run(api.set_clock(request_for("POST", {}))).status_code, 401)

    def test_diagnostics_carry_the_clock(self):
        asyncio.run(server_settings.set_settings({"timezone": "Africa/Lagos"}))
        response = asyncio.run(api.server_diagnostics(request_for()))
        data = json.loads(response.body) if hasattr(response, "body") else response
        self.assertEqual(data["clock"]["name"], "Africa/Lagos")

    def test_get_settings_carries_it_too(self):
        response = asyncio.run(api.settings_get(request_for()))
        # The route answers a dict (FastAPI serializes it); a JSONResponse
        # would carry .body. Accept either so this cannot rot quietly.
        data = json.loads(response.body) if hasattr(response, "body") else response
        self.assertIn("timezone", data["settings"])
        self.assertIn("clock", data)


class PanelTest(unittest.TestCase):
    def test_the_picker_exists(self):
        self.assertIn('id="settings-timezone"', ADMIN_HTML)

    def test_it_is_loaded_and_saved(self):
        self.assertIn("$('settings-timezone').value           = s.timezone || ''", ADMIN_HTML)
        self.assertIn("timezone:                    (document.getElementById('settings-timezone') || {}).value || ''",
                      ADMIN_HTML)

    def test_it_offers_the_common_zones_and_offsets(self):
        self.assertIn("'Africa/Lagos'", ADMIN_HTML)
        self.assertIn("'Asia/Tokyo'", ADMIN_HTML)
        self.assertIn("'America/New_York'", ADMIN_HTML)
        self.assertIn("'Fixed offset (never shifts for DST)'", ADMIN_HTML)

    def test_it_shows_a_live_clock_and_says_what_is_active(self):
        self.assertIn('id="settings-clock-now"', ADMIN_HTML)
        self.assertIn("setInterval(paintClockNow, 1000)", ADMIN_HTML)
        self.assertIn("preview, press Save to apply", ADMIN_HTML)
        self.assertIn("— active", ADMIN_HTML)

    def test_it_uses_the_browser_timezone_database_when_it_can(self):
        self.assertIn("Intl.supportedValuesOf('timeZone')", ADMIN_HTML)
        self.assertIn("/api/admin/clock", ADMIN_HTML)

    def test_the_panel_prints_times_in_that_clock(self):
        # An alert at 14:02 must not sit above a session row that says 06:02.
        self.assertIn("window.clockFormat = clockFormat;", ADMIN_HTML)
        self.assertIn("window.clockTime = clockTime;", ADMIN_HTML)
        self.assertIn("const firstSeen = clockFormat(session.first_seen);", ADMIN_HTML)
        self.assertNotIn("toLocaleTimeString()", ADMIN_HTML)

    def test_the_phone_panel_follows_it_too(self):
        mini = (ROOT / "mini-admin.html").read_text(encoding="utf-8")
        self.assertIn("async function loadClock()", mini)
        self.assertIn("loadClock();", mini)
        self.assertIn("clockFormat((ts||Date.now()/1000)*1000", mini)

    def test_it_refreshes_when_the_server_pushes_a_change(self):
        # The Settings tab's listener (there is another one for LPV), then the
        # clock handling inside it.
        at = ADMIN_HTML.index("SETTINGS_STATE.current = s;")
        body = ADMIN_HTML[at - 400:at + 900]
        self.assertIn("detail.clock", body)
        self.assertIn("Timezone applied", body)


class DocsTest(unittest.TestCase):
    def test_the_readme_documents_the_setting(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Timezone", readme)
        self.assertIn("Africa/Lagos", readme)
        self.assertIn("no restart", readme)
        self.assertIn("tests/test_timezone.py", readme)
        deploy = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
        self.assertIn("timezone", deploy)


if __name__ == "__main__":
    unittest.main(verbosity=1)
