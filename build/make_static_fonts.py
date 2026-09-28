"""
Cut the static fonts in fonts/ from the variable ones in fonts/variable/.

WHY STATIC
──────────
Chromium's PDF backend (Skia) writes any font that declares variation
axes as Type 3: every glyph re-emitted as a little drawing program,
with no hinting and nothing a PDF reader can treat as a real font.
Static instances with TrueType outlines are embedded as TrueType. On
the template résumé that made the preview's raster step and the PDF
itself much cheaper; the numbers are in CHANGELOG.md.

WHAT IS CUT
───────────
One file per weight and, for Newsreader, per optical size the
stylesheet actually uses — INSTANCES below. Chromium sets Newsreader's
`opsz` axis to the font size in CSS px (not pt) when
font-optical-sizing is auto, so each Newsreader instance is pinned at
the px size of the one element that uses it: that keeps every glyph
the shape it had with the variable font. styles/_fonts.scss names
each face after its optical size, and tests/test_font_faces.js checks
that every element drawn in Newsreader uses the face cut for its size
and that no text asks for a weight that has no file.

Change a size or a weight in the stylesheet and the test fails until
this table, _fonts.scss and the files agree again.

TRACKED CUTS
────────────
A cut can also carry letter-spacing in the font itself: every advance
width made wider by a fraction of the em. The role line under the name
is tracked at 0.1em. As CSS letter-spacing, pdftotext takes a gap that
wide between two letters of a 9pt TrueType font for a word break and
reads "I M P E R AT O R". The same space built into the advances is no
gap at all to an extractor, since each letter's width now includes it,
and the page is pixel for pixel the same (checked at 4× on the template
résumé). The stylesheet then sets letter-spacing to 0 on that text, and
tests/test_font_faces.js checks that it does.

RUNNING IT
──────────
A one-off tool, not a build step: the files it writes are committed.
It needs fontTools and brotli, which the project does not otherwise
use and requirements.txt does not list:

    py -m pip install fonttools brotli
    py build/make_static_fonts.py

It rewrites the files in INSTANCES and nothing else, byte for byte the
same each time from the same sources.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "fonts"
SOURCES = FONTS / "variable"

# (output file, source file, family name inside the file, axis location,
# tracking in em built into the advances)
# The family name is what a PDF viewer lists; CSS uses its own names
# (styles/_fonts.scss).
INSTANCES = [
    ("Manrope-350.woff2", "Manrope.woff2", "Manrope", {"wght": 350}, 0),
    ("Manrope-400.woff2", "Manrope.woff2", "Manrope", {"wght": 400}, 0),
    ("Manrope-500.woff2", "Manrope.woff2", "Manrope", {"wght": 500}, 0),
    ("Manrope-600.woff2", "Manrope.woff2", "Manrope", {"wght": 600}, 0),
    # The role line under the name: 500, tracked at 0.1em (--ls-wide).
    ("Manrope-500-tracked0.1.woff2", "Manrope.woff2", "Manrope Tracked", {"wght": 500}, 0.1),
    # The name: 30pt = 40px.
    ("Newsreader-600-opsz40.woff2", "Newsreader.woff2", "Newsreader", {"wght": 600, "opsz": 40}, 0),
    # Section headings: 14pt = 18.67px.
    ("Newsreader-700-opsz18.67.woff2", "Newsreader.woff2", "Newsreader", {"wght": 700, "opsz": 56 / 3}, 0),
    # The letter's signature: 10pt = 13.33px.
    ("Newsreader-600-opsz13.33.woff2", "Newsreader.woff2", "Newsreader", {"wght": 600, "opsz": 40 / 3}, 0),
]


def _label(location, tracking):
    parts = [f"{location['wght']:g}"]
    if "opsz" in location:
        parts.append(f"opsz{round(location['opsz'], 2):g}")
    if tracking:
        parts.append(f"tracked{tracking:g}")
    return " ".join(parts)


def _track(font, em):
    """Widen every glyph's advance by `em`, on the right: CSS letter-spacing."""
    add = round(em * font["head"].unitsPerEm)
    metrics = font["hmtx"].metrics
    for glyph, (advance, lsb) in list(metrics.items()):
        if advance > 0:   # combining marks keep their zero advance, as with letter-spacing
            metrics[glyph] = (advance + add, lsb)
    font["hhea"].advanceWidthMax = max(advance for advance, _ in metrics.values())


def _rename(font, family, location, tracking):
    """Names that say which instance this is; the rest of the table is kept."""
    label = _label(location, tracking)
    full = f"{family} {label}"
    # PostScript names allow no spaces (Chromium mangles one in the PDF).
    postscript = f"{family.replace(' ', '')}-{label.replace(' ', '-').replace('.', '_')}"
    name = font["name"]
    for rec in list(name.names):
        if rec.nameID in (16, 17, 18, 21, 22, 25):
            name.removeNames(nameID=rec.nameID)
    version = name.getDebugName(5) or ""
    values = {1: full, 2: "Regular", 3: f"{version};{postscript}", 4: full, 6: postscript}
    for name_id, value in values.items():
        name.setName(value, name_id, 3, 1, 0x409)
        name.setName(value, name_id, 1, 0, 0)


def main():
    try:
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer
        import brotli  # noqa: F401  (fontTools needs it to write WOFF2)
    except ImportError as err:
        sys.exit(f"{err}. This tool needs fontTools and brotli: py -m pip install fonttools brotli")

    for out, source, family, location, tracking in INSTANCES:
        font = TTFont(SOURCES / source)
        if "glyf" not in font:
            sys.exit(f"{source} has no TrueType outlines; Chromium would still write it as Type 3")
        static = instancer.instantiateVariableFont(font, location)
        if "fvar" in static:
            sys.exit(f"{out}: an axis was left free; pin every axis of {source}")
        if tracking:
            _track(static, tracking)
        _rename(static, family, location, tracking)
        # The source's own date, not today's: the same sources give the
        # same bytes, so running this again changes nothing in git.
        static["head"].modified = font["head"].modified
        static.recalcTimestamp = False
        static.flavor = "woff2"
        static.save(FONTS / out)
        print(f"  wrote fonts/{out} ({(FONTS / out).stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
