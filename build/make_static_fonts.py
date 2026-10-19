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
is tracked at 0.1em, and so are the section headings and the labels of
a details block. As CSS letter-spacing, pdftotext takes a gap that wide
between two letters of a 9pt TrueType font for a word break and reads
"I M P E R AT O R"; Xpdf 4.06's pdftotext (the one Git for Windows
ships) read the section headings "C E RT I F ICAT ION S" and
"E DUCAT ION" the same way, and poppler's read a details label as
"SECURIT Y CLEARANCE". The same space built into the advances is no gap
at all to an extractor, since each letter's width now includes it, and
the page is pixel for pixel the same (checked at 4× on the template
résumé). The stylesheet then sets letter-spacing to 0 on that text, and
tests/test_font_faces.js checks that it does.

NO KERNING AGAINST A SPACE
──────────────────────────
Both families kern some letters against the space glyph (GPOS pair
adjustments). Manrope has 35 pairs per cut, all a capital before a
space: L, Lslash, Lcaron and Lacute by −0.05em, Y-like letters by
−0.04em, T-like by −0.03em, K-like by −0.02em. Newsreader has 207 to
246 per cut, capitals such as A, V, Y, K and quotes on either side of
a space, most by −0.02em to −0.036em. The no-break space has the same
pairs. Kerned that way, the letter eats most of the space: in
"Digital Tools in ESL Classrooms" (Manrope 400, 10pt) the L's advance
ended at 120.74pt and the 2.0pt-wide space began at 120.24pt, and
pdftotext -raw (Xpdf 4.00) and Tesseract at 300 dpi read
"ESLClassrooms". So every cut drops each pair with a space or a
no-break space on either side, in both GPOS pair formats and in a
legacy kern table if a source ever has one; every other pair is kept
as it was. A class-based subtable then puts the space in class 0,
which must carry no adjustment, so the cut stops if one does. On the
template résumé only the text after such a pair moves, right by 0.3
to 0.5pt; nothing moves vertically and the layout is unchanged.
tests/test_font_faces.js checks the files for any pair left.

RUNNING IT
──────────
A one-off tool, not a build step: the files it writes are committed.
It needs fontTools and brotli, which the project does not otherwise
use and requirements.txt does not list:

    py -m pip install fonttools==4.67.0 brotli==1.2.0
    py build/make_static_fonts.py

It rewrites the files in INSTANCES and nothing else, byte for byte the
same each time from the same sources and the same fontTools and brotli.
The committed files were cut with fontTools 4.67.0 and brotli 1.2.0,
which also reproduced the earlier cut, made before the spaces were
unkerned, byte for byte. Another fontTools can lay the tables out
differently and another brotli compress them differently, so a run
with other versions can change the bytes, and possibly the glyphs, of
every file. Compare the PDFs' pixels before committing what it writes.
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
    # The labels of a details block: 600, tracked the same.
    ("Manrope-600-tracked0.1.woff2", "Manrope.woff2", "Manrope Tracked", {"wght": 600}, 0.1),
    # The name: 30pt = 40px.
    ("Newsreader-600-opsz40.woff2", "Newsreader.woff2", "Newsreader", {"wght": 600, "opsz": 40}, 0),
    # Section headings: 14pt = 18.67px, tracked at 0.1em like the role.
    ("Newsreader-700-opsz18.67-tracked0.1.woff2", "Newsreader.woff2", "Newsreader", {"wght": 700, "opsz": 56 / 3}, 0.1),
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


# The space and the no-break space: no glyph is kerned against them.
SPACES = (0x0020, 0x00A0)


def _has_adjustment(record):
    """Whether a PairPos record moves either glyph at all."""
    values = (getattr(record, "Value1", None), getattr(record, "Value2", None))
    return any(getattr(v, field) for v in values if v is not None for field in vars(v))


def _unkern_spaces(font, out):
    """Drop every kerning pair with a space on either side; keep the rest."""
    cmap = font.getBestCmap()
    spaces = {cmap[cp] for cp in SPACES if cp in cmap}
    lookups = font["GPOS"].table.LookupList.Lookup if "GPOS" in font else []
    for lookup in lookups:
        for sub in lookup.SubTable:
            if lookup.LookupType == 9:   # Extension: the real subtable is inside
                sub = sub.ExtSubTable
            if getattr(sub, "LookupType", None) != 2:
                continue
            if sub.Format == 1:   # glyph pairs: one PairSet per first glyph
                kept = []
                for first, pairs in zip(sub.Coverage.glyphs, sub.PairSet):
                    if first in spaces:
                        continue
                    pairs.PairValueRecord = [r for r in pairs.PairValueRecord if r.SecondGlyph not in spaces]
                    pairs.PairValueCount = len(pairs.PairValueRecord)
                    if pairs.PairValueRecord:
                        kept.append((first, pairs))
                sub.Coverage.glyphs = [first for first, _ in kept]
                sub.PairSet = [pairs for _, pairs in kept]
                sub.PairSetCount = len(sub.PairSet)
            else:   # class pairs: a glyph left out of ClassDef2 is class 0
                if any(_has_adjustment(row.Class2Record[0]) for row in sub.Class1Record):
                    sys.exit(f"{out}: class 0 of a kerning subtable moves glyphs; a space put there would still be kerned")
                sub.Coverage.glyphs = [g for g in sub.Coverage.glyphs if g not in spaces]
                for glyph in spaces:
                    sub.ClassDef1.classDefs.pop(glyph, None)
                    sub.ClassDef2.classDefs.pop(glyph, None)
    if "kern" in font:
        for table in font["kern"].kernTables:
            pairs = getattr(table, "kernTable", {})
            for pair in [p for p in pairs if spaces & set(p)]:
                del pairs[pair]


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
        _unkern_spaces(static, out)
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
