#!/usr/bin/env python3
"""Which rule does a click on a mapped control actually run?

The visitor's browser sends the index it was given with the page *and* the
matcher it saw. An operator can save a new action list while somebody is still
sitting on the page, so the index can point at the wrong rule by the time the
click arrives. These tests pin the resolution order:

    1. the index, when its matcher still agrees with what was clicked
    2. the matcher, when the index has drifted
    3. nothing (a recorded reason) when neither says anything

They also pin device pairs: one page to the operator is two rows in the
database, and an action saved on the page a workflow names must still fire
when the visitor was served the paired build.

    python3 tests/test_click_action_resolution.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="lpv-click-actions-"))
PAGES_DIR = TMP / "pages"
PAGES_DIR.mkdir(parents=True, exist_ok=True)

import lpv_store  # noqa: E402

lpv_store.LPV_DB_PATH = TMP / "lpv.sqlite3"
lpv_store.LPV_PAGES_DIR = PAGES_DIR
lpv_store.init_db()

try:
    import api  # noqa: E402
except ModuleNotFoundError as exc:
    # The click handler lives in the app, so this suite needs the app's own
    # dependencies (fastapi). Everything else here is stdlib only — skip like
    # the jsdom suites do rather than fail a bare checkout.
    print("click action resolution: skipped (%s not installed)" % exc.name)
    raise SystemExit(0)


class PickActionCase(unittest.TestCase):
    RULES = [
        {"match": "text:Sign in", "do": "page", "page_id": "p2"},
        {"match": ".js-next", "do": "link", "url": "https://example.com/next"},
        {"match": "#submit", "do": "link", "url": "https://example.com/send"},
    ]

    def test_index_wins_when_the_matcher_agrees(self):
        rule, why = api._pick_action(self.RULES, 1, ".js-next")
        self.assertEqual(rule["url"], "https://example.com/next")
        self.assertEqual(why, "")

    def test_index_alone_is_enough_when_the_client_sent_no_matcher(self):
        rule, why = api._pick_action(self.RULES, 0, "")
        self.assertEqual(rule["page_id"], "p2")
        self.assertEqual(why, "")

    def test_a_drifted_index_falls_back_to_the_matcher(self):
        # The operator inserted a rule at the top after this visitor loaded
        # the page, so index 1 now means something else entirely.
        drifted = [{"match": "text:New", "do": "link", "url": "https://example.com/new"}] + self.RULES
        rule, why = api._pick_action(drifted, 1, ".js-next")
        self.assertEqual(rule["match"], ".js-next")
        self.assertEqual(why, "")

    def test_the_matcher_is_case_and_space_insensitive(self):
        rule, _why = api._pick_action(self.RULES, 99, "  .JS-NEXT ")
        self.assertEqual(rule["url"], "https://example.com/next")

    def test_an_unknown_control_is_refused_with_a_reason(self):
        rule, why = api._pick_action(self.RULES, 0, "text:Never mapped")
        self.assertIsNone(rule)
        self.assertEqual(why, "rule_not_found")

    def test_a_removed_control_is_refused(self):
        rule, why = api._pick_action(self.RULES, 7, "text:Sign in")
        self.assertEqual(rule["match"], "text:Sign in")   # still saved
        rule, why = api._pick_action([], 7, "text:Sign in")
        self.assertIsNone(rule)
        self.assertEqual(why, "index_out_of_range")


class DevicePairCase(unittest.TestCase):
    seq = 0

    def setUp(self):
        # save_page upserts by name, so every test needs its own pair.
        DevicePairCase.seq += 1
        n = DevicePairCase.seq
        self.base = lpv_store.save_page(name=f"pair-base-{n}", html="<html><body>a</body></html>")
        self.variant = lpv_store.save_page(name=f"pair-variant-{n}", html="<html><body>b</body></html>")
        lpv_store.set_page_device(self.base["id"], "desktop", self.variant["id"])

    def test_actions_saved_on_the_named_page_reach_the_paired_build(self):
        lpv_store.set_page_actions(self.base["id"], [
            {"match": "text:Continue", "do": "link", "url": "https://example.com/go"},
        ])
        got = api._page_actions(self.variant["id"])
        self.assertEqual(len(got), 1, "the variant would otherwise have no clickable mapping at all")
        self.assertEqual(got[0]["url"], "https://example.com/go")

    def test_a_page_with_its_own_actions_never_inherits(self):
        lpv_store.set_page_actions(self.base["id"], [
            {"match": "text:Base", "do": "link", "url": "https://example.com/base"},
        ])
        lpv_store.set_page_actions(self.variant["id"], [
            {"match": "text:Own", "do": "link", "url": "https://example.com/own"},
        ])
        got = api._page_actions(self.variant["id"])
        self.assertEqual([r["match"] for r in got], ["text:Own"])

    def test_an_unpaired_page_is_unchanged(self):
        lone = lpv_store.save_page(name="pair-lone", html="<html><body>c</body></html>")
        lpv_store.set_page_actions(lone["id"], [])
        self.assertEqual(api._page_actions(lone["id"]), [])


class LiveUpdateCase(unittest.TestCase):
    """Saving actions reaches the visitors who are already on the page."""

    def setUp(self):
        self.base = lpv_store.save_page(name="live-base", html="<html><body>a</body></html>")
        self.variant = lpv_store.save_page(name="live-variant", html="<html><body>b</body></html>")
        lpv_store.set_page_device(self.base["id"], "desktop", self.variant["id"])
        self.sent = []
        self._real_send = api._send_to_client

        async def fake_send(client_id, message, **kwargs):
            self.sent.append((client_id, message))
            return True

        api._send_to_client = fake_send

    def tearDown(self):
        api._send_to_client = self._real_send

    def test_a_visitor_on_the_named_page_gets_the_new_list(self):
        lpv_store.set_current_page("c-live", self.base["id"], self.base["name"])
        asyncio.run(api._push_actions_to_viewers(self.base["id"], [
            {"match": "#go", "do": "link", "url": "https://example.com"},
        ]))
        self.assertEqual(len(self.sent), 1)
        _cid, msg = self.sent[0]
        self.assertEqual(msg["type"], "lpv_actions")
        self.assertEqual(msg["page_id"], self.base["id"])
        self.assertEqual(msg["actions"][0]["url"], "https://example.com")

    def test_a_visitor_on_the_paired_build_gets_it_too(self):
        # They are looking at the variant, so the message must carry that id
        # or the client will (rightly) ignore a page it is not showing.
        lpv_store.set_current_page("c-live-pair", self.variant["id"], self.variant["name"])
        asyncio.run(api._push_actions_to_viewers(self.base["id"], [
            {"match": "#go", "do": "link", "url": "https://example.com"},
        ]))
        targets = [m for m in self.sent if m[0] == "c-live-pair"]
        self.assertEqual(len(targets), 1)
        msg = targets[0][1]
        self.assertEqual(msg["page_id"], self.variant["id"])
        self.assertEqual(msg["actions"][0]["match"], "#go")


class HandleClickCase(unittest.TestCase):
    """The websocket handler end to end, with the transport stubbed."""

    def setUp(self):
        self.push = lpv_store.save_page(name="click-target", html="<html><body>t</body></html>")
        self.page = lpv_store.save_page(name="click-source", html="<html><body>s</body></html>")
        self.sent = []
        self._real_send = api._send_to_client

        async def fake_send(client_id, message, **kwargs):
            self.sent.append(message)
            return True

        api._send_to_client = fake_send
        lpv_store.set_current_page("c-click", self.page["id"], self.page["name"])
        lpv_store.set_page_actions(self.page["id"], [
            {"match": "text:Sign in", "do": "page", "page_id": self.push["id"]},
            {"match": "text:Google", "do": "link", "url": "https://google.com"},
        ])

    def tearDown(self):
        api._send_to_client = self._real_send

    def click(self, payload):
        payload.setdefault("type", "lpv_action")
        payload.setdefault("page_id", self.page["id"])
        payload.setdefault("page_name", self.page["name"])
        asyncio.run(api._handle_click_action("c-click", payload))
        return self.sent

    def types(self):
        return [m.get("type") for m in self.sent]

    def test_a_link_target_navigates(self):
        self.click({"index": 1, "match": "text:Google"})
        nav = [m for m in self.sent if m.get("type") == "navigate"]
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0]["url"], "https://google.com")

    def test_a_page_target_is_pushed(self):
        self.click({"index": 0, "match": "text:Sign in"})
        shown = [m for m in self.sent if m.get("type") == "lpv_show_page"]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["page_id"], self.push["id"])

    def test_a_drifted_index_still_finds_the_clicked_control(self):
        # index 1 now points at a rule that has since been inserted above.
        lpv_store.set_page_actions(self.page["id"], [
            {"match": "text:New", "do": "link", "url": "https://example.com/new"},
            {"match": "text:Sign in", "do": "page", "page_id": self.push["id"]},
            {"match": "text:Google", "do": "link", "url": "https://google.com"},
        ])
        self.click({"index": 1, "match": "text:Google"})
        nav = [m for m in self.sent if m.get("type") == "navigate"]
        self.assertEqual([m["url"] for m in nav], ["https://google.com"])

    def test_a_click_from_a_page_they_left_is_ignored(self):
        lpv_store.set_current_page("c-click", self.push["id"], self.push["name"])
        self.click({"index": 0, "match": "text:Sign in"})
        self.assertEqual(self.types(), [])
        kinds = [e.get("event_type") for e in lpv_store.list_events("c-click", limit=5)]
        self.assertIn("page_action_skipped", kinds)

    def test_a_vanished_target_says_so(self):
        lpv_store.set_page_actions(self.page["id"], [
            {"match": "text:Sign in", "do": "page", "page_id": self.push["id"]},
        ])
        # The mapping was saved before the target page was deleted; a click
        # now has nowhere to go, and the visitor is told rather than left
        # staring at a button that does nothing.
        lpv_store.delete_page(self.push["id"])
        self.click({"index": 0, "match": "text:Sign in"})
        self.assertIn("lpv_action_failed", self.types())


if __name__ == "__main__":
    unittest.main(verbosity=1)
