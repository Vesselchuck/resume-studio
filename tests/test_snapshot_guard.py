"""
Tests for the guards in build/snapshot_pdf.py that decide WHICH fixture
a build may be compared against or copied over.

Nothing here rasterizes anything, so the snapshot tool's heavy optional
dependencies (pypdfium2, Pillow) are not needed: every path under test
returns or exits before the lazy import.

What is pinned:

  • A build of an arbitrary file (data_source='explicit') has no
    fixture. Compare, bootstrap and --update refuse it rather than
    guessing — guessing is how private data ends up in a fixture.
  • --update and --auto-bootstrap refuse to write a fixture when the
    build's metadata is missing: a fixture is only overwritten by a
    build that says which data file it came from.
  • --update-all strips RESUME_DATA_FILE / LETTER_DATA_FILE from the
    child build's environment, and refuses to copy a pass whose
    manifest reports a different data source than the one it asked for.
  • The data source is the one the BUILD recorded beside its PDF
    (dist/outputs.json), not dist/pdf_meta.json, which a preview
    rewrites; a PDF whose bytes changed since is refused.
  • Exit 1 means a visible difference and nothing else.

Every path the tool writes to is redirected into a temp dir; the real
tests/fixtures/ is never touched.
"""

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import snapshot_pdf  # noqa: E402
from _env_contract import (  # noqa: E402
    ENV_RESUME_DATA_FILE,
    ENV_LETTER_DATA_FILE,
    ENV_RESUME_DATA_SOURCE,
)


class SandboxedSnapshot(unittest.TestCase):
    """Point every path snapshot_pdf uses at a throwaway project root."""

    STEM = "Gaius_Caesar_Resume"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dist = self.tmp / "dist"
        self.fixtures = self.tmp / "tests" / "fixtures"
        self.dist.mkdir()
        self.fixtures.mkdir(parents=True)
        (self.tmp / "data").mkdir()
        self.meta = self.dist / "pdf_meta.json"
        self.record = self.dist / "outputs.json"
        patches = {
            "ROOT": self.tmp,
            "FIXTURE_DIR": self.fixtures,
            "DIST_DIR": self.dist,
            "RECORD_FILE": self.record,
            "FIXTURE_DEFAULT": self.fixtures / "expected_resume.pdf",
            "FIXTURE_MINE": self.fixtures / "expected_resume.mine.pdf",
        }
        self._patches = [mock.patch.object(snapshot_pdf, k, v)
                         for k, v in patches.items()]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_build(self, data_source, content=b"%PDF-fake"):
        """Leave dist/ as a build of `data_source` would: the metadata,
        the PDF, and the record resume.js writes beside it."""
        meta = {"output_stem": self.STEM}
        if data_source is not None:
            meta["data_source"] = data_source
        self.meta.write_text(json.dumps(meta), encoding="utf-8")
        (self.dist / f"{self.STEM}.pdf").write_bytes(content)
        self.record.write_text(json.dumps({"resume": {
            "pdf": f"{self.STEM}.pdf",
            "data_source": data_source,
            "sha256": hashlib.sha256(content).hexdigest(),
            "written": [f"{self.STEM}.pdf"],
        }}), encoding="utf-8")

    def preview(self, data_source):
        """What a Studio preview does: rewrite the metadata, nothing else."""
        self.meta.write_text(json.dumps(
            {"output_stem": self.STEM, "data_source": data_source}),
            encoding="utf-8")

    def run_main(self, *args):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["snapshot_pdf.py", *args]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            try:
                code = snapshot_pdf.main()
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()

    def fixture_files(self):
        return sorted(p.name for p in self.fixtures.iterdir())


class TestResolveFixturePath(SandboxedSnapshot):
    def resolve(self, **kw):
        return snapshot_pdf.resolve_fixture_path("DEFAULT", "MINE", **kw)

    def test_default_and_mine(self):
        self.write_build("default")
        self.assertEqual(self.resolve(), "DEFAULT")
        self.write_build("mine")
        self.assertEqual(self.resolve(), "MINE")

    def test_explicit_is_refused(self):
        self.write_build("explicit")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("exist only for the default template", err.getvalue())
        self.assertIn("data/resume.yml", err.getvalue())

    def test_missing_meta_falls_back_only_for_a_compare(self):
        self.assertEqual(self.resolve(), "DEFAULT")
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as ctx:
            self.resolve(require_meta=True)
        self.assertEqual(ctx.exception.code, 2)


class TestMainRefusesExplicit(SandboxedSnapshot):
    def test_update_refuses_and_writes_nothing(self):
        self.write_build("explicit")
        code, err = self.run_main("--update")
        self.assertEqual(code, 2)
        self.assertIn("explicit", err)
        self.assertEqual(self.fixture_files(), [])

    def test_auto_bootstrap_refuses_and_writes_nothing(self):
        self.write_build("explicit")
        code, _ = self.run_main("--auto-bootstrap")
        self.assertEqual(code, 2)
        self.assertEqual(self.fixture_files(), [])

    def test_compare_refuses(self):
        self.write_build("explicit")
        code, _ = self.run_main()
        self.assertEqual(code, 2)

    def test_update_without_a_data_source_writes_nothing(self):
        self.write_build(None)
        code, _ = self.run_main("--update")
        self.assertEqual(code, 2)
        self.assertEqual(self.fixture_files(), [])

    def test_update_of_a_mine_build_writes_only_the_mine_fixture(self):
        self.write_build("mine")
        code, _ = self.run_main("--update")
        self.assertEqual(code, 0)
        self.assertEqual(self.fixture_files(), ["expected_resume.mine.pdf"])


class TestTheRecordNotThePreview(SandboxedSnapshot):
    """A preview rewrites pdf_meta.json; only a Build writes the PDF.

    Reproduced end to end before the fix: a profile still carrying the
    template's name, a Build of data/resume.yml, then a preview of the
    template in the Studio. pdf_meta.json then said 'default' and named
    the same file, and --update copied the private résumé into the
    committed tests/fixtures/expected_resume.pdf.
    """

    def test_update_follows_the_build_not_the_last_preview(self):
        self.write_build("mine", content=b"%PDF-yours")
        self.preview("default")
        code, _ = self.run_main("--update")
        self.assertEqual(code, 0)
        self.assertEqual(self.fixture_files(), ["expected_resume.mine.pdf"])
        self.assertEqual(
            (self.fixtures / "expected_resume.mine.pdf").read_bytes(),
            b"%PDF-yours")

    def test_a_pdf_changed_since_the_build_is_refused(self):
        self.write_build("default")
        (self.dist / f"{self.STEM}.pdf").write_bytes(b"%PDF-someone-else")
        code, err = self.run_main("--update")
        self.assertEqual(code, 2)
        self.assertIn("not the PDF the last build recorded", err)
        self.assertEqual(self.fixture_files(), [])

    def test_no_record_means_nothing_to_compare_or_copy(self):
        self.write_build("default")
        self.record.unlink()
        for args in (("--update",), ("--auto-bootstrap",), ()):
            with self.subTest(args=args):
                code, _ = self.run_main(*args)
                self.assertEqual(code, 2)
        self.assertEqual(self.fixture_files(), [])


class TestExitCodes(SandboxedSnapshot):
    """Exit 1 means "the PDF differs", and nothing else may use it.

    resume.js answers 1 with "Snapshot differs" and advice to run
    --update. A comparison that never ran must not get that advice:
    following it overwrites the fixture with an uncompared PDF.
    """

    def test_an_unrecognized_data_source_is_not_a_difference(self):
        self.write_build("local")
        code, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("does not recognize", err)

    def test_a_crash_is_not_a_difference(self):
        self.write_build("default")
        (self.fixtures / "expected_resume.pdf").write_bytes(b"x")
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["snapshot_pdf.py"]), \
                mock.patch.object(snapshot_pdf, "main",
                                  side_effect=RuntimeError("pdfium says no")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = snapshot_pdf.cli()
        self.assertEqual(code, 2)
        self.assertIn("pdfium says no", err.getvalue())
        self.assertIn("Nothing was compared", err.getvalue())


class TestDiffImage(SandboxedSnapshot):
    def test_the_amplified_panel_is_brighter_than_the_difference(self):
        """The third panel used ImageChops.multiply, which divides by 255:
        a difference of 60 came out as 1, a black panel."""
        try:
            from PIL import Image, ImageChops
        except ImportError:
            self.skipTest("Pillow not installed")
        a = Image.new("RGB", (100, 100), (255, 255, 255))
        b = a.copy()
        b.paste((195, 195, 195), (0, 0, 50, 50))
        ok, _, _, path = snapshot_pdf.diff_images(Image, ImageChops, a, b, 1)
        self.assertFalse(ok)
        panel = Image.open(path).crop((200, 0, 300, 100))
        self.assertEqual(panel.getpixel((10, 10)),
                         (min(255, 60 * snapshot_pdf.DIFF_GAIN),) * 3)
        self.assertEqual(panel.getpixel((80, 80)), (0, 0, 0))


class TestUpdateAll(SandboxedSnapshot):
    def setUp(self):
        super().setUp()
        (self.tmp / "data" / "resume_default.yml").write_text("x: 1\n",
                                                              encoding="utf-8")

    def fake_build(self, stamps):
        """A subprocess.run stand-in: records env, leaves a build behind
        that stamps `stamps(requested_source)` as its data source."""
        self.envs = []

        def run(cmd, cwd=None, env=None):
            self.envs.append(dict(env))
            self.write_build(stamps(env[ENV_RESUME_DATA_SOURCE]),
                             content=env[ENV_RESUME_DATA_SOURCE].encode())
            return mock.Mock(returncode=0)
        return run

    def update_all(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return snapshot_pdf.update_all_fixtures()

    def test_explicit_data_file_is_not_inherited_by_the_child_build(self):
        env = {ENV_RESUME_DATA_FILE: "/private/resume.yml",
               ENV_LETTER_DATA_FILE: "/private/letter.yml"}
        with mock.patch.dict("os.environ", env), \
                mock.patch("subprocess.run",
                           side_effect=self.fake_build(lambda s: s)):
            self.assertEqual(self.update_all(), 0)
        self.assertEqual(len(self.envs), 1)          # no data/resume.yml here
        self.assertNotIn(ENV_RESUME_DATA_FILE, self.envs[0])
        self.assertNotIn(ENV_LETTER_DATA_FILE, self.envs[0])
        self.assertEqual(self.envs[0][ENV_RESUME_DATA_SOURCE], "default")
        self.assertEqual(
            (self.fixtures / "expected_resume.pdf").read_bytes(),
            b"default")

    def test_a_pass_that_built_the_wrong_source_is_not_copied(self):
        """If the default pass somehow read your data anyway, the
        committed fixture must not receive it."""
        with mock.patch("subprocess.run",
                        side_effect=self.fake_build(lambda s: "mine")):
            self.assertEqual(self.update_all(), 1)
        self.assertEqual(self.fixture_files(), [])

    def test_a_pass_that_built_an_explicit_file_is_not_copied(self):
        with mock.patch("subprocess.run",
                        side_effect=self.fake_build(lambda s: "explicit")):
            self.assertEqual(self.update_all(), 1)
        self.assertEqual(self.fixture_files(), [])

    def test_both_passes_copy_to_their_own_fixtures(self):
        (self.tmp / "data" / "resume.yml").write_text("x: 1\n", encoding="utf-8")
        with mock.patch("subprocess.run",
                        side_effect=self.fake_build(lambda s: s)):
            self.assertEqual(self.update_all(), 0)
        self.assertEqual(
            (self.fixtures / "expected_resume.pdf").read_bytes(),
            b"default")
        self.assertEqual(
            (self.fixtures / "expected_resume.mine.pdf").read_bytes(),
            b"mine")


if __name__ == "__main__":
    unittest.main()
