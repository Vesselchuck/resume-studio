"""
Tests for the live preview's rasterize step — build/_png.py,
build/_pdf_page_keys.py and build/worker.py's raster op.

WHAT THIS GUARDS
----------------
The preview's rasterize step takes three shortcuts, and each one is
only acceptable if it never changes what is shown:

  • PNGs are written by build/_png.py (filter 0, zlib level 1) instead
    of Pillow. The decoded pixels must be exactly the source pixels.
  • Pages whose PDF-level key did not change are not rendered again.
    A page whose pixels changed must never be skipped: every raster
    with skipping is checked here against a full render of the same
    file, and a file the key reader does not understand must fall back
    to rendering every page.
  • The pixel hash is SHA-256 (cut to 128 bits); it only has to be a
    stable fingerprint.

The same guarantees against real Chromium prints — an edit on page 1,
an edit on page 2, an edit that adds a glyph — are checked in
tests/test_engine_equivalence.js, which can print.

Needs pypdfium2 and Pillow (requirements.txt); skips without them.
"""

import base64
import io
import os
import random
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

try:
    import pypdfium2  # noqa: F401,E402
    from PIL import Image  # noqa: E402
    HAVE_RASTER = True
except ImportError:  # pragma: no cover — requirements.txt installs both
    HAVE_RASTER = False

import _pdf_page_keys  # noqa: E402
import _png  # noqa: E402


# ── Synthetic PDFs ───────────────────────────────────────────────

def make_pdf(page_ops, shared_gray=0.5, share_content=False, size=(612, 792)) -> bytes:
    """A PDF with one page per entry in `page_ops` (content-stream text).

    Every page also paints a Form XObject that all pages share, filled
    with `shared_gray` — the stand-in for a font or any other resource
    two pages have in common. With share_content, every page uses the
    first page's content stream object.
    """
    from pypdf import PdfWriter
    from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                               FloatObject, NameObject)

    w = PdfWriter()
    form = DecodedStreamObject()
    form.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject(FloatObject(v) for v in (0, 0, 100, 100)),
    })
    form.set_data(f"{shared_gray} g 0 0 100 100 re f".encode("ascii"))
    form_ref = w._add_object(form)

    first_contents = None
    for ops in page_ops:
        page = w.add_blank_page(width=size[0], height=size[1])
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({NameObject("/Fm1"): form_ref}),
        })
        if share_content and first_contents is not None:
            page[NameObject("/Contents")] = first_contents
            continue
        stream = DecodedStreamObject()
        stream.set_data((ops + "\nq 1 0 0 1 400 600 cm /Fm1 Do Q\n").encode("ascii"))
        ref = w._add_object(stream)
        page[NameObject("/Contents")] = ref
        first_contents = first_contents or ref
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


PAGE1 = "0 0 1 rg 50 50 200 100 re f"
PAGE2 = "1 0 0 rg 60 300 150 80 re f"
PAGE2_EDITED = "1 0 0 rg 60 300 150 81 re f"


def incremental_update(data: bytes) -> bytes:
    """`data` with an incremental update appended: a second xref section
    (pypdf writes it as a cross-reference stream) and a /Prev."""
    from pypdf import PdfWriter
    w = PdfWriter(io.BytesIO(data), incremental=True)
    w.add_metadata({"/Title": "updated"})
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# ── The PNG writer ───────────────────────────────────────────────

@unittest.skipUnless(HAVE_RASTER, "Pillow not installed")
class TestPngWriter(unittest.TestCase):
    def _round_trip(self, img):
        data = _png.encode(img)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        decoded = Image.open(io.BytesIO(data))
        decoded.load()
        return decoded

    def test_decodes_to_the_source_pixels(self):
        rng = random.Random(1234)
        for mode, size in (("RGB", (1, 1)), ("RGB", (7, 3)), ("RGB", (613, 17)),
                           ("L", (5, 9)), ("RGBA", (11, 4))):
            channels = len(mode)
            raw = bytes(rng.randrange(256) for _ in range(size[0] * size[1] * channels))
            img = Image.frombytes(mode, size, raw)
            decoded = self._round_trip(img)
            self.assertEqual(decoded.mode, mode, f"{mode} {size}")
            self.assertEqual(decoded.size, size, f"{mode} {size}")
            self.assertEqual(decoded.tobytes(), img.tobytes(), f"{mode} {size}")

    def test_other_modes_are_converted_to_rgb(self):
        img = Image.new("P", (4, 4))
        decoded = self._round_trip(img)
        self.assertEqual(decoded.tobytes(), img.convert("RGB").tobytes())

    def test_a_rendered_page_round_trips(self):
        import pypdfium2 as pdfium
        import snapshot_pdf
        path = Path(tempfile.mkdtemp()) / "p.pdf"
        path.write_bytes(make_pdf([PAGE1]))
        try:
            img = snapshot_pdf.render_pdf_pages(pdfium, path)[0]
        finally:
            path.unlink()
            path.parent.rmdir()
        self.assertEqual(self._round_trip(img).tobytes(), img.tobytes())


# ── Page keys ────────────────────────────────────────────────────

class TestPageKeys(unittest.TestCase):
    def test_identical_files_give_identical_keys(self):
        a = _pdf_page_keys.page_keys(make_pdf([PAGE1, PAGE2]))
        b = _pdf_page_keys.page_keys(make_pdf([PAGE1, PAGE2]))
        self.assertIsNotNone(a)
        self.assertEqual(len(a), 2)
        self.assertEqual(a, b)
        self.assertNotEqual(a[0], a[1])

    def test_an_edit_to_one_page_changes_only_its_key(self):
        a = _pdf_page_keys.page_keys(make_pdf([PAGE1, PAGE2]))
        b = _pdf_page_keys.page_keys(make_pdf([PAGE1, PAGE2_EDITED]))
        self.assertEqual(a[0], b[0])
        self.assertNotEqual(a[1], b[1])

    def test_a_shared_resource_change_changes_every_key(self):
        a = _pdf_page_keys.page_keys(make_pdf([PAGE1, PAGE2], shared_gray=0.5))
        b = _pdf_page_keys.page_keys(make_pdf([PAGE1, PAGE2], shared_gray=0.25))
        self.assertNotEqual(a[0], b[0])
        self.assertNotEqual(a[1], b[1])

    def test_a_page_box_change_changes_the_key(self):
        a = _pdf_page_keys.page_keys(make_pdf([PAGE1]))
        b = _pdf_page_keys.page_keys(make_pdf([PAGE1], size=(612.12, 792.24)))
        self.assertNotEqual(a, b)

    def test_the_info_dictionary_is_ignored(self):
        """The print's timestamp lives in /Info and is never drawn."""
        data = make_pdf([PAGE1, PAGE2])
        self.assertIn(b"/Producer (pypdf)", data)
        # Same length, so no offset in the xref table moves.
        stamped = data.replace(b"/Producer (pypdf)", b"/Producer (other)")
        a = _pdf_page_keys.page_keys(data)
        b = _pdf_page_keys.page_keys(stamped)
        self.assertIsNotNone(b)
        self.assertEqual(a, b)

    def test_unsupported_files_give_none(self):
        good = make_pdf([PAGE1, PAGE2])
        xref_at = int(good[good.rfind(b"startxref") + 9:].split()[0])
        cases = {
            "empty": b"",
            "garbage": b"%PDF-1.4\nnot really a pdf\n%%EOF\n",
            "truncated": good[: len(good) // 2],
            "incremental update": incremental_update(good),
            "xref offset wrong": good.replace(b"startxref\n%d" % xref_at,
                                              b"startxref\n%d" % (xref_at - 3)),
            "an object stream": good.replace(b"/Type /XObject", b"/Type /ObjStm", 1),
            "a shared content stream": make_pdf([PAGE1, PAGE2], share_content=True),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertIsNone(_pdf_page_keys.page_keys(data))

    def test_a_moved_object_gives_none(self):
        """An xref entry that points at the wrong object is not trusted."""
        good = make_pdf([PAGE1, PAGE2])
        # Swap two xref offsets: each now names the other's object.
        at = good.rfind(b"\nxref")
        table = good[at:]
        entries = list(re.finditer(rb"\d{10} \d{5} n", table))
        a, b = entries[1], entries[2]
        swapped = (table[:a.start()] + table[b.start():b.end()] + table[a.end():b.start()]
                   + table[a.start():a.end()] + table[b.end():])
        self.assertIsNone(_pdf_page_keys.page_keys(good[:at] + swapped))

    def test_a_chromium_shaped_file_is_read(self):
        """Classic xref, one section, Chromium's own spacing — see the JS
        suite for real Chromium prints; this is the dist/ build if there is one."""
        pdfs = sorted((ROOT / "dist").glob("*.pdf")) if (ROOT / "dist").is_dir() else []
        if not pdfs:
            self.skipTest("no built PDFs in dist/")
        for pdf in pdfs:
            with self.subTest(pdf.name):
                self.assertIsNotNone(_pdf_page_keys.page_keys(pdf.read_bytes()))


# ── The worker's raster op ───────────────────────────────────────

@unittest.skipUnless(HAVE_RASTER, "pypdfium2 / Pillow not installed")
class TestRasterSkipsOnlyUnchangedPages(unittest.TestCase):
    """Every raster with `slot` must equal a full render of the same file."""

    @classmethod
    def setUpClass(cls):
        import worker
        cls.worker = worker

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.slot = f"test-{os.getpid()}-{id(self)}"
        self.addCleanup(self.worker._RENDERED.pop, self.slot, None)

    def tearDown(self):
        for f in self.tmp.iterdir():
            f.unlink()
        self.tmp.rmdir()

    def _file(self, name, data):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def _raster(self, path, **extra):
        req = {"path": str(path), "scale": 1.0, **extra}
        return self.worker.op_raster(req)

    def _assert_matches_full_render(self, result, path, **extra):
        full = self._raster(path, **extra)  # no slot: every page rendered
        self.assertEqual(full["rendered"], full["pageCount"])
        self.assertEqual([im["hash"] for im in result["images"]],
                         [im["hash"] for im in full["images"]])
        for im, ref in zip(result["images"], full["images"]):
            if "png" in im:
                decoded = Image.open(io.BytesIO(base64.b64decode(im["png"])))
                ref_img = Image.open(io.BytesIO(base64.b64decode(ref["png"])))
                self.assertEqual(decoded.convert("RGB").tobytes(), ref_img.convert("RGB").tobytes())

    def test_hash_is_sha256_of_the_pixels(self):
        import hashlib
        import pypdfium2 as pdfium
        import snapshot_pdf
        path = self._file("a.pdf", make_pdf([PAGE1]))
        r = self._raster(path)
        saved = snapshot_pdf.SCALE
        snapshot_pdf.SCALE = 1.0
        try:
            img = snapshot_pdf.render_pdf_pages(pdfium, path)[0]
        finally:
            snapshot_pdf.SCALE = saved
        want = hashlib.sha256(f"RGB:{img.width}x{img.height}:".encode("ascii") + img.tobytes())
        self.assertEqual(r["images"][0]["hash"], want.hexdigest()[:32])

    def test_sequence_of_edits(self):
        first = self._file("1.pdf", make_pdf([PAGE1, PAGE2]))
        r = self._raster(first, slot=self.slot)
        self.assertEqual(r["rendered"], 2, "first render renders everything")
        self._assert_matches_full_render(r, first)

        # The same print again: nothing rendered, same hashes and PNGs.
        again = self._file("2.pdf", make_pdf([PAGE1, PAGE2]))
        r2 = self._raster(again, slot=self.slot)
        self.assertEqual(r2["rendered"], 0)
        self.assertEqual([im["png"] for im in r2["images"]], [im["png"] for im in r["images"]])
        self._assert_matches_full_render(r2, again)

        # The caller holds both: sent back as unchanged, nothing rendered.
        known = {str(im["page"]): im["hash"] for im in r["images"]}
        r3 = self._raster(again, slot=self.slot, known=known)
        self.assertEqual(r3["rendered"], 0)
        self.assertTrue(all(im.get("unchanged") and "png" not in im for im in r3["images"]))

        # Page 2 edited: only page 2 is rendered.
        edited = self._file("3.pdf", make_pdf([PAGE1, PAGE2_EDITED]))
        r4 = self._raster(edited, slot=self.slot, known=known)
        self.assertEqual(r4["rendered"], 1)
        self.assertTrue(r4["images"][0].get("unchanged"))
        self.assertNotEqual(r4["images"][1]["hash"], known["2"])
        self._assert_matches_full_render(r4, edited)

        # A shared resource changed: every page is rendered.
        shared = self._file("4.pdf", make_pdf([PAGE1, PAGE2_EDITED], shared_gray=0.1))
        r5 = self._raster(shared, slot=self.slot)
        self.assertEqual(r5["rendered"], 2)
        self._assert_matches_full_render(r5, shared)

        # Another scale is another render: nothing carried over.
        r6 = self._raster(shared, slot=self.slot, scale=1.5)
        self.assertEqual(r6["rendered"], 2)

    def test_an_unreadable_file_renders_every_page(self):
        base = make_pdf([PAGE1, PAGE2])
        path = self._file("a.pdf", base)
        self._raster(path, slot=self.slot)
        updated = self._file("b.pdf", incremental_update(base))
        self.assertIsNone(_pdf_page_keys.page_keys(updated.read_bytes()))
        r = self._raster(updated, slot=self.slot)
        self.assertEqual(r["rendered"], 2)
        self._assert_matches_full_render(r, updated)
        # ...and the slot is forgotten, so the next render starts afresh.
        r2 = self._raster(path, slot=self.slot)
        self.assertEqual(r2["rendered"], 2)

    def test_crop_is_part_of_the_key(self):
        path = self._file("a.pdf", make_pdf([PAGE1], size=(612.12, 792.24)))
        plain = self._raster(path, slot=self.slot)
        cropped = self._raster(path, slot=self.slot, crop="letter")
        self.assertEqual(cropped["rendered"], 1)
        self.assertEqual((cropped["images"][0]["width"], cropped["images"][0]["height"]), (612, 792))
        self.assertNotEqual(plain["images"][0]["hash"], cropped["images"][0]["hash"])
        self._assert_matches_full_render(cropped, path, crop="letter")

    def test_wanted_pages_only(self):
        path = self._file("a.pdf", make_pdf([PAGE1, PAGE2]))
        r = self._raster(path, slot=self.slot, pages=[2])
        self.assertEqual([im["page"] for im in r["images"]], [2])
        self.assertEqual(r["rendered"], 1)
        self.assertEqual(r["pageCount"], 2)


if __name__ == "__main__":
    unittest.main()
