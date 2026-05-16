"""
Tests for build.load_data — YAML loader with env-var precedence.

Covers every branch of the load_data control flow:
  • Unset RESUME_DATA_SOURCE: prefer local, else default, else fail.
  • RESUME_DATA_SOURCE=local: require local, fail if missing.
  • RESUME_DATA_SOURCE=default: require default, fail if missing.
  • RESUME_DATA_SOURCE=<other>: fail with diagnostic.
  • The env-var value is normalized via .strip().lower().
  • Returns a (data, source) tuple where source is 'local' | 'default'.

Each test patches the module-level path constants (DATA_FILE_LOCAL,
DATA_FILE_DEFAULT, ROOT) to point at a temp dir so the test isn't
sensitive to the real data files. stdout/stderr are silenced because
load_data emits c.ok_pair / c.err / c.detail and that would clutter
the test runner output.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import build  # noqa: E402
from _env_contract import ENV_RESUME_DATA_SOURCE  # noqa: E402


@contextlib.contextmanager
def silenced():
    """Redirect both stdout and stderr to /dev/null-equivalent buffers."""
    with contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()):
        yield


# A minimal-but-valid YAML body. load_data only checks it parses to a
# dict at the top level; structural validation runs later in build().
MINIMAL_YAML = """\
name: {first: Gaius, last: Caesar}
meta: {description: Test, maxPages: 1}
sidebar: {blocks: []}
mainColumn: []
"""


class TestLoadData(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.default_path = self.tmpdir / "resume_default.yml"
        self.local_path = self.tmpdir / "resume.local.yml"
        # Patch the module-level path constants for the duration of
        # the test. ROOT must be patched too because load_data calls
        # path.relative_to(ROOT) for the c.ok_pair message; if ROOT
        # is still the real project root, that call raises ValueError.
        self._patches = [
            mock.patch.object(build, "DATA_FILE_DEFAULT", self.default_path),
            mock.patch.object(build, "DATA_FILE_LOCAL", self.local_path),
            mock.patch.object(build, "ROOT", self.tmpdir),
        ]
        for p in self._patches:
            p.start()
        # Strip the env var; each test sets it as needed.
        self._saved_env = os.environ.pop(ENV_RESUME_DATA_SOURCE, None)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        # Best-effort cleanup of the temp dir.
        for f in self.tmpdir.iterdir():
            f.unlink()
        self.tmpdir.rmdir()
        # Restore the env var if it was set externally.
        if self._saved_env is not None:
            os.environ[ENV_RESUME_DATA_SOURCE] = self._saved_env
        else:
            os.environ.pop(ENV_RESUME_DATA_SOURCE, None)

    # ── env var unset ──────────────────────────────────────────────

    def test_unset_only_default_exists_loads_default(self):
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "default")
        self.assertEqual(data["name"]["first"], "Gaius")
        # Returned data is the raw YAML — no _data_source field stamped.
        self.assertNotIn("_data_source", data)

    def test_unset_only_local_exists_loads_local(self):
        self.local_path.write_text(MINIMAL_YAML, encoding="utf-8")
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "local")

    def test_unset_both_exist_prefers_local(self):
        # Make the files distinguishable so we can verify which was loaded.
        self.default_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromDefault"), encoding="utf-8"
        )
        self.local_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromLocal"), encoding="utf-8"
        )
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "local")
        self.assertEqual(data["name"]["first"], "FromLocal")

    def test_unset_neither_exists_fails(self):
        with silenced(), self.assertRaises(SystemExit) as ctx:
            build.load_data()
        self.assertEqual(ctx.exception.code, 1)

    # ── env var explicit ───────────────────────────────────────────

    def test_default_explicit_loads_default_even_if_local_exists(self):
        self.default_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromDefault"), encoding="utf-8"
        )
        self.local_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromLocal"), encoding="utf-8"
        )
        os.environ[ENV_RESUME_DATA_SOURCE] = "default"
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "default")
        self.assertEqual(data["name"]["first"], "FromDefault")

    def test_default_explicit_default_missing_fails(self):
        os.environ[ENV_RESUME_DATA_SOURCE] = "default"
        with silenced(), self.assertRaises(SystemExit):
            build.load_data()

    def test_local_explicit_loads_local(self):
        self.local_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "local"
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "local")

    def test_local_explicit_local_missing_fails(self):
        # Default exists but env says require local — still fails.
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "local"
        with silenced(), self.assertRaises(SystemExit):
            build.load_data()

    def test_invalid_env_value_fails(self):
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "neither"
        with silenced(), self.assertRaises(SystemExit):
            build.load_data()

    # ── env var normalization ──────────────────────────────────────

    def test_env_value_uppercase_normalized(self):
        self.local_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "LOCAL"
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "local")

    def test_env_value_whitespace_stripped(self):
        self.local_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "  local  "
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "local")

    def test_env_value_empty_treated_as_unset(self):
        # Empty string falls into the "unset" branch (the conditional
        # uses `elif override:` after the explicit value checks).
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = ""
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "default")

    # ── YAML shape validation ──────────────────────────────────────

    def test_yaml_top_level_not_a_mapping_fails(self):
        # A YAML list at the top level should be rejected — load_data
        # explicitly checks `isinstance(data, dict)`.
        self.default_path.write_text("- one\n- two\n", encoding="utf-8")
        with silenced(), self.assertRaises(SystemExit):
            build.load_data()

    def test_yaml_empty_file_fails(self):
        # yaml.safe_load("") returns None; load_data treats this as
        # "not a mapping" and bails.
        self.default_path.write_text("", encoding="utf-8")
        with silenced(), self.assertRaises(SystemExit):
            build.load_data()


if __name__ == "__main__":
    unittest.main()
