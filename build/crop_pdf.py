#!/usr/bin/env python3
"""
crop_pdf.py — Post-process Playwright's PDF: crop to US Letter
              and stamp authoritative metadata.

CROP
────
Chromium's `page.pdf()` quantizes page dimensions to a 0.12-pt grid,
producing pages that are slightly oversized. Chromium anchors content
at the lower-left of the page, so the excess sits as empty space at
the upper-right edges.

So the crop shaves the excess from the upper-right edges only. The
lower-left corner stays put; we shrink the MediaBox upper-right
inward until width × height equals exactly 8.5 × 11 in
(612 × 792 pt).

This crop has TWO important guarantees:
  1. The page is exactly US Letter (8.5 × 11 in).
  2. If the source HTML uses uniform padding (e.g. 0.5 in on all four
     sides), the visible margins in the cropped PDF will be EXACTLY
     that value on all four sides. (A symmetric/centered crop would
     fail this — it would shift the lower-left inward and reduce the
     left and bottom margins below the requested value.)

The crop only adjusts the MediaBox (the page's physical extent).
No content is moved; nothing is rasterized; the PDF stays vector.

METADATA
────────
With --meta <file>, reads a JSON manifest produced by build.py and
writes its values into the PDF's /Info dictionary (Title, Author,
Subject, Keywords). /Creator and /Producer are NOT overridden —
Chromium's defaults (Chromium / Skia/PDF) pass through, truthfully
describing what produced the bytes. Without --meta, all metadata
present on the input PDF is preserved as-is.

The manifest's `lang` field (default 'en-US' if absent) is stamped
onto the PDF catalog's /Lang entry — this is what assistive tech
reads for pronunciation. Note: /Lang alone is a Level 1 a11y win;
full screen-reader support requires PDF tagging (a structure tree),
which Chromium's page.pdf() does not produce. See L4 in the audit.

Usage:
    python3 build/crop_pdf.py <input.pdf> <output.pdf>
    python3 build/crop_pdf.py <input.pdf> <output.pdf> --meta dist/pdf_meta.json
    (run from project root)

Requires: pypdf (`pip install pypdf`).
"""

import sys

# Suppress writing of __pycache__/ next to source files. Set this
# before any other (non-builtin) import. Equivalent to `python -B`
# but enforces the no-cache rule even for direct invocations.
sys.dont_write_bytecode = True

import argparse
import json
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject

# Local console helper.
sys.path.insert(0, str(Path(__file__).parent))
import _console as c  # noqa: E402

# True US Letter in PDF points (1 pt = 1/72 in; Letter = 8.5 × 11 in).
LETTER_W_PT = 8.5 * 72   # 612.0
LETTER_H_PT = 11 * 72    # 792.0


def crop_pages(reader: PdfReader, writer: PdfWriter) -> None:
    """Copy reader's pages to writer with MediaBox/CropBox cropped to Letter.

    Chromium anchors content at the lower-left of the page and parks
    the (small) width and height excess as empty space at the
    upper-right edges. So we only shrink the upper-right corner
    inward — the lower-left stays where it is.
    """
    for page in reader.pages:
        box = page.mediabox
        cur_w = float(box.width)
        cur_h = float(box.height)

        # Total excess in each axis; will be shaved entirely from
        # the upper-right edge.
        excess_w = cur_w - LETTER_W_PT
        excess_h = cur_h - LETTER_H_PT

        if excess_w < -0.001 or excess_h < -0.001:
            # Page is SMALLER than Letter — refuse to "negative-crop" by
            # extending the box, which would just add blank space.
            c.warn(
                f"page is smaller than Letter "
                f"({cur_w:.2f} × {cur_h:.2f} pt) — leaving as-is"
            )
            writer.add_page(page)
            continue

        # Lower-left stays put; pull the upper-right inward by the excess.
        new_urx = float(box.upper_right[0]) - excess_w
        new_ury = float(box.upper_right[1]) - excess_h

        page.mediabox.upper_right = (new_urx, new_ury)
        # Also align CropBox so readers that honor it agree with MediaBox.
        page.cropbox.upper_right = (new_urx, new_ury)

        writer.add_page(page)


def apply_metadata(writer: PdfWriter, reader: PdfReader, meta_path: Path | None) -> None:
    """
    Stamp the writer's /Info dictionary.

    Precedence (highest first):
      1. Values from --meta JSON manifest, if provided.
      2. Values present in the input PDF's /Info dictionary.
    Any field not in either source is left blank.

    /Producer and /Creator preservation
    ───────────────────────────────────
    Chromium's page.pdf() writes /Producer="Skia/PDF mNNN" and
    /Creator="Mozilla/5.0 …" (or similar). We want both to pass
    through to the cropped output so the metadata truthfully describes
    what produced the bytes. The mechanism is two-step:

      1. Copy ALL /Info entries from the input into `info`, including
         /Producer and /Creator.
      2. Overlay only the four manifest-mapped keys (title, author,
         subject, keywords). /Producer and /Creator are NOT in the
         mapping, so they keep their copied-from-input values.

    The final `writer.add_metadata(info)` call respects explicitly-
    passed /Producer / /Creator values (verified in pypdf 5.9.0, the
    pinned version). Future pypdf versions could in principle change
    that — the regression gate is
    `test_does_not_override_creator_or_producer` in
    tests/test_crop_pdf.py, which round-trips a synthetic PDF and
    asserts the input's values survive. A pypdf bump that breaks this
    will fail that test before reaching production.
    """
    info = {}

    # Copy any pre-existing metadata from the input first. This is
    # what carries /Producer and /Creator through (see docstring).
    if reader.metadata:
        for k, v in reader.metadata.items():
            # pypdf returns keys already prefixed with '/'.
            info[k] = str(v) if v is not None else ''

    # Overlay manifest values.
    if meta_path is not None:
        manifest = json.loads(meta_path.read_text(encoding='utf-8'))
        # Map our manifest keys to PDF /Info keys. The mapping
        # deliberately omits /Creator and /Producer — they pass through
        # from the input via the copy above. See the docstring's
        # "/Producer and /Creator preservation" section.
        mapping = {
            'title':    '/Title',
            'author':   '/Author',
            'subject':  '/Subject',
            'keywords': '/Keywords',
        }
        for src, dst in mapping.items():
            if src in manifest and manifest[src]:
                info[dst] = str(manifest[src])

    if info:
        writer.add_metadata(info)


def apply_language(writer: PdfWriter, meta_path: Path | None) -> None:
    """
    Stamp the PDF catalog's /Lang entry from the manifest.

    The /Lang entry on the document's root catalog is what assistive
    technology (screen readers, refreshable braille displays) reads to
    pick pronunciation rules and voice. /Info's /Lang is metadata that
    most AT does not consult.

    Defaults silently to en-US if the manifest is missing or has no
    'lang' key — better to ship a reasonable default than nothing,
    since untagged language is treated as "unspecified" by AT and
    can produce wrong-language pronunciation.

    Note: this is a Level 1 accessibility improvement only. Without a
    /StructTreeRoot (i.e. proper PDF tagging), reading order and
    semantic structure are still inaccessible to AT. /Lang alone helps
    pronunciation but does not make the PDF screen-reader-friendly.
    """
    lang = 'en-US'
    if meta_path is not None:
        try:
            manifest = json.loads(meta_path.read_text(encoding='utf-8'))
            value = manifest.get('lang')
            if isinstance(value, str) and value.strip():
                lang = value.strip()
        except (OSError, json.JSONDecodeError):
            # Manifest unreadable — keep the default rather than fail.
            pass
    # `writer.root_object` is pypdf's public accessor for the document
    # catalog (verified in pypdf 5.9.0, the pinned version). The
    # leading-underscore `_root_object` works too but is private and
    # subject to rename across versions; the public name is the
    # forward-compatible choice.
    writer.root_object[NameObject('/Lang')] = TextStringObject(lang)


def main() -> int:
    parser = argparse.ArgumentParser(description="Crop a PDF to US Letter and stamp metadata.")
    parser.add_argument('input', help="Input PDF path")
    parser.add_argument('output', help="Output PDF path")
    parser.add_argument(
        '--meta',
        type=Path,
        default=None,
        help="Optional JSON manifest with title/author/subject/keywords/lang.",
    )
    parser.add_argument(
        '--quiet', action='store_true',
        help="Suppress the success-summary lines (Cropped, Stamped metadata). "
             "Errors and warnings still print. Used by render.js's dual-PDF "
             "flow to avoid emitting identical summary lines twice — the "
             "first crop invocation runs normally, the second runs with "
             "--quiet so only the per-variant 'Wrote PDF' lines vary.",
    )
    args = parser.parse_args()

    if args.meta is not None and not args.meta.exists():
        c.err(f"--meta file not found: {args.meta}")
        return 2

    reader = PdfReader(args.input)
    writer = PdfWriter()

    crop_pages(reader, writer)
    apply_metadata(writer, reader, args.meta)
    apply_language(writer, args.meta)

    try:
        with open(args.output, 'wb') as f:
            writer.write(f)
    except PermissionError:
        # Most common cause on Windows: a PDF viewer (Adobe Reader,
        # Edge, Chrome's built-in viewer, SumatraPDF etc.) holds a
        # write lock on the file while displaying it. Linux/macOS
        # viewers usually don't take this lock, but some IDE PDF
        # previews on any platform will. Print actionable guidance
        # instead of a raw stack trace.
        c.err(f"Permission denied writing {args.output}.")
        c.detail("Another program is holding the file open. Most often this is")
        c.detail("a PDF viewer (Adobe Reader, Edge, Chrome, SumatraPDF, an IDE")
        c.detail("preview pane). Close the viewer and re-run.")
        return 1

    if args.quiet:
        return 0

    # Report final dimensions for visibility. The relative-to-cwd display
    # path keeps the output uniform with build.py and snapshot_pdf.py.
    final = PdfReader(args.output)
    p0 = final.pages[0].mediabox
    w_in = float(p0.width) / 72
    h_in = float(p0.height) / 72
    n = len(final.pages)
    page_word = 'page' if n == 1 else 'pages'
    c.ok_pair("Cropped", f"{n} {page_word}, {w_in:g} × {h_in:g} in (US Letter)")

    # crop_pdf does two distinct stamps — /Info dict (title/author/
    # subject/keywords) from the manifest, and /Lang catalog entry,
    # both derived from pdf_meta.json. The lang value is part of the
    # manifest, so the metadata line implicitly covers it; no need
    # to spell it out in the log.
    if args.meta is not None:
        try:
            display_meta = args.meta.relative_to(Path.cwd())
        except ValueError:
            display_meta = args.meta
        c.ok_pair("Stamped metadata", str(display_meta))
    else:
        # No --meta arg: /Lang may still have been stamped via the
        # default fallback. Report it standalone if so.
        catalog_lang = final.trailer['/Root'].get('/Lang')
        if catalog_lang is not None:
            c.ok_pair("Stamped /Lang", str(catalog_lang))
    return 0


if __name__ == "__main__":
    sys.exit(main())
