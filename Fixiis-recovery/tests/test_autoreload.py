#!/usr/bin/env python3
"""Tests for deploy/autoreload.py — no dependencies: python3 tests/test_autoreload.py

The watcher decides whether a file upload restarts the live service, so the
rules deserve tests: runtime state must never trigger a restart (that would
be an endless restart loop), and a broken upload must never take the service
down.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "deploy"))

import autoreload as ar  # noqa: E402

EXTS = ar.DEFAULT_EXTS


class WatchRules(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ar-root-"))

    def watched(self, rel, extra=()):
        path = self.root / rel
        return ar.is_watched(path, self.root, EXTS, extra)

    def test_source_files_are_watched(self):
        for rel in ("api.py", "client.html", "session/manager.py", ".env",
                    "requirements.txt", "Admin.html"):
            self.assertTrue(self.watched(rel), rel)

    def test_runtime_state_is_ignored(self):
        # These are written by the app while it runs. Watching them would
        # restart the process forever.
        for rel in ("lpv/lpv.sqlite3", "lpv/pages/x.html", "logs/app.log",
                    "data/telegram.json", "profiles/u1/fingerprint.json",
                    "persistent_links.json", "cache/a.bin", "standalone/client.html"):
            self.assertFalse(self.watched(rel), rel)

    def test_dependency_and_build_noise_is_ignored(self):
        for rel in ("__pycache__/api.cpython-311.pyc", ".git/config",
                    "venv/lib/python3.11/site-packages/x.py", "node_modules/a.js",
                    "build/main.py", "coverage/index.html"):
            self.assertFalse(self.watched(rel), rel)

    def test_deploy_and_tests_do_not_restart_the_app(self):
        # Editing the watcher itself or a test must not bounce production.
        self.assertFalse(self.watched("deploy/autoreload.py"))
        self.assertFalse(self.watched("tests/test_api.py"))

    def test_extra_ignore(self):
        self.assertTrue(self.watched("mobile_devices.json"))
        self.assertFalse(self.watched("mobile_devices.json", extra=("mobile_devices.json",)))
        self.assertFalse(self.watched("plugins/x.py", extra=("plugins",)))

    def test_extra_extensions(self):
        self.assertFalse(self.watched("app.js"))
        self.assertTrue(ar.is_watched(self.root / "app.js", self.root, EXTS + (".js",)))
        # ...but not for binary payloads even then
        self.assertFalse(ar.is_watched(self.root / "icon.png", self.root, EXTS + (".png",)))


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ar-snap-"))

    def write(self, rel, text="x", mtime=None):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_detects_change_add_and_remove(self):
        self.write("api.py", "a")
        self.write("lpv/state.json", "ignored")
        first = ar.snapshot(self.root, EXTS)
        self.assertEqual(list(first), ["api.py"])

        self.write("api.py", "ab", mtime=time.time() + 5)
        self.write("new.py", "n")
        os.remove(self.root / "api.py")  # remove the changed one
        third = ar.snapshot(self.root, EXTS)

        changed, added, removed = ar.diff_snapshots(first, third)
        self.assertEqual(added, ["new.py"])
        self.assertEqual(removed, ["api.py"])
        self.assertEqual(changed, [])

    def test_identical_snapshot_has_no_diff(self):
        self.write("api.py", "same")
        a = ar.snapshot(self.root, EXTS)
        b = ar.snapshot(self.root, EXTS)
        self.assertEqual(ar.diff_snapshots(a, b), ([], [], []))


class SyntaxGate(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ar-syn-"))

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_valid_python_passes(self):
        self.write("ok.py", "def f():\n    return 1\n")
        self.assertEqual(ar.syntax_errors(self.root, ["ok.py"]), [])

    def test_broken_python_is_reported(self):
        self.write("bad.py", "def f(:\n    pass\n")
        problems = ar.syntax_errors(self.root, ["bad.py"])
        self.assertEqual(len(problems), 1)
        self.assertIn("bad.py", problems[0][0])

    def test_non_python_files_are_skipped(self):
        self.write("page.html", "<html><body><div></body>")
        self.assertEqual(ar.syntax_errors(self.root, ["page.html"]), [])

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(ar.syntax_errors(self.root, ["gone.py"]), [])


class Debounce(unittest.TestCase):
    def test_waits_for_the_quiet_window(self):
        self.assertFalse(ar.should_restart(100, None, 0, 3, 10))
        self.assertFalse(ar.should_restart(101, 100, 0, 3, 10))
        self.assertTrue(ar.should_restart(103.1, 100, 0, 3, 10))

    def test_respects_minimum_gap(self):
        self.assertFalse(ar.should_restart(104, 100, 100, 3, 10))
        self.assertTrue(ar.should_restart(110.1, 100, 100, 3, 10))


class EndToEnd(unittest.TestCase):
    """Run the real CLI in --once mode against a temp directory."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ar-e2e-"))
        (self.root / "api.py").write_text("print('v1')\n", encoding="utf-8")
        self.marker = self.root / "restarted.txt"
        self.state = self.root / "snapshot.json"

    def run_cli(self, *extra):
        cmd = [
            sys.executable, str(HERE.parent / "deploy" / "autoreload.py"),
            "--dir", str(self.root),
            "--restart-cmd", f"sh -c 'echo ran >> {self.marker}'",
            "--state-file", str(self.state),
            "--debounce", "0", "--settle", "0", "--once", "--quiet",
            *extra,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    def test_change_triggers_restart(self):
        # First pass records the baseline; nothing to do.
        self.run_cli()
        self.assertFalse(self.marker.exists(), "no change should mean no restart")

        (self.root / "api.py").write_text("print('v2')\n", encoding="utf-8")
        os.utime(self.root / "api.py", (time.time() + 5, time.time() + 5))
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.marker.exists(), "a changed .py must restart the service")

    def test_broken_upload_does_not_restart(self):
        self.run_cli()
        bad = self.root / "api.py"
        bad.write_text("def broken(:\n    pass\n", encoding="utf-8")
        os.utime(bad, (time.time() + 5, time.time() + 5))
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.marker.exists(), "must not restart into broken code")
        self.assertIn("does not compile", proc.stderr)

    def test_runtime_state_never_restarts(self):
        self.run_cli()
        state = self.root / "lpv" / "state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"a": 1}', encoding="utf-8")
        (self.root / "logs").mkdir(exist_ok=True)
        (self.root / "logs" / "app.log").write_text("line", encoding="utf-8")
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.marker.exists(), "runtime writes must not restart the app")

    def test_dry_run_never_executes(self):
        self.run_cli()
        (self.root / "api.py").write_text("print('v3')\n", encoding="utf-8")
        os.utime(self.root / "api.py", (time.time() + 9, time.time() + 9))
        proc = self.run_cli("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.marker.exists(), "dry-run must not run the restart command")


if __name__ == "__main__":
    failed = unittest.main(exit=False, verbosity=1).result
    total = failed.testsRun
    bad = len(failed.failures) + len(failed.errors)
    print(f"{total - bad} checks passed" if not bad else f"{bad} failed")
    sys.exit(1 if bad else 0)
