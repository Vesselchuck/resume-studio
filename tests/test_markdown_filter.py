"""
Tests for build.markdown_filter.

The filter handles **bold** spans only. Anything else (other markdown
syntax, raw HTML, special chars) is left as-is.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

from build import markdown_filter


class TestMarkdownFilter(unittest.TestCase):
    def test_simple_bold(self):
        self.assertEqual(
            markdown_filter("a **bold** word"),
            "a <strong>bold</strong> word",
        )

    def test_bold_at_start(self):
        self.assertEqual(
            markdown_filter("**Hi** there"),
            "<strong>Hi</strong> there",
        )

    def test_bold_at_end(self):
        self.assertEqual(
            markdown_filter("This is **important**"),
            "This is <strong>important</strong>",
        )

    def test_multiple_bolds(self):
        self.assertEqual(
            markdown_filter("**A** and **B**"),
            "<strong>A</strong> and <strong>B</strong>",
        )

    def test_bold_with_punctuation(self):
        self.assertEqual(
            markdown_filter("**42%+ faster** turnaround"),
            "<strong>42%+ faster</strong> turnaround",
        )

    def test_no_bold_unchanged(self):
        self.assertEqual(
            markdown_filter("plain text with no markdown"),
            "plain text with no markdown",
        )

    def test_empty_bold_not_matched(self):
        # **** shouldn't produce <strong></strong> — require non-empty content.
        self.assertEqual(markdown_filter("****"), "****")

    def test_unmatched_double_asterisk_left_alone(self):
        self.assertEqual(markdown_filter("foo ** bar"), "foo ** bar")

    def test_bold_does_not_cross_newlines(self):
        # Multi-line bold spans aren't allowed — we want compact bullet text only.
        self.assertEqual(
            markdown_filter("**line one\nline two**"),
            "**line one\nline two**",
        )

    def test_html_entities_left_alone(self):
        # The template chain runs `bullet | e | md | safe`, so Jinja's `e`
        # filter has already encoded entities by the time markdown_filter sees
        # the text. markdown_filter must not touch entities or it would
        # double-encode them.
        self.assertEqual(
            markdown_filter("&amp; **bold**"),
            "&amp; <strong>bold</strong>",
        )

    def test_none_input(self):
        self.assertEqual(markdown_filter(None), "")

    def test_non_string_coerced(self):
        self.assertEqual(markdown_filter(42), "42")

    def test_em_dash_inside_bold(self):
        # Em dash passes through as literal UTF-8 (the source files and
        # rendered HTML are both UTF-8).
        self.assertEqual(
            markdown_filter("**a — b**"),
            "<strong>a — b</strong>",
        )

    def test_triple_asterisks_left_alone(self):
        # ***x*** is ambiguous (not part of this filter's intentional
        # syntax) — the lookaround guards leave the literal alone instead
        # of bleeding to *<strong>x</strong>*.
        self.assertEqual(markdown_filter("***triple***"), "***triple***")
        self.assertEqual(markdown_filter("a ***x*** b"), "a ***x*** b")

    def test_adjacent_bolds_without_separator_left_alone(self):
        # `**a****b**` is ambiguous: could mean two adjacent bolds, or
        # `**a** + **** + b**`. The lookaround guards reject either
        # parse and leave the literal — preferable to a half-rendered
        # output. Realistic resumes always have whitespace or punctuation
        # between bolds, so this edge case never appears in practice.
        self.assertEqual(markdown_filter("**a****b**"), "**a****b**")


if __name__ == "__main__":
    unittest.main()
