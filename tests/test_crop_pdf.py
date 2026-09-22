"""
Tests for crop_pdf.py — round-trip a synthetic PDF and verify:
  • crop_pages trims an oversized MediaBox to exactly 612 × 792
    (US Letter in points), anchored at the lower-left
  • apply_metadata writes manifest values into /Info (Title, Author,
    Subject, Keywords), without overriding /Creator or /Producer
  • apply_language writes /Lang onto the document catalog root, using
    the manifest's lang field or defaulting to en-US

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


if __name__ == "__main__":
    unittest.main()
