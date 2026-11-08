#!/usr/bin/env python3
"""Tests for the live-chat switch (on/off + which device sees the bubble).

The operator's controls are two settings:

    chat_enabled   show the floating chat bubble on live pages at all
    chat_devices   both | desktop | mobile

They are read in two places: the boot payload a client gets when it connects,
and the re-broadcast sent to everyone already connected when an admin flips a
switch. Both must agree, or a phone would keep a bubble after "desktop only".

Offline only:

    python3 tests/test_chat.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server_settings as ss  # noqa: E402


class Defaults(unittest.TestCase):
    def test_chat_is_off_until_somebody_turns_it_on(self):
        self.assertFalse(ss.ServerSettings().chat_enabled)

    def test_when_on_it_is_for_every_device(self):
        self.assertEqual(ss.ServerSettings().chat_devices, "both")


class DeviceChoice(unittest.TestCase):
    def test_both_lets_everyone_have_it(self):
        self.assertTrue(ss.chat_device_allows("both", "desktop"))
        self.assertTrue(ss.chat_device_allows("both", "mobile"))

    def test_desktop_only_hides_it_on_a_phone(self):
        self.assertTrue(ss.chat_device_allows("desktop", "desktop"))
        self.assertFalse(ss.chat_device_allows("desktop", "mobile"))

    def test_mobile_only_hides_it_on_a_desktop(self):
        self.assertTrue(ss.chat_device_allows("mobile", "mobile"))
        self.assertFalse(ss.chat_device_allows("mobile", "desktop"))

    def test_unknown_device_is_left_to_the_client(self):
        # The client also gets `devices` and checks its own user agent, so
        # an unknown device must not be blocked outright.
        self.assertTrue(ss.chat_device_allows("desktop", None))
        self.assertTrue(ss.chat_device_allows("mobile", ""))

    def test_junk_means_both(self):
        self.assertTrue(ss.chat_device_allows("junk", "mobile"))
        self.assertTrue(ss.chat_device_allows(None, "desktop"))


class Normalise(unittest.TestCase):
    def test_device_choice_is_coerced(self):
        self.assertEqual(ss.normalise_setting("chat_devices", " MOBILE "), "mobile")
        self.assertEqual(ss.normalise_setting("chat_devices", "tablet"), "both")
        self.assertEqual(ss.normalise_setting("chat_devices", None), "both")

    def test_other_keys_are_untouched(self):
        self.assertEqual(ss.normalise_setting("chat_enabled", 1), 1)
        self.assertEqual(ss.normalise_setting("lpv_tab_title", "Hi"), "Hi")


class SettingsRoundTrip(unittest.TestCase):
    """set_settings stores the switch and answers with it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(ss.reload_from_disk)
        self.patch = mock.patch.object(ss, "_SETTINGS_PATH",
                                       Path(self.tmp.name) / "settings.json")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        ss.reload_from_disk()

    def test_switch_and_device_are_saved(self):
        import asyncio
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            ss.set_settings({"chat_enabled": True, "chat_devices": "desktop"})
        )
        live = ss.get_settings()
        self.assertTrue(live.chat_enabled)
        self.assertEqual(live.chat_devices, "desktop")

    def test_a_typo_cannot_wedge_the_setting(self):
        import asyncio
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            ss.set_settings({"chat_devices": "everything"})
        )
        self.assertEqual(ss.get_settings().chat_devices, "both")


if __name__ == "__main__":
    unittest.main(verbosity=2)
