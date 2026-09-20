"""
Tests for build.load_data — YAML loader with env-var precedence.

Covers every branch of the load_data control flow:
  • Unset RESUME_DATA_SOURCE: prefer yours, else the template, else fail.
  • RESUME_DATA_SOURCE=mine: require yours, fail if missing.
  • RESUME_DATA_SOURCE=default: require default, fail if missing.
  • RESUME_DATA_SOURCE=<other>: fail with diagnostic.
  • The env-var value is normalized via .strip().lower().
  • Returns a (data, source) tuple where source is 'mine' | 'default'.

Each test patches the module-level path constants (DATA_FILE_MINE,
DATA_FILE_DEFAULT, ROOT) to point at a temp dir so the test isn't
sensitive to the real data files. stdout/stderr are silenced because
load_data emits c.ok_pair / c.err / c.detail and that would clutter
the test runner output.
"""

import contextlib
import io
import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import build  # noqa: E402
from _env_contract import (  # noqa: E402
    ENV_RESUME_DATA_SOURCE,
    ENV_RESUME_DATA_FILE,
)


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
        self.mine_path = self.tmpdir / "resume.yml"
        assert self.default_path != self.mine_path
        # Patch the module-level path constants for the duration of
        # the test. ROOT must be patched too because load_data calls
        # path.relative_to(ROOT) for the c.ok_pair message; if ROOT
        # is still the real project root, that call raises ValueError.
        self._patches = [
            mock.patch.object(build, "DATA_FILE_DEFAULT", self.default_path),
            mock.patch.object(build, "DATA_FILE_MINE", self.mine_path),
            mock.patch.object(build, "ROOT", self.tmpdir),
        ]
        for p in self._patches:
            p.start()
        # Strip EVERY environment input the loader reads; each test sets
        # what it needs. Both are stripped, not just the data source:
        # load_data checks RESUME_DATA_FILE first and returns early when
        # it is set, so a value inherited from the surrounding process
        # silently bypasses every branch under test. That is not
        # hypothetical — Studio sets it when you pick a data file, and
        # the build that runs these tests inherited it, turning ten of
        # them into failures that pointed at the loader rather than at
        # the environment.
        self._saved_env = {
            name: os.environ.pop(name, None)
            for name in (ENV_RESUME_DATA_SOURCE, ENV_RESUME_DATA_FILE)
        }

    def tearDown(self):
        for p in self._patches:
            p.stop()
        # Best-effort cleanup of the temp dir, which may now contain a
        # nested directory (test_explicit_file_relative_to_root).
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Restore anything that was set externally.
        for name, value in self._saved_env.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

    # ── explicit file (RESUME_DATA_FILE) ───────────────────────────
    #
    # The override exists so a tool can render an arbitrary YAML file
    # without copying it over the user's own data file. These pin the
    # part that matters: it wins over everything else, and it reads the
    # file where it lies.

    def test_explicit_file_is_read_in_place(self):
        other = self.tmpdir / "somewhere-else.yml"
        other.write_text(MINIMAL_YAML.replace("Gaius", "Explicit"), encoding="utf-8")
        os.environ[ENV_RESUME_DATA_FILE] = str(other)
        with silenced():
            data, source = build.load_data()
        self.assertEqual(data["name"]["first"], "Explicit")
        self.assertEqual(source, "mine")
        # The file it was told to read is the file it read — and the
        # ones it was not told to read are untouched.
        self.assertFalse(self.default_path.exists())
        self.assertFalse(self.mine_path.exists())

    def test_explicit_file_beats_both_the_source_var_and_the_search(self):
        self.default_path.write_text(MINIMAL_YAML.replace("Gaius", "FromDefault"),
                                     encoding="utf-8")
        self.mine_path.write_text(MINIMAL_YAML.replace("Gaius", "FromLocal"),
                                   encoding="utf-8")
        other = self.tmpdir / "picked.yml"
        other.write_text(MINIMAL_YAML.replace("Gaius", "Picked"), encoding="utf-8")

        os.environ[ENV_RESUME_DATA_SOURCE] = "default"
        os.environ[ENV_RESUME_DATA_FILE] = str(other)
        with silenced():
            data, _ = build.load_data()
        self.assertEqual(data["name"]["first"], "Picked")

    def test_explicit_file_relative_to_root(self):
        (self.tmpdir / "nested").mkdir()
        rel = Path("nested") / "cv.yml"
        (self.tmpdir / rel).write_text(MINIMAL_YAML.replace("Gaius", "Nested"),
                                       encoding="utf-8")
        os.environ[ENV_RESUME_DATA_FILE] = str(rel)
        with silenced():
            data, _ = build.load_data()
        self.assertEqual(data["name"]["first"], "Nested")

    def test_explicit_file_missing_fails(self):
        os.environ[ENV_RESUME_DATA_FILE] = str(self.tmpdir / "not-here.yml")
        with silenced(), self.assertRaises(SystemExit):
            build.load_data()

    def test_explicit_file_empty_fails(self):
        blank = self.tmpdir / "blank.yml"
        blank.write_text("", encoding="utf-8")
        os.environ[ENV_RESUME_DATA_FILE] = str(blank)
        with silenced(), self.assertRaises(SystemExit):
            build.load_data()

    def test_explicit_file_blank_value_treated_as_unset(self):
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_FILE] = "   "
        with silenced():
            _, source = build.load_data()
        self.assertEqual(source, "default")

    # ── env var unset ──────────────────────────────────────────────

    def test_unset_only_default_exists_loads_default(self):
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "default")
        self.assertEqual(data["name"]["first"], "Gaius")
        # Returned data is the raw YAML — no _data_source field stamped.
        self.assertNotIn("_data_source", data)

    def test_unset_only_mine_exists_loads_mine(self):
        self.mine_path.write_text(MINIMAL_YAML, encoding="utf-8")
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "mine")

    def test_unset_both_exist_prefers_mine(self):
        # Make the files distinguishable so we can verify which was loaded.
        self.default_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromDefault"), encoding="utf-8"
        )
        self.mine_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromLocal"), encoding="utf-8"
        )
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "mine")
        self.assertEqual(data["name"]["first"], "FromLocal")

    def test_unset_neither_exists_fails(self):
        with silenced(), self.assertRaises(SystemExit) as ctx:
            build.load_data()
        self.assertEqual(ctx.exception.code, 1)

    # ── env var explicit ───────────────────────────────────────────

    def test_default_explicit_loads_default_even_if_mine_exists(self):
        self.default_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromDefault"), encoding="utf-8"
        )
        self.mine_path.write_text(
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

    def test_mine_explicit_loads_mine(self):
        self.mine_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "mine"
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "mine")

    def test_mine_explicit_mine_missing_fails(self):
        # Template exists but env says require mine — still fails.
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "mine"
        with silenced(), self.assertRaises(SystemExit):
            build.load_data()

    def test_invalid_env_value_fails(self):
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "neither"
        with silenced(), self.assertRaises(SystemExit):
            build.load_data()

    # ── env var normalization ──────────────────────────────────────

    def test_env_value_uppercase_normalized(self):
        self.mine_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "MINE"
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "mine")

    def test_env_value_whitespace_stripped(self):
        self.mine_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "  mine  "
        with silenced():
            data, source = build.load_data()
        self.assertEqual(source, "mine")

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
