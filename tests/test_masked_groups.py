"""
Tests that crop_pdf's artifact marking is right on what Chromium prints
for a masked element (-webkit-mask-image): one tagged form XObject,
drawn twice.

WHY
───
mark_untagged_as_artifacts rewrote a tagged form's stream only when the
form was drawn once, on the grounds that a form drawn twice might be
drawn once inside tagged content, and warned "its untagged drawing is
left unmarked" for any other. Chromium draws a masked element as a
group whose form draws the element's own tagged form twice (once per
soft mask), both times outside marked content. So:

  • a masked paragraph got the warning although nothing in its form
    was untagged (the checkers reported the file clean);
  • a masked element with a background of its own got the warning, and
    its background really was left loose, in a PDF that still declared
    PDF/UA-1.

Both drawings are outside marked content, so the same markers are
right for both, and the form is now marked. This prints such a page
with Playwright's Chromium, tagged as the deliverables are, runs the
real crop_and_stamp() on it, and checks the result with
tests/_marked_content.py, forms included. It skips, saying why, where
Node or Playwright's Chromium is not available.
"""

import contextlib
import io
import logging
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))
sys.path.insert(0, str(ROOT / "tests"))

from pypdf import PdfReader, PdfWriter  # noqa: E402

import crop_pdf  # noqa: E402
import _marked_content  # noqa: E402

# A page background (an artifact), a masked paragraph, and a masked
# block with a background of its own around tagged text.
HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Masks</title></head>
<body style="margin:0;background:#f4f4f0;font:16px sans-serif">
<h1>Heading</h1>
<p style="-webkit-mask-image:linear-gradient(black,transparent)">Masked text.</p>
<div style="-webkit-mask-image:linear-gradient(black,transparent);background:#dde">
<p>Masked text on a background.</p></div>
</body></html>
"""

PRINT = """
const fs = require('fs');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await page.setContent(fs.readFileSync(process.argv[1], 'utf-8'));
    await page.pdf({ path: process.argv[2], width: '8.5in', height: '11in',
                     printBackground: true, tagged: true });
  } finally {
    await browser.close();
  }
})().catch(err => { console.error(err.message); process.exit(1); });
"""

RAW = None
_TMP = None


def setUpModule():
    global RAW, _TMP
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not found, so nothing can print a tagged PDF")
    _TMP = Path(tempfile.mkdtemp(prefix="masked-groups-"))
    html, pdf = _TMP / "page.html", _TMP / "page.pdf"
    html.write_text(HTML, encoding="utf-8")
    try:
        r = subprocess.run([node, "-e", PRINT, str(html), str(pdf)], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode != 0 or not pdf.exists():
        why = "timed out" if r is None else (r.stderr or r.stdout).strip().splitlines()[-1:]
        shutil.rmtree(_TMP, ignore_errors=True)
        raise unittest.SkipTest(f"could not print with Playwright's Chromium ({why})")
    RAW = pdf.read_bytes()


def tearDownModule():
    if _TMP is not None:
        shutil.rmtree(_TMP, ignore_errors=True)


def _stamped():
    """crop_and_stamp() on the print: (the PDF's bytes, what it warned)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
        writer = crop_pdf.crop_and_stamp(PdfReader(io.BytesIO(RAW)), None)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), err.getvalue()


def _tagged_forms_drawn_twice(reader):
    """Tagged forms drawn more than once from one stream, as (name, count)."""
    found = []
    seen = set()

    def walk(stream, resources):
        xobjects = (resources.get_object() if resources is not None else {}).get("/XObject") or {}
        data = stream.get_data()
        for name, ref in xobjects.items():
            form = ref.get_object()
            if form.get("/Subtype") != "/Form":
                continue
            count = data.count(name.encode() + b" Do")
            if "/StructParents" in form and count > 1:
                found.append((name, count))
            if ref.idnum not in seen:
                seen.add(ref.idnum)
                walk(form, form.get("/Resources"))

    for page in reader.pages:
        walk(page.get_contents(), page.get("/Resources"))
    return found


class TestMaskedGroups(unittest.TestCase):
    def test_chromium_draws_a_masked_element_twice(self):
        # What the rest of this suite relies on. If a Chromium update
        # stops doing this, the suite no longer tests the case.
        reader = PdfReader(io.BytesIO(RAW))
        self.assertTrue(crop_pdf.is_tagged(PdfWriter(clone_from=reader)))
        self.assertEqual(len(_tagged_forms_drawn_twice(reader)), 2,
                         "the paragraph's and the block's tagged forms, each drawn twice")
        loose, _ = _marked_content.problems(reader)
        self.assertIn("f", [where.split(": ")[-1] for where in loose if " /X" in where],
                      "the block's background is untagged drawing inside its form")

    def test_the_masked_forms_are_marked_without_a_warning(self):
        data, warned = _stamped()
        self.assertEqual(warned, "")
        report = _marked_content.check(PdfReader(io.BytesIO(data)))
        self.assertEqual(report.loose, [], "drawing that is neither tagged nor an artifact")
        self.assertEqual(report.hidden, [], "tagged content inside an /Artifact")
        self.assertEqual(report.unbalanced, [])
        self.assertEqual(report.orphans, [])

    def test_the_file_declares_pdfua_1(self):
        data, _ = _stamped()
        metadata = PdfReader(io.BytesIO(data)).trailer["/Root"]["/Metadata"].get_object()
        self.assertIn(crop_pdf.PDFUA_NS.encode(), metadata.get_data())

    def test_marking_changes_no_pixel(self):
        try:
            import pypdfium2 as pdfium
        except ImportError:
            self.skipTest("pypdfium2 not installed")
        cropped = PdfWriter()
        with contextlib.redirect_stderr(io.StringIO()):
            crop_pdf.crop_pages(PdfReader(io.BytesIO(RAW)), cropped)
        plain = io.BytesIO()
        cropped.write(plain)
        renders = []
        for data in (plain.getvalue(), _stamped()[0]):
            doc = pdfium.PdfDocument(data)
            try:
                renders.append([doc[i].render(scale=2).to_pil().tobytes()
                                for i in range(len(doc))])
            finally:
                doc.close()
        self.assertEqual(len(renders[0]), len(renders[1]))
        for number, (a, b) in enumerate(zip(*renders), 1):
            self.assertEqual(a, b, f"page {number}")


if __name__ == "__main__":
    unittest.main()
