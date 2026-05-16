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

It is also auto-invoked by render.js as the final pipeline step.

Workflow
────────
1. First build (no fixtures yet):
       node render.js
   render.js auto-bootstraps any missing fixture on its first
   snapshot call.

2. Routine verification (subsequent builds):
       node render.js
   The snapshot test runs automatically. Exit 0 = both variants
   match within tolerance. Exit 1 = visible regression; side-by-
   side diff images written to tests/fixtures/diff_<variant>_pageN.png.

3. After an INTENTIONAL change (new content, design tweak, etc.):
       python3 build/snapshot_pdf.py --update
   Refreshes BOTH variants of the fixture matching the data source
   of the most recent build (read from dist/pdf_meta.json).

4. To refresh ALL four fixtures (color + grayscale × default + local):
       python3 build/snapshot_pdf.py --update-all
   Re-runs the full build pipeline twice — once forcing default data,
   once forcing local data (if data/resume.local.yml exists) — and
   replaces both variants' fixtures each time.

Dual variants × dual data sources = four fixtures
─────────────────────────────────────────────────
Render produces two PDFs per build:
  • dist/resume-color.pdf
  • dist/resume-grayscale.pdf

The build can use either data/resume_default.yml (placeholder, committed)
or data/resume.local.yml (real data, gitignored). The snapshot
test reads dist/pdf_meta.json to learn which one was loaded and
picks the matching fixture for each variant:

  data_source = 'default' → tests/fixtures/expected_resume-color.pdf       (committed)
                            tests/fixtures/expected_resume-grayscale.pdf   (committed)
  data_source = 'local'   → tests/fixtures/expected_resume-color.local.pdf (gitignored)
                            tests/fixtures/expected_resume-grayscale.local.pdf (gitignored)

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
from _env_contract import (  # noqa: E402
    ENV_RESUME_DATA_SOURCE,
    ENV_SKIP_SNAPSHOT,
    ENV_RESUME_PIPELINE_SUFFIX,
)


ROOT = Path(__file__).parent.parent          # project root (build/ → ..)
FIXTURE_DIR = ROOT / "tests" / "fixtures"
PDF_META_FILE = ROOT / "dist" / "pdf_meta.json"

# Built PDFs — render.js produces both color and grayscale variants
# in dist/.
PDF_COLOR     = ROOT / "dist" / "resume-color.pdf"
PDF_GRAYSCALE = ROOT / "dist" / "resume-grayscale.pdf"

# Fixtures — four combinations of (variant × data_source).
# The default fixtures are committed to the repo; the local fixtures
# are gitignored and only exist on a machine that has resume.local.yml.
FIXTURE_COLOR_DEFAULT     = FIXTURE_DIR / "expected_resume-color.pdf"
FIXTURE_COLOR_LOCAL       = FIXTURE_DIR / "expected_resume-color.local.pdf"
FIXTURE_GRAYSCALE_DEFAULT = FIXTURE_DIR / "expected_resume-grayscale.pdf"
FIXTURE_GRAYSCALE_LOCAL   = FIXTURE_DIR / "expected_resume-grayscale.local.pdf"

# Variants live as (label, pdf_path, default_fixture, local_fixture)
# tuples so the main flow can iterate uniformly. The label feeds the
# Snapshot phase output ("Colored page 1", "Grayscale page 1"). The
# diff filename slug is derived separately (see diff_images) so the
# label stays grammatical while the slug aligns with the rest of the
# codebase's "color"/"grayscale" naming.
VARIANTS = [
    ('Colored',   PDF_COLOR,     FIXTURE_COLOR_DEFAULT,     FIXTURE_COLOR_LOCAL),
    ('Grayscale', PDF_GRAYSCALE, FIXTURE_GRAYSCALE_DEFAULT, FIXTURE_GRAYSCALE_LOCAL),
]


def resolve_fixture_path(default_fixture, local_fixture):
    """
    Choose the fixture file based on which data file backed the build.

    Reads dist/pdf_meta.json's `data_source` field:
      • 'default' → committed fixture
      • 'local'   → gitignored fixture

    Falls back to the default fixture if pdf_meta.json is missing or
    malformed — handles the bootstrap case (no build has produced the
    metadata file yet) and protects against a partially-written file
    from an interrupted previous run.
    """
    try:
        meta = json.loads(PDF_META_FILE.read_text(encoding='utf-8'))
        source = meta.get('data_source', 'default')
    except (FileNotFoundError, json.JSONDecodeError):
        source = 'default'
    return local_fixture if source == 'local' else default_fixture


def safe_copy(src: Path, dst: Path) -> bool:
    """
    Copy `src` to `dst` with metadata, and convert PermissionError
    into a friendly stderr message instead of a traceback.

    Returns True on success, False on permission error. Callers
    should propagate False as a non-zero exit code.

    Most common cause on Windows: `dst` is a snapshot fixture or one
    of the resume-*.pdf files currently open in a PDF viewer. The
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
    """Rasterize all pages of a PDF to PIL Images at RENDER_DPI."""
    pdf = pdfium.PdfDocument(str(path))
    images = []
    for page in pdf:
        bitmap = page.render(scale=SCALE)
        images.append(bitmap.to_pil().convert("RGB"))
    return images


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
    # canonical "color"/"grayscale" naming (matches resume-color.pdf,
    # expected_resume-color.pdf, etc.) while the label stays
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
       Copy dist/resume-color.pdf to expected_resume-color.pdf and
       dist/resume-grayscale.pdf to expected_resume-grayscale.pdf.
    2. If data/resume.local.yml exists, rebuild with
       RESUME_DATA_SOURCE=local and copy the two PDFs to the
       matching .local.pdf fixtures. Otherwise skip step 2 with a
       notice.

    SKIP_SNAPSHOT prevents render.js from running the snapshot
    against the about-to-be-replaced fixtures and failing.
    """
    import subprocess

    default_yml = ROOT / "data" / "resume_default.yml"
    local_yml = ROOT / "data" / "resume.local.yml"

    def run_build(source, label):
        env = os.environ.copy()
        env[ENV_RESUME_DATA_SOURCE] = source
        env[ENV_SKIP_SNAPSHOT] = '1'
        # Tells render.js to suffix its first phase banner with this
        # label so the user sees "Tests (default data)" / "Tests (local
        # data)" at the top of each pass, identifying which data source
        # is currently being built.
        env[ENV_RESUME_PIPELINE_SUFFIX] = label
        result = subprocess.run(
            ['node', 'render.js'],
            cwd=str(ROOT),
            env=env,
        )
        if result.returncode != 0:
            c.err(f"render.js failed for source={source!r} (exit {result.returncode})")
            return False
        return True

    def copy_variants_for(source_kind):
        """
        After a successful build, copy both variants (color + grayscale)
        from dist/ to the matching fixtures for `source_kind` ('default'
        or 'local'). Returns the list of fixture paths copied, or None
        on error.
        """
        copied = []
        for label, pdf_path, default_fixture, local_fixture in VARIANTS:
            if not pdf_path.exists():
                c.err(f"{pdf_path.relative_to(ROOT)} missing after build.")
                return None
            target = local_fixture if source_kind == 'local' else default_fixture
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

    # Step 2: local data, if present.
    if local_yml.exists():
        # Blank line separator between subprocess passes — each subprocess
        # has its own _firstBanner state and suppresses the leading blank
        # of its first banner, so we emit one here from the parent to keep
        # the inter-phase rhythm consistent across the process boundary.
        print('', flush=True)
        if not run_build('local', 'local data'):
            return 1
        local_copied = copy_variants_for('local')
        if local_copied is None:
            return 1
        updated.extend(local_copied)
    else:
        c.info_pair("Skipped local fixture",
                    f"{local_yml.relative_to(ROOT)} (no such file)")

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
             "dist/pdf_meta.json) with the current dist/resume-*.pdf files.",
    )
    parser.add_argument(
        "--update-all", action="store_true",
        help="Run the full build pipeline twice (once with default data, "
             "once with local data if present) and refresh ALL four "
             "fixtures (color + grayscale × default + local). Use after "
             "intentional design changes that affect both modes.",
    )
    parser.add_argument(
        "--auto-bootstrap", action="store_true",
        help="If a fixture is missing, create it from the matching "
             "dist/resume-*.pdf and exit 0 (rather than failing). "
             "Intended for render.js invocation on first build.",
    )
    args = parser.parse_args()

    if args.update_all:
        if args.update or args.auto_bootstrap:
            c.err("--update-all is mutually exclusive with --update and --auto-bootstrap")
            return 2
        return update_all_fixtures()

    # Verify both built PDFs exist before doing anything else.
    for label, pdf_path, _, _ in VARIANTS:
        if not pdf_path.exists():
            c.err(f"{pdf_path.relative_to(ROOT)} not found. Run `node render.js` first.")
            return 2

    # --update: refresh both variants of the current data-source fixture.
    if args.update:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        for label, pdf_path, default_fix, local_fix in VARIANTS:
            target = resolve_fixture_path(default_fix, local_fix)
            if not safe_copy(pdf_path, target):
                return 1
            c.ok_pair("Updated fixture", str(target.relative_to(ROOT)))
        return 0

    # --auto-bootstrap or normal compare path.
    # Bootstrap any missing fixtures before comparing.
    if args.auto_bootstrap:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        bootstrapped = False
        for label, pdf_path, default_fix, local_fix in VARIANTS:
            target = resolve_fixture_path(default_fix, local_fix)
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
    for label, pdf_path, default_fix, local_fix in VARIANTS:
        target = resolve_fixture_path(default_fix, local_fix)
        if not target.exists():
            c.err(f"fixture missing at {target.relative_to(ROOT)}.")
            c.detail("To create it from the current dist/resume-*.pdf, run:")
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
    for variant_label, pdf_path, default_fix, local_fix in VARIANTS:
        expected_pdf = resolve_fixture_path(default_fix, local_fix)
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
