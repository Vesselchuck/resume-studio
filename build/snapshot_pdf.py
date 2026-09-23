#!/usr/bin/env python3
"""
snapshot_pdf.py — Visual regression test for the rendered PDFs.

Compares the freshly-built PDF against a committed fixture using
per-pixel RGB diffs with configurable tolerances. Designed to catch
CSS / template / data regressions that change visible output.

This is a verification TOOL, not a unittest. It lives in build/
because unittest discovery in tests/ would import it eagerly and fail
on environments where the optional snapshot dependencies aren't
installed. Run it directly:

    python3 build/snapshot_pdf.py [--update]

It is also auto-invoked by resume.js as the final pipeline step.

Workflow
────────
1. First build (no fixture yet):
       node resume.js
   resume.js auto-bootstraps a missing fixture on its first
   snapshot call.

2. Routine verification (subsequent builds):
       node resume.js
   The snapshot test runs automatically. Exit 0 = the PDF matches
   within tolerance. Exit 1 = visible regression; side-by-side diff
   images written to tests/fixtures/diff_pageN.png.

3. After an INTENTIONAL change (new content, design tweak, etc.):
       python3 build/snapshot_pdf.py --update
   Refreshes the fixture matching the data source of the most recent
   build (read from dist/pdf_meta.json).

4. To refresh BOTH fixtures (default + mine):
       python3 build/snapshot_pdf.py --update-all
   Re-runs the full build pipeline twice — once forcing default data,
   once forcing your data (if data/resume.yml exists) — and
   replaces the matching fixture each time.

One PDF, two data sources = two fixtures
────────────────────────────────────────
Render produces one PDF per build, named after you rather than
after the document (see build/_output_name.py):
  • dist/<Your_Name>_Resume.pdf

(There used to be a second, grayscale PDF and therefore four
fixtures. The palette prints correctly in color and in black and
white from the one file now, so there is one of each.)

The fixtures below keep their fixed, document-shaped names. A
fixture is a committed reference image, and naming it after
whoever last built would both churn the directory and put a real
name into a repository the placeholder data exists to keep it out
of. The build's own dist/pdf_meta.json says where to find the
PDF to compare.

The build can use either data/resume_default.yml (placeholder, committed)
or data/resume.yml (your real data, gitignored). The snapshot
test reads dist/pdf_meta.json to learn which one was loaded and
picks the matching fixture:

  data_source = 'default' → tests/fixtures/expected_resume.pdf      (committed)
  data_source = 'mine'    → tests/fixtures/expected_resume.mine.pdf (gitignored)
  data_source = 'explicit' → refused. The build read some other file,
                            named through RESUME_DATA_FILE, and no
                            fixture describes it. Compare, bootstrap and
                            --update all stop with an explanation.

Tolerances
──────────
  • PIXEL_RGB_TOLERANCE: max per-channel difference for a pixel to
    count as "matching." Allows for sub-pixel anti-aliasing variance
    between runs.
  • MAX_DIFF_FRACTION: max fraction of pixels per page that may
    differ. 0.001 = 0.1% of pixels — generous enough to absorb
    font-hinting jitter, tight enough to flag any real layout shift.

Requirements
────────────
  pypdfium2, Pillow  (installed via `pip install -r requirements.txt`)
  Imports are LAZY: a missing dependency produces a clear install
  hint instead of an ImportError traceback at module-load time.
"""

import sys

# Suppress writing of __pycache__/ next to source files. Set this
# before any other (non-builtin) import. Equivalent to `python -B`
# but enforces the no-cache rule even for direct invocations.
sys.dont_write_bytecode = True

import argparse
import json
import os
import shutil
from pathlib import Path

# Local console helper.
sys.path.insert(0, str(Path(__file__).parent))
import _console as c  # noqa: E402
import _output_name  # noqa: E402  (where the built PDFs ended up)
from _env_contract import (  # noqa: E402
    ENV_RESUME_DATA_SOURCE,
    ENV_RESUME_DATA_FILE,
    ENV_LETTER_DATA_FILE,
    ENV_SKIP_SNAPSHOT,
    ENV_RESUME_PIPELINE_SUFFIX,
)


ROOT = Path(__file__).parent.parent          # project root (build/ → ..)
FIXTURE_DIR = ROOT / "tests" / "fixtures"
PDF_META_FILE = ROOT / "dist" / "pdf_meta.json"

# Built PDF — resume.js produces one, in dist/, named after you
# (Gaius_Caesar_Resume.pdf). The stem comes from the build's own
# metadata rather than from a constant here, because it follows
# `name.first` / `name.last` and therefore changes when they do.

# Fixtures — one per data_source. The default fixture is committed to
# the repo; the 'mine' fixture is gitignored and only exists on a
# machine that has data/resume.yml.
FIXTURE_DEFAULT = FIXTURE_DIR / "expected_resume.pdf"
FIXTURE_MINE    = FIXTURE_DIR / "expected_resume.mine.pdf"


def built_pdf():
    """Where the last build put the PDF.

    A function rather than a module constant because the path is
    resolved from dist/pdf_meta.json, which the build rewrites on every
    run — reading it once at import time would pin the path to whatever
    the *previous* build was called, and snapshot_pdf.py's --update-all
    runs two builds inside one process.

    The FIXTURES are not renamed along with the output, and that is
    deliberate. A fixture is a committed reference image; naming it
    after whoever last ran the build would churn tests/fixtures/ every
    time someone edited their surname, and would leak that name into a
    repository the placeholder data exists precisely to keep it out of.
    """
    stem = _output_name.stem_from_meta(PDF_META_FILE, 'resume')
    return _output_name.output_pdf(ROOT / "dist", stem)


#: The data_source values that have fixtures. build.py can also stamp
#: 'explicit' — a file named through RESUME_DATA_FILE that is neither
#: of these — and that one deliberately has none.
FIXTURE_SOURCES = ('default', 'mine')


def read_data_source():
    """
    The `data_source` the last build stamped into dist/pdf_meta.json,
    or None if the file is missing, unreadable or has no such field.
    """
    try:
        meta = json.loads(PDF_META_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    return meta.get('data_source')


def refuse_explicit_source():
    """Explain why a build of an arbitrary file has no fixture, and exit."""
    c.err(
        f"{PDF_META_FILE.relative_to(ROOT)} says the last build read an "
        f"explicitly named data file (data_source='explicit')."
    )
    c.detail("Snapshot fixtures exist only for the default template")
    c.detail("(data/resume_default.yml) and for data/resume.yml. A file picked")
    c.detail(f"in the Studio or named through {ENV_RESUME_DATA_FILE} has none, and")
    c.detail("comparing it against — or saving it over — one of those would")
    c.detail("mean a fixture that no longer describes its own data file.")
    c.detail(f"Unset {ENV_RESUME_DATA_FILE} (in the Studio: choose a data source")
    c.detail("instead of a file) and rebuild with `node resume.js`.")
    sys.exit(2)


def resolve_fixture_path(default_fixture, mine_fixture, *, require_meta=False):
    """
    Choose the fixture file based on which data file backed the build.

    Reads dist/pdf_meta.json's `data_source` field:
      • 'default'  → committed fixture
      • 'mine'     → gitignored fixture
      • 'explicit' → refused: an arbitrary file has no fixture, and
                     writing its pixels into either one would leave that
                     fixture describing a different document

    A MISSING file falls back to the committed fixture for a compare.
    That is the bootstrap case — no build has written the metadata yet
    — and it also covers a partially-written file from an interrupted
    run. With `require_meta` (every path that WRITES a fixture) it is
    refused instead: a fixture is only ever overwritten by a build that
    says which data file it came from.

    An UNRECOGNIZED value does not fall back. It used to: the line
    read `return mine if source == 'mine' else default`, so any token
    the function did not know about quietly selected the committed
    fixture. That is the worst available behavior, because it means a
    snapshot of your resume gets compared against the shipped Caesar
    placeholder and reports a huge diff — or, if someone had just
    refreshed that fixture, reports a clean pass on a comparison that
    never happened. Renaming this token from 'local' to 'mine' is
    exactly the kind of change that would have tripped it, so the
    failure is now loud.
    """
    source = read_data_source()
    if source is None:
        if require_meta:
            c.err(
                f"{PDF_META_FILE.relative_to(ROOT)} is missing or has no "
                f"data_source, so there is no telling which data file the "
                f"PDFs in dist/ came from."
            )
            c.detail("Refusing to overwrite a fixture with them. Rebuild with")
            c.detail("`node resume.js` and re-run.")
            sys.exit(2)
        return default_fixture
    if source == 'default':
        return default_fixture
    if source == 'mine':
        return mine_fixture
    if source == 'explicit':
        refuse_explicit_source()
    c.err(
        f"{PDF_META_FILE.relative_to(ROOT)} has data_source={source!r}, "
        f"which this script does not recognize."
    )
    c.detail("Expected 'default' or 'mine'.")
    c.detail(
        "A value of 'local' means the file was written before the data "
        "files were renamed — rebuild with `node resume.js` to refresh it."
    )
    c.detail("Refusing to guess which fixture to compare against.")
    sys.exit(1)


def safe_copy(src: Path, dst: Path) -> bool:
    """
    Copy `src` to `dst` with metadata, and convert PermissionError
    into a friendly stderr message instead of a traceback.

    Returns True on success, False on permission error. Callers
    should propagate False as a non-zero exit code.

    Most common cause on Windows: `dst` is a snapshot fixture or one
    of the built resume PDFs currently open in a PDF viewer. The
    viewer holds a write lock on the file. Linux/macOS viewers
    usually don't take this lock, but some IDE preview panes do on
    every platform.
    """
    try:
        shutil.copy2(src, dst)
        return True
    except PermissionError:
        c.err(f"Permission denied writing {dst}.")
        c.detail("Another program is holding the file open. Most often this is")
        c.detail("a PDF viewer (Adobe Reader, Edge, Chrome, SumatraPDF, an IDE")
        c.detail("preview pane). Close the viewer and re-run.")
        return False

# Rendering DPI for the comparison. 150 is enough to catch any human-
# visible difference; higher DPIs slow the test without adding signal.
RENDER_DPI = 150
SCALE = RENDER_DPI / 72  # PDF points per inch / 72

# Tolerances — see module docstring.
PIXEL_RGB_TOLERANCE = 4         # 0–255 per channel
MAX_DIFF_FRACTION = 0.001       # 0.1% of pixels


def render_pdf_pages(pdfium, path: Path, prepare=None, only=None) -> list:
    """Rasterize all pages of a PDF to PIL Images at RENDER_DPI.

    `prepare`, when given, is called with each pypdfium2 page before it
    is rendered. The Studio's live preview passes
    crop_pdf.crop_pdfium_page_to_letter, so it rasterizes Chromium's raw
    print with the crop applied in memory rather than a cropped copy
    written to disk. The snapshot test passes nothing and renders the
    file exactly as it is.

    `only`, when given, is a set of 0-based page indices to render; the
    other entries of the returned list are None (the list still has one
    entry per page). The preview uses it to skip pages it already has.

    The document is closed before returning. pypdfium2 holds an open
    file handle for the lifetime of the PdfDocument, and leaving that to
    garbage collection makes the rasterized file undeletable on Windows
    until the interpreter happens to collect it — POSIX allows unlinking
    an open file, so the leak is invisible there.

    That cost nothing while this function was only ever pointed at
    committed fixtures and dist/ outputs, which are overwritten rather
    than deleted. It matters now that build/worker.py rasterizes
    throwaway preview PDFs it then removes.
    """
    pdf = pdfium.PdfDocument(str(path))
    try:
        images = []
        for index in range(len(pdf)):
            if only is not None and index not in only:
                images.append(None)
                continue
            page = pdf[index]
            if prepare is not None:
                prepare(page)
            bitmap = page.render(scale=SCALE)
            images.append(bitmap.to_pil().convert("RGB"))
        return images
    finally:
        pdf.close()


def diff_images(Image, ImageChops, actual, expected, page_num: int):
    """
    Compare two same-page images.

    Returns (ok, label, value, diff_path) where label is the column
    heading ("Page 1"), value is the result text, and diff_path is the
    path to the written diff image on regression (None otherwise). The
    caller emits aligned `c.ok_pair(label, value)` on pass and a
    `❌ Page N` + `ℹ️ Diff file` pair on regression.
    """
    label = f"Page {page_num}"
    if actual.size != expected.size:
        return False, label, (
            f"size mismatch — actual {actual.size}, expected {expected.size}"
        ), None

    diff = ImageChops.difference(actual, expected)
    # Per-pixel max-channel difference.
    diff_max = diff.getchannel("R")
    for ch in ("G", "B"):
        diff_max = ImageChops.lighter(diff_max, diff.getchannel(ch))

    bbox = diff_max.getbbox()
    if bbox is None:
        return True, label, "identical", None

    # Count pixels exceeding the per-channel tolerance.
    # `tobytes()` is the long-term-stable Pillow API for flat byte
    # access; getdata() is deprecated as of Pillow 12.
    diff_bytes = diff_max.tobytes()
    differing_pixels = sum(1 for v in diff_bytes if v > PIXEL_RGB_TOLERANCE)
    total_pixels = diff_max.size[0] * diff_max.size[1]
    fraction = differing_pixels / total_pixels

    if fraction <= MAX_DIFF_FRACTION:
        return True, label, (
            f"within tolerance "
            f"({differing_pixels}/{total_pixels} = {fraction:.4%} differ)"
        ), None

    # Write side-by-side diff image: [expected | actual | diff×4 amplified]
    w, h = actual.size
    side_by_side = Image.new("RGB", (w * 3, h), (255, 255, 255))
    side_by_side.paste(expected, (0, 0))
    side_by_side.paste(actual, (w, 0))
    # Amplify diff for visibility (raw differences are usually invisible).
    amplified = ImageChops.multiply(diff, Image.new("RGB", diff.size, (8, 8, 8)))
    side_by_side.paste(amplified, (w * 2, 0))
    out_path = FIXTURE_DIR / f"diff_page{page_num}.png"
    side_by_side.save(out_path)

    return False, label, (
        f"{fraction:.4%} difference (limit {MAX_DIFF_FRACTION:.4%})"
    ), out_path


def update_all_fixtures():
    """
    Refresh BOTH fixtures by running the full build pipeline twice and
    copying the PDF from each pass.

    1. Rebuild with RESUME_DATA_SOURCE=default and SKIP_SNAPSHOT=1.
       Copy that build's PDF to expected_resume.pdf. It is located
       through dist/pdf_meta.json, which each pass rewrites — the two
       passes load different data files and so produce differently
       named outputs.
    2. If data/resume.yml exists, rebuild with
       RESUME_DATA_SOURCE=mine and copy its PDF to
       expected_resume.mine.pdf. Otherwise skip step 2 with a notice.

    SKIP_SNAPSHOT prevents resume.js from running the snapshot
    against the about-to-be-replaced fixtures and failing.
    """
    import subprocess

    # These two must stay distinct. They are deliberately spelled out
    # rather than imported from build.py, which would drag Jinja2 in as
    # a hard dependency of the snapshot tool — but that means they can
    # drift, so if the data file names change again, change them here.
    default_yml = ROOT / "data" / "resume_default.yml"   # shipped template
    mine_yml = ROOT / "data" / "resume.yml"              # yours, gitignored
    assert default_yml != mine_yml, "the two data sources collapsed to one file"

    def run_build(source, label):
        env = os.environ.copy()
        # An explicit data file beats RESUME_DATA_SOURCE in build.py's
        # load_data. Inherited from the caller — a Studio session with a
        # file picked sets it — it would make the "default" pass render
        # that file instead, and the copy below would commit whatever it
        # contains (your real data) as the template's fixture. Removed,
        # not blanked, so the child sees exactly what a fresh shell sees.
        for name in (ENV_RESUME_DATA_FILE, ENV_LETTER_DATA_FILE):
            env.pop(name, None)
        env[ENV_RESUME_DATA_SOURCE] = source
        env[ENV_SKIP_SNAPSHOT] = '1'
        # Tells resume.js to suffix its first phase banner with this
        # label so the user sees "Tests (default data)" / "Tests (my
        # data)" at the top of each pass, identifying which data source
        # is currently being built.
        env[ENV_RESUME_PIPELINE_SUFFIX] = label
        result = subprocess.run(
            ['node', 'resume.js'],
            cwd=str(ROOT),
            env=env,
        )
        if result.returncode != 0:
            c.err(f"resume.js failed for source={source!r} (exit {result.returncode})")
            return False
        return True

    def copy_fixture_for(source_kind):
        """
        After a successful build, copy the PDF from dist/ to the
        matching fixture for `source_kind` ('default' or 'mine').
        Returns the list of fixture paths copied, or None on error.

        Checks first that the build it is copying from really read that
        data source, by what the build itself stamped into
        dist/pdf_meta.json. The environment above is the intent; the
        manifest is the fact, and a fixture is committed on the fact.
        """
        actual = read_data_source()
        if actual != source_kind:
            c.err(
                f"the {source_kind!r} pass built from data_source="
                f"{actual!r} (per {PDF_META_FILE.relative_to(ROOT)}), not "
                f"{source_kind!r}."
            )
            c.detail("Refusing to copy its PDFs over the "
                     f"{source_kind} fixtures. Nothing was changed.")
            c.detail(f"Check that {ENV_RESUME_DATA_FILE} is not set by "
                     "something outside this script, then re-run.")
            return None
        pdf_path = built_pdf()
        if not pdf_path.exists():
            c.err(f"{pdf_path.relative_to(ROOT)} missing after build.")
            return None
        target = FIXTURE_MINE if source_kind == 'mine' else FIXTURE_DEFAULT
        if not safe_copy(pdf_path, target):
            return None
        c.ok_pair("Updated fixture", str(target.relative_to(ROOT)))
        return [target]

    updated = []

    # Step 1: default data.
    if not default_yml.exists():
        c.err(f"{default_yml.relative_to(ROOT)} not found.")
        return 2
    if not run_build('default', 'default data'):
        return 1
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    default_copied = copy_fixture_for('default')
    if default_copied is None:
        return 1
    updated.extend(default_copied)

    # Step 2: your own data, if present.
    if mine_yml.exists():
        # Blank line separator between subprocess passes — each subprocess
        # has its own _firstBanner state and suppresses the leading blank
        # of its first banner, so we emit one here from the parent to keep
        # the inter-phase rhythm consistent across the process boundary.
        print('', flush=True)
        if not run_build('mine', 'my data'):
            return 1
        mine_copied = copy_fixture_for('mine')
        if mine_copied is None:
            return 1
        updated.extend(mine_copied)
    else:
        c.info_pair("Skipped private fixture",
                    f"{mine_yml.relative_to(ROOT)} (no such file)")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--update", action="store_true",
        help="Replace the fixture matching the LAST build's data source "
             "(read from dist/pdf_meta.json) with the PDF that build "
             "wrote to dist/.",
    )
    parser.add_argument(
        "--update-all", action="store_true",
        help="Run the full build pipeline twice (once with default data, "
             "once with your data if present) and refresh BOTH fixtures "
             "(default + mine). Use after intentional design changes.",
    )
    parser.add_argument(
        "--auto-bootstrap", action="store_true",
        help="If a fixture is missing, create it from the matching "
             "PDF in dist/ and exit 0 (rather than failing). "
             "Intended for resume.js invocation on first build.",
    )
    args = parser.parse_args()

    if args.update_all:
        if args.update or args.auto_bootstrap:
            c.err("--update-all is mutually exclusive with --update and --auto-bootstrap")
            return 2
        return update_all_fixtures()

    # Check that the last build produced something at all.
    pdf_path = built_pdf()
    if not pdf_path.exists():
        c.err("No built PDF in dist/. Run `node resume.js` first.")
        return 2

    # --update: refresh the current data-source fixture.
    if args.update:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        target = resolve_fixture_path(FIXTURE_DEFAULT, FIXTURE_MINE,
                                      require_meta=True)
        if not safe_copy(pdf_path, target):
            return 1
        c.ok_pair("Updated fixture", str(target.relative_to(ROOT)))
        return 0

    # --auto-bootstrap or normal compare path.
    # Bootstrap a missing fixture before comparing.
    if args.auto_bootstrap:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        target = resolve_fixture_path(FIXTURE_DEFAULT, FIXTURE_MINE,
                                      require_meta=True)
        if not target.exists():
            if not safe_copy(pdf_path, target):
                return 1
            c.info_pair("Created fixture", str(target.relative_to(ROOT)))
            c.info_pair("Update with", "python build/snapshot_pdf.py --update")
            return 0
        # The fixture exists — fall through to normal compare.

    # Normal compare path: verify the fixture exists.
    expected_pdf = resolve_fixture_path(FIXTURE_DEFAULT, FIXTURE_MINE)
    if not expected_pdf.exists():
        c.err(f"fixture missing at {expected_pdf.relative_to(ROOT)}.")
        c.detail("To create it from the PDF the last build wrote, run:")
        c.detail(f"  python {Path(__file__).relative_to(ROOT)} --update")
        return 2

    # Lazy-import the heavy snapshot deps. If they're missing, fail
    # with an actionable message instead of an ImportError traceback.
    try:
        import pypdfium2 as pdfium
        from PIL import Image, ImageChops
    except ImportError as e:
        c.err("snapshot test requires extra dependencies.")
        c.detail("Install with:  pip install -r requirements.txt")
        c.detail(f"Missing: {e.name}")
        return 2

    all_ok = True
    actual_pages = render_pdf_pages(pdfium, pdf_path)
    expected_pages = render_pdf_pages(pdfium, expected_pdf)

    if len(actual_pages) != len(expected_pages):
        c.err(f"page count differs — actual {len(actual_pages)}, "
              f"expected {len(expected_pages)}")
        all_ok = False
    else:
        for i, (a, e) in enumerate(zip(actual_pages, expected_pages), start=1):
            ok, label, value, diff_path = diff_images(Image, ImageChops, a, e, i)
            if ok:
                c.ok_pair(label, value)
            else:
                c.err_pair(label, value)
                if diff_path is not None:
                    try:
                        display_diff = diff_path.relative_to(Path.cwd())
                    except ValueError:
                        display_diff = diff_path
                    c.info_pair("Diff file", str(display_diff))
                all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
