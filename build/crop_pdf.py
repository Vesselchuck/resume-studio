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

The Studio's live preview applies the same geometry in memory, to the
page it is about to rasterize, instead of running this file on every
keystroke — see crop_pdfium_page_to_letter(). Both go through
letter_upper_right(), so the two crops cannot disagree.

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
reads for pronunciation.

The same values, with Chromium's /Producer, /Creator and dates, are
also written as an XMP metadata packet (the catalog's /Metadata), the
place PDF/UA and most document-management tools read them from; on a
tagged PDF it also declares PDF/UA-1 (see ACCESSIBILITY). The
stream is labelled /Type /Metadata /Subtype /XML, as ISO 32000
requires; pypdf leaves both out, and veraPDF failed the files on it.

ACCESSIBILITY (PDF/UA-1)
────────────────────────
Chromium tags the PDF (`tagged: true`) but leaves two things a
screen reader and a PDF/UA validator need:
  • Link annotations carry no /Contents, the link's text alternative.
    describe_links() gives each the destination it points to: the
    address for a mailto: link, host and path for a web link.
  • Whatever Chromium does not tag — the page background, the rules,
    the page footer — is left as unmarked content, which PDF/UA
    forbids: content is either tagged or an artifact.
    mark_untagged_as_artifacts() wraps each such drawing operation in
    /Artifact BMC … EMC. Marked content draws nothing, so the pages
    render to the same pixels.
Both run only on a tagged PDF (the deliverable); the preview prints
untagged and never comes here. With them, veraPDF passes every PDF/UA-1
rule, so the tagged PDF's XMP declares it (pdfuaid:part 1). veraPDF
checks what a machine can; PDF/UA also asks for human judgement
(reading order, whether headings and link texts make sense); the
heading order was checked with NVDA and Acrobat on 2026-09-27.

WHAT THE CROP KEEPS
───────────────────
The crop works on a clone of the whole document, not on its pages
copied into an empty file. Copying pages keeps the pages and loses the
catalog: the structure tree that `page.pdf({ tagged: true })` writes
(/StructTreeRoot, /MarkInfo, and each page's /StructParents, which is
what makes the PDF readable by a screen reader), and Chromium's
/ViewerPreferences /DisplayDocTitle, which tells a viewer to show the
title rather than the file name (WCAG technique PDF18). Cloning keeps
all of it, and the crop only moves each page's boxes.

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
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream, NameObject, TextStringObject
from pypdf.xmp import XmpInformation

# Local console helper.
sys.path.insert(0, str(Path(__file__).parent))
import _console as c  # noqa: E402

# True US Letter in PDF points (1 pt = 1/72 in; Letter = 8.5 × 11 in).
LETTER_W_PT = 8.5 * 72   # 612.0
LETTER_H_PT = 11 * 72    # 792.0


def letter_upper_right(left: float, bottom: float, right: float, top: float):
    """The upper-right corner that makes a page box exactly US Letter.

    The one place the crop geometry is computed. crop_pages() applies it
    to the file the CLI writes; crop_pdfium_page_to_letter() applies it
    in memory to the page the live preview rasterizes. Both call this,
    so the preview cannot drift from the deliverable's crop.

    The lower-left stays where it is and the width and height excess is
    shaved from the upper-right, because that is where Chromium parks it
    (see the module docstring).

    Returns (new_right, new_top), or None when the box is SMALLER than
    Letter in either axis — extending it would only add blank space, so
    such a page is left as it is.
    """
    # Total excess in each axis; shaved entirely from the upper-right edge.
    excess_w = (right - left) - LETTER_W_PT
    excess_h = (top - bottom) - LETTER_H_PT
    if excess_w < -0.001 or excess_h < -0.001:
        return None
    return right - excess_w, top - excess_h


def _warn_smaller_than_letter(width: float, height: float) -> None:
    c.warn(
        f"page is smaller than Letter "
        f"({width:.2f} × {height:.2f} pt) — leaving as-is"
    )


def crop_pages(reader: PdfReader, writer: PdfWriter) -> None:
    """Clone reader's document into writer, with every page's
    MediaBox/CropBox cropped to Letter.

    The whole document is cloned, catalog included, so the structure
    tree and the viewer preferences survive (see WHAT THE CROP KEEPS in
    the module docstring). `writer` is expected to be empty.

    Chromium anchors content at the lower-left of the page and parks
    the (small) width and height excess as empty space at the
    upper-right edges. So we only shrink the upper-right corner
    inward — the lower-left stays where it is. The geometry itself is
    letter_upper_right()'s.
    """
    writer.clone_document_from_reader(reader)
    for page in writer.pages:
        box = page.mediabox
        corner = letter_upper_right(
            float(box.left), float(box.bottom),
            float(box.upper_right[0]), float(box.upper_right[1]),
        )
        if corner is None:
            _warn_smaller_than_letter(float(box.width), float(box.height))
            continue

        # Lower-left stays put; pull the upper-right inward by the excess.
        page.mediabox.upper_right = corner
        # Also align CropBox so readers that honor it agree with MediaBox.
        page.cropbox.upper_right = corner


class NoOwnMediaBox(ValueError):
    """The page inherits its MediaBox, which pdfium's box API cannot see."""


def crop_pdfium_page_to_letter(page) -> None:
    """crop_pages()' crop, applied in memory to an open pypdfium2 page.

    For the Studio's live preview. Rasterizing Chromium's raw print with
    these boxes set gives the pixels that rasterizing crop_pages()'
    output gives, without pypdf parsing and re-serializing the whole
    document on every keystroke. Nothing is written anywhere; the
    deliverables still go through crop_pages() and the metadata stamps.

    Mirrors crop_pages() box for box: the MediaBox's upper-right moves
    to letter_upper_right(), and so does the CropBox's (whose lower-left
    stays its own, or the MediaBox's when the page has no CropBox — the
    same default pypdf applies). A page smaller than Letter is left as
    it is, as crop_pages() leaves it.

    pdfium's box getters do not inherit from the page tree, so a page
    without its own MediaBox raises NoOwnMediaBox rather than being
    cropped against a guessed box; the caller falls back to the file
    crop. Chromium writes a MediaBox on every page.
    """
    media = page.get_mediabox(fallback_ok=False)
    if media is None:
        raise NoOwnMediaBox("page has no MediaBox of its own")
    left, bottom, right, top = media
    corner = letter_upper_right(left, bottom, right, top)
    if corner is None:
        _warn_smaller_than_letter(right - left, top - bottom)
        return
    crop_left, crop_bottom, _, _ = page.get_cropbox(fallback_ok=True)
    page.set_mediabox(left, bottom, *corner)
    page.set_cropbox(crop_left, crop_bottom, *corner)


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
    passed /Producer / /Creator values (verified in pypdf 6.16.1, the
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

    /Lang only sets the language. Reading order and structure come from
    the structure tree Chromium writes when printing with
    `tagged: true`, which crop_pages() carries through.

    Returns the language it stamped, for apply_xmp().
    """
    lang = read_lang(meta_path)
    # `writer.root_object` is pypdf's public accessor for the document
    # catalog (verified in pypdf 6.16.1, the pinned version). The
    # leading-underscore `_root_object` works too but is private and
    # subject to rename across versions; the public name is the
    # forward-compatible choice.
    writer.root_object[NameObject('/Lang')] = TextStringObject(lang)
    return lang


def read_lang(meta_path: Path | None) -> str:
    """The manifest's `lang`, stripped, or 'en-US' when there is none."""
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
    return lang


_PDF_DATE = re.compile(
    r"^D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:(Z)|([+-])(\d{2})'?(\d{2})?'?)?$"
)


def pdf_date_to_datetime(value) -> datetime | None:
    """A PDF date string (`D:20260927174004+00'00'`) as a datetime.

    None when it is missing or not in that form: an XMP date that
    disagrees with /Info is worse than none, so an unreadable one is
    left out rather than guessed. A date without a zone is read as UTC.
    """
    if value is None:
        return None
    m = _PDF_DATE.match(str(value).strip())
    if not m:
        return None
    year, month, day, hour, minute, second, z, sign, tzh, tzm = m.groups()
    offset = timedelta(0)
    if sign:
        offset = timedelta(hours=int(tzh), minutes=int(tzm or 0))
        if sign == '-':
            offset = -offset
    try:
        return datetime(
            int(year), int(month or 1), int(day or 1),
            int(hour or 0), int(minute or 0), int(second or 0),
            tzinfo=timezone(offset),
        )
    except ValueError:
        return None


PDFUA_NS = 'http://www.aiim.org/pdfua/ns/id/'


def apply_xmp(writer: PdfWriter, lang: str, pdfua: bool = False) -> None:
    """Write an XMP metadata packet that mirrors the final /Info.

    Call it after apply_metadata(), so it copies the values actually
    stamped — the manifest's over Chromium's. Each /Info key goes to its
    standard XMP property, and a key that is absent or empty in /Info is
    absent here too, so the two never disagree:

      /Title    → dc:title         /Author   → dc:creator
      /Subject  → dc:description   /Keywords → pdf:Keywords
      /Producer → pdf:Producer     /Creator  → xmp:CreatorTool
      /CreationDate → xmp:CreateDate   /ModDate → xmp:ModifyDate

    plus dc:language from the catalog's /Lang.

    With `pdfua`, it also declares PDF/UA-1 (pdfuaid:part 1, ISO
    14289-1 clause 5). crop_and_stamp() asks for that on a tagged PDF,
    the deliverable, which veraPDF passes on every other PDF/UA-1 rule
    (tests/test_pdf_accessibility.py keeps those in place). It never
    claims PDF/A: the files are not built or checked for it.
    """
    info = writer.metadata or {}

    def text(key):
        value = info.get(key)
        return str(value) if value else None

    xmp = XmpInformation.create()
    if text('/Title'):
        xmp.dc_title = {'x-default': text('/Title')}
    if text('/Author'):
        xmp.dc_creator = [text('/Author')]
    if text('/Subject'):
        xmp.dc_description = {'x-default': text('/Subject')}
    if text('/Keywords'):
        xmp.pdf_keywords = text('/Keywords')
    if text('/Producer'):
        xmp.pdf_producer = text('/Producer')
    if text('/Creator'):
        xmp.xmp_creator_tool = text('/Creator')
    created = pdf_date_to_datetime(info.get('/CreationDate'))
    if created is not None:
        xmp.xmp_create_date = created
    modified = pdf_date_to_datetime(info.get('/ModDate'))
    if modified is not None:
        xmp.xmp_modify_date = modified
    xmp.dc_language = [lang]
    writer.xmp_metadata = xmp
    # pypdf writes the packet as a bare stream; ISO 32000 (14.3.2)
    # requires both keys, and PDF/UA validators reject the file without.
    stream = writer.root_object['/Metadata'].get_object()
    stream[NameObject('/Type')] = NameObject('/Metadata')
    stream[NameObject('/Subtype')] = NameObject('/XML')
    if pdfua:
        # pypdf has no property for the PDF/UA identification schema, so
        # its description goes into the packet pypdf wrote, as the last
        # child of rdf:RDF.
        packet = stream.get_data().decode('utf-8')
        end = packet.rindex('</rdf:RDF>')
        ua = ('<rdf:Description rdf:about="" xmlns:pdfuaid="' + PDFUA_NS + '">'
              '<pdfuaid:part>1</pdfuaid:part></rdf:Description>')
        stream.set_data((packet[:end] + ua + packet[end:]).encode('utf-8'))


def is_tagged(writer: PdfWriter) -> bool:
    """Whether Chromium wrote a structure tree (the deliverable) or not."""
    mark = writer.root_object.get('/MarkInfo')
    return (
        '/StructTreeRoot' in writer.root_object
        and mark is not None
        and bool(mark.get_object().get('/Marked'))
    )


def link_description(uri: str) -> str | None:
    """A link's text alternative: where it goes, in the words a reader
    would see. The address of a mailto: or tel: link; host and path of
    a web link, without the scheme or a trailing slash. None for a URI
    with nothing readable in it."""
    uri = uri.strip()
    for scheme in ('mailto:', 'tel:'):
        if uri.lower().startswith(scheme):
            rest = uri[len(scheme):].split('?', 1)[0]
            return rest or None
    m = re.match(r'^[a-z][a-z0-9+.-]*://(?:www\.)?(.*)$', uri, re.IGNORECASE)
    rest = (m.group(1) if m else uri).rstrip('/')
    return rest or None


def describe_links(writer: PdfWriter) -> int:
    """Give every link annotation without one a /Contents (PDF/UA-1
    7.18.1 and 7.18.5). Returns how many were described."""
    count = 0
    for page in writer.pages:
        for ref in page.get('/Annots') or []:
            annot = ref.get_object()
            if annot.get('/Subtype') != '/Link' or annot.get('/Contents'):
                continue
            action = annot.get('/A')
            uri = action.get_object().get('/URI') if action is not None else None
            text = link_description(str(uri)) if uri else None
            if text:
                annot[NameObject('/Contents')] = TextStringObject(text)
                count += 1
    return count


# Content-stream operators, by what they do (ISO 32000, Annex A).
_PATH = {b'm', b'l', b'c', b'v', b'y', b'h', b're'}
_PAINT = {b'f', b'F', b'f*', b'S', b's', b'B', b'B*', b'b', b'b*'}
_SHOW = {b'Tj', b'TJ', b"'", b'"', b'Do', b'sh', b'INLINE IMAGE'}


def mark_untagged_as_artifacts(writer: PdfWriter) -> int:
    """Wrap drawing that is outside every marked-content sequence in
    /Artifact BMC … EMC (PDF/UA-1 7.1: content is tagged or an
    artifact). Returns how many operations were wrapped.

    A path is wrapped whole, from its first construction operator to
    the operator that paints it: nothing may come between the two but
    a clip (W, W*). A path that is only a clip (ending in n) paints
    nothing and is left as it is. Text is wrapped one show operator at
    a time, inside its BT … ET; an XObject, a shading and an inline
    image, one operator each. Drawing already inside a marked-content
    sequence — Chromium's tagged content, or anything else — is not
    touched.
    """
    artifact = ([NameObject('/Artifact')], b'BMC')
    end = ([], b'EMC')
    wrapped = 0
    for page in writer.pages:
        contents = page.get_contents()
        if contents is None:
            continue
        stream = ContentStream(contents, writer)
        out = []
        depth = 0
        path_start = None
        changed = False
        for operands, op in stream.operations:
            if op in (b'BDC', b'BMC'):
                depth += 1
                out.append((operands, op))
            elif op == b'EMC':
                depth = max(0, depth - 1)
                out.append((operands, op))
            elif op in _PATH:
                if depth == 0 and path_start is None:
                    path_start = len(out)
                out.append((operands, op))
            elif op in _PAINT or op == b'n':
                if depth == 0 and op != b'n':
                    out.insert(path_start if path_start is not None else len(out), artifact)
                    out.append((operands, op))
                    out.append(end)
                    wrapped += 1
                    changed = True
                else:
                    out.append((operands, op))
                path_start = None
            elif op in _SHOW and depth == 0:
                out.extend([artifact, (operands, op), end])
                wrapped += 1
                changed = True
            else:
                out.append((operands, op))
        if changed:
            stream.operations = out
            page.replace_contents(stream)
            page.compress_content_streams()
    return wrapped


def crop_and_stamp(reader: PdfReader, meta_path: Path | None) -> PdfWriter:
    """The whole post-process, in its one order: crop, /Info, /Lang, XMP,
    and on a tagged PDF the link descriptions and artifacts.

    Both callers go through this — main() below and the Studio worker's
    crop op (build/worker.py) — so a step added here reaches both. They
    used to spell the sequence out separately.
    """
    writer = PdfWriter()
    crop_pages(reader, writer)
    apply_metadata(writer, reader, meta_path)
    lang = apply_language(writer, meta_path)
    tagged = is_tagged(writer)
    apply_xmp(writer, lang, pdfua=tagged)
    if tagged:
        describe_links(writer)
        mark_untagged_as_artifacts(writer)
    return writer


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
             "Errors and warnings still print. Used by resume.js's dual-PDF "
             "flow to avoid emitting identical summary lines twice — the "
             "first crop invocation runs normally, the second runs with "
             "--quiet so only the per-variant 'Wrote PDF' lines vary.",
    )
    args = parser.parse_args()

    if args.meta is not None and not args.meta.exists():
        c.err(f"--meta file not found: {args.meta}")
        return 2

    writer = crop_and_stamp(PdfReader(args.input), args.meta)

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
