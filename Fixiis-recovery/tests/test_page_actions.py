#!/usr/bin/env python3
"""Tests for two things an operator does to an archive after uploading it:

    * re-upload its HTML (keeping bindings / guard / actions / id / name)
    * map a control to somewhere else ("click Sign in -> show that page")

Both live in lpv_store; the HTTP layer is exercised by the same functions,
and tests/test_page_actions.cjs pins the panel wiring.

    python3 tests/test_page_actions.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="lpv-actions-"))
DB_PATH = TMP / "lpv.sqlite3"
PAGES_DIR = TMP / "pages"
PAGES_DIR.mkdir(parents=True, exist_ok=True)

import lpv_store  # noqa: E402  (after the temp paths exist)


class StoreCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lpv_store.LPV_DB_PATH = DB_PATH
        lpv_store.LPV_PAGES_DIR = PAGES_DIR
        lpv_store.init_db()

    def make_page(self, name, html="<html><body>hi</body></html>"):
        return lpv_store.save_page(name=name, html=html)


class Actions(unittest.TestCase):
    def test_nothing_is_dropped_silently(self):
        self.assertEqual(lpv_store.normalise_actions(None), [])
        self.assertEqual(lpv_store.normalise_actions("nope"), [])
        self.assertEqual(lpv_store.normalise_actions([None, 3, {}, {"match": ""}]), [])

    def test_page_target(self):
        out = lpv_store.normalise_actions([
            {"match": "text:Sign in", "do": "page", "page_id": "abc", "page_name": "Next"}
        ])
        self.assertEqual(out[0]["match"], "text:Sign in")
        self.assertEqual(out[0]["do"], "page")
        self.assertEqual(out[0]["page_id"], "abc")

    def test_link_target(self):
        out = lpv_store.normalise_actions([{"match": ".help", "do": "link", "url": "https://example.com"}])
        self.assertEqual(out[0]["do"], "link")
        self.assertEqual(out[0]["url"], "https://example.com")
        self.assertFalse(out[0]["new_tab"])

    def test_a_target_is_required(self):
        self.assertEqual(lpv_store.normalise_actions([{"match": "a", "do": "page"}]), [])
        self.assertEqual(lpv_store.normalise_actions([{"match": "a", "do": "link"}]), [])

    def test_aliases_land_on_the_two_real_kinds(self):
        self.assertEqual(lpv_store.normalise_actions([{"match": "a", "do": "goto", "url": "x.com"}])[0]["do"], "link")
        self.assertEqual(lpv_store.normalise_actions([{"match": "a", "do": "workflow", "page_id": "p"}])[0]["do"], "page")
        # Unknown verbs fall back to the page target rather than vanishing.
        self.assertEqual(lpv_store.normalise_actions([{"match": "a", "do": "???", "page_id": "p"}])[0]["do"], "page")

    def test_selector_key_is_accepted(self):
        out = lpv_store.normalise_actions([{"selector": "#next", "page_id": "p"}])
        self.assertEqual(out[0]["match"], "#next")

    def test_rule_and_count_caps(self):
        many = [{"match": "m%d" % i, "page_id": "p"} for i in range(60)]
        self.assertEqual(len(lpv_store.normalise_actions(many)), lpv_store.ACTION_MAX_RULES)
        long_match = lpv_store.normalise_actions([{"match": "x" * 900, "page_id": "p"}])[0]["match"]
        self.assertEqual(len(long_match), lpv_store.ACTION_MAX_MATCH)


class Storage(StoreCase):
    def test_actions_round_trip_through_a_page(self):
        page = self.make_page("actions.html")
        saved = lpv_store.set_page_actions(page["id"], [
            {"match": "text:Continue", "do": "page", "page_id": "p2"},
            {"match": ".next", "do": "link", "url": "https://example.com"},
        ])
        self.assertEqual(len(saved), 2)
        again = lpv_store.get_page(page["id"])
        self.assertEqual(len(again["actions"]), 2)
        self.assertEqual(lpv_store.get_page_actions(page["id"])[0]["do"], "page")

    def test_clearing_actions(self):
        page = self.make_page("clear.html")
        lpv_store.set_page_actions(page["id"], [{"match": "a", "page_id": "p"}])
        self.assertEqual(lpv_store.set_page_actions(page["id"], []), [])
        self.assertEqual(lpv_store.get_page_actions(page["id"]), [])

    def test_missing_page_returns_none(self):
        self.assertIsNone(lpv_store.set_page_actions("nope", [{"match": "a", "page_id": "p"}]))


class ReplaceHtml(StoreCase):
    def test_reupload_keeps_identity_and_settings(self):
        page = self.make_page("invoice.html", "<html><body>v1 Sign</body></html>")
        pid = page["id"]
        lpv_store.set_page_bindings(pid, [{"find": "Sign", "value": "Continue"}])
        lpv_store.set_page_guard(pid, {"enabled": True, "allow": ["text:Continue"]})
        lpv_store.set_page_actions(pid, [{"match": "text:Continue", "page_id": "p2"}])

        updated = lpv_store.replace_page_html(pid, "<html><body>v2 Sign</body></html>")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["id"], pid)
        self.assertEqual(updated["name"], page["name"])
        self.assertEqual(updated["version"], 2)
        self.assertEqual(len(updated["bindings"]), 1, "bindings survive an edit")
        self.assertTrue(updated["guard"]["enabled"], "the guard survives an edit")
        self.assertEqual(len(updated["actions"]), 1, "click actions survive an edit")
        self.assertIn("v2", lpv_store.read_page_html(pid))
        self.assertNotIn("v1", lpv_store.read_page_html(pid))

    def test_previous_version_is_kept(self):
        page = self.make_page("history.html", "<html><body>old</body></html>")
        lpv_store.replace_page_html(page["id"], "<html><body>new</body></html>")
        versions = lpv_store.list_versions(page["id"])
        self.assertTrue(any(v["version"] == 1 for v in versions))
        self.assertIn("old", lpv_store.read_version_html(page["id"], 1))

    def test_empty_or_missing_html_is_refused(self):
        page = self.make_page("empty.html")
        with self.assertRaises(ValueError):
            lpv_store.replace_page_html(page["id"], "")
        with self.assertRaises(ValueError):
            lpv_store.replace_page_html(page["id"], "   \n  ")

    def test_unknown_page_is_refused(self):
        self.assertIsNone(lpv_store.replace_page_html("does-not-exist", "<html></html>"))

    def test_edit_does_not_change_the_name(self):
        page = self.make_page("keep-name.html")
        lpv_store.replace_page_html(page["id"], "<html><body>x</body></html>")
        self.assertEqual(lpv_store.get_page(page["id"])["name"], page["name"])


class WorkflowStep(StoreCase):
    def test_action_step_is_saved(self):
        wf = lpv_store.save_workflow(None, "Flow", "", [
            {"kind": "action", "match": "text:Sign in", "do": "page",
             "page_id": "p2", "page_name": "Next.html", "wait_seconds": 30},
        ])
        step = wf["steps"][0]
        self.assertEqual(step["kind"], "action")
        self.assertEqual(step["match"], "text:Sign in")
        self.assertEqual(step["page_id"], "p2")
        self.assertEqual(step["wait_seconds"], 30)

    def test_action_step_to_a_link(self):
        wf = lpv_store.save_workflow(None, "Flow2", "", [
            {"kind": "action", "match": ".next", "do": "link", "url": "https://example.com"},
        ])
        self.assertEqual(wf["steps"][0]["url"], "https://example.com")
        self.assertEqual(wf["steps"][0]["wait_seconds"], 0)

    def test_a_step_with_no_match_is_rejected(self):
        with self.assertRaises(ValueError):
            lpv_store.save_workflow(None, "Bad", "", [{"kind": "action", "do": "page", "page_id": "p"}])

    def test_a_step_with_no_target_is_rejected(self):
        with self.assertRaises(ValueError):
            lpv_store.save_workflow(None, "Bad2", "", [{"kind": "action", "match": "a", "do": "page"}])


class Migration(unittest.TestCase):
    def test_old_database_gains_the_actions_column(self):
        tmp = Path(tempfile.mkdtemp(prefix="lpv-actions-old-"))
        db = tmp / "old.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE lpv_pages (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
                tags TEXT DEFAULT '[]', file_path TEXT NOT NULL, size_bytes INTEGER DEFAULT 0,
                version INTEGER DEFAULT 1, created_at REAL, updated_at REAL
            );
            INSERT INTO lpv_pages (id, name, file_path) VALUES ('old1', 'old.html', 'x');
            """
        )
        conn.commit()
        conn.close()
        old_db, old_dir = lpv_store.LPV_DB_PATH, lpv_store.LPV_PAGES_DIR
        try:
            lpv_store.LPV_DB_PATH = db
            lpv_store.LPV_PAGES_DIR = tmp
            lpv_store.init_db()
            with lpv_store.get_conn() as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(lpv_pages)").fetchall()}
            self.assertIn("actions", cols)
            self.assertEqual(lpv_store.get_page("old1")["actions"], [])
        finally:
            lpv_store.LPV_DB_PATH, lpv_store.LPV_PAGES_DIR = old_db, old_dir


if __name__ == "__main__":
    unittest.main(verbosity=2)
