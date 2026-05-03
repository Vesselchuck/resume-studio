"""
Tests for build.markdown_filter.

The filter handles **bold** spans only. Anything else (other markdown
syntax, raw HTML, special chars) is left as-is.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

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
        # The typo filter handles entities later; markdown filter shouldn't touch them.
        self.assertEqual(
            markdown_filter("&amp; **bold**"),
            "&amp; <strong>bold</strong>",
        )

    def test_none_input(self):
        self.assertEqual(markdown_filter(None), "")

    def test_non_string_coerced(self):
        self.assertEqual(markdown_filter(42), "42")

    def test_em_dash_inside_bold(self):
        # Em dash should pass through; typo filter will encode it later.
        self.assertEqual(
            markdown_filter("**a — b**"),
            "<strong>a — b</strong>",
        )


if __name__ == "__main__":
    unittest.main()
