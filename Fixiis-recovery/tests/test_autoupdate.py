#!/usr/bin/env python3
"""Tests for autoupdate.py + the `auto_update` switch in config.py.

No dependencies: python3 tests/test_autoupdate.py

The watcher decides whether an upload restarts the live service, so the rules
deserve tests: runtime state must never trigger a restart (that would be an
endless loop), a broken upload must never take the service down, and the
switch must be OFF unless config.py says otherwise.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import autoupdate as au  # noqa: E402

EXTS = au.DEFAULT_EXTS


class WatchRules(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="au-root-"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def watched(self, rel, extra=()):
        path = self.root / rel
        return au.is_watched(path, self.root, EXTS, extra)

    def test_source_files_are_watched(self):
        for rel in ("api.py", "client.html", "package/module.py", ".env",
                    "requirements.txt", "Admin.html"):
            self.assertTrue(self.watched(rel), rel)

    def test_runtime_state_is_ignored(self):
        for rel in ("lpv/pages/1.html", "logs/app.log", "data/links.json",
                    "profiles/u1/Cookies", "cache/x.html", "mobile-shots/a.png",
                    "lpv.sqlite3", "lpv.sqlite3-wal", "persistent_links.json",
                    "__pycache__/api.cpython-311.pyc", ".env.example"):
            self.assertFalse(self.watched(rel), rel)

    def test_tests_and_deploy_do_not_restart_production(self):
        self.assertFalse(self.watched("tests/test_api.py"))
        self.assertFalse(self.watched("deploy/setup.sh"))

    def test_venv_and_git_are_ignored(self):
        self.assertFalse(self.watched("venv/lib/x.py"))
        self.assertFalse(self.watched(".git/hooks/x.py"))

    def test_plain_state_files_are_ignored(self):
        for rel in ("state.json", "notes.txt", "photo.png", "archive.zip"):
            self.assertFalse(self.watched(rel), rel)

    def test_snapshot_only_contains_watched_files(self):
        (self.root / "api.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "lpv").mkdir()
        (self.root / "lpv" / "page.html").write_text("<html>", encoding="utf-8")
        (self.root / "logs").mkdir()
        (self.root / "logs" / "a.log").write_text("line", encoding="utf-8")
        snap = au.snapshot(self.root, EXTS)
        self.assertEqual(list(snap), ["api.py"])

    def test_snapshot_ignores_extra_names(self):
        (self.root / "keep.py").write_text("", encoding="utf-8")
        (self.root / "skip.py").write_text("", encoding="utf-8")
        snap = au.snapshot(self.root, EXTS, ("skip.py",))
        self.assertEqual(list(snap), ["keep.py"])


class DiffAndGates(unittest.TestCase):
    def test_diff_reports_changed_added_removed(self):
        old = {"a.py": (1, 10), "b.py": (2, 20), "gone.py": (3, 30)}
        new = {"a.py": (1, 10), "b.py": (9, 20), "new.py": (4, 40)}
        self.assertEqual(au.diff_snapshots(old, new),
                         (["b.py"], ["new.py"], ["gone.py"]))

    def test_no_change_means_no_restart(self):
        snap = {"a.py": (1, 10)}
        self.assertEqual(au.diff_snapshots(snap, snap), ([], [], []))

    def test_debounce_needs_a_quiet_period(self):
        self.assertFalse(au.should_restart(100.0, None, 0, 3, 10))
        self.assertFalse(au.should_restart(101.0, 100.0, 0, 3, 10))   # 1s quiet
        self.assertTrue(au.should_restart(104.0, 100.0, 0, 3, 10))    # 4s quiet

    def test_min_interval_prevents_a_restart_storm(self):
        self.assertFalse(au.should_restart(105.0, 100.0, 100.0, 3, 10))
        self.assertTrue(au.should_restart(115.0, 100.0, 100.0, 3, 10))

    def test_syntax_gate_flags_a_broken_python_file(self):
        root = Path(tempfile.mkdtemp(prefix="au-syn-"))
        (root / "good.py").write_text("x = 1\n", encoding="utf-8")
        (root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
        self.assertEqual(au.syntax_errors(root, ["good.py"]), [])
        problems = au.syntax_errors(root, ["bad.py"])
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0][0], "bad.py")
        self.assertIn("SyntaxError", problems[0][1])

    def test_syntax_gate_ignores_non_python(self):
        root = Path(tempfile.mkdtemp(prefix="au-syn2-"))
        (root / "page.html").write_text("<div>", encoding="utf-8")
        self.assertEqual(au.syntax_errors(root, ["page.html"]), [])


class WatcherThread(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="au-thread-"))
        self.seen = []
        self.updater = au.AutoUpdater(
            self.root,
            on_change=lambda files: self.seen.append(files),
            interval=0.2, debounce=0.3, min_interval=0.0, budget=0,
            log=lambda msg: None,
        )

    def tearDown(self):
        self.updater.stop()
        self.updater.join(timeout=5)
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def _wait(self, seconds=6):
        deadline = time.time() + seconds
        while time.time() < deadline and not self.seen:
            time.sleep(0.05)

    def test_change_triggers_the_callback(self):
        self.updater.start()
        time.sleep(0.5)
        (self.root / "api.py").write_text("x = 2\n", encoding="utf-8")
        self._wait()
        self.assertTrue(self.seen, "a changed .py must trigger a restart")
        self.assertIn("api.py", self.seen[0])

    def test_runtime_writes_never_trigger(self):
        self.updater.start()
        time.sleep(0.5)
        (self.root / "lpv").mkdir()
        (self.root / "lpv" / "page.html").write_text("<html>", encoding="utf-8")
        (self.root / "logs").mkdir()
        (self.root / "logs" / "app.log").write_text("line", encoding="utf-8")
        time.sleep(1.5)
        self.assertEqual(self.seen, [], "runtime writes must not restart the app")

    def test_broken_upload_does_not_restart(self):
        self.updater.start()
        time.sleep(0.5)
        (self.root / "broken.py").write_text("def nope(:\n", encoding="utf-8")
        time.sleep(1.5)
        self.assertEqual(self.seen, [], "a file that does not compile must not be loaded")

    def test_stop_is_honoured(self):
        self.updater.start()
        self.updater.stop()
        self.updater.join(timeout=3)
        self.assertFalse(self.updater.is_alive())


class TheSwitch(unittest.TestCase):
    """config.py holds the only switch, and it is off unless set."""

    def test_default_is_off(self):
        code = ("import sys, json; sys.path.insert(0, %r); import config; "
                "print(json.dumps(config.CONFIG.auto_update))" % str(ROOT))
        env = {k: v for k, v in os.environ.items() if k != "AUTO_UPDATE"}
        done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=120, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "false")

    def test_flag_in_config_py_turns_it_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = (ROOT / "config.py").read_text(encoding="utf-8")
            (root / "config.py").write_text(
                source.replace("auto_update: bool = False",
                               "auto_update: bool = True", 1),
                encoding="utf-8",
            )
            code = ("import sys, json; sys.path.insert(0, %r); import config; "
                    "print(json.dumps(config.CONFIG.auto_update))" % str(root))
            env = {k: v for k, v in os.environ.items() if k != "AUTO_UPDATE"}
            done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                  text=True, timeout=120, env=env, cwd=str(root))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), "true")

    def test_health_reports_the_switch(self):
        source = (ROOT / "api.py").read_text(encoding="utf-8")
        self.assertIn('"auto_update": _auto_update_enabled()', source)
        self.assertIn("def _auto_update_enabled()", source)

    def test_no_watcher_unit_or_script_is_shipped(self):
        self.assertFalse((ROOT / "deploy" / "autoreload.py").exists())
        self.assertFalse((ROOT / "deploy" / "autoreload.sh").exists())
        self.assertFalse((ROOT / "deploy" / "fixiis-reloader.service").exists())

    def test_setup_does_not_install_a_watcher(self):
        setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
        self.assertNotIn("systemctl enable --now fixiis-reloader", setup)
        self.assertIn("auto_update", setup)

    def test_check_reports_the_switch_and_its_source(self):
        env = {k: v for k, v in os.environ.items() if k != "AUTO_UPDATE"}
        off = subprocess.run([sys.executable, str(ROOT / "main.py"), "--check"],
                             capture_output=True, text=True, timeout=180, env=env)
        self.assertIn("auto update (from config.py)", off.stdout)
        self.assertIn("off - restart yourself after changes", off.stdout)

        # The environment can override config.py, and --check says so.
        on = subprocess.run([sys.executable, str(ROOT / "main.py"), "--check"],
                            capture_output=True, text=True, timeout=180,
                            env={**env, "AUTO_UPDATE": "1"})
        self.assertIn("auto update (from environment)", on.stdout)
        self.assertIn("ON - a file change restarts the app", on.stdout)

    def test_main_starts_the_watcher_only_from_the_config_flag(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('getattr(self.config, "auto_update", False)', main)
        self.assertIn("import autoupdate", main)


if __name__ == "__main__":
    failed = unittest.main(exit=False, verbosity=1).result
    total = failed.testsRun
    bad = len(failed.failures) + len(failed.errors)
    print(f"{total - bad} checks passed" if not bad else f"{bad} failed")
    sys.exit(1 if bad else 0)
