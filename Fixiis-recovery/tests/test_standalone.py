#!/usr/bin/env python3
"""The standalone folder people upload to a host.

`deploy/make-standalone.sh` copies the three front-ends and injects one line
saying where the backend is. Two things have to hold, because these files are
shipped to a host and opened by visitors:

  * the copy is the SAME page as the source, plus that one line — a broken
    copy must never be uploadable
  * "no address" really means no address, so the folder can be published

It also pins the committed `standalone/client.html` to the current
`client.html`: that file went stale once and shipped an old client page to
visitors (missing the startup fixes), which is invisible until somebody
complains that the panel is slow.

    python3 tests/test_standalone.py
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "make-standalone.sh"
TAG = '<script>window.SERVER_ENDPOINT="%s";</script>'
PANELS = {
    "mini-admin.html": 'id="stChatMode"',
    "Admin.html": 'id="lpv-archive-list"',
    "client.html": 'id="loadingScreen"',
}


def run(outdir: Path, server: str = "--none"):
    return subprocess.run(
        ["bash", str(SCRIPT), server, str(outdir)],
        capture_output=True, text=True,
    )


class BuildCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="standalone-"))
        cls.out = cls.tmp / "out"
        cls.result = run(cls.out)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_build_succeeds(self):
        self.assertEqual(self.result.returncode, 0, self.result.stderr)

    def test_every_file_is_there(self):
        for name in list(PANELS) + ["index.html", "README.txt", "BUILD.txt"]:
            path = self.out / name
            self.assertTrue(path.exists(), name + " was not generated")
            self.assertGreater(path.stat().st_size, 0, name + " is empty")

    def test_a_copy_is_the_source_plus_one_line(self):
        for name in PANELS:
            source = (ROOT / name).read_text(encoding="utf-8")
            copied = (self.out / name).read_text(encoding="utf-8")
            head_end = source.index(">", source.lower().index("<head")) + 1
            expected = source[:head_end] + "\n" + (TAG % "") + source[head_end:]
            self.assertEqual(copied, expected, name + " is not a clean copy of the source")

    def test_exactly_one_address_assignment_inside_head(self):
        for name in PANELS:
            text = (self.out / name).read_text(encoding="utf-8")
            self.assertEqual(len(re.findall(r"window\.SERVER_ENDPOINT\s*=", text)), 1,
                             name + " has the wrong number of address assignments")
            self.assertLess(text.lower().index("<script>window.server_endpoint"),
                            text.lower().index("</head>"),
                            name + " injects outside <head>")

    def test_no_address_is_published_by_default(self):
        for name in PANELS:
            text = (self.out / name).read_text(encoding="utf-8")
            self.assertIn('window.SERVER_ENDPOINT="";', text, name + " carries an address")
            # No backend URL may be baked in anywhere: only documentation
            # addresses (192.0.2.x and friends) appear in the panels' own help
            # text, and the copy must not add one.
            hosts = [
                host for host in re.findall(r"https?://(\d{1,3}(?:\.\d{1,3}){3})", text)
                if not host.startswith(("192.0.2.", "198.51.100.", "203.0.113.",
                                        "127.0.0.", "0.0.0.0"))
            ]
            self.assertEqual(hosts, [], name + " points at a real server address")

    def test_the_folder_can_be_opened_as_a_site(self):
        index = (self.out / "index.html").read_text(encoding="utf-8")
        for name in PANELS:
            self.assertIn('href="%s"' % name, index, "index.html does not link " + name)
        self.assertIsNone(re.search(r'(?:src|href)="https?://', index),
                          "the menu must not depend on another host")
        self.assertNotIn("<script", index.lower(), "the menu needs no JavaScript")

    def test_build_stamp_matches_the_files(self):
        stamp = (self.out / "BUILD.txt").read_text(encoding="utf-8")
        for name in PANELS:
            digest = hashlib.sha1((self.out / name).read_bytes()).hexdigest()[:12]
            self.assertIn(digest, stamp, "BUILD.txt does not describe " + name)
            self.assertIn(name, stamp)
        self.assertIn("no address", stamp.lower().replace("<none - resolved at runtime>", "no address"))

    def test_readme_says_how_to_point_it_and_update_it(self):
        text = (self.out / "README.txt").read_text(encoding="utf-8")
        self.assertIn("?server=", text)
        self.assertIn("make-standalone.sh", text)
        self.assertIn("mixed content", text.lower())


class BakedCase(unittest.TestCase):
    ADDRESS = "http://192.0.2.10:8446"          # RFC 5737 documentation address

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="standalone-baked-"))
        self.out = self.tmp / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_baked_address_lands_in_every_copy(self):
        result = run(self.out, self.ADDRESS)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in PANELS:
            text = (self.out / name).read_text(encoding="utf-8")
            self.assertIn('window.SERVER_ENDPOINT="%s";' % self.ADDRESS, text,
                          name + " did not get the address")
            self.assertEqual(len(re.findall(r"window\.SERVER_ENDPOINT\s*=", text)), 1)

    def test_the_baked_copy_is_otherwise_identical(self):
        run(self.out, self.ADDRESS)
        for name in PANELS:
            baked = (self.out / name).read_text(encoding="utf-8")
            plain = baked.replace('window.SERVER_ENDPOINT="%s";' % self.ADDRESS,
                                  'window.SERVER_ENDPOINT="";')
            source = (ROOT / name).read_text(encoding="utf-8")
            head_end = source.index(">", source.lower().index("<head")) + 1
            self.assertEqual(plain, source[:head_end] + "\n" + (TAG % "") + source[head_end:],
                             name + " changed more than the address")

    def test_a_bare_host_gets_a_scheme(self):
        result = run(self.out, "192.0.2.10:8446")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('window.SERVER_ENDPOINT="http://192.0.2.10:8446";',
                      (self.out / "Admin.html").read_text(encoding="utf-8"))


class GuardCase(unittest.TestCase):
    """A build that would ship the wrong page must fail, not warn."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="standalone-guard-"))
        self.repo = self.tmp / "repo"
        (self.repo / "deploy").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.repo / "deploy" / "make-standalone.sh")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, body):
        (self.repo / name).write_text(body, encoding="utf-8")

    def build(self):
        return subprocess.run(
            ["bash", str(self.repo / "deploy" / "make-standalone.sh"), "--none",
             str(self.tmp / "out")],
            capture_output=True, text=True,
        )

    def test_a_panel_that_lost_its_feature_fails_the_build(self):
        # Every source present but Admin.html is not really Admin.html: the
        # generated copy must not be reported as ok.
        self.write("mini-admin.html", '<html><head></head><body id="stChatMode"></body></html>')
        self.write("Admin.html", "<html><head></head><body>nothing here</body></html>")
        self.write("client.html", '<html><head></head><body id="loadingScreen"></body></html>')
        result = self.build()
        self.assertNotEqual(result.returncode, 0, "a broken copy was reported as ok")
        self.assertIn("marker", result.stderr + result.stdout)

    def test_a_missing_source_is_skipped_not_faked(self):
        self.write("mini-admin.html", '<html><head></head><body id="stChatMode"></body></html>')
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.tmp / "out" / "Admin.html").exists(),
                         "a file was generated from nothing")
        self.assertIn("skip", result.stdout)

    def test_a_page_without_head_is_refused(self):
        self.write("mini-admin.html", '<html><head></head><body id="stChatMode"></body></html>')
        self.write("Admin.html", "<html><body>no head at all</body></html>")
        self.write("client.html", '<html><head></head><body id="loadingScreen"></body></html>')
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no <head>", result.stderr + result.stdout)


class CommittedCopyCase(unittest.TestCase):
    """standalone/client.html is tracked, so it must never lag the source."""

    def test_the_committed_client_page_is_current(self):
        committed = ROOT / "standalone" / "client.html"
        if not committed.exists():
            self.skipTest("standalone/client.html is not in this checkout")
        source = (ROOT / "client.html").read_text(encoding="utf-8")
        head_end = source.index(">", source.lower().index("<head")) + 1
        expected = source[:head_end] + "\n" + (TAG % "") + source[head_end:]
        self.assertEqual(
            committed.read_text(encoding="utf-8"), expected,
            "standalone/client.html is out of date — run ./deploy/make-standalone.sh --none",
        )

    def test_the_committed_client_page_carries_no_address(self):
        # It is published with the repository, so it must stay the "no
        # address" variant: a baked copy would put somebody's server in a
        # commit, which the project forbids everywhere else too.
        committed = ROOT / "standalone" / "client.html"
        if not committed.exists():
            self.skipTest("standalone/client.html is not in this checkout")
        text = committed.read_text(encoding="utf-8")
        self.assertIn('window.SERVER_ENDPOINT="";', text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
