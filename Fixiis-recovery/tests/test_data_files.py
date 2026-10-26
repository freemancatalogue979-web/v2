#!/usr/bin/env python3
"""Runtime state belongs in data/, not beside the code.

What was wrong:

  * ``persistent_links.json`` was written to the *working directory* (a bare
    relative name), so starting the app from anywhere else silently forgot
    every generated link — and starting it from the repo root left a .json
    next to api.py that came back as an untracked file after every run;
  * the other state files (Telegram config, server settings, remembered
    sessions, remembered profiles) each hard-coded ``Path(__file__).parent /
    "data"`` in their own module, so "where does state live" had four answers;
  * an install that predates any of that keeps its files at the root, and a
    folder move must not lose them.

Now there is one answer — ``config.DATA_DIR`` / ``config.data_path()`` — and
one migration that moves the old root files in on boot.

    python3 tests/test_data_files.py
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

try:
    import api  # noqa: E402
    import server_settings  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - bare checkout
    print("data files: skipped (%s not installed)" % exc.name)
    raise SystemExit(0)


class LocationTest(unittest.TestCase):
    def test_the_data_dir_is_one_absolute_folder(self):
        self.assertTrue(config.DATA_DIR.is_absolute())
        self.assertEqual(config.DATA_DIR.name, "data")
        self.assertTrue(config.DATA_DIR.is_dir(), "the folder is created at import")

    def test_only_the_file_name_is_used(self):
        self.assertEqual(config.data_path("persistent_links.json"),
                         config.DATA_DIR / "persistent_links.json")
        # A path that tries to climb out stays inside data/.
        escaped = config.data_path("../../etc/passwd")
        self.assertEqual(escaped.parent, config.DATA_DIR, "path traversal escaped data/")
        self.assertEqual(escaped.name, "passwd")
        self.assertEqual(config.data_path("sub/dir/links.json"), config.DATA_DIR / "links.json")

    def test_a_nameless_file_is_refused(self):
        for bad in ("", "   ", "."):
            with self.assertRaises(ValueError):
                config.data_path(bad)

    def test_the_links_file_is_in_data_not_the_repo_root(self):
        links = Path(api.LINKS_FILE)
        self.assertTrue(links.is_absolute(), "a relative path follows the working directory")
        self.assertEqual(links.parent, config.DATA_DIR)
        self.assertNotEqual(links.parent, ROOT)
        self.assertEqual(links.name, "persistent_links.json")

    def test_every_state_file_shares_the_one_folder(self):
        for path in (api.CONFIG_FILE, api.SERVER_SESSIONS_FILE,
                     api._profiles_persistence_path,
                     Path(server_settings.settings_file_path())):
            self.assertEqual(Path(path).parent, config.DATA_DIR, str(path))

    def test_the_folder_can_be_moved_for_a_container(self):
        # NEO_DATA_DIR is read at import in a fresh process.
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run(
                [sys.executable, "-c",
                 "import config; print(config.DATA_DIR); print(config.data_path('x.json'))"],
                cwd=str(ROOT), capture_output=True, text=True,
                env=dict(os.environ, NEO_DATA_DIR=tmp, PYTHONPATH=str(ROOT)),
            )
            self.assertEqual(out.returncode, 0, out.stderr[-800:])
            lines = out.stdout.strip().splitlines()
            self.assertEqual(Path(lines[0]), Path(tmp))
            self.assertEqual(Path(lines[1]), Path(tmp) / "x.json")


class LinksRoundTripTest(unittest.TestCase):
    def setUp(self):
        self._saved_file = api.LINKS_FILE
        self._saved_links = dict(api.generated_links)
        self._tmp = tempfile.TemporaryDirectory()
        api.LINKS_FILE = str(Path(self._tmp.name) / "nested" / "persistent_links.json")

    def tearDown(self):
        api.LINKS_FILE = self._saved_file
        api.generated_links.clear()
        api.generated_links.update(self._saved_links)
        self._tmp.cleanup()

    def test_save_creates_the_folder_and_load_reads_it_back(self):
        api.generated_links.clear()
        api.generated_links["abc123"] = {"target": "example.com", "workflow_id": "wf-1"}
        api.save_persistent_links()
        path = Path(api.LINKS_FILE)
        self.assertTrue(path.is_file(), "save did not write the file")
        self.assertEqual(json.loads(path.read_text())["abc123"]["workflow_id"], "wf-1")

        api.generated_links.clear()
        api.load_persistent_links()
        self.assertIn("abc123", api.generated_links, "load did not read the file back")

    def test_saving_never_touches_the_repo_root(self):
        api.generated_links.clear()
        api.generated_links["x"] = {"target": "example.com"}
        api.save_persistent_links()
        self.assertFalse((ROOT / "persistent_links.json").exists(),
                         "a link file reappeared at the repo root")


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def test_root_files_are_moved_into_data(self):
        (self.root / "persistent_links.json").write_text(
            json.dumps({"a": {"target": "one.example"}}), encoding="utf-8")
        (self.root / "telegram_config.json").write_text('{"chat_id": "42"}', encoding="utf-8")

        moved = config.migrate_legacy_state_files(self.root, self.data)

        self.assertEqual(sorted(moved), ["persistent_links.json", "telegram_config.json"])
        self.assertFalse((self.root / "persistent_links.json").exists(), "the old copy is left behind")
        payload = json.loads((self.data / "persistent_links.json").read_text())
        self.assertEqual(payload["a"]["target"], "one.example", "the content must survive the move")
        self.assertEqual(json.loads((self.data / "telegram_config.json").read_text())["chat_id"], "42")

    def test_it_is_a_no_op_the_second_time(self):
        (self.root / "server_settings.json").write_text("{}", encoding="utf-8")
        self.assertEqual(config.migrate_legacy_state_files(self.root, self.data),
                         ["server_settings.json"])
        self.assertEqual(config.migrate_legacy_state_files(self.root, self.data), [])
        self.assertEqual(config.migrate_legacy_state_files(self.root, self.data), [])

    def test_a_clean_tree_is_untouched(self):
        (self.root / "api.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(config.migrate_legacy_state_files(self.root, self.data), [])
        self.assertTrue((self.root / "api.py").exists(), "source files are never moved")

    def test_the_data_copy_wins_when_both_exist(self):
        (self.root / "persistent_links.json").write_text('{"old": {}}', encoding="utf-8")
        self.data.mkdir()
        (self.data / "persistent_links.json").write_text('{"new": {}}', encoding="utf-8")

        moved = config.migrate_legacy_state_files(self.root, self.data)

        self.assertEqual(moved, [], "nothing to move: the data copy already exists")
        self.assertEqual(json.loads((self.data / "persistent_links.json").read_text()),
                         {"new": {}}, "the live copy must not be overwritten")
        self.assertTrue((self.root / "persistent_links.json").exists(),
                        "nothing is deleted on the operator's behalf")

    def test_a_read_only_root_does_not_break_boot(self):
        # A folder we cannot write to must not raise out of config import.
        self.data.mkdir()
        (self.root / "persistent_links.json").write_text("{}", encoding="utf-8")
        self.assertIsInstance(config.migrate_legacy_state_files(self.root, self.data), list)


class RepoHygieneTest(unittest.TestCase):
    def test_the_state_list_is_the_known_one(self):
        self.assertEqual(set(config.LEGACY_ROOT_STATE_FILES),
                         {"persistent_links.json", "telegram_config.json",
                          "server_settings.json", "server_sessions.json",
                          "client_profiles.json"})

    def test_gitignore_covers_data_and_the_old_root_names(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/", ignore)
        for name in config.LEGACY_ROOT_STATE_FILES:
            self.assertIn("/" + name, ignore, name + " is not ignored at the root")

    def test_the_restart_watcher_still_ignores_them(self):
        import autoupdate
        self.assertIn("data", autoupdate.IGNORED_DIRS)
        self.assertIn("persistent_links.json", autoupdate.IGNORED_FILES)

    def test_nothing_else_hardcodes_a_data_path(self):
        # One helper, not four literals: a new state file must go through it.
        for name in ("api.py", "server_settings.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn('parent / "data"', src, name + " still builds a data path by hand")

    def test_the_docs_say_where_state_lives(self):
        deployment = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
        table = deployment[deployment.index("## Where data lives"):
                           deployment.index("## Operations")]
        self.assertIn("`data/`", table)
        self.assertIn("links", table.lower(), "the links file is not mentioned under data/")


if __name__ == "__main__":
    unittest.main(verbosity=1)
