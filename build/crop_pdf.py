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

The crop only moves each page's MediaBox (the page's physical extent)
and CropBox. No content is moved; nothing is rasterized; the PDF stays
vector. On a tagged PDF the content streams are rewritten as well, but
only to add marked-content operators, which draw nothing (see
ACCESSIBILITY).

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
    address for a mailto: link, host and path for a web link, the page
    for a link inside the document.
  • Whatever Chromium does not tag — the page background, the rules,
    the page footer — is left as unmarked content, which PDF/UA
    forbids: content is either tagged or an artifact.
    mark_untagged_as_artifacts() wraps each such drawing operation in
    /Artifact BMC … EMC, inside a form XObject when the form holds
    tagged content itself. Marked content draws nothing, so the pages
    render to the same pixels.
Both run only on a tagged PDF (the deliverable); the preview prints
untagged and never comes here. With them, veraPDF passes every PDF/UA-1
rule, so the tagged PDF's XMP declares it (pdfuaid:part 1), unless the
marking had to leave some drawing unmarked, which it warns about and
which Chromium's output never needs. veraPDF
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
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, ContentStream, DecodedStreamObject,
                           EncodedStreamObject, IndirectObject, NameObject,
                           TextStringObject)
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


#: Largest excess the crop shaves without a word. Chromium's quantization
#: overshoots by at most 0.12 pt per axis; anything past a point is not
#: rounding but a page printed at another size (a Legal or A4 @page, a
#: stylesheet whose size rule was lost), and the crop would cut real
#: content off its top or right edge.
OVERSHOOT_WARN_PT = 1.0


def _warn_if_overshoot(width: float, height: float) -> None:
    """Warn when a page is larger than Letter by more than rounding."""
    if max(width - LETTER_W_PT, height - LETTER_H_PT) > OVERSHOOT_WARN_PT:
        c.warn(
            f"page is {width:.2f} × {height:.2f} pt, well over Letter "
            f"(612 × 792) — cropping it cuts off whatever is beyond the "
            f"top and right edges. Check the page size the stylesheet sets"
        )


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
        _warn_if_overshoot(float(box.width), float(box.height))

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
    _warn_if_overshoot(right - left, top - bottom)
    crop_left, crop_bottom, _, _ = page.get_cropbox(fallback_ok=True)
    page.set_mediabox(left, bottom, *corner)
    page.set_cropbox(crop_left, crop_bottom, *corner)


#: Characters XML 1.0 cannot hold, even escaped: C0 controls other than
#: tab, LF and CR, lone surrogates (U+D800–U+DFFF) and U+FFFE/U+FFFF.
#: YAML lets "\x01" or "\f" through in a double-quoted string; copied
#: into the XMP they made the packet ill-formed, and pypdf and PDF/UA
#: validators reject the whole packet. A lone surrogate ("\ud800" in
#: JSON or YAML) never got that far: no UTF can encode it, and writing
#: /Title or /Lang stopped the crop with UnicodeEncodeError.
#: Removed from the manifest values before /Info is stamped, so /Info
#: and the XMP apply_xmp() copies from it still say the same thing.
#: A str.translate() table (each code point mapped to None, i.e. deleted)
#: rather than a regex character class, so the set is spelled out as
#: code points and no range can be misread as wider than it is.
_XML_ILLEGAL = dict.fromkeys(
    [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), *range(0xD800, 0xE000),
     0xFFFE, 0xFFFF])


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
    passed /Producer / /Creator values (verified in pypdf 6.19.0, the
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
                info[dst] = str(manifest[src]).translate(_XML_ILLEGAL)

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
    # catalog (verified in pypdf 6.19.0, the pinned version). The
    # leading-underscore `_root_object` works too but is private and
    # subject to rename across versions; the public name is the
    # forward-compatible choice.
    writer.root_object[NameObject('/Lang')] = TextStringObject(lang)
    return lang


def read_lang(meta_path: Path | None) -> str:
    """The manifest's `lang`, stripped, or 'en-US' when there is none.

    Characters XML cannot hold are removed first, as from the /Info
    values (_XML_ILLEGAL): the language goes into the XMP packet as
    dc:language, and a U+0001 in the language made that packet
    ill-formed in a PDF that still declared PDF/UA-1."""
    lang = 'en-US'
    if meta_path is not None:
        try:
            manifest = json.loads(meta_path.read_text(encoding='utf-8'))
            value = manifest.get('lang')
            if isinstance(value, str):
                value = value.translate(_XML_ILLEGAL).strip()
            if isinstance(value, str) and value:
                lang = value
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
    (tests/test_pdf_accessibility.py keeps those in place), unless the
    artifact marking had to leave drawing unmarked. It never claims
    PDF/A: the files are not built or checked for it.
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
    7.18.1 and 7.18.5). Returns how many were described.

    A web or mail link is described by link_description(). A link
    inside the document (`<a href="#id">`, which Chromium writes as a
    /Dest, or a /GoTo action) has no address to read out, so it says
    which page it goes to. A file: link is described like any other, and
    warned about: it puts a path on this computer in the PDF."""
    count = 0
    pages = None
    for page in writer.pages:
        for ref in page.get('/Annots') or []:
            annot = ref.get_object()
            if annot.get('/Subtype') != '/Link' or annot.get('/Contents'):
                continue
            action = annot.get('/A')
            action = action.get_object() if action is not None else {}
            uri = action.get('/URI')
            dest = annot.get('/Dest')
            if dest is None and action.get('/S') == '/GoTo':
                dest = action.get('/D')
            text = None
            if uri:
                if str(uri).strip().lower().startswith('file:'):
                    c.warn(f"a link points at a file on this computer ({uri}) "
                           f"— the PDF shows that path to whoever opens it")
                text = link_description(str(uri))
            elif dest is not None:
                if pages is None:
                    pages = {p.indirect_reference.idnum: n
                             for n, p in enumerate(writer.pages, 1)}
                number = _destination_page(writer, dest, pages)
                text = f"Link to page {number}" if number else None
            if text:
                annot[NameObject('/Contents')] = TextStringObject(text)
                count += 1
    return count


def _destination_page(writer: PdfWriter, dest, pages) -> int | None:
    """The 1-based page a destination goes to: an explicit one (an
    array starting with the page) or a named one. None if unknown."""
    dest = dest.get_object()
    if not isinstance(dest, ArrayObject):
        named = writer.named_destinations.get(str(dest))
        if named is None:
            return None
        number = writer.get_destination_page_number(named)
        return None if number is None or number < 0 else number + 1
    target = dest[0] if dest else None
    return pages.get(target.idnum) if isinstance(target, IndirectObject) else None


# Content-stream operators, by what they do (ISO 32000, Annex A).
_PATH = {b'm', b'l', b'c', b'v', b'y', b'h', b're'}
_PAINT = {b'f', b'F', b'f*', b'S', b's', b'B', b'B*', b'b', b'b*'}
_SHOW = {b'Tj', b'TJ', b"'", b'"', b'Do', b'sh', b'INLINE IMAGE'}


def _artifact_spans(ops, holds_tags=lambda i: False, covers=lambda i: True):
    """(first, last) operator index of each drawing operation, in
    `ops` (a content stream's operators in order), that lies outside
    every marked-content sequence that covers it. covers(index) says
    whether the BMC or BDC there does: an /Artifact or a sequence with
    an MCID (_covers) — a sequence that is neither, such as optional
    content (/OC) or a bare /Span, leaves its drawing neither tagged
    nor an artifact, so that drawing is wrapped too. A `Do` for which
    holds_tags(index) is true draws a form with tagged content inside,
    and is left out: see mark_untagged_as_artifacts."""
    stack = []     # whether each open sequence covers what it holds
    covered = 0    # how many of them do
    path_start = None
    for i, op in enumerate(ops):
        if op in (b'BDC', b'BMC'):
            stack.append(bool(covers(i)))
            covered += stack[-1]
        elif op == b'EMC':
            if stack:
                covered -= stack.pop()
        elif op in _PATH:
            if not covered and path_start is None:
                path_start = i
        elif op in _PAINT or op == b'n':
            if not covered and op != b'n':
                yield (i if path_start is None else path_start, i)
            path_start = None
        elif op in _SHOW and not covered:
            if op != b'Do' or not holds_tags(i):
                yield (i, i)


def _bare_draws(ops, covers):
    """The indices of the `Do` operators in `ops` that lie outside every
    marked-content sequence that covers them (as in _artifact_spans)."""
    stack = []
    covered = 0
    for i, op in enumerate(ops):
        if op in (b'BDC', b'BMC'):
            stack.append(bool(covers(i)))
            covered += stack[-1]
        elif op == b'EMC':
            if stack:
                covered -= stack.pop()
        elif op == b'Do' and not covered:
            yield i


_TAG = re.compile(rb'\s*(/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*)\s*(.*)', re.DOTALL)


def _mark_kind(operator, operands, resources):
    """(tag, whether it carries an MCID) for the BMC or BDC whose
    operands are `operands`: raw bytes from the scanner, or pypdf's
    objects. The property list of a BDC is inline (<< /MCID 0 >>, as
    Chromium writes it) or named (/MC0), in the stream's /Properties."""
    if isinstance(operands, bytes):
        m = _TAG.match(operands)
        if not m:
            return None, False
        tag, props = m.group(1).decode('latin-1'), m.group(2).strip()
        if operator != b'BDC' or not props:
            return tag, False
        if props.startswith(b'<<'):
            return tag, b'/MCID' in props
        props = NameObject.unnumber(props).decode('utf-8', 'replace')
    else:
        if not operands:
            return None, False
        tag = str(operands[0])
        props = operands[1] if operator == b'BDC' and len(operands) > 1 else None
    if isinstance(props, str):
        resources = resources.get_object() if resources is not None else None
        named = resources.get('/Properties') if resources else None
        props = named.get_object().get(props) if named is not None else None
    props = props.get_object() if props is not None else None
    return tag, hasattr(props, 'get') and '/MCID' in props


def _covers(operator, operands, resources):
    """Whether a marked-content sequence leaves what it holds alone: an
    /Artifact, or tagged content (an MCID)."""
    tag, mcid = _mark_kind(operator, operands, resources)
    return tag == '/Artifact' or mcid


# One content-stream token (ISO 32000, 7.2 and 7.8.2): whitespace or a
# comment (no group), an operator (or true/false/null, which read the
# same), an operand, an array or dictionary bracket, or a byte nothing
# else matches. A literal string may nest one pair of parentheses;
# deeper nesting, valid but never seen from Chromium, is `bad`.
_REGULAR = rb'[^\x00\t\n\x0c\r ()<>\[\]{}/%]*'
_STRING = rb'\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\)'
_TOKEN = re.compile(
    rb'[\x00\t\n\x0c\r ]+|%[^\r\n]*'
    rb'|(?P<op>[A-Za-z\'"]' + _REGULAR + rb')'
    rb'|(?P<arg>[-+.0-9]' + _REGULAR + rb'|/' + _REGULAR
    + rb'|<[0-9A-Fa-f\x00\t\n\x0c\r ]*>|' + _STRING + rb')'
    rb'|(?P<open><<|\[)|(?P<close>>>|\])|(?P<bad>.)',
    re.DOTALL)


def _scan_operators(data: bytes):
    """Each operator in a decoded content stream, as (operator, start,
    end): `start` is where its first operand begins (or the operator,
    if it has none) and `end` is just past the operator. Raises
    ValueError on anything it does not read, an inline image included;
    the caller then falls back to pypdf's parser."""
    ops = []
    nesting = 0
    op_start = None
    for m in _TOKEN.finditer(data):
        kind = m.lastgroup
        if kind is None:
            continue
        if kind == 'op' and not nesting:
            word = m.group()
            if word not in (b'true', b'false', b'null'):
                if word in (b'BI', b'ID', b'EI'):
                    raise ValueError('inline image')
                ops.append((word, m.start() if op_start is None else op_start, m.end()))
                op_start = None
                continue
        if kind == 'bad':
            raise ValueError(f'unreadable content at byte {m.start()}')
        if op_start is None:
            op_start = m.start()
        if kind == 'open':
            nesting += 1
        elif kind == 'close':
            nesting -= 1
            if nesting < 0:
                raise ValueError(f'unbalanced {m.group()!r} at byte {m.start()}')
    if nesting or op_start is not None:
        raise ValueError('content ends inside an operation')
    return ops


def mark_untagged_as_artifacts(writer: PdfWriter) -> int:
    """Wrap drawing that is neither tagged nor an artifact in
    /Artifact BMC … EMC (PDF/UA-1 7.1: content is tagged or an
    artifact). Returns how many operations were wrapped.

    A path is wrapped whole, from its first construction operator to
    the operator that paints it: nothing may come between the two but
    a clip (W, W*). A path that is only a clip (ending in n) paints
    nothing and is left as it is. Text is wrapped one show operator at
    a time, inside its BT … ET; an XObject, a shading and an inline
    image, one operator each. Drawing inside an /Artifact or inside a
    sequence with an MCID (Chromium's tagged content) is not touched.
    Drawing inside any other marked-content sequence (optional content,
    a bare /Span) is neither, and is wrapped where it is, inside it.

    A form XObject that holds tagged content is the exception to "one
    operator each". Chromium draws a group (opacity, a blend mode, a
    mask) as a form, and tags what is inside it there: the form has
    /StructParents and its content a BDC with an MCID, which the
    structure tree reaches through an MCR /Stm. Its `Do` sits outside
    any marked content on the page, and wrapping it put that tagged
    content inside an artifact, which PDF/UA forbids too (ISO 32000
    14.8.2). On the templates these are the section rules (/NonStruct);
    `<p style="opacity:.5">` put a paragraph there. Such a `Do` is left
    as it is, and the form's own stream gets the same treatment as a
    page's, forms inside it included.

    A form is one stream however often it is drawn, so its stream is
    marked only when every drawing of it is outside marked content.
    Chromium draws a masked element (-webkit-mask-image) as one form
    drawn twice, both times like that, and the same markers are then
    right for both. A form also drawn inside marked content, which
    Chromium never writes, would get the artifacts inside that content
    too (inside tagged content, where an artifact is wrong as well), so
    it is left as it is, with a warning when it has drawing to mark;
    crop_and_stamp() then does not declare PDF/UA-1.

    The markers are spliced into the stream's own bytes, which are
    otherwise kept as Chromium wrote them. Parsing every operand into
    pypdf objects and writing them all back out, as this used to,
    was most of this step's time. A stream the small scanner above does
    not read (an inline image, say) still goes through pypdf.
    """
    return _mark_document(writer)[0]


def _mark_document(writer: PdfWriter):
    """mark_untagged_as_artifacts(), returning (how many operations were
    wrapped, how many drawing operations were left unmarked)."""
    pages = []
    for page in writer.pages:
        contents = page.get_contents()
        if contents is not None:
            pages.append((_Content(contents, writer, page), page.get('/Resources')))
    forms = _Forms(writer)
    for content, resources in pages:
        forms.count(content, resources)
    wrapped = sum(_mark(content, resources, forms) for content, resources in pages)
    return wrapped, forms.left


class _Content:
    """One content stream's operators, read once: the scanner's
    (operator, start, end) in `ops`, or, when the scanner cannot read
    it, pypdf's parse in `parsed`. `page` is the page it belongs to, or
    None for a form's stream."""

    def __init__(self, stream, pdf, page=None):
        self.stream = stream
        self.page = page
        self.data = stream.get_data()
        try:
            self.ops = _scan_operators(self.data)
            self.parsed = None
            self.names = [op for op, _, _ in self.ops]
        except ValueError:
            self.ops = None
            self.parsed = stream if page is not None else ContentStream(stream, pdf)
            self.names = [op for _, op in self.parsed.operations]

    def operands(self, i):
        """Operator i's operands: raw bytes from the scanner, else pypdf's."""
        if self.ops is not None:
            _, start, end = self.ops[i]
            return self.data[start:end - len(self.names[i])]
        return self.parsed.operations[i][0]

    def xobject(self, i):
        """The resource name a `Do` at index i draws, or None."""
        operands = self.operands(i)
        if isinstance(operands, bytes):
            raw = operands.strip()
            return NameObject.unnumber(raw).decode('utf-8', 'replace') if raw else None
        return str(operands[0]) if operands else None

    def covers(self, i, resources):
        """Whether the BMC or BDC at index i is an /Artifact or tagged."""
        return _covers(self.names[i], self.operands(i), resources)

    def marks_tags(self, resources):
        """Whether any marked-content sequence here carries an MCID."""
        return any(op == b'BDC' and _mark_kind(op, self.operands(i), resources)[1]
                   for i, op in enumerate(self.names))


def _form(resources, name):
    """The form XObject `name` in `resources`, as (key, form), the key
    telling one object from another; None for anything else."""
    resources = resources.get_object() if resources is not None else None
    xobjects = resources.get('/XObject') if resources and name else None
    ref = xobjects.get_object().get(name) if xobjects is not None else None
    if ref is None:
        return None
    form = ref.get_object()
    if form.get('/Subtype') != '/Form':
        return None
    return (ref.idnum if isinstance(ref, IndirectObject) else id(form)), form


def _form_resources(form, resources):
    """A form's resources: its own, or (deprecated, ISO 32000 7.8.3)
    those of the stream that draws it."""
    return form.get('/Resources') or resources


class _Forms:
    """The form XObjects drawn from the pages, each read once: how many
    times each is drawn, how many of those inside marked content, and
    whether it holds tagged content."""

    def __init__(self, pdf):
        self.pdf = pdf
        self.content = {}
        self.uses = {}
        self.inside = {}       # of those uses, how many inside marked content
        self.reached = set()   # (key, whether outside marked content)
        self.tagged = {}
        self.handled = set()   # forms marked inside, or warned about
        self.left = 0          # drawing operations left unmarked

    def read(self, key, form):
        if key not in self.content:
            self.content[key] = _Content(form, self.pdf)
        return self.content[key]

    def draws(self, content, resources):
        """(index, key, form, its resources) for each `Do` of a form in
        content."""
        for i, op in enumerate(content.names):
            if op == b'Do':
                found = _form(resources, content.xobject(i))
                if found:
                    yield (i,) + found + (_form_resources(found[1], resources),)

    def count(self, content, resources, outside=True):
        """Count every form `content` draws, at any depth, and inside them:
        how often, and how often inside a marked-content sequence that
        covers it (_covers), its own `Do` or the one that draws `content`
        (`outside` false). Each form is read at most twice, once for
        each."""
        bare = set(_bare_draws(content.names, lambda i: content.covers(i, resources))
                   if outside else ())
        for i, key, form, inner in self.draws(content, resources):
            free = i in bare
            self.uses[key] = self.uses.get(key, 0) + 1
            if not free:
                self.inside[key] = self.inside.get(key, 0) + 1
            if (key, free) not in self.reached:
                self.reached.add((key, free))
                self.count(self.read(key, form), inner, free)

    def holds_tags(self, key, form, inner):
        """/StructParents, an MCID, or a form inside that holds tags."""
        if key not in self.tagged:
            self.tagged[key] = False   # so a form that draws itself cannot loop
            content = self.read(key, form)
            self.tagged[key] = (
                '/StructParents' in form
                or content.marks_tags(inner)
                or any(self.holds_tags(*found[1:]) for found in self.draws(content, inner))
            )
        return self.tagged[key]


def _mark(content, resources, forms, key=None) -> int:
    """mark_untagged_as_artifacts for one content stream, and the forms
    with tagged content it draws outside marked content. `key` is the
    form's, for a form's stream. A form also drawn inside marked
    content is only read: what it would wrap is counted in forms.left
    and warned about."""
    descend = []

    def holds_tags(i):
        # Asked only of a `Do` outside every marked-content sequence
        # that covers it.
        found = _form(resources, content.xobject(i))
        if found is None:
            return False
        found += (_form_resources(found[1], resources),)
        if not forms.holds_tags(*found):
            return False
        descend.append(found)
        return True

    def covers(i):
        return content.covers(i, resources)

    if forms.inside.get(key):
        spans = list(_artifact_spans(content.names, holds_tags, covers))
        wrapped = 0
        if spans:
            forms.left += len(spans)
            c.warn(f"a group holding tagged content is drawn {forms.uses[key]} times, "
                   f"{forms.inside[key]} of them inside marked content — its untagged "
                   f"drawing is left unmarked (PDF/UA 7.1), and the PDF does not "
                   f"declare PDF/UA-1")
    elif content.ops is None and content.page is not None:
        wrapped = _mark_with_pypdf(content.page, content.stream, holds_tags, covers)
    else:
        spans = list(_artifact_spans(content.names, holds_tags, covers))
        wrapped = len(spans)
        if spans and content.ops is not None:
            data = _splice(content.data, content.ops, spans)
            if content.page is not None:
                stream = DecodedStreamObject()
                stream.set_data(data)
                content.page.replace_contents(stream.flate_encode())
            else:
                _replace_form_data(content.stream, data)
        elif spans:
            content.parsed.operations = _wrap_operations(content.parsed.operations, spans)
            _replace_form_data(content.stream, content.parsed.get_data())
    for key, form, inner in descend:
        if key in forms.handled:
            continue
        forms.handled.add(key)
        wrapped += _mark(forms.read(key, form), inner, forms, key)
    return wrapped


def _replace_form_data(form, data: bytes) -> None:
    """Give a form XObject the decoded content `data`, in the same
    object: the resources that draw it, and the structure tree (an MCR's
    /Stm), point at that object.

    pypdf's set_data re-encodes only a stream whose one filter is
    FlateDecode: any other filter ([/ASCII85Decode /FlateDecode], say)
    raised PdfReadError and stopped the crop, and a /DecodeParms (a
    predictor) was kept while the data was encoded without it, which
    no reader can decode. So an encoded form loses its filters and its
    parameters and is flate-encoded afresh, which for Chromium's forms
    (FlateDecode alone) is what set_data did. An unencoded one stays so.
    """
    if isinstance(form, EncodedStreamObject):
        form.get_data()   # decoded with the old filters, and kept
        form.pop(NameObject('/DecodeParms'), None)
        form[NameObject('/Filter')] = NameObject('/FlateDecode')
    form.set_data(data)


def _splice(data: bytes, ops, spans) -> bytes:
    """`data` with /Artifact BMC … EMC around each span of `ops`."""
    cuts = sorted([(ops[a][1], 1, b'/Artifact BMC\n') for a, _ in spans]
                  + [(ops[b][2], 0, b'\nEMC\n') for _, b in spans])
    out = []
    last = 0
    for at, _, marker in cuts:
        out += [data[last:at], marker]
        last = at
    out.append(data[last:])
    return b''.join(out)


def _wrap_operations(operations, spans):
    """pypdf's `operations` with /Artifact BMC … EMC around each span."""
    starts = [a for a, _ in spans]
    ends = [b for _, b in spans]
    out = []
    for i, operation in enumerate(operations):
        out += [([NameObject('/Artifact')], b'BMC')] * starts.count(i)
        out.append(operation)
        out += [([], b'EMC')] * ends.count(i)
    return out


def _mark_with_pypdf(page, contents, holds_tags=lambda i: False, covers=None) -> int:
    """mark_untagged_as_artifacts for one page, on pypdf's parse."""
    operations = contents.operations
    if covers is None:
        def covers(i):
            return _covers(operations[i][1], operations[i][0], page.get('/Resources'))
    spans = list(_artifact_spans((op for _, op in operations), holds_tags, covers))
    if not spans:
        return 0
    contents.operations = _wrap_operations(operations, spans)
    page.replace_contents(contents)
    page.compress_content_streams()
    return len(spans)


VENDORED_FONT_PREFIXES = ("Manrope", "Newsreader")


def foreign_fonts(reader: PdfReader) -> list[str]:
    """Fonts in the PDF that are not the vendored cuts.

    Text in a script Manrope and Newsreader do not cover (CJK, Arabic,
    emoji, many symbols) is drawn by Chromium in whatever system font
    has the glyph. That font is embedded silently, differs by machine,
    and can arrive as Type 3 (a colour emoji font, or a faux bold of a
    CJK font), which the project otherwise guarantees never happens.
    """
    found = set()
    # Fonts are listed per resource dictionary, and a page's is not the
    # only one: text Skia draws inside a group (opacity, a blend mode)
    # goes into a Form XObject with Resources of its own, and a Type 3
    # glyph can itself be drawn from resources. Walked by object
    # identity, so a form shared by every page is read once and a form
    # that (wrongly) contains itself cannot loop.
    seen = set()

    def walk(resources):
        resources = resources.get_object() if resources is not None else None
        if not resources or id(resources) in seen:
            return
        seen.add(id(resources))
        for ref in (resources.get("/Font") or {}).values():
            font = ref.get_object()
            if font.get("/Subtype") == "/Type3":
                found.add("a Type 3 font")
                walk(font.get("/Resources"))
                continue
            base = str(font.get("/BaseFont", ""))
            name = base.lstrip("/").split("+", 1)[-1]
            if not name.startswith(VENDORED_FONT_PREFIXES):
                found.add(name)
        for ref in (resources.get("/XObject") or {}).values():
            xobject = ref.get_object()
            if xobject.get("/Subtype") == "/Form":
                walk(xobject.get("/Resources"))

    for page in reader.pages:
        walk(page.get("/Resources"))
    return sorted(found)


def crop_and_stamp(reader: PdfReader, meta_path: Path | None) -> PdfWriter:
    """The whole post-process, in its one order: crop, /Info, /Lang, on a
    tagged PDF the link descriptions and artifacts, then XMP.

    Both callers go through this — main() below and the Studio worker's
    crop op (build/worker.py) — so a step added here reaches both. They
    used to spell the sequence out separately.

    The XMP comes last because it declares PDF/UA-1 only when the
    marking left nothing unmarked (see mark_untagged_as_artifacts).
    """
    writer = PdfWriter()
    crop_pages(reader, writer)
    apply_metadata(writer, reader, meta_path)
    lang = apply_language(writer, meta_path)
    tagged = is_tagged(writer)
    unmarked = 0
    if tagged:
        describe_links(writer)
        unmarked = _mark_document(writer)[1]
    apply_xmp(writer, lang, pdfua=tagged and not unmarked)
    return writer


def write_pdf(writer: PdfWriter, dst) -> None:
    """Write `writer` to `dst` so that `dst` is never half a PDF.

    The bytes go to a temporary file beside `dst` and are moved over it
    in one os.replace. Writing straight into `dst` truncated the
    previous PDF first: a build stopped mid-write (Ctrl+C, the Studio
    closing, a full disk, an exception inside pypdf) left a résumé in
    dist/ that no viewer can open, under the name you attach.

    Beside `dst`, not in the temp directory, because a rename is only
    atomic within one filesystem. Its name starts with a dot and ends
    in `.tmp`, so it never matches the output pattern the prune reads
    (build/_output_name.js). It is removed if anything fails.

    A PDF viewer that holds `dst` open without sharing delete access
    (Adobe Reader on Windows) makes os.replace raise PermissionError,
    the same error the old in-place open raised, so callers report it
    exactly as before.
    """
    dst = Path(dst)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, 'wb') as f:
            writer.write(f)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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
             "Errors and warnings still print. For a caller cropping to a "
             "throwaway file (the tests), where the summary is only noise.",
    )
    args = parser.parse_args()

    if args.meta is not None and not args.meta.exists():
        c.err(f"--meta file not found: {args.meta}")
        return 2

    reader = PdfReader(args.input)
    writer = crop_and_stamp(reader, args.meta)
    foreign = foreign_fonts(reader)
    if foreign:
        c.warn(f"Text drawn in system fonts: {', '.join(foreign)}")
        c.detail("Some characters are not in Manrope or Newsreader (CJK, Arabic,")
        c.detail("emoji, symbols). The PDF will look different on other machines.")

    try:
        write_pdf(writer, args.output)
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
