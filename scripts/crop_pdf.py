#!/usr/bin/env python3
"""
crop_pdf.py — Post-process Playwright's PDF: crop to ISO A4
              and stamp authoritative metadata.

CROP
────
Chromium's `page.pdf()` quantizes page dimensions to a 0.12-pt grid,
producing pages that are ~0.23 mm oversized in width (and ~0.01 mm
in height). Investigation (see scripts/investigate_crop.js) confirmed
that Chromium anchors content at the lower-left of the page and the
excess sits as empty space at the upper-right edges.

So the crop shaves the excess from the upper-right edges only. The
lower-left corner stays put; we shrink the MediaBox upper-right
inward until width × height equals exactly 210 × 297 mm
(595.276 × 841.890 pt).

This crop has TWO important guarantees:
  1. The page is exactly ISO A4 (210 × 297 mm).
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

Usage:
    python3 scripts/crop_pdf.py <input.pdf> <output.pdf>
    python3 scripts/crop_pdf.py <input.pdf> <output.pdf> --meta dist/pdf_meta.json
    (run from project root)

Requires: pypdf (`pip install pypdf`).
"""

import argparse
import json
import sys
from pathlib import Path
from pypdf import PdfReader, PdfWriter

# True ISO A4 in PDF points (1 pt = 1/72 in).
A4_W_PT = 210 * 72 / 25.4   # 595.275590...
A4_H_PT = 297 * 72 / 25.4   # 841.889763...


def crop_pages(reader: PdfReader, writer: PdfWriter) -> None:
    """Copy reader's pages to writer with MediaBox/CropBox cropped to A4.

    Chromium anchors content at the lower-left of the page and parks
    the ~0.64 pt of width excess (and ~0.03 pt of height excess) as
    empty space at the upper-right edges. So we only shrink the
    upper-right corner inward — the lower-left stays where it is.
    """
    for page in reader.pages:
        box = page.mediabox
        cur_w = float(box.width)
        cur_h = float(box.height)

        # Total excess in each axis; will be shaved entirely from
        # the upper-right edge.
        excess_w = cur_w - A4_W_PT  # ≈ 0.64 pt for Chromium A4 output
        excess_h = cur_h - A4_H_PT  # ≈ 0.03 pt for Chromium A4 output

        if excess_w < -0.001 or excess_h < -0.001:
            # Page is SMALLER than A4 — refuse to "negative-crop" by
            # extending the box, which would just add blank space.
            print(
                f"  ⚠  page is smaller than A4 ({cur_w:.2f} × {cur_h:.2f} pt) — "
                f"leaving as-is",
                file=sys.stderr,
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
    """
    info = {}

    # Copy any pre-existing metadata from the input first.
    if reader.metadata:
        for k, v in reader.metadata.items():
            # pypdf returns keys already prefixed with '/'.
            info[k] = str(v) if v is not None else ''

    # Overlay manifest values.
    if meta_path is not None:
        manifest = json.loads(meta_path.read_text(encoding='utf-8'))
        # Map our manifest keys to PDF /Info keys. We deliberately do
        # NOT override /Creator or /Producer — Chromium's defaults
        # (Chromium / Skia/PDF) pass through, truthfully describing
        # what produced the bytes.
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Crop a PDF to ISO A4 and stamp metadata.")
    parser.add_argument('input', help="Input PDF path")
    parser.add_argument('output', help="Output PDF path")
    parser.add_argument(
        '--meta',
        type=Path,
        default=None,
        help="Optional JSON manifest with title/author/subject/keywords/creator/producer.",
    )
    args = parser.parse_args()

    if args.meta is not None and not args.meta.exists():
        print(f"ERROR: --meta file not found: {args.meta}", file=sys.stderr)
        return 2

    reader = PdfReader(args.input)
    writer = PdfWriter()

    crop_pages(reader, writer)
    apply_metadata(writer, reader, args.meta)

    with open(args.output, 'wb') as f:
        writer.write(f)

    # Report final dimensions for visibility.
    final = PdfReader(args.output)
    p0 = final.pages[0].mediabox
    w_mm = float(p0.width) * 25.4 / 72
    h_mm = float(p0.height) * 25.4 / 72
    print(f"✓ Cropped {len(final.pages)} page(s) to {w_mm:.3f} × {h_mm:.3f} mm")
    if args.meta is not None:
        print(f"✓ Stamped metadata from {args.meta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
