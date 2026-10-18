"""
Tests that crop_pdf's artifact marking is right on what Chromium
actually prints for a group: content drawn with opacity (or a blend
mode) goes into a form XObject, and Chromium tags it inside the form.

WHY
───
mark_untagged_as_artifacts used to wrap every `Do` outside marked
content in /Artifact BMC … EMC. For a form that holds tagged content
(it has /StructParents, and its content a BDC with an MCID), that put
the tagged content inside an artifact, which PDF/UA forbids (ISO 32000
14.8.2; veraPDF 7.1). On the templates the section rules are such forms
(/NonStruct); `<p style="opacity:.5">` put a whole paragraph there. A
check of each page's own content stream could not see it: one `Do`,
wrapped, looks like a correctly marked artifact.

This prints a small page with Playwright's Chromium, tagged as the
deliverables are, runs the real crop_and_stamp() on it, and checks the
result the way a validator reads it, forms included
(tests/_marked_content.py): nothing drawn untagged outside an
artifact, nothing tagged inside one, and not a pixel changed by the
marking. It skips, saying why, where Node or Playwright's Chromium is
not available; a print that fails with both available is a failure.
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
from _project import NO_NODE, chromium_problem  # noqa: E402

# A page background (untagged, an artifact), a paragraph drawn with
# opacity, a rule drawn with opacity like the templates' section rules,
# a group with a background of its own around tagged text, groups
# nested, and a link inside the document.
HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Groups</title></head>
<body style="margin:0;background:#f4f4f0;font:16px sans-serif">
<h1>Heading</h1>
<p style="opacity:.5">A paragraph drawn with opacity.</p>
<hr style="opacity:.5">
<div style="opacity:.5;background:#dde"><p>Text on a background, in a group.</p></div>
<div style="opacity:.5"><div style="opacity:.5"><p>Nested groups.</p></div></div>
<p>A link <a href="#end">to the end</a>.</p>
<p id="end">The end.</p>
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
        raise unittest.SkipTest(f"{NO_NODE}, so nothing can print a tagged PDF")
    _TMP = Path(tempfile.mkdtemp(prefix="tagged-groups-"))
    html, pdf = _TMP / "page.html", _TMP / "page.pdf"
    html.write_text(HTML, encoding="utf-8")
    try:
        r = subprocess.run([node, "-e", PRINT, str(html), str(pdf)], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode != 0 or not pdf.exists():
        shutil.rmtree(_TMP, ignore_errors=True)
        # A skip only when Chromium cannot start here, asked directly as
        # the building suites ask (tests/_project.py). Any other failure
        # to print is a failure: this used to skip on all of them.
        problem = chromium_problem()
        if problem:
            raise unittest.SkipTest(f"chromium unavailable ({problem})")
        why = "timed out" if r is None else (
            ((r.stderr or r.stdout).strip().splitlines() or ["no output"])[-1])
        raise RuntimeError(f"could not print with Playwright's Chromium ({why})")
    RAW = pdf.read_bytes()


def tearDownModule():
    if _TMP is not None:
        shutil.rmtree(_TMP, ignore_errors=True)


def _stamped():
    with contextlib.redirect_stderr(io.StringIO()):
        writer = crop_pdf.crop_and_stamp(PdfReader(io.BytesIO(RAW)), None)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _tagged_forms(reader):
    """Names of the forms a page draws that carry /StructParents."""
    found = []
    for page in reader.pages:
        xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
        for name, ref in xobjects.items():
            if "/StructParents" in ref.get_object():
                found.append(name)
    return found


class TestTaggedGroups(unittest.TestCase):
    def test_chromium_tags_inside_groups(self):
        # What the rest of this suite relies on. If a Chromium update
        # stops doing this, the suite no longer tests the case.
        reader = PdfReader(io.BytesIO(RAW))
        self.assertTrue(crop_pdf.is_tagged(PdfWriter(clone_from=reader)))
        self.assertGreaterEqual(len(_tagged_forms(reader)), 3,
                                "the paragraph, the rule and the backed group are tagged forms")

    def test_the_raw_print_needs_marking(self):
        # Without crop_and_stamp, the page background is loose.
        loose, _ = _marked_content.problems(PdfReader(io.BytesIO(RAW)))
        self.assertTrue(loose)

    def test_nothing_tagged_ends_up_an_artifact(self):
        loose, hidden = _marked_content.problems(PdfReader(io.BytesIO(_stamped())))
        self.assertEqual(hidden, [], "tagged content inside an /Artifact")
        self.assertEqual(loose, [], "drawing that is neither tagged nor an artifact")

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
        for data in (plain.getvalue(), _stamped()):
            doc = pdfium.PdfDocument(data)
            try:
                renders.append([doc[i].render(scale=2).to_pil().tobytes()
                                for i in range(len(doc))])
            finally:
                doc.close()
        self.assertEqual(len(renders[0]), len(renders[1]))
        for number, (a, b) in enumerate(zip(*renders), 1):
            self.assertEqual(a, b, f"page {number}")

    def test_a_link_inside_the_document_is_described(self):
        reader = PdfReader(io.BytesIO(_stamped()))
        links = [a.get_object() for p in reader.pages for a in (p.get("/Annots") or [])
                 if a.get_object().get("/Subtype") == "/Link"]
        self.assertEqual([str(a.get("/Contents")) for a in links], ["Link to page 1"])


if __name__ == "__main__":
    unittest.main()
