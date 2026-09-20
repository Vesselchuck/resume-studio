#!/usr/bin/env python3
"""
snapshot_pdf.py — Visual regression test for the rendered PDFs.

Compares the freshly-built color and grayscale PDFs against four
committed fixtures using per-pixel RGB diffs with configurable
tolerances. Designed to catch CSS / template / data regressions
that change visible output in either variant.

This is a verification TOOL, not a unittest. It lives in build/
because unittest discovery in tests/ would import it eagerly and fail
on environments where the optional snapshot dependencies aren't
installed. Run it directly:

    python3 build/snapshot_pdf.py [--update]

It is also auto-invoked by resume.js as the final pipeline step.

Workflow
────────
1. First build (no fixtures yet):
       npm run resume
   resume.js auto-bootstraps any missing fixture on its first
   snapshot call.

2. Routine verification (subsequent builds):
       npm run resume
   The snapshot test runs automatically. Exit 0 = both variants
   match within tolerance. Exit 1 = visible regression; side-by-
   side diff images written to tests/fixtures/diff_<variant>_pageN.png.

3. After an INTENTIONAL change (new content, design tweak, etc.):
       python3 build/snapshot_pdf.py --update
   Refreshes BOTH variants of the fixture matching the data source
   of the most recent build (read from dist/pdf_meta.json).

4. To refresh ALL four fixtures (color + grayscale × default + mine):
       python3 build/snapshot_pdf.py --update-all
   Re-runs the full build pipeline twice — once forcing default data,
   once forcing your data (if data/resume.yml exists) — and
   replaces both variants' fixtures each time.

Dual variants × dual data sources = four fixtures
─────────────────────────────────────────────────
Render produces two PDFs per build, named after you rather than
after the document (see build/_output_name.py):
  • dist/<Your_Name>_Resume.pdf
  • dist/<Your_Name>_Resume_Grayscale.pdf

The fixtures below keep their fixed, document-shaped names. A
fixture is a committed reference image, and naming it after
whoever last built would both churn the directory and put a real
name into a repository the placeholder data exists to keep it out
of. The build's own dist/pdf_meta.json says where to find the
PDFs to compare.

The build can use either data/resume_default.yml (placeholder, committed)
or data/resume.yml (your real data, gitignored). The snapshot
test reads dist/pdf_meta.json to learn which one was loaded and
picks the matching fixture for each variant:

  data_source = 'default' → tests/fixtures/expected_resume-color.pdf       (committed)
                            tests/fixtures/expected_resume-grayscale.pdf   (committed)
  data_source = 'mine'    → tests/fixtures/expected_resume-color.mine.pdf (gitignored)
                            tests/fixtures/expected_resume-grayscale.mine.pdf (gitignored)

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
    ENV_SKIP_SNAPSHOT,
    ENV_RESUME_PIPELINE_SUFFIX,
    ENV_RESUME_VARIANTS,
)


ROOT = Path(__file__).parent.parent          # project root (build/ → ..)
FIXTURE_DIR = ROOT / "tests" / "fixtures"
PDF_META_FILE = ROOT / "dist" / "pdf_meta.json"

# Built PDFs — resume.js produces both color and grayscale variants
# in dist/, named after you (Gaius_Iulius_Resume.pdf and
# Gaius_Iulius_Resume_Grayscale.pdf). The stem comes from the build's
# own metadata rather than from a constant here, because it follows
# `name.first` / `name.last` and therefore changes when they do.

# Fixtures — four combinations of (variant × data_source).
# The default fixtures are committed to the repo; the 'mine' fixtures
# are gitignored and only exist on a machine that has data/resume.yml.
FIXTURE_COLOR_DEFAULT     = FIXTURE_DIR / "expected_resume-color.pdf"
FIXTURE_COLOR_MINE        = FIXTURE_DIR / "expected_resume-color.mine.pdf"
FIXTURE_GRAYSCALE_DEFAULT = FIXTURE_DIR / "expected_resume-grayscale.pdf"
FIXTURE_GRAYSCALE_MINE    = FIXTURE_DIR / "expected_resume-grayscale.mine.pdf"

def pdf_variants():
    """The (label, pdf_path, default_fixture, mine_fixture) tuples.

    A function rather than a module constant because `pdf_path` is
    resolved from dist/pdf_meta.json, which the build rewrites on every
    run — reading it once at import time would pin the paths to
    whatever the *previous* build was called, and snapshot_pdf.py's
    --update-all runs two builds inside one process.

    The label feeds the Snapshot phase output ("Colored page 1",
    "Grayscale page 1"). The diff filename slug is derived separately
    (see diff_images) so the label stays grammatical while the slug
    aligns with the rest of the codebase's "color"/"grayscale" naming.

    The FIXTURES are not renamed along with the outputs, and that is
    deliberate. A fixture is a committed reference image; naming it
    after whoever last ran the build would churn tests/fixtures/ every
    time someone edited their surname, and would leak that name into a
    repository the placeholder data exists precisely to keep it out of.
    """
    dist = ROOT / "dist"
    stem = _output_name.stem_from_meta(PDF_META_FILE, 'resume')
    return [
        ('Colored',
         _output_name.color_pdf(dist, stem),
         FIXTURE_COLOR_DEFAULT, FIXTURE_COLOR_MINE),
        ('Grayscale',
         _output_name.grayscale_pdf(dist, stem),
         FIXTURE_GRAYSCALE_DEFAULT, FIXTURE_GRAYSCALE_MINE),
    ]


def active_variants():
    """The variants the last build actually produced.

    A build can be told to skip a variant (RESUME_VARIANTS), and when it
    does it removes that variant's PDF rather than leaving the previous
    run's file behind. So presence on disk is a truthful signal here:
    if the grayscale PDF is absent, this build did not make one and
    there is nothing to compare.

    Returns (variants, missing_labels). An empty `variants` means no
    build has run at all, which is a different problem and stays an
    error at the call site.
    """
    present, missing = [], []
    for variant in pdf_variants():
        if variant[1].exists():
            present.append(variant)
        else:
            missing.append(variant[0])
    return present, missing


def resolve_fixture_path(default_fixture, mine_fixture):
    """
    Choose the fixture file based on which data file backed the build.

    Reads dist/pdf_meta.json's `data_source` field:
      • 'default' → committed fixture
      • 'mine'    → gitignored fixture

    A MISSING file falls back to the committed fixture. That is the
    bootstrap case — no build has written the metadata yet — and it
    also covers a partially-written file from an interrupted run.

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
    try:
        meta = json.loads(PDF_META_FILE.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return default_fixture

    source = meta.get('data_source', 'default')
    if source == 'default':
        return default_fixture
    if source == 'mine':
        return mine_fixture
    c.err(
        f"{PDF_META_FILE.relative_to(ROOT)} has data_source={source!r}, "
        f"which this script does not recognize."
    )
    c.detail("Expected 'default' or 'mine'.")
    c.detail(
        "A value of 'local' means the file was written before the data "
        "files were renamed — rebuild with `npm run resume` to refresh it."
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


def render_pdf_pages(pdfium, path: Path) -> list:
    """Rasterize all pages of a PDF to PIL Images at RENDER_DPI.

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
        for page in pdf:
            bitmap = page.render(scale=SCALE)
            images.append(bitmap.to_pil().convert("RGB"))
        return images
    finally:
        pdf.close()


def diff_images(Image, ImageChops, actual, expected, page_num: int, variant_label: str):
    """
    Compare two same-page images.

    `variant_label` is e.g. "Colored" or "Grayscale" — folded into the
    output label ("Colored page 1") and (via lowercase + mapping) into
    the diff filename slug ("color"/"grayscale", giving
    diff_color_page1.png) so concurrent variants don't collide. The
    label and slug differ deliberately: the label is a human-readable
    phrase, the slug aligns with the rest of the codebase's
    color/grayscale naming.

    Returns (ok, label, value, diff_path) where label is the column
    heading, value is the result text, and diff_path is the path to
    the written diff image on regression (None otherwise). The caller
    emits aligned `c.ok_pair(label, value)` on pass and a
    `❌ Colored page N` + `ℹ️ Diff file` pair on regression.
    """
    label = f"{variant_label} page {page_num}"
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
    # Map label → slug so the diff filename uses the codebase's
    # canonical "color"/"grayscale" naming (matches the fixtures'
    # expected_resume-color.pdf spelling) while the label stays
    # grammatical for human-readable output.
    _LABEL_TO_SLUG = {'Colored': 'color', 'Grayscale': 'grayscale'}
    variant_slug = _LABEL_TO_SLUG.get(variant_label,
                                     variant_label.lower().replace(' ', '-'))
    out_path = FIXTURE_DIR / f"diff_{variant_slug}_page{page_num}.png"
    side_by_side.save(out_path)

    return False, label, (
        f"{fraction:.4%} difference (limit {MAX_DIFF_FRACTION:.4%})"
    ), out_path


def update_all_fixtures():
    """
    Refresh ALL fixtures by running the full build pipeline twice
    and copying both PDF variants (color + grayscale) from each pass.

    1. Rebuild with RESUME_DATA_SOURCE=default and SKIP_SNAPSHOT=1.
       Copy that build's color PDF to expected_resume-color.pdf and
       its grayscale PDF to expected_resume-grayscale.pdf. Both are
       located through dist/pdf_meta.json, which each pass rewrites —
       the two passes load different data files and so produce
       differently named outputs.
    2. If data/resume.yml exists, rebuild with
       RESUME_DATA_SOURCE=mine and copy the two PDFs to the
       matching .mine.pdf fixtures. Otherwise skip step 2 with a
       notice.

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
        env[ENV_RESUME_DATA_SOURCE] = source
        env[ENV_SKIP_SNAPSHOT] = '1'
        # --update-all refreshes all four fixtures, so both variants have
        # to be built regardless of what the caller's environment asks
        # for. Without this, running it from a Studio session configured
        # for color-only would silently leave the grayscale fixtures
        # stale while reporting success.
        env[ENV_RESUME_VARIANTS] = 'color,grayscale'
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

    def copy_variants_for(source_kind):
        """
        After a successful build, copy both variants (color + grayscale)
        from dist/ to the matching fixtures for `source_kind` ('default'
        or 'mine'). Returns the list of fixture paths copied, or None
        on error.
        """
        copied = []
        for label, pdf_path, default_fixture, mine_fixture in pdf_variants():
            if not pdf_path.exists():
                c.err(f"{pdf_path.relative_to(ROOT)} missing after build.")
                return None
            target = mine_fixture if source_kind == 'mine' else default_fixture
            if not safe_copy(pdf_path, target):
                return None
            copied.append(target)
            c.ok_pair("Updated fixture", str(target.relative_to(ROOT)))
        return copied

    updated = []

    # Step 1: default data.
    if not default_yml.exists():
        c.err(f"{default_yml.relative_to(ROOT)} not found.")
        return 2
    if not run_build('default', 'default data'):
        return 1
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    default_copied = copy_variants_for('default')
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
        mine_copied = copy_variants_for('mine')
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
        help="Replace BOTH variants (color + grayscale) of the fixture "
             "matching the LAST build's data source (read from "
             "dist/pdf_meta.json) with the PDFs that build wrote to dist/.",
    )
    parser.add_argument(
        "--update-all", action="store_true",
        help="Run the full build pipeline twice (once with default data, "
             "once with your data if present) and refresh ALL four "
             "fixtures (color + grayscale × default + mine). Use after "
             "intentional design changes that affect both modes.",
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

    # Check what the last build produced. A variant that was
    # deliberately not built is skipped with a note; nothing built at
    # all is still an error.
    variants, missing = active_variants()
    if not variants:
        c.err("No built PDFs in dist/. Run `npm run resume` first.")
        return 2
    for label in missing:
        c.info_pair("Skipped variant", f"{label.lower()} — not built by the last run")

    # --update: refresh the current data-source fixture for every
    # variant the last build produced.
    if args.update:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        # A variant that was not built cannot be refreshed, and saying
        # nothing would leave its fixture quietly describing an older
        # document — the next build that does produce that variant would
        # then fail for reasons that look unrelated to this moment.
        for label in missing:
            c.warn_pair(
                "Fixture NOT refreshed",
                f"{label.lower()} — build it first "
                f"({ENV_RESUME_VARIANTS}=color,grayscale npm run resume) "
                f"then re-run --update",
            )
        for label, pdf_path, default_fix, mine_fix in variants:
            target = resolve_fixture_path(default_fix, mine_fix)
            if not safe_copy(pdf_path, target):
                return 1
            c.ok_pair("Updated fixture", str(target.relative_to(ROOT)))
        return 0

    # --auto-bootstrap or normal compare path.
    # Bootstrap any missing fixtures before comparing.
    if args.auto_bootstrap:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        bootstrapped = False
        for label, pdf_path, default_fix, mine_fix in variants:
            target = resolve_fixture_path(default_fix, mine_fix)
            if not target.exists():
                if not safe_copy(pdf_path, target):
                    return 1
                c.info_pair("Created fixture", str(target.relative_to(ROOT)))
                bootstrapped = True
        if bootstrapped:
            c.info_pair("Update with", "python build/snapshot_pdf.py --update")
            return 0
        # All fixtures exist — fall through to normal compare.

    # Normal compare path: verify all fixtures exist.
    for label, pdf_path, default_fix, mine_fix in variants:
        target = resolve_fixture_path(default_fix, mine_fix)
        if not target.exists():
            c.err(f"fixture missing at {target.relative_to(ROOT)}.")
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
    for variant_label, pdf_path, default_fix, mine_fix in variants:
        expected_pdf = resolve_fixture_path(default_fix, mine_fix)
        actual_pages = render_pdf_pages(pdfium, pdf_path)
        expected_pages = render_pdf_pages(pdfium, expected_pdf)

        if len(actual_pages) != len(expected_pages):
            c.err(f"{variant_label} page count differs — "
                  f"actual {len(actual_pages)}, expected {len(expected_pages)}")
            all_ok = False
            continue

        for i, (a, e) in enumerate(zip(actual_pages, expected_pages), start=1):
            ok, label, value, diff_path = diff_images(
                Image, ImageChops, a, e, i, variant_label,
            )
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
