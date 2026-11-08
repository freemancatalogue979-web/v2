#!/usr/bin/env python3
"""Tests for page_bindings.py — the page rewrite engine.

An operator binds something on a page (a word, an element, a regex) to a
name. At push time the panel asks for that name's value and the visitor gets
a copy of the page with the value in it; the stored original never changes.

Offline only, no dependencies:

    python3 tests/test_page_bindings.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import page_bindings as pb  # noqa: E402

PAGE = """<!doctype html><html><body>
<div id="card"><span class="amount">NGN 0.00</span></div>
<p class="note">Contact me@me.com or me@me.com</p>
<span class="amount">NGN 0.00</span>
</body></html>"""


class Normalise(unittest.TestCase):
    def test_text_is_the_default_matcher(self):
        b = pb.normalise_binding({"find": "hello", "replace": "bye"})
        self.assertEqual(b["match"], "text")
        self.assertEqual(b["value"], "bye")

    def test_name_is_sanitised_and_kept(self):
        b = pb.normalise_binding({"name": "  amount due! ", "find": "x"})
        self.assertEqual(b["name"], "amount due")

    def test_legacy_rows_still_load(self):
        # Rows saved before names/matchers existed: find -> replace.
        out = pb.normalise_bindings([{"find": "a", "replace": "b"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "")
        self.assertEqual(out[0]["match"], "text")

    def test_bad_regex_is_reported_not_crashed(self):
        with self.assertRaises(ValueError):
            pb.normalise_binding({"find": "(", "match": "regex"})

    def test_unknown_matcher_falls_back_to_text(self):
        self.assertEqual(pb.normalise_binding({"find": "a", "match": "junk"})["match"], "text")

    def test_rule_and_size_caps(self):
        many = [{"find": "x"}] * (pb.MAX_BINDINGS + 5)
        self.assertEqual(len(pb.normalise_bindings(many)), pb.MAX_BINDINGS)
        long = pb.normalise_binding({"find": "y" * 9000})
        self.assertEqual(len(long["find"]), pb.MAX_MATCH)

    def test_garbage_input_is_dropped(self):
        self.assertEqual(pb.normalise_bindings(None), [])
        self.assertEqual(pb.normalise_bindings(["nope", 3, {}, {"find": ""}]), [])


class Fields(unittest.TestCase):
    def test_every_binding_is_a_question(self):
        # Nothing is a "universal rewrite": both the named and the plain
        # find->replace row are asked for at push time.
        bindings = pb.normalise_bindings([
            {"name": "amount", "find": "NGN 0.00", "value": "NGN 1.00"},
            {"find": "me@me.com", "value": "you@you.com"},
        ])
        fields = pb.fields_for(bindings)
        self.assertEqual([f["name"] for f in fields], ["amount", "me@me.com"])
        self.assertEqual(fields[0]["default"], "NGN 1.00")
        self.assertEqual(fields[1]["default"], "you@you.com")

    def test_unnamed_binding_is_labelled_with_what_it_binds(self):
        bindings = pb.normalise_bindings([
            {"find": "Sign"},                                       # no name, no value
            {"find": "#ref", "match": "selector"},
            {"find": r"\d+", "match": "regex"},
        ])
        fields = pb.fields_for(bindings)
        self.assertEqual([f["name"] for f in fields], ["Sign", "#ref", "\d+"])
        self.assertEqual([f["label"] for f in fields], ["Sign", "#ref", "\d+"])
        self.assertEqual([f["match"] for f in fields], ["text", "selector", "regex"])
        self.assertEqual([f["default"] for f in fields], ["", "", ""])

    def test_long_find_is_shortened_for_display_only(self):
        long_text = "x" * 200
        fields = pb.fields_for([{"find": long_text}])
        self.assertEqual(fields[0]["name"], long_text)            # the key is whole
        self.assertLessEqual(len(fields[0]["label"]), 48)

    def test_duplicate_names_only_ask_once(self):
        bindings = pb.normalise_bindings([
            {"name": "amount", "find": "a"}, {"name": "amount", "find": "b"},
        ])
        self.assertEqual(len(pb.fields_for(bindings)), 1)


class Apply(unittest.TestCase):
    def test_text_replaces_every_occurrence(self):
        html, report = pb.apply_bindings(PAGE, [{"find": "me@me.com", "value": "you@you.com"}])
        self.assertNotIn("me@me.com", html)
        self.assertEqual(html.count("you@you.com"), 2)
        self.assertEqual(report["applied"][0]["count"], 2)

    def test_selector_replaces_element_text(self):
        html, report = pb.apply_bindings(
            PAGE, [{"find": ".amount", "match": "selector", "value": "NGN 50,000"}])
        self.assertEqual(html.count("NGN 50,000"), 2)
        self.assertNotIn("NGN 0.00", html)
        self.assertEqual(report["applied"][0]["count"], 2)

    def test_descendant_selector(self):
        html, _ = pb.apply_bindings(
            PAGE, [{"find": "#card .amount", "match": "selector", "value": "X"}])
        self.assertEqual(html.count("X"), 1)
        self.assertEqual(html.count("NGN 0.00"), 1)      # the second one survives

    def test_regex(self):
        html, _ = pb.apply_bindings(
            PAGE, [{"find": r"NGN [\d,]+\.\d\d", "match": "regex", "value": "NGN 7.00"}])
        self.assertEqual(html.count("NGN 7.00"), 2)

    def test_unnamed_binding_takes_a_value_per_visitor(self):
        # The point of the feature: same page, two people, different words.
        bindings = pb.normalise_bindings([{"find": "Sign"}])
        one, _ = pb.apply_bindings("<a>Sign</a>", bindings, {"Sign": "Continue"})
        two, _ = pb.apply_bindings("<a>Sign</a>", bindings, {"Sign": "Unlock"})
        self.assertEqual(one, "<a>Continue</a>")
        self.assertEqual(two, "<a>Unlock</a>")

    def test_push_time_value_beats_saved_default(self):
        b = pb.normalise_bindings([{"name": "amount", "find": "NGN 0.00", "value": "NGN 1.00"}])
        html, report = pb.apply_bindings(PAGE, b, {"amount": "NGN 99.00"})
        self.assertEqual(html.count("NGN 99.00"), 2)
        self.assertTrue(report["applied"][0]["from_value"])

    def test_saved_default_used_when_nothing_is_typed(self):
        b = pb.normalise_bindings([{"name": "amount", "find": "NGN 0.00", "value": "NGN 1.00"}])
        html, report = pb.apply_bindings(PAGE, b, {})
        self.assertEqual(html.count("NGN 1.00"), 2)
        self.assertFalse(report["applied"][0]["from_value"])

    def test_empty_value_leaves_the_page_alone(self):
        b = pb.normalise_bindings([{"name": "amount", "find": "NGN 0.00"}])
        html, report = pb.apply_bindings(PAGE, b, {"amount": ""})
        self.assertEqual(html, PAGE)
        self.assertEqual(report["skipped"][0]["why"], "empty value")

    def test_html_is_escaped_unless_told_otherwise(self):
        html, _ = pb.apply_bindings(PAGE, [{"find": "NGN 0.00", "value": "<b>1</b>"}])
        self.assertIn("&lt;b&gt;1&lt;/b&gt;", html)
        html2, _ = pb.apply_bindings(
            PAGE, [{"find": "NGN 0.00", "value": "<b>1</b>", "escape": False}])
        self.assertIn("<b>1</b>", html2)

    def test_no_bindings_returns_the_page_untouched(self):
        html, report = pb.apply_bindings(PAGE, [], {})
        self.assertEqual(html, PAGE)
        self.assertEqual(report["applied"], [])
        self.assertEqual(report["total_replacements"], 0)

    def test_broken_pattern_does_not_break_the_serve(self):
        # A rule that cannot run is skipped and reported; the page still serves.
        html, report = pb.apply_bindings(PAGE, [{"find": "(", "match": "regex", "value": "x"}])
        self.assertEqual(html, PAGE)
        self.assertEqual(report["skipped"][0]["why"], "regex does not compile")


if __name__ == "__main__":
    unittest.main(verbosity=2)
