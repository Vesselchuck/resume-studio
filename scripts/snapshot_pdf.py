#!/usr/bin/env python3
"""
snapshot_pdf.py — Visual regression test for the rendered PDF.

Compares the freshly-built `print.pdf` against a committed fixture
`tests/fixtures/expected_print.pdf` using a per-pixel RGB diff with
configurable tolerances. Designed to catch CSS / template / data
regressions that change visible output.

This is a verification TOOL, not a unittest. It lives in scripts/
because unittest discovery in tests/ would import it eagerly and fail
on environments where the optional snapshot dependencies aren't
installed. Run it directly:

    python3 scripts/snapshot_pdf.py [--update]

It is also auto-invoked by render.js as the final pipeline step.

Workflow
────────
1. First build (no fixture yet):
       node render.js
   render.js auto-bootstraps the fixture on its first snapshot
   call when the matching fixture is missing.

2. Routine verification (subsequent builds):
       node render.js
   The snapshot test runs automatically. Exit 0 = match within
   tolerance. Exit 1 = visible regression; side-by-side diff
   images written to tests/fixtures/diff_pageN.png.

3. After an INTENTIONAL change (new content, design tweak, etc.):
       python3 scripts/snapshot_pdf.py --update
   Refreshes the fixture matching the data source of the most recent
   build (read from dist/pdf_meta.json).

4. To refresh BOTH fixtures (default and local) in one go:
       python3 scripts/snapshot_pdf.py --update-both
   Re-runs the full build pipeline twice — once forcing default data,
   once forcing local data (if data/resume.local.yml exists) — and
   replaces the corresponding fixture each time.

Dual fixtures
─────────────
The build can use either data/resume_default.yml (placeholder, committed)
or data/resume.local.yml (real data, gitignored). The snapshot
test reads dist/pdf_meta.json to learn which one was loaded and
picks the matching fixture:

  data_source = 'default' → tests/fixtures/expected_print.pdf       (committed)
  data_source = 'local'   → tests/fixtures/expected_print.local.pdf (gitignored)

This way the visual regression test is meaningful for both modes
without one fixture pretending to represent the other.

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


ROOT = Path(__file__).parent.parent          # project root (scripts/ → ..)
FIXTURE_DIR = ROOT / "tests" / "fixtures"
PRINT_PDF = ROOT / "print.pdf"
PDF_META_FILE = ROOT / "dist" / "pdf_meta.json"
DEFAULT_FIXTURE = FIXTURE_DIR / "expected_print.pdf"
LOCAL_FIXTURE = FIXTURE_DIR / "expected_print.local.pdf"


def resolve_fixture_path():
    """
    Choose the fixture file based on which data file backed the build.

    Reads dist/pdf_meta.json's `data_source` field:
      • 'default' → tests/fixtures/expected_print.pdf (committed)
      • 'local'   → tests/fixtures/expected_print.local.pdf (gitignored)

    Falls back to DEFAULT_FIXTURE if pdf_meta.json is missing or
    malformed (preserves backward compatibility when the snapshot
    tool runs without a fresh build).
    """
    try:
        meta = json.loads(PDF_META_FILE.read_text(encoding='utf-8'))
        source = meta.get('data_source', 'default')
    except (FileNotFoundError, json.JSONDecodeError):
        source = 'default'
    return LOCAL_FIXTURE if source == 'local' else DEFAULT_FIXTURE


def safe_copy(src: Path, dst: Path) -> bool:
    """
    Copy `src` to `dst` with metadata, and convert PermissionError
    into a friendly stderr message instead of a traceback.

    Returns True on success, False on permission error. Callers
    should propagate False as a non-zero exit code.

    Most common cause on Windows: `dst` is the snapshot fixture or
    print.pdf that's currently open in a PDF viewer. The viewer
    holds a write lock on the file. Linux/macOS viewers usually
    don't take this lock, but some IDE preview panes do on every
    platform.
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


def diff_images(Image, ImageChops, actual, expected, page_num: int):
    """
    Compare two same-page images.

    Returns (ok, label, value) where label is the column heading
    (e.g. "Page 1") and value is the result text. Splitting them
    lets the caller emit aligned `c.ok_pair(label, value)` lines.
    Writes diff_pageN.png on failure.
    """
    label = f"Page {page_num}"
    if actual.size != expected.size:
        return False, label, (
            f"size mismatch — actual {actual.size}, expected {expected.size}"
        )

    diff = ImageChops.difference(actual, expected)
    # Per-pixel max-channel difference.
    diff_max = diff.getchannel("R")
    for ch in ("G", "B"):
        diff_max = ImageChops.lighter(diff_max, diff.getchannel(ch))

    bbox = diff_max.getbbox()
    if bbox is None:
        return True, label, "identical"

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
        )

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
        f"REGRESSION — {differing_pixels}/{total_pixels} pixels differ "
        f"({fraction:.4%}, limit {MAX_DIFF_FRACTION:.4%}). "
        f"Diff written to {out_path}"
    )


def update_both_fixtures():
    """
    Refresh BOTH fixtures by running the full build pipeline twice.

    1. Rebuild with RESUME_DATA_SOURCE=default and SKIP_SNAPSHOT=1.
       Copy the resulting print.pdf to expected_print.pdf.
    2. If data/resume.local.yml exists, rebuild with
       RESUME_DATA_SOURCE=local and copy print.pdf to
       expected_print.local.pdf. Otherwise skip step 2 with a notice.

    SKIP_SNAPSHOT prevents render.js from running this very tool
    against the (about-to-be-replaced) fixture and failing.
    """
    import subprocess

    default_yml = ROOT / "data" / "resume_default.yml"
    local_yml = ROOT / "data" / "resume.local.yml"

    def run_build(source):
        env = os.environ.copy()
        env['RESUME_DATA_SOURCE'] = source
        env['SKIP_SNAPSHOT'] = '1'
        result = subprocess.run(
            ['node', 'render.js'],
            cwd=str(ROOT),
            env=env,
        )
        if result.returncode != 0:
            c.err(f"render.js failed for source={source!r} (exit {result.returncode})")
            return False
        return True

    updated = []

    # Step 1: default data.
    if not default_yml.exists():
        c.err(f"{default_yml.relative_to(ROOT)} not found.")
        return 2
    c.info(f"Building with default data ({default_yml.relative_to(ROOT)})")
    if not run_build('default'):
        return 1
    if not PRINT_PDF.exists():
        c.err(f"{PRINT_PDF.relative_to(ROOT)} missing after build.")
        return 1
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    if not safe_copy(PRINT_PDF, DEFAULT_FIXTURE):
        return 1
    updated.append(DEFAULT_FIXTURE)
    c.ok_pair("Updated fixture", str(DEFAULT_FIXTURE.relative_to(ROOT)))

    # Step 2: local data, if present.
    if local_yml.exists():
        c.info(f"Building with local data ({local_yml.relative_to(ROOT)})")
        if not run_build('local'):
            return 1
        if not PRINT_PDF.exists():
            c.err(f"{PRINT_PDF.relative_to(ROOT)} missing after build.")
            return 1
        if not safe_copy(PRINT_PDF, LOCAL_FIXTURE):
            return 1
        updated.append(LOCAL_FIXTURE)
        c.ok_pair("Updated fixture", str(LOCAL_FIXTURE.relative_to(ROOT)))
    else:
        c.info(f"No local data file at {local_yml.relative_to(ROOT)}; "
               f"skipped local fixture refresh.")

    plural = 'fixture' if len(updated) == 1 else 'fixtures'
    c.ok(f"Refreshed {len(updated)} {plural}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--update", action="store_true",
        help="Replace the fixture matching the LAST build's data source "
             "(read from dist/pdf_meta.json) with the current print.pdf.",
    )
    parser.add_argument(
        "--update-both", action="store_true",
        help="Run the full build pipeline twice (once with default data, "
             "once with local data if present) and refresh both fixtures. "
             "Use after intentional design changes that affect both modes.",
    )
    parser.add_argument(
        "--auto-bootstrap", action="store_true",
        help="If the fixture is missing, create it from the current "
             "print.pdf and exit 0 (rather than failing). Intended for "
             "render.js invocation on first build.",
    )
    args = parser.parse_args()

    if args.update_both:
        if args.update or args.auto_bootstrap:
            c.err("--update-both is mutually exclusive with --update and --auto-bootstrap")
            return 2
        return update_both_fixtures()

    if not PRINT_PDF.exists():
        c.err(f"{PRINT_PDF.relative_to(ROOT)} not found. Run `node render.js` first.")
        return 2

    expected_pdf = resolve_fixture_path()

    if args.update:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        if not safe_copy(PRINT_PDF, expected_pdf):
            return 1
        c.ok_pair("Updated fixture", str(expected_pdf.relative_to(ROOT)))
        return 0

    if not expected_pdf.exists():
        if args.auto_bootstrap:
            FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
            if not safe_copy(PRINT_PDF, expected_pdf):
                return 1
            c.info_pair("Created fixture", str(expected_pdf.relative_to(ROOT)))
            c.info_pair("Update with", "python scripts/snapshot_pdf.py --update")
            return 0
        c.err(f"fixture missing at {expected_pdf.relative_to(ROOT)}.")
        c.detail("To create it from the current print.pdf, run:")
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

    actual_pages = render_pdf_pages(pdfium, PRINT_PDF)
    expected_pages = render_pdf_pages(pdfium, expected_pdf)

    if len(actual_pages) != len(expected_pages):
        c.err(f"page count differs — actual {len(actual_pages)}, expected {len(expected_pages)}")
        return 1

    all_ok = True
    for i, (a, e) in enumerate(zip(actual_pages, expected_pages), start=1):
        ok, label, value = diff_images(Image, ImageChops, a, e, i)
        if ok:
            c.ok_pair(label, value)
        else:
            c.err_pair(label, value)
            all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
