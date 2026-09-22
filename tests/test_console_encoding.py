"""
Tests for build/_console.py's stream encoding.

On Windows a piped stdout uses the ANSI code page (cp1252 on most
Western machines), which has no ✅ — so the first ok() of a build run
through resume.js, or through `| more`, raised UnicodeEncodeError.
PYTHONIOENCODING=cp1252 reproduces that stream on any platform.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent

SCRIPT = (
    "import sys; sys.path.insert(0, 'build'); import _console as c; "
    "c.ok_pair('Wrote PDF', 'Zoë_山田_Resume.pdf'); c.warn('careful'); "
    "c.err('broken')"
)


class TestConsoleOnALegacyCodePage(unittest.TestCase):
    def run_with(self, encoding):
        env = dict(os.environ, PYTHONIOENCODING=encoding)
        return subprocess.run(
            [sys.executable, "-c", SCRIPT], cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )

    def test_cp1252_pipe_does_not_crash(self):
        result = self.run_with("cp1252")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"UnicodeEncodeError", result.stderr)

    def test_output_is_utf8(self):
        result = self.run_with("cp1252")
        out = result.stdout.decode("utf-8")
        self.assertIn("✅", out)
        self.assertIn("Zoë_山田_Resume.pdf", out)
        self.assertIn("❌", result.stderr.decode("utf-8"))

    def test_a_stringio_stream_is_left_alone(self):
        """The warm worker and the tests swap in io.StringIO, which has no
        reconfigure(); importing _console must not trip over it."""
        script = (
            "import io, sys; sys.stdout = io.StringIO(); "
            "sys.path.insert(0, 'build'); import _console as c; c.ok('x'); "
            "v = sys.stdout.getvalue(); sys.stdout = sys.__stdout__; "
            "print('OK' if '✅ x' in v else 'BAD')"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"OK", result.stdout)


if __name__ == "__main__":
    unittest.main()


class TestColorEnvMatchesNode(unittest.TestCase):
    """_color_enabled follows the same NO_COLOR / FORCE_COLOR rules as
    colorEnabled() in build/_console.js, so one setting means the same
    thing to every step of a build."""

    def color(self, **env):
        sys.path.insert(0, str(ROOT / "build"))
        import _console
        saved = {k: os.environ.get(k) for k in ("NO_COLOR", "FORCE_COLOR")}
        try:
            for k in saved:
                os.environ.pop(k, None)
            os.environ.update(env)

            class NotATty:
                def isatty(self):
                    return False
            return _console._color_enabled(NotATty())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_force_color_zero_or_false_is_off(self):
        self.assertFalse(self.color(FORCE_COLOR="0"))
        self.assertFalse(self.color(FORCE_COLOR=" False "))

    def test_force_color_on_values(self):
        for v in ("", "1", "2", "3", "true"):
            with self.subTest(v=v):
                self.assertTrue(self.color(FORCE_COLOR=v))

    def test_unknown_value_falls_back_to_tty(self):
        self.assertFalse(self.color(FORCE_COLOR="maybe"))

    def test_no_color_wins(self):
        self.assertFalse(self.color(NO_COLOR="1", FORCE_COLOR="1"))
