"""
Tests for build.read_accent — extract --accent hex from _tokens.scss.

The function is a thin regex extractor with a few sharp edges worth
pinning down:
  • It must return the FIRST --accent it sees (the canonical :root
    declaration), not any later context-specific override.
  • It must fail clearly when the file is missing or has no --accent.
  • It accepts any hex form the regex allows (#fff, #aabbcc, #aabbccdd).

The audit's M3 finding (regex is brittle to leading SCSS comments
containing the literal '--accent:') is encoded here as a regression-
documenting test that records current behavior. If M3 is ever fixed,
that test should be updated to assert the new, safer behavior.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import build  # noqa: E402


@contextlib.contextmanager
def silenced():
    with contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()):
        yield


class TestReadAccent(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.tokens = self.tmpdir / "_tokens.scss"
        self._patches = [
            mock.patch.object(build, "TOKENS_FILE", self.tokens),
            mock.patch.object(build, "ROOT", self.tmpdir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for f in self.tmpdir.iterdir():
            f.unlink()
        self.tmpdir.rmdir()

    # ── Happy paths ────────────────────────────────────────────────

    def test_normal_six_digit_hex(self):
        self.tokens.write_text(
            ":root {\n  --accent: #2d4a3e;\n}\n", encoding="utf-8"
        )
        self.assertEqual(build.read_accent(), "#2d4a3e")

    def test_three_digit_shorthand(self):
        self.tokens.write_text(
            ":root {\n  --accent: #abc;\n}\n", encoding="utf-8"
        )
        self.assertEqual(build.read_accent(), "#abc")

    def test_eight_digit_hex_with_alpha(self):
        self.tokens.write_text(
            ":root {\n  --accent: #aabbccdd;\n}\n", encoding="utf-8"
        )
        self.assertEqual(build.read_accent(), "#aabbccdd")

    def test_mixed_case_preserved(self):
        # Regex allows both [0-9a-fA-F]; verify the captured value
        # is returned verbatim (not normalized to lower-case).
        self.tokens.write_text(
            ":root {\n  --accent: #AaBbCc;\n}\n", encoding="utf-8"
        )
        self.assertEqual(build.read_accent(), "#AaBbCc")

    def test_returns_first_when_multiple_declarations(self):
        # _tokens.scss in this project does NOT have multiple :root
        # decls, and nothing overrides --accent anywhere any more —
        # the monochrome block that used to is gone. Pin the "first
        # wins" semantics anyway, in case someone adds an @media block
        # to _tokens.scss.
        self.tokens.write_text(
            ":root {\n"
            "  --accent: #111111;\n"
            "}\n"
            "@media print {\n"
            "  :root { --accent: #222222; }\n"
            "}\n",
            encoding="utf-8",
        )
        self.assertEqual(build.read_accent(), "#111111")

    # ── Error paths ────────────────────────────────────────────────

    def test_missing_file_fails(self):
        # tokens file is not created in setUp; setUp made an empty
        # temp dir.
        with silenced(), self.assertRaises(SystemExit) as ctx:
            build.read_accent()
        self.assertEqual(ctx.exception.code, 1)

    def test_no_accent_declaration_fails(self):
        self.tokens.write_text(
            ":root {\n  --text-primary: #1a1a1a;\n}\n", encoding="utf-8"
        )
        with silenced(), self.assertRaises(SystemExit):
            build.read_accent()

    def test_non_hex_value_fails(self):
        # Regex requires `#` followed by hex digits; a CSS variable
        # reference or function call won't match.
        self.tokens.write_text(
            ":root {\n  --accent: var(--brand-green);\n}\n",
            encoding="utf-8",
        )
        with silenced(), self.assertRaises(SystemExit):
            build.read_accent()

    # ── Audit M3 regression-documenting ────────────────────────────

    def test_M3_regex_currently_matches_leading_comment(self):
        # AUDIT M3: the regex doesn't strip comments before scanning,
        # so a comment containing '--accent: #xxxxxx;' earlier in the
        # file will be matched in preference to the real declaration.
        #
        # This test documents the CURRENT (buggy) behavior so the
        # next person knows about M3 and can spot when the fix lands.
        # If/when M3 is fixed (the audit's recommended approach is to
        # strip /* */ comments before regex, or anchor the search to
        # the :root block), update this test to assert the new safer
        # behavior: read_accent() should return '#2d4a3e', not
        # '#ff0000'.
        self.tokens.write_text(
            "/* TODO: change --accent: #ff0000; later */\n"
            ":root {\n  --accent: #2d4a3e;\n}\n",
            encoding="utf-8",
        )
        self.assertEqual(
            build.read_accent(), "#ff0000",
            msg="M3 may have been fixed — update this test to assert #2d4a3e",
        )


if __name__ == "__main__":
    unittest.main()
