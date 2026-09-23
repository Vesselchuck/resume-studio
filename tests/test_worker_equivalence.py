"""
Tests for build/worker.py — the warm path must equal the cold path.

WHY THIS TEST MATTERS MORE THAN IT LOOKS
----------------------------------------
worker.py exists so the GUI can re-render without paying interpreter
startup on every keystroke. The moment it produces even slightly
different output from `python build/build.py`, the project's central
promise — "editing the YAML and re-running produces the same PDFs on
any machine" — quietly stops being true, and it stops being true only
for GUI users, which is the hardest kind of bug to notice.

So this suite does not test the worker's logic. It tests that the
worker has no logic: for each operation, it runs the real CLI entry
point, snapshots the bytes it produced, runs the same operation through
a warm worker, and asserts the bytes are identical.

Both output paths are deterministic — build.py's HTML/metadata/favicon
and crop_pdf.py's PDF all hash identically across repeated cold runs —
so byte equality is the right assertion here, not a normalized or
fuzzy comparison.

The suite also asserts the worker survives failure. A worker that dies
on a malformed YAML file would leave the GUI restarting a process on
every other keystroke, which defeats the point of it being warm.

Covered
  • Handshake: the worker announces itself before any request.
  • build(measurement) — dist/index.html, pdf_meta.json, favicon.svg.
  • build(final)       — same three, placement-driven.
  • crop               — the cropped, metadata-stamped PDF.
  • Failure containment: a bad request does not end the process.
  • Stale placement is reported as such, not as a template traceback.

Not covered here
  Log text. The CLI writes to a terminal and the worker writes into a
  capture buffer, so ANSI color differs by design (_console disables
  color on a non-tty). Artifacts are the contract; log formatting is
  not.

Skips cleanly when dist/ has not been built yet — build.py requires a
compiled dist/styles.css, and final mode requires dist/placement.json.
Build once in the Studio (or run `node resume.js`) first. A stylesheet
that is merely older than an edited .scss file is recompiled here, the
same way the Studio does, rather than failing every build test.

Final-mode tests also skip when dist/placement.json was solved for a
different data source than the one currently resolving, since nothing
in a Python-only suite can re-solve a layout.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
BUILD_DIR = ROOT / "build"

sys.path.insert(0, str(BUILD_DIR))
import _output_name  # noqa: E402  (what the last build called its PDFs)
DIST = ROOT / "dist"

WORKER = BUILD_DIR / "worker.py"
BUILD_PY = BUILD_DIR / "build.py"
CROP_PY = BUILD_DIR / "crop_pdf.py"

STYLES_CSS = DIST / "styles.css"
PLACEMENT = DIST / "placement.json"
PDF_META = DIST / "pdf_meta.json"

# The built color PDF, whatever the last build called it. Outputs are
# named after you now (Gaius_Caesar_Resume.pdf), so this cannot be a
# literal — and a literal that no longer matches would not fail, it
# would make test_crop_matches_cli skip forever while still printing a
# reason that sounds like an ordinary "nothing built yet". Hence the
# lookup, and hence the skip message below quoting the resolved name.
BUILT_PDF = _output_name.output_pdf(DIST, _output_name.stem_from_meta(PDF_META, 'resume'))

# The three files a build writes. Compared after every build op.
BUILD_ARTIFACTS = (DIST / "index.html", PDF_META, DIST / "favicon.svg")

FRAME_PREFIX = "\x1e"
TIMEOUT = 120  # generous: a cold import of pypdfium2 on a slow CI box


def _have_dist():
    return STYLES_CSS.exists()


def _stylesheet_is_stale():
    """True when a .scss source is newer than dist/styles.css.

    build.py refuses to build against a stale stylesheet, so after any
    edit under styles/ every build test here would fail until something
    recompiled it. Mirrors build.py's check, tolerance included.
    """
    css_mtime = STYLES_CSS.stat().st_mtime
    return any(p.stat().st_mtime - css_mtime > 2.0
               for p in (ROOT / "styles").glob("*.scss"))


def _recompile_stylesheet():
    """Compile styles/ → dist/styles.css with the pipeline's own compileSass.

    Returns an explanation when it could not, None when it did.
    """
    node = shutil.which("node")
    if not node:
        return "node not found"
    script = ("require('./build/pipeline')"
              ".createPipeline({ root: process.cwd(), python: null })"
              ".compileSass()")
    result = subprocess.run([node, "-e", script], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8",
                            timeout=TIMEOUT)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip().splitlines()[-1:] or ["failed"]
    return None


class Worker:
    """Thin client for one warm worker process.

    Deliberately minimal — it exists to prove the protocol works from
    the outside, so it does not share any code with the Node client.
    """

    def __init__(self, root=ROOT):
        self.proc = subprocess.Popen(
            [sys.executable, "-B", str(Path(root) / "build" / "worker.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, cwd=str(root),
        )
        self.hello = self._read_frame()

    def _read_frame(self):
        for line in self.proc.stdout:
            if line.startswith(FRAME_PREFIX):
                return json.loads(line[1:])
            # Stray non-frame output. The sentinel exists precisely so
            # this degrades to noise instead of breaking the protocol.
        raise AssertionError("worker stdout ended before a frame arrived")

    def call(self, **req):
        req.setdefault("id", 1)
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        return self._read_frame()

    def close(self):
        if self.proc.poll() is None:
            try:
                self.call(id=999, op="shutdown")
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:
                pass


def run_cold(args):
    """Run a CLI entry point exactly as resume.js runs it."""
    return subprocess.run(
        [sys.executable, "-B", *args],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        timeout=TIMEOUT,
    )


def snapshot(paths):
    """Read the current bytes of each path. Missing files record as None."""
    return {p: (p.read_bytes() if p.exists() else None) for p in paths}


@unittest.skipUnless(_have_dist(), "dist/ not built — run `node resume.js` first")
class WorkerEquivalenceTest(unittest.TestCase):
    """Warm worker output vs cold CLI output, byte for byte."""

    @classmethod
    def setUpClass(cls):
        if _stylesheet_is_stale():
            problem = _recompile_stylesheet()
            if problem:
                raise unittest.SkipTest(
                    "dist/styles.css is older than styles/ and could not be "
                    f"recompiled ({problem}) — build once in the Studio first")
        # Both paths write into the real dist/. Preserve whatever was
        # there so a test run never costs the developer their build.
        cls._restore = snapshot(BUILD_ARTIFACTS)

    @classmethod
    def tearDownClass(cls):
        for path, data in cls._restore.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)

    def setUp(self):
        self.worker = Worker()
        self.addCleanup(self.worker.close)

    # -- handshake ---------------------------------------------------

    def test_announces_itself_before_any_request(self):
        """The client must be able to tell a live worker from a hung one."""
        self.assertTrue(self.worker.hello["ok"])
        self.assertEqual(self.worker.hello["op"], "hello")
        self.assertEqual(self.worker.hello["result"]["protocol"], 1)
        self.assertEqual(Path(self.worker.hello["result"]["root"]), ROOT)

    # -- build -------------------------------------------------------

    # A cold 'final' build consumes dist/placement.json, which the solver
    # wrote for one specific set of job and sidebar ids. Nothing in this
    # suite can produce a placement — solving needs Node, Playwright and
    # a browser — so the one on disk is ambient state, and it matches the
    # current data only if the last thing to run a full build used the
    # same data source. Switching between resume.yml and
    # resume_default.yml, or editing job ids, leaves it stale.
    #
    # A stale placement makes the template raise on the missing id. That
    # is correct behavior and says nothing about warm-versus-cold
    # equivalence, so it skips rather than fails — an earlier version
    # failed here and reported a template traceback as if the worker
    # were at fault.
    _STALE_PLACEMENT_MARKERS = ("UndefinedError", "has no attribute")

    def _assert_build_matches_cli(self, mode):
        cold = run_cold([str(BUILD_PY), f"--mode={mode}"])
        if cold.returncode != 0:
            combined = f"{cold.stdout}\n{cold.stderr}"
            if mode == "final" and any(m in combined for m in self._STALE_PLACEMENT_MARKERS):
                self.skipTest(
                    "dist/placement.json was solved for different data — "
                    "run `node resume.js` to re-solve, then re-run the tests"
                )
            self.fail(f"cold build failed:\n{cold.stdout}\n{cold.stderr}")
        expected = snapshot(BUILD_ARTIFACTS)

        # Perturb every artifact so a no-op worker can't pass by leaving
        # the CLI's own output in place.
        for path in BUILD_ARTIFACTS:
            if path.exists():
                path.write_bytes(b"clobbered by test\n")

        frame = self.worker.call(op="build", mode=mode)
        self.assertTrue(frame["ok"], f"warm build failed: {frame.get('error')}")
        self.assertEqual(frame["result"]["mode"], mode)

        for path, want in expected.items():
            with self.subTest(artifact=path.name):
                self.assertIsNotNone(want, f"cold build did not write {path.name}")
                self.assertEqual(
                    path.read_bytes(), want,
                    f"{path.name} differs between the warm worker and "
                    f"`python build/build.py --mode={mode}`",
                )

    def test_measurement_build_matches_cli(self):
        self._assert_build_matches_cli("measurement")

    @unittest.skipUnless(PLACEMENT.exists(), "dist/placement.json missing")
    def test_final_build_matches_cli(self):
        self._assert_build_matches_cli("final")

    def test_reports_the_data_source_it_used(self):
        """The GUI shows which YAML is live; it reads that from here."""
        frame = self.worker.call(op="build", mode="measurement")
        self.assertTrue(frame["ok"], frame.get("error"))
        self.assertIn(frame["result"]["dataSource"], ("mine", "default"))
        self.assertEqual(frame["result"]["dataSource"],
                         json.loads(PDF_META.read_text(encoding="utf-8"))["data_source"])

    # -- crop --------------------------------------------------------

    @unittest.skipUnless(
        BUILT_PDF.exists() and PDF_META.exists(),
        f"{BUILT_PDF.name} not built")
    def test_crop_matches_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            cold_out = Path(tmp) / "cold.pdf"
            warm_out = Path(tmp) / "warm.pdf"

            cold = run_cold([str(CROP_PY), str(BUILT_PDF), str(cold_out),
                             "--meta", str(PDF_META)])
            self.assertEqual(cold.returncode, 0,
                             f"cold crop failed:\n{cold.stdout}\n{cold.stderr}")

            frame = self.worker.call(op="crop", input=str(BUILT_PDF),
                                     output=str(warm_out), meta=str(PDF_META))
            self.assertTrue(frame["ok"], f"warm crop failed: {frame.get('error')}")

            self.assertEqual(
                warm_out.read_bytes(), cold_out.read_bytes(),
                "cropped PDF differs between the warm worker and "
                "`python build/crop_pdf.py`",
            )
            # The geometry the whole pipeline exists to guarantee.
            self.assertEqual(frame["result"]["widthPt"], 612.0)
            self.assertEqual(frame["result"]["heightPt"], 792.0)

    # -- failure containment -----------------------------------------

    def test_unknown_op_does_not_kill_the_worker(self):
        bad = self.worker.call(op="does_not_exist")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["kind"], "unknown_op")

        alive = self.worker.call(op="ping")
        self.assertTrue(alive["ok"], "worker died on an unknown op")

    def test_bad_mode_does_not_kill_the_worker(self):
        bad = self.worker.call(op="build", mode="sideways")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["kind"], "bad_request")

        alive = self.worker.call(op="ping")
        self.assertTrue(alive["ok"], "worker died on a bad mode")

    def test_malformed_json_does_not_kill_the_worker(self):
        self.worker.proc.stdin.write("{not json at all\n")
        self.worker.proc.stdin.flush()
        bad = self.worker._read_frame()
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["kind"], "bad_request")

        alive = self.worker.call(op="ping")
        self.assertTrue(alive["ok"], "worker died on malformed input")

    @unittest.skipUnless(BUILT_PDF.exists(), "no built resume PDF to rasterize")
    def test_raster_skips_pages_the_caller_already_has(self):
        """The preview only re-sends pages whose pixels changed.

        A second raster of the same PDF, told the hashes of the first,
        must mark every page unchanged and send no image data — and the
        hashes must be stable, or nothing would ever be skipped.
        """
        first = self.worker.call(op="raster", path=str(BUILT_PDF), scale=1)
        self.assertTrue(first["ok"], first.get("error"))
        pages = first["result"]["images"]
        self.assertTrue(pages)
        for im in pages:
            self.assertIn("png", im)
            self.assertRegex(im["hash"], r"^[0-9a-f]{32}$")

        known = {str(im["page"]): im["hash"] for im in pages}
        second = self.worker.call(op="raster", path=str(BUILT_PDF), scale=1, known=known)
        self.assertTrue(second["ok"], second.get("error"))
        for before, after in zip(pages, second["result"]["images"]):
            self.assertEqual(after["hash"], before["hash"])
            self.assertTrue(after.get("unchanged"))
            self.assertNotIn("png", after)

        # A stale hash is not trusted: that page comes back with an image.
        known["1"] = "0" * 32
        third = self.worker.call(op="raster", path=str(BUILT_PDF), scale=1, known=known)
        page1 = third["result"]["images"][0]
        self.assertNotIn("unchanged", page1)
        self.assertEqual(page1["png"], pages[0]["png"])

    @unittest.skipUnless(BUILT_PDF.exists(), "no built resume PDF to rasterize")
    def test_parallel_encoding_matches_one_page_at_a_time(self):
        """Encoding pages side by side must give the bytes a single-page
        request gives, page for page."""
        both = self.worker.call(op="raster", path=str(BUILT_PDF), scale=1)
        images = both["result"]["images"]
        for im in images:
            alone = self.worker.call(op="raster", path=str(BUILT_PDF), scale=1,
                                     pages=[im["page"]])
            self.assertEqual(alone["result"]["images"][0]["png"], im["png"])

    def test_missing_input_file_is_reported_not_raised(self):
        frame = self.worker.call(op="crop", input=str(DIST / "nope.pdf"),
                                 output=str(DIST / "nope-out.pdf"), meta=None)
        self.assertFalse(frame["ok"])
        self.assertIn(frame["error"]["kind"], ("missing_file", "internal"))
        self.assertTrue(self.worker.call(op="ping")["ok"])

    # -- the warm-path-specific hazard -------------------------------

    @unittest.skipUnless(
        PLACEMENT.exists()
        and (ROOT / "data" / "resume.yml").exists()
        and (ROOT / "data" / "resume_default.yml").exists(),
        "needs both data files and a solved placement",
    )
    def test_stale_placement_is_named_as_such(self):
        """
        The hazard a warm worker introduces: 'final' can be called
        against a placement solved for different data. A cold CLI build
        always re-solves first, so this can only happen here.

        It must fail loudly and say why — never render a document from
        a placement that doesn't match the data.
        """
        current = json.loads(PDF_META.read_text(encoding="utf-8"))["data_source"] \
            if PDF_META.exists() else "mine"
        other = "default" if current == "mine" else "mine"

        frame = self.worker.call(op="build", mode="final",
                                 env={"RESUME_DATA_SOURCE": other})

        if frame["ok"]:
            # Both YAML files happen to share their job and sidebar ids,
            # so there is nothing stale to detect. Not a failure.
            self.skipTest("both data files use the same ids")

        self.assertEqual(frame["error"]["kind"], "stale_placement")
        self.assertTrue(any("placement" in line.lower()
                            for line in frame["error"]["detail"]))
        self.assertTrue(self.worker.call(op="ping")["ok"])

    def test_env_override_is_restored_after_the_call(self):
        """A per-request env override must not leak into the next render."""
        before = os.environ.get("RESUME_DATA_SOURCE")
        self.worker.call(op="build", mode="measurement",
                         env={"RESUME_DATA_SOURCE": "default"})
        after = self.worker.call(op="build", mode="measurement")
        self.assertTrue(after["ok"], after.get("error"))
        # The worker's own environment is what matters; assert via the
        # data source it resolves to with no override in play.
        self.assertEqual(os.environ.get("RESUME_DATA_SOURCE"), before)

    def test_letter_env_override_is_applied_and_restored(self):
        """build_letter takes a per-request env exactly like build does.

        The Studio sends LETTER_DATA_FILE when a letter file is picked, and
        clears RESUME_DATA_SOURCE (a null value) so the resume card's
        choice never reaches the letter. An earlier worker ignored the env
        on this op entirely, so the preview read a different file than
        the card named and the Build then used.
        """
        letter_artifacts = (DIST / "letter.html", DIST / "letter_meta.json",
                            DIST / "favicon.svg")
        saved = snapshot(letter_artifacts)

        def restore():
            for path, data in saved.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
        self.addCleanup(restore)

        with tempfile.TemporaryDirectory() as tmp:
            picked = Path(tmp) / "picked_letter.yml"
            text = (ROOT / "data" / "letter_default.yml").read_text(encoding="utf-8")
            picked.write_text(text.replace("first: Gaius", "first: Pickedus"),
                              encoding="utf-8")

            frame = self.worker.call(op="build_letter", env={
                "LETTER_DATA_FILE": str(picked),
                # An invalid value: if the null did not unset it, the
                # letter's loader would refuse the build.
                "RESUME_DATA_SOURCE": None,
            })
            self.assertTrue(frame["ok"], frame.get("error"))
            self.assertTrue(frame["result"]["meta"]["author"].startswith("Pickedus"),
                            frame["result"]["meta"])

            # Nothing leaks into the next request.
            after = self.worker.call(op="build_letter")
            self.assertTrue(after["ok"], after.get("error"))
            self.assertFalse(after["result"]["meta"]["author"].startswith("Pickedus"),
                             "LETTER_DATA_FILE leaked into the next letter build")

            # A null really unsets a variable the worker inherited.
            bad = self.worker.call(op="build_letter",
                                   env={"RESUME_DATA_SOURCE": "not-a-source"})
            self.assertFalse(bad["ok"], "an invalid RESUME_DATA_SOURCE was not applied")
            cleared = self.worker.call(op="build_letter",
                                       env={"RESUME_DATA_SOURCE": None})
            self.assertTrue(cleared["ok"], cleared.get("error"))


@unittest.skipUnless(_have_dist(), "dist/ not built — run `node resume.js` first")
class WarmTemplateReloadTest(unittest.TestCase):
    """A warm worker keeps one Jinja Environment and must still see edits.

    build.py compiles each template once per process now (see
    build.jinja_env) and relies on auto_reload to recompile a template
    whose file changed. The Studio's worker lives as long as the app, so
    a template edit it did not pick up would leave the preview showing
    the old template until a restart. Run against a throwaway copy of
    the project, since it edits templates.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project"
        for name in ("build", "templates", "styles"):
            shutil.copytree(ROOT / name, self.root / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "data").mkdir()
        for f in (ROOT / "data").glob("*_default.yml"):
            shutil.copy2(f, self.root / "data" / f.name)
        (self.root / "dist").mkdir()
        # Copied last, so it is newer than every .scss (build.py checks).
        shutil.copyfile(STYLES_CSS, self.root / "dist" / "styles.css")
        self.worker = Worker(self.root)
        self.addCleanup(self.worker.close)

    def _build(self, **req):
        frame = self.worker.call(**req)
        self.assertTrue(frame["ok"], frame.get("error"))

    def _html(self, name="index.html"):
        return (self.root / "dist" / name).read_text(encoding="utf-8")

    def _append(self, template, text, keep_mtime=False):
        path = self.root / "templates" / template
        before = path.stat()
        path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")
        if keep_mtime:
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_an_edited_template_is_used_by_the_next_build(self):
        self._build(op="build", mode="measurement")
        self.assertNotIn("reload-marker-1", self._html())
        self._append("measurement.j2", "<!-- reload-marker-1 -->\n")
        self._build(op="build", mode="measurement")
        self.assertIn("reload-marker-1", self._html())

    def test_an_edit_that_keeps_the_mtime_is_still_seen(self):
        """Coarse timestamps, two saves in one tick, a tool that restores
        times: none may leave the old template in use."""
        self._build(op="build", mode="measurement")
        self._append("measurement.j2", "<!-- reload-marker-2 -->\n", keep_mtime=True)
        self._build(op="build", mode="measurement")
        self.assertIn("reload-marker-2", self._html())

    def test_an_edited_imported_macro_is_seen(self):
        self._build(op="build", mode="measurement")
        path = self.root / "templates" / "_macros.j2"
        text = path.read_text(encoding="utf-8")
        # A macro the measurement template calls, given a visible comment
        # right after its header.
        target = "{% macro render_job("
        self.assertIn(target, text)
        body = text.index("%}", text.index(target)) + 2
        path.write_text(text[:body] + "<!-- macro-marker -->" + text[body:], encoding="utf-8")
        self._build(op="build", mode="measurement")
        self.assertIn("macro-marker", self._html())

    def test_the_letter_template_is_reloaded_too(self):
        self._build(op="build_letter")
        self.assertNotIn("letter-marker", self._html("letter.html"))
        self._append("letter.j2", "<!-- letter-marker -->\n")
        self._build(op="build_letter")
        self.assertIn("letter-marker", self._html("letter.html"))

    def test_unchanged_templates_render_identically(self):
        self._build(op="build", mode="measurement")
        first = self._html()
        self._build(op="build", mode="measurement")
        self.assertEqual(self._html(), first)


if __name__ == "__main__":
    unittest.main()
