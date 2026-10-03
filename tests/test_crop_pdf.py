"""
Tests for crop_pdf.py — round-trip a synthetic PDF and verify:
  • crop_pages trims an oversized MediaBox to exactly 612 × 792
    (US Letter in points), anchored at the lower-left
  • apply_metadata writes manifest values into /Info (Title, Author,
    Subject, Keywords), without overriding /Creator or /Producer
  • apply_language writes /Lang onto the document catalog root, using
    the manifest's lang field or defaulting to en-US
  • crop_pages keeps the catalog: the structure tree a tagged print
    carries (/StructTreeRoot, /MarkInfo, each page's /StructParents)
    and /ViewerPreferences
  • apply_xmp writes an XMP packet that says what /Info says, no more,
    in a stream labelled /Type /Metadata /Subtype /XML
  • on a tagged PDF, link annotations get a /Contents (describe_links)
    and drawing outside every marked-content sequence is wrapped as an
    /Artifact (mark_untagged_as_artifacts), without changing a pixel;
    an untagged PDF is left as it is

These tests originally motivated audit-H7: production code accessed
pypdf's private `_root_object` to set /Lang, which a pypdf version
bump could silently rename or remove, leaving a /Lang-less PDF that
the pixel-diff snapshot test would not notice. Phase-1 swapped to the
public `root_object` accessor (verified in pypdf 5.9.0); the /Lang
assertions here remain the regression gate against any future
accessor change.

The round-trip pattern (write to in-memory bytes, re-read with a
fresh PdfReader, inspect the catalog) keeps the test side off of
pypdf private internals entirely.

Also covered: the live preview's in-memory crop
(crop_pdfium_page_to_letter) must rasterize to exactly the pixels of
the file crop_pages writes — for an oversized page like Chromium's, a
page whose box does not start at the origin, a page with its own
CropBox, and a page smaller than Letter. Those tests need pypdfium2 and
Pillow and skip without them.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

from pypdf import PdfReader, PdfWriter  # noqa: E402

import crop_pdf  # noqa: E402


def make_pdf_with_page(width_pt: float, height_pt: float,
                       producer: str = "Test/Producer",
                       creator: str = "Test/Creator") -> io.BytesIO:
    """Synthesize an in-memory PDF with one blank page of the given size.

    Stamps a recognizable Producer/Creator so we can verify that
    apply_metadata leaves them alone.
    """
    w = PdfWriter()
    w.add_blank_page(width=width_pt, height=height_pt)
    w.add_metadata({"/Producer": producer, "/Creator": creator})
    buf = io.BytesIO()
    w.write(buf)
    buf.seek(0)
    return buf


def round_trip(writer: PdfWriter) -> PdfReader:
    """Write the writer to bytes and return a fresh PdfReader.

    This is the indirect-inspection pattern: rather than poking the
    writer's internals (which would couple the test to pypdf private
    APIs the same way crop_pdf.apply_language currently does), we
    serialize to bytes and re-parse. The resulting PdfReader sees
    exactly what a downstream consumer would see.
    """
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return PdfReader(buf)


class TestCropPages(unittest.TestCase):
    def test_oversized_page_trimmed_to_letter(self):
        # Chromium typically produces pages a few tenths-of-a-point
        # oversized due to its 0.12-pt grid quantization.
        reader = PdfReader(make_pdf_with_page(615.5, 794.2))
        writer = PdfWriter()
        crop_pdf.crop_pages(reader, writer)

        page = writer.pages[0]
        self.assertAlmostEqual(float(page.mediabox.width), 612.0, places=3)
        self.assertAlmostEqual(float(page.mediabox.height), 792.0, places=3)

    def test_lower_left_anchored(self):
        # Per crop_pdf's docstring: the lower-left stays fixed; only
        # the upper-right is pulled in. Verify by reading the
        # post-crop MediaBox corners.
        reader = PdfReader(make_pdf_with_page(620.0, 800.0))
        writer = PdfWriter()
        crop_pdf.crop_pages(reader, writer)

        mb = writer.pages[0].mediabox
        self.assertAlmostEqual(float(mb.left), 0.0, places=3)
        self.assertAlmostEqual(float(mb.bottom), 0.0, places=3)
        self.assertAlmostEqual(float(mb.right), 612.0, places=3)
        self.assertAlmostEqual(float(mb.top), 792.0, places=3)

    def test_exactly_letter_sized_unchanged(self):
        # If the input is already exactly Letter, crop must be a no-op
        # (no rounding drift, no off-by-epsilon).
        reader = PdfReader(make_pdf_with_page(612.0, 792.0))
        writer = PdfWriter()
        crop_pdf.crop_pages(reader, writer)

        mb = writer.pages[0].mediabox
        self.assertAlmostEqual(float(mb.width), 612.0, places=3)
        self.assertAlmostEqual(float(mb.height), 792.0, places=3)


def make_tagged_pdf() -> io.BytesIO:
    """A two-page, oversized PDF with the catalog a tagged Chromium
    print has: a structure tree whose elements point at the pages,
    /MarkInfo, /ViewerPreferences, and /StructParents on each page."""
    from pypdf.generic import (ArrayObject, BooleanObject, DictionaryObject,
                               NameObject, NumberObject, TextStringObject)
    w = PdfWriter()
    pages = [w.add_blank_page(width=612.12, height=792.24) for _ in range(2)]
    kids = ArrayObject()
    for i, page in enumerate(pages):
        page[NameObject("/StructParents")] = NumberObject(i)
        kids.append(w._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/StructElem"),
            NameObject("/S"): NameObject("/P"),
            NameObject("/Pg"): page.indirect_reference,
            NameObject("/K"): NumberObject(0),
        })))
    root = w.root_object
    root[NameObject("/StructTreeRoot")] = w._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/StructTreeRoot"),
        NameObject("/K"): kids,
    }))
    root[NameObject("/MarkInfo")] = DictionaryObject({
        NameObject("/Marked"): BooleanObject(True),
    })
    root[NameObject("/ViewerPreferences")] = DictionaryObject({
        NameObject("/DisplayDocTitle"): BooleanObject(True),
    })
    w.add_metadata({"/Title": "T", "/Producer": "Skia/PDF m141",
                    "/CreationDate": TextStringObject("D:20260927174004+00'00'")})
    buf = io.BytesIO()
    w.write(buf)
    buf.seek(0)
    return buf


class TestCropKeepsTheCatalog(unittest.TestCase):
    """Copying pages into an empty writer dropped all of this; the crop
    clones the document instead (crop_pdf.py, WHAT THE CROP KEEPS)."""

    def setUp(self):
        writer = PdfWriter()
        crop_pdf.crop_pages(PdfReader(make_tagged_pdf()), writer)
        self.out = round_trip(writer)
        self.catalog = self.out.trailer["/Root"]

    def test_structure_tree_and_mark_info_survive(self):
        self.assertIn("/StructTreeRoot", self.catalog)
        self.assertTrue(self.catalog["/MarkInfo"]["/Marked"])

    def test_structure_elements_point_at_the_output_pages(self):
        pages = [p.indirect_reference.idnum for p in self.out.pages]
        kids = self.catalog["/StructTreeRoot"]["/K"]
        self.assertEqual(len(kids), 2)
        for i, kid in enumerate(kids):
            self.assertEqual(kid.get_object().raw_get("/Pg").idnum, pages[i],
                             f"element {i} points at page {i + 1}")

    def test_pages_keep_struct_parents_and_are_cropped(self):
        self.assertEqual(len(self.out.pages), 2)
        for i, page in enumerate(self.out.pages):
            self.assertEqual(page["/StructParents"], i)
            self.assertAlmostEqual(float(page.mediabox.width), 612.0, places=3)
            self.assertAlmostEqual(float(page.mediabox.height), 792.0, places=3)

    def test_display_doc_title_survives(self):
        self.assertTrue(self.catalog["/ViewerPreferences"]["/DisplayDocTitle"])


class TestApplyMetadata(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.meta_path = self.tmpdir / "meta.json"

    def tearDown(self):
        for f in self.tmpdir.iterdir():
            f.unlink()
        self.tmpdir.rmdir()

    def _write_manifest(self, payload: dict) -> Path:
        self.meta_path.write_text(json.dumps(payload), encoding="utf-8")
        return self.meta_path

    def test_writes_title_author_subject_keywords(self):
        reader = PdfReader(make_pdf_with_page(612.0, 792.0))
        writer = PdfWriter()
        crop_pdf.crop_pages(reader, writer)  # need pages first

        meta = self._write_manifest({
            "title": "Gaius Caesar — Resume",
            "author": "Gaius Caesar",
            "subject": "Test subject",
            "keywords": "python, design, pdf",
        })
        crop_pdf.apply_metadata(writer, reader, meta)

        info = round_trip(writer).metadata
        self.assertEqual(info["/Title"], "Gaius Caesar — Resume")
        self.assertEqual(info["/Author"], "Gaius Caesar")
        self.assertEqual(info["/Subject"], "Test subject")
        self.assertEqual(info["/Keywords"], "python, design, pdf")

    def test_control_characters_do_not_break_the_xmp(self):
        # YAML lets "\x01" and "\f" through in a double-quoted string.
        # XML 1.0 cannot hold them even escaped, and the XMP copied from
        # /Info became ill-formed — pypdf refused to read it back.
        reader = PdfReader(make_pdf_with_page(612.0, 792.0))
        writer = PdfWriter()
        crop_pdf.crop_pages(reader, writer)
        meta = self._write_manifest({
            "title": "Gaius\x01 Caesar — Resume",
            "author": "Gaius Caesar",
            "subject": "Tab\tstays, \x0cform feed and \x1b escape go",
        })
        crop_pdf.apply_metadata(writer, reader, meta)
        crop_pdf.apply_xmp(writer, "en-US")

        out = round_trip(writer)
        self.assertEqual(out.metadata["/Title"], "Gaius Caesar — Resume")
        self.assertEqual(out.metadata["/Subject"], "Tab\tstays, form feed and  escape go")
        xmp = out.xmp_metadata   # raises PdfReadError on ill-formed XML
        self.assertEqual(xmp.dc_title["x-default"], out.metadata["/Title"])
        self.assertEqual(xmp.dc_description["x-default"], out.metadata["/Subject"])

    def test_does_not_override_creator_or_producer(self):
        # apply_metadata's mapping deliberately omits Creator/Producer —
        # Chromium's defaults pass through, truthfully describing what
        # produced the bytes. Verify the input's values are preserved.
        reader = PdfReader(make_pdf_with_page(
            612.0, 792.0,
            producer="ChromiumProducer", creator="ChromiumCreator",
        ))
        writer = PdfWriter()
        crop_pdf.crop_pages(reader, writer)

        meta = self._write_manifest({
            "title": "Hello",
            # Even if manifest tries to set creator/producer, it has
            # no effect — they aren't in the mapping.
            "creator": "Hacker",
            "producer": "Hacker",
        })
        crop_pdf.apply_metadata(writer, reader, meta)

        info = round_trip(writer).metadata
        self.assertEqual(info["/Title"], "Hello")
        self.assertEqual(info["/Producer"], "ChromiumProducer")
        self.assertEqual(info["/Creator"], "ChromiumCreator")

    def test_no_manifest_preserves_input_metadata(self):
        reader = PdfReader(make_pdf_with_page(
            612.0, 792.0,
            producer="OriginalProducer", creator="OriginalCreator",
        ))
        writer = PdfWriter()
        crop_pdf.crop_pages(reader, writer)
        crop_pdf.apply_metadata(writer, reader, meta_path=None)

        info = round_trip(writer).metadata
        self.assertEqual(info["/Producer"], "OriginalProducer")
        self.assertEqual(info["/Creator"], "OriginalCreator")

    def test_empty_manifest_values_skipped(self):
        # apply_metadata's overlay is `if src in manifest and manifest[src]:`
        # so falsy values (empty string, None) don't overwrite. Pin
        # this so a future "always write" change is intentional.
        reader = PdfReader(make_pdf_with_page(
            612.0, 792.0,
            producer="ChromiumProducer",
        ))
        writer = PdfWriter()
        crop_pdf.crop_pages(reader, writer)
        # First, stamp a real title via manifest.
        meta = self._write_manifest({"title": "Original", "author": ""})
        crop_pdf.apply_metadata(writer, reader, meta)

        info = round_trip(writer).metadata
        self.assertEqual(info["/Title"], "Original")
        # Empty author from manifest should not appear as /Author.
        self.assertNotIn("/Author", info)


class TestApplyLanguage(unittest.TestCase):
    """Catalog /Lang round-trip — the regression gate for audit H7."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.meta_path = self.tmpdir / "meta.json"

    def tearDown(self):
        for f in self.tmpdir.iterdir():
            f.unlink()
        self.tmpdir.rmdir()

    def _writer_with_blank_page(self) -> PdfWriter:
        # apply_language needs the writer to have a root catalog, which
        # pypdf creates lazily on first page-add. A page is needed
        # before round-tripping anyway, since pypdf refuses to write a
        # zero-page PDF.
        w = PdfWriter()
        w.add_blank_page(width=612, height=792)
        return w

    def test_lang_from_manifest_stamped_on_catalog(self):
        w = self._writer_with_blank_page()
        self.meta_path.write_text(
            json.dumps({"lang": "fr-FR"}), encoding="utf-8"
        )
        crop_pdf.apply_language(w, self.meta_path)

        # Round-trip and inspect the document catalog.
        buf = io.BytesIO()
        w.write(buf)
        buf.seek(0)
        r = PdfReader(buf)
        catalog = r.trailer["/Root"]
        self.assertEqual(str(catalog["/Lang"]), "fr-FR")

    def test_lang_defaults_to_en_us_when_manifest_absent(self):
        w = self._writer_with_blank_page()
        crop_pdf.apply_language(w, meta_path=None)

        buf = io.BytesIO()
        w.write(buf)
        buf.seek(0)
        r = PdfReader(buf)
        self.assertEqual(str(r.trailer["/Root"]["/Lang"]), "en-US")

    def test_lang_defaults_to_en_us_when_manifest_missing_key(self):
        w = self._writer_with_blank_page()
        self.meta_path.write_text(
            json.dumps({"title": "T"}), encoding="utf-8"
        )
        crop_pdf.apply_language(w, self.meta_path)

        buf = io.BytesIO()
        w.write(buf)
        buf.seek(0)
        r = PdfReader(buf)
        self.assertEqual(str(r.trailer["/Root"]["/Lang"]), "en-US")

    def test_lang_whitespace_stripped(self):
        w = self._writer_with_blank_page()
        self.meta_path.write_text(
            json.dumps({"lang": "  en-GB  "}), encoding="utf-8"
        )
        crop_pdf.apply_language(w, self.meta_path)

        buf = io.BytesIO()
        w.write(buf)
        buf.seek(0)
        r = PdfReader(buf)
        self.assertEqual(str(r.trailer["/Root"]["/Lang"]), "en-GB")

    def test_lang_empty_string_falls_back_to_default(self):
        # apply_language's guard is
        # `if isinstance(value, str) and value.strip():` —
        # whitespace-only or empty strings fall through to en-US.
        w = self._writer_with_blank_page()
        self.meta_path.write_text(
            json.dumps({"lang": "   "}), encoding="utf-8"
        )
        crop_pdf.apply_language(w, self.meta_path)

        buf = io.BytesIO()
        w.write(buf)
        buf.seek(0)
        r = PdfReader(buf)
        self.assertEqual(str(r.trailer["/Root"]["/Lang"]), "en-US")

    def test_lang_unreadable_manifest_falls_back_silently(self):
        # apply_language explicitly catches OSError / JSONDecodeError
        # and falls back to en-US rather than failing the build.
        w = self._writer_with_blank_page()
        self.meta_path.write_text("{not valid json", encoding="utf-8")
        crop_pdf.apply_language(w, self.meta_path)

        buf = io.BytesIO()
        w.write(buf)
        buf.seek(0)
        r = PdfReader(buf)
        self.assertEqual(str(r.trailer["/Root"]["/Lang"]), "en-US")


class TestApplyXmp(unittest.TestCase):
    def _xmp(self, info: dict, lang: str = "en-US"):
        w = PdfWriter()
        w.add_blank_page(width=612, height=792)
        w.add_metadata(info)
        crop_pdf.apply_xmp(w, lang)
        return round_trip(w).xmp_metadata

    def test_mirrors_info(self):
        xmp = self._xmp({
            "/Title": "Gaius Caesar — Resume", "/Author": "Gaius Caesar",
            "/Subject": "S", "/Keywords": "a, b",
            "/Producer": "Skia/PDF m141", "/Creator": "Chromium",
            "/CreationDate": "D:20260927174004+00'00'",
            "/ModDate": "D:20260927104004-07'00'",
        }, lang="en-GB")
        self.assertEqual(xmp.dc_title, {"x-default": "Gaius Caesar — Resume"})
        self.assertEqual(xmp.dc_creator, ["Gaius Caesar"])
        self.assertEqual(xmp.dc_description, {"x-default": "S"})
        self.assertEqual(xmp.pdf_keywords, "a, b")
        self.assertEqual(xmp.pdf_producer, "Skia/PDF m141")
        self.assertEqual(xmp.xmp_creator_tool, "Chromium")
        self.assertEqual(xmp.dc_language, ["en-GB"])
        # pypdf reads XMP dates back as naive UTC; both are 17:40:04 UTC.
        self.assertEqual(xmp.xmp_create_date.isoformat(), "2026-09-27T17:40:04")
        self.assertEqual(xmp.xmp_modify_date.isoformat(), "2026-09-27T17:40:04")

    def test_absent_or_empty_info_is_absent_from_xmp(self):
        xmp = self._xmp({"/Title": "T", "/Author": ""})
        self.assertEqual(xmp.dc_title, {"x-default": "T"})
        self.assertEqual(xmp.dc_creator, [])
        self.assertIsNone(xmp.pdf_keywords)
        self.assertIsNone(xmp.xmp_create_date)

    def test_claims_no_conformance_by_default(self):
        xmp = self._xmp({"/Title": "T"})
        self.assertIsNone(xmp.pdfaid_part)
        packet = xmp.stream.get_data().decode("utf-8")
        self.assertNotIn("pdfaid:part", packet)
        self.assertNotIn("pdfuaid:", packet)

    def test_declares_pdfua_when_asked_and_never_pdfa(self):
        w = PdfWriter()
        w.add_blank_page(width=612, height=792)
        w.add_metadata({"/Title": "T"})
        crop_pdf.apply_xmp(w, "en-US", pdfua=True)
        xmp = round_trip(w).xmp_metadata
        self.assertEqual(xmp.dc_title, {"x-default": "T"}, "the rest of the packet still reads")
        nodes = list(xmp.get_nodes_in_namespace("", crop_pdf.PDFUA_NS))
        self.assertEqual([n.localName for n in nodes], ["part"])
        self.assertEqual(nodes[0].firstChild.data, "1")
        self.assertIsNone(xmp.pdfaid_part)
        self.assertNotIn("pdfaid:", xmp.stream.get_data().decode("utf-8"))

    def test_crop_and_stamp_declares_pdfua_only_on_a_tagged_pdf(self):
        tagged = round_trip(crop_pdf.crop_and_stamp(PdfReader(make_tagged_pdf()), None))
        self.assertIn(b"pdfuaid:part>1<", tagged.xmp_metadata.stream.get_data())
        plain = io.BytesIO()
        w = PdfWriter()
        w.add_blank_page(width=612.12, height=792.24)
        w.write(plain)
        plain.seek(0)
        untagged = round_trip(crop_pdf.crop_and_stamp(PdfReader(plain), None))
        self.assertNotIn(b"pdfuaid", untagged.xmp_metadata.stream.get_data())

    def test_the_stream_is_labelled_metadata_xml(self):
        # ISO 32000 14.3.2; pypdf leaves both keys out, and veraPDF
        # failed the PDFs on it (PDF/UA-1 7.1, test 8).
        w = PdfWriter()
        w.add_blank_page(width=612, height=792)
        w.add_metadata({"/Title": "T"})
        crop_pdf.apply_xmp(w, "en-US")
        stream = round_trip(w).trailer["/Root"]["/Metadata"].get_object()
        self.assertEqual(stream.get("/Type"), "/Metadata")
        self.assertEqual(stream.get("/Subtype"), "/XML")

    def test_the_cli_writes_it(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            src, out, meta = tmp / "in.pdf", tmp / "out.pdf", tmp / "meta.json"
            src.write_bytes(make_tagged_pdf().getvalue())
            meta.write_text(json.dumps({"title": "From manifest", "lang": "fr-FR"}),
                            encoding="utf-8")
            argv = sys.argv
            sys.argv = ["crop_pdf.py", str(src), str(out), "--meta", str(meta), "--quiet"]
            try:
                self.assertEqual(crop_pdf.main(), 0)
            finally:
                sys.argv = argv
            r = PdfReader(str(out))
            self.assertEqual(r.xmp_metadata.dc_title, {"x-default": "From manifest"})
            self.assertEqual(r.xmp_metadata.dc_language, ["fr-FR"])
            self.assertEqual(str(r.trailer["/Root"]["/Lang"]), "fr-FR")
            self.assertIn("/StructTreeRoot", r.trailer["/Root"])
        finally:
            for f in tmp.iterdir():
                f.unlink()
            tmp.rmdir()


class TestWriteIsAtomic(unittest.TestCase):
    """A write that stops part-way leaves the previous PDF as it was.

    crop_pdf used to open the output with 'wb', which empties the last
    good PDF before the new bytes exist: a build interrupted there left
    an unreadable résumé in dist/ under the name you attach.
    """

    def run_cli(self, src, out):
        argv = sys.argv
        sys.argv = ["crop_pdf.py", str(src), str(out), "--quiet"]
        try:
            return crop_pdf.main()
        finally:
            sys.argv = argv

    def test_an_interrupted_write_keeps_the_previous_pdf(self):
        from unittest import mock
        tmp = Path(tempfile.mkdtemp())
        try:
            src, out = tmp / "in.pdf", tmp / "out.pdf"
            src.write_bytes(make_tagged_pdf().getvalue())
            self.assertEqual(self.run_cli(src, out), 0)
            good = out.read_bytes()

            def half_then_stop(self_, stream):
                stream.write(good[:len(good) // 2])
                raise KeyboardInterrupt

            with mock.patch.object(PdfWriter, "write", half_then_stop), \
                    self.assertRaises(KeyboardInterrupt):
                self.run_cli(src, out)
            self.assertEqual(out.read_bytes(), good)
            self.assertEqual(sorted(f.name for f in tmp.iterdir()),
                             ["in.pdf", "out.pdf"], "no temporary file left")
        finally:
            for f in tmp.iterdir():
                f.unlink()
            tmp.rmdir()


class TestPdfDate(unittest.TestCase):
    def test_forms(self):
        from datetime import datetime, timedelta, timezone
        d = crop_pdf.pdf_date_to_datetime
        self.assertEqual(d("D:20260927174004+00'00'"),
                         datetime(2026, 9, 27, 17, 40, 4, tzinfo=timezone.utc))
        self.assertEqual(d("D:20260927104004-07'00'"),
                         datetime(2026, 9, 27, 10, 40, 4, tzinfo=timezone(timedelta(hours=-7))))
        self.assertEqual(d("D:20260927174004Z"),
                         datetime(2026, 9, 27, 17, 40, 4, tzinfo=timezone.utc))
        self.assertEqual(d("D:2026"), datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_unreadable_is_none(self):
        d = crop_pdf.pdf_date_to_datetime
        for bad in (None, "", "2026-09-27", "D:20261327000000Z", "D:x"):
            self.assertIsNone(d(bad), repr(bad))


# ── The preview's in-memory crop ─────────────────────────────────

try:
    import pypdfium2 as pdfium  # noqa: E402
    import snapshot_pdf  # noqa: E402
    HAVE_PDFIUM = True
except ImportError:  # pragma: no cover — requirements.txt installs both
    HAVE_PDFIUM = False


def make_pdf_with_content(pages) -> bytes:
    """A PDF whose pages carry paint right up to (and past) the crop line.

    `pages` is a list of dicts: {"media": (l, b, r, t)} and optionally
    {"crop": (l, b, r, t)}. Each page gets a background fill over its
    whole MediaBox, a block in the lower-left corner, hairlines at the
    Letter edges, and a red block in the upper-right excess — so a crop
    that is off by any fraction of a point changes pixels.
    """
    from pypdf.generic import (ArrayObject, DecodedStreamObject,
                               FloatObject, NameObject)
    w = PdfWriter()
    for spec in pages:
        l, b, r, t = spec["media"]
        page = w.add_blank_page(width=r - l, height=t - b)
        page[NameObject("/MediaBox")] = ArrayObject(FloatObject(v) for v in (l, b, r, t))
        if "crop" in spec:
            page[NameObject("/CropBox")] = ArrayObject(FloatObject(v) for v in spec["crop"])
        ops = (
            f"0.93 0.95 0.97 rg {l} {b} {r - l} {t - b} re f\n"
            f"0.18 0.29 0.24 rg {l + 36} {b + 36} 180 90 re f\n"
            f"0 0 0 RG 0.35 w {l} {b + 792 - 0.5} m {l + 612} {b + 792 - 0.5} l S\n"
            f"{l + 612 - 0.5} {b} m {l + 612 - 0.5} {b + 792} l S\n"
            f"1 0 0 rg {l + 611.8} {b + 791.8} 5 5 re f\n"
        ).encode("ascii")
        stream = DecodedStreamObject()
        stream.set_data(ops)
        page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@unittest.skipUnless(HAVE_PDFIUM, "pypdfium2 / Pillow not installed")
class TestInMemoryCropMatchesFileCrop(unittest.TestCase):
    """crop_pdfium_page_to_letter + render == render of crop_pages' file."""

    PAGES = [
        # Chromium's 0.12-pt quantization, oversized both ways.
        {"media": (0, 0, 612.12, 792.24)},
        # Exactly Letter already: a no-op either way.
        {"media": (0, 0, 612, 792)},
        # A box that does not start at the origin, with its own CropBox.
        {"media": (10, 20, 625.5, 815.25), "crop": (12, 21, 625.5, 815.25)},
        # Smaller than Letter: both paths leave it alone.
        {"media": (0, 0, 600, 780)},
    ]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.raw = self.tmp / "raw.pdf"
        self.raw.write_bytes(make_pdf_with_content(self.PAGES))
        # The smaller-than-Letter page draws a warning from both crops;
        # it is expected here, so keep it out of the test output.
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        writer = PdfWriter()
        crop_pdf.crop_pages(PdfReader(str(self.raw)), writer)
        self.cropped = self.tmp / "cropped.pdf"
        with open(self.cropped, "wb") as f:
            writer.write(f)
        self.saved_scale = snapshot_pdf.SCALE

    def tearDown(self):
        snapshot_pdf.SCALE = self.saved_scale
        for f in self.tmp.iterdir():
            f.unlink()
        self.tmp.rmdir()

    def _render(self, path, prepare=None, scale=None):
        snapshot_pdf.SCALE = scale or self.saved_scale
        return snapshot_pdf.render_pdf_pages(pdfium, path, prepare)

    def test_pixels_equal_at_preview_and_snapshot_scale(self):
        for scale in (2.0, self.saved_scale, 1.0):
            in_memory = self._render(self.raw, crop_pdf.crop_pdfium_page_to_letter, scale)
            from_file = self._render(self.cropped, None, scale)
            self.assertEqual(len(in_memory), len(from_file))
            for i, (a, b) in enumerate(zip(in_memory, from_file), 1):
                self.assertEqual(a.size, b.size, f"page {i} at scale {scale}: size")
                self.assertEqual(a.tobytes(), b.tobytes(), f"page {i} at scale {scale}: pixels")

    def test_oversized_page_really_is_cropped(self):
        """Guards the test above against comparing two uncropped renders."""
        uncropped = self._render(self.raw, None, 1.0)[0]
        in_memory = self._render(self.raw, crop_pdf.crop_pdfium_page_to_letter, 1.0)[0]
        self.assertEqual(in_memory.size, (612, 792))
        self.assertNotEqual(uncropped.size, in_memory.size)

    def test_boxes_match_crop_pages(self):
        pdf = pdfium.PdfDocument(str(self.raw))
        cropped = PdfReader(str(self.cropped))
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                crop_pdf.crop_pdfium_page_to_letter(page)
                ref = cropped.pages[i]
                for got, want in ((page.get_mediabox(), ref.mediabox),
                                  (page.get_cropbox(), ref.cropbox)):
                    for g, w in zip(got, (want.left, want.bottom, want.right, want.top)):
                        self.assertAlmostEqual(g, float(w), places=3, msg=f"page {i + 1}")
        finally:
            pdf.close()

    def test_only_and_prepare_are_optional(self):
        """The snapshot test's call — no hook, every page — is unchanged."""
        pages = snapshot_pdf.render_pdf_pages(pdfium, self.cropped)
        self.assertEqual(len(pages), len(self.PAGES))
        some = snapshot_pdf.render_pdf_pages(pdfium, self.cropped, None, {1})
        self.assertIsNone(some[0])
        self.assertEqual(some[1].tobytes(), pages[1].tobytes())


class TestLetterUpperRight(unittest.TestCase):
    def test_shaves_the_excess_from_the_upper_right(self):
        r, t = crop_pdf.letter_upper_right(10, 20, 625.5, 815.25)
        self.assertAlmostEqual(r, 622.0, places=9)
        self.assertAlmostEqual(t, 812.0, places=9)

    def test_smaller_than_letter_is_left_alone(self):
        self.assertIsNone(crop_pdf.letter_upper_right(0, 0, 611, 792))
        self.assertIsNone(crop_pdf.letter_upper_right(0, 0, 612, 791))

    def test_within_tolerance_counts_as_letter(self):
        self.assertIsNotNone(crop_pdf.letter_upper_right(0, 0, 611.9995, 792))


class TestOvershootWarning(unittest.TestCase):
    """A page far larger than Letter is cropped, but not in silence."""

    def crop_and_capture(self, width, height):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            crop_pdf.crop_pages(PdfReader(make_pdf_with_page(width, height)),
                                PdfWriter())
        return err.getvalue() + out.getvalue()

    def test_chromium_rounding_is_shaved_quietly(self):
        self.assertEqual(self.crop_and_capture(612.12, 792.12), "")

    def test_a_legal_page_says_what_the_crop_cuts_off(self):
        said = self.crop_and_capture(612, 1008)
        self.assertIn("well over Letter", said)
        self.assertIn("612.00 × 1008.00", said)


def make_pdf_with_form_font(base_font: str) -> io.BytesIO:
    """One page whose only font sits inside a Form XObject.

    Skia puts text drawn in a group (opacity, a blend mode) in a form
    with its own /Resources, so a system font can be in the PDF without
    appearing in any page's /Font.
    """
    from pypdf.generic import (DictionaryObject, NameObject, StreamObject)
    w = PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/TrueType"),
        NameObject("/BaseFont"): NameObject(f"/ABCDEF+{base_font}"),
    })
    form = StreamObject()
    form.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/Resources"): DictionaryObject({
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): w._add_object(font)}),
        }),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject(
            {NameObject("/X1"): w._add_object(form)}),
    })
    buf = io.BytesIO()
    w.write(buf)
    buf.seek(0)
    return buf


class TestForeignFonts(unittest.TestCase):
    def test_a_system_font_inside_a_form_xobject_is_found(self):
        reader = PdfReader(make_pdf_with_form_font("NotoSansCJKjp-Regular"))
        self.assertEqual(crop_pdf.foreign_fonts(reader), ["NotoSansCJKjp-Regular"])

    def test_the_vendored_fonts_inside_a_form_are_not_foreign(self):
        reader = PdfReader(make_pdf_with_form_font("Manrope-Regular"))
        self.assertEqual(crop_pdf.foreign_fonts(reader), [])


class TestLinkDescription(unittest.TestCase):
    def test_forms(self):
        cases = {
            "mailto:gcaesar@email.com": "gcaesar@email.com",
            "mailto:a@b.org?subject=Hi": "a@b.org",
            "tel:+10000000000": "+10000000000",
            "https://www.linkedin.com/in/gcaesar/": "linkedin.com/in/gcaesar",
            "http://example.org": "example.org",
            "https://example.org/a/b?x=1": "example.org/a/b?x=1",
            "example.org/page": "example.org/page",
            "mailto:": None,
            "https://": None,
        }
        for uri, want in cases.items():
            with self.subTest(uri=uri):
                self.assertEqual(crop_pdf.link_description(uri), want)


def make_tagged_pdf_with_content(content: bytes | None = None) -> PdfWriter:
    """One tagged page like Chromium's: a background fill and a rule
    outside any marked content, a tagged text run inside BDC … EMC,
    an untagged text run, a clip path, and two link annotations, one
    of which already has a /Contents. `content` replaces the page's
    content stream."""
    from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject,
                               DictionaryObject, FloatObject, NameObject,
                               NumberObject, TextStringObject)
    w = PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    font = w._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    stream = DecodedStreamObject()
    stream.set_data(
        b"0.95 0.95 0.95 rg 0 0 612 792 re f\n"
        b"q 36 36 540 720 re W n\n"
        b"/P <</MCID 0>> BDC BT /F1 14 Tf 0 0 0 rg 72 700 Td (Tagged) Tj ET EMC\n"
        b"0 0 0 RG 1 w 72 690 m 540 690 l S\n"
        b"BT /F1 9 Tf 500 40 Td (Page 1 of 1) Tj ET\n"
        b"Q\n"
    )
    if content is not None:
        stream.set_data(content)
    page[NameObject("/Contents")] = w._add_object(stream)

    def link(uri, contents=None):
        d = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): ArrayObject(FloatObject(v) for v in (72, 600, 200, 614)),
            NameObject("/A"): DictionaryObject({
                NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject(uri),
            }),
        })
        if contents is not None:
            d[NameObject("/Contents")] = TextStringObject(contents)
        return w._add_object(d)

    page[NameObject("/Annots")] = ArrayObject([
        link("mailto:gcaesar@email.com"),
        link("https://example.org/", contents="Kept as written"),
    ])
    page[NameObject("/StructParents")] = NumberObject(0)
    root = w.root_object
    root[NameObject("/StructTreeRoot")] = w._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/StructTreeRoot"),
    }))
    root[NameObject("/MarkInfo")] = DictionaryObject({
        NameObject("/Marked"): BooleanObject(True),
    })
    return w


def _operations(page, pdf):
    from pypdf.generic import ContentStream
    return ContentStream(page.get_contents(), pdf).operations


def _unmarked_painting(page, pdf):
    """Drawing operators outside every marked-content sequence."""
    drawing = {b"f", b"F", b"f*", b"S", b"s", b"B", b"B*", b"b", b"b*",
               b"Tj", b"TJ", b"'", b'"', b"Do", b"sh", b"INLINE IMAGE"}
    depth, found = 0, []
    for _, op in _operations(page, pdf):
        if op in (b"BDC", b"BMC"):
            depth += 1
        elif op == b"EMC":
            depth -= 1
        elif op in drawing and depth == 0:
            found.append(op)
    return found


class TestAccessibility(unittest.TestCase):
    def test_links_get_a_description(self):
        w = make_tagged_pdf_with_content()
        self.assertEqual(crop_pdf.describe_links(w), 1)
        annots = [a.get_object() for a in round_trip(w).pages[0]["/Annots"]]
        self.assertEqual(annots[0]["/Contents"], "gcaesar@email.com")
        self.assertEqual(annots[1]["/Contents"], "Kept as written")

    def test_untagged_drawing_becomes_artifacts(self):
        w = make_tagged_pdf_with_content()
        r0 = round_trip(make_tagged_pdf_with_content())
        self.assertEqual(_unmarked_painting(r0.pages[0], r0), [b"f", b"S", b"Tj"])
        # The fill, the rule and the footer; not the clip, not the tagged text.
        self.assertEqual(crop_pdf.mark_untagged_as_artifacts(w), 3)
        r = round_trip(w)
        self.assertEqual(_unmarked_painting(r.pages[0], r), [])
        ops = _operations(r.pages[0], r)
        artifacts = [o for o, op in ops if op == b"BMC" and o and o[0] == "/Artifact"]
        self.assertEqual(len(artifacts), 3)
        tagged = [op for _, op in ops if op == b"BDC"]
        self.assertEqual(len(tagged), 1, "Chromium's own tags are left as they are")
        # A path is wrapped whole: nothing between its construction and its paint.
        names = [op for _, op in ops]
        i = names.index(b"re")
        self.assertEqual(names[i - 1], b"BMC")
        self.assertEqual(names[i + 1:i + 3], [b"f", b"EMC"])
        # The clip path (W n) is not wrapped.
        j = names.index(b"W")
        self.assertEqual(names[j + 1], b"n")
        self.assertNotEqual(names[j + 2], b"EMC")

    def test_a_second_pass_changes_nothing(self):
        w = make_tagged_pdf_with_content()
        crop_pdf.mark_untagged_as_artifacts(w)
        self.assertEqual(crop_pdf.mark_untagged_as_artifacts(w), 0)

    # Operands a scanner can get wrong: a comment, a dictionary holding a
    # literal string with an escaped parenthesis, hex strings with and
    # without spaces around them, a boolean, a kerned TJ array whose
    # string nests parentheses, operators against a delimiter.
    TRICKY = (
        b"% Chromium writes none, but a comment is legal\n"
        b"/P <</MCID 0 /ActualText (a \\) b) /Alt <48 49> /Flag true>> BDC\n"
        b"BT /F1 9 Tf 1 0 0 1 72 650 Tm [(Ke\\(rn) -20 (ed (nested) x)]TJ ET EMC\n"
        b"BT/F1 9 Tf 72 640 Td<48656C6C6F>Tj ET\n"
        b"0 0 1 rg 10 10 m 20 20 l h f*\nQ q\n"
        b"[] 0 d 0.5 w 1 1 m 2 2 l S Q\n"
    )

    def test_the_scanner_reads_what_pypdf_reads(self):
        w = make_tagged_pdf_with_content(self.TRICKY)
        page = w.pages[0]
        scanned = [op for op, _, _ in crop_pdf._scan_operators(page.get_contents().get_data())]
        self.assertEqual(scanned, [op for _, op in _operations(page, w)])

    def test_splicing_matches_marking_on_pypdf_objects(self):
        # The fast path splices markers into the bytes; the fallback
        # rewrites pypdf's parse. Both must leave the same operations.
        spliced = make_tagged_pdf_with_content(self.TRICKY)
        parsed = make_tagged_pdf_with_content(self.TRICKY)
        self.assertEqual(crop_pdf.mark_untagged_as_artifacts(spliced), 3)
        page = parsed.pages[0]
        self.assertEqual(crop_pdf._mark_with_pypdf(page, page.get_contents()), 3)
        a, b = round_trip(spliced), round_trip(parsed)
        self.assertEqual(_operations(a.pages[0], a), _operations(b.pages[0], b))
        self.assertEqual(_unmarked_painting(a.pages[0], a), [])

    def test_what_the_scanner_cannot_read_goes_through_pypdf(self):
        for content in (
            # An inline image, whose data is binary.
            b"q 10 0 0 10 0 0 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x80 EI Q\n",
            # A string nested deeper than one pair of parentheses.
            b"BT /F1 9 Tf 72 640 Td (a (b (c) b) a) Tj ET\n",
        ):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    crop_pdf._scan_operators(content)
                w = make_tagged_pdf_with_content(content)
                self.assertEqual(crop_pdf.mark_untagged_as_artifacts(w), 1)
                r = round_trip(w)
                self.assertEqual(_unmarked_painting(r.pages[0], r), [])

    @unittest.skipUnless(HAVE_PDFIUM, "pypdfium2 / Pillow not installed")
    def test_same_pixels(self):
        before = io.BytesIO()
        make_tagged_pdf_with_content().write(before)
        w = make_tagged_pdf_with_content()
        crop_pdf.mark_untagged_as_artifacts(w)
        after = io.BytesIO()
        w.write(after)
        imgs = []
        for buf in (before, after):
            doc = pdfium.PdfDocument(buf.getvalue())
            imgs.append(doc[0].render(scale=2).to_pil().tobytes())
            doc.close()
        self.assertEqual(imgs[0], imgs[1])

    def test_an_untagged_pdf_is_left_alone(self):
        # The same content with no structure tree: nothing is added.
        src = make_tagged_pdf_with_content()
        del src.root_object["/StructTreeRoot"]
        del src.root_object["/MarkInfo"]
        buf = io.BytesIO()
        src.write(buf)
        buf.seek(0)
        out = round_trip(crop_pdf.crop_and_stamp(PdfReader(buf), None))
        self.assertNotIn(b"BMC", [op for _, op in _operations(out.pages[0], out)])
        self.assertNotIn("/Contents", out.pages[0]["/Annots"][0].get_object())


if __name__ == "__main__":
    unittest.main()
