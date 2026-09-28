"""
Tests that the built PDFs embed their fonts as TrueType, not Type 3.

WHY
───
Chromium's PDF backend writes any font with variation axes as Type 3:
each glyph a small drawing program, with no hinting and nothing a PDF
reader treats as a real font. With the variable fonts the project used
to ship, every font in both PDFs was Type 3; the static cuts in fonts/
(build/make_static_fonts.py) are embedded as TrueType, and the PDFs
shrank to about a third. A variable font slipping back in — a new
weight added as a range, a file swapped — would bring Type 3 back
without changing anything on screen.

WHAT THIS CHECKS
────────────────
Both documents are built from the templates in a throwaway copy of the
project (tests/_project.py). In each PDF, every font on every page:

  • is not Type 3;
  • carries its outlines as an embedded TrueType program (FontFile2);
  • is one of the vendored families, Manrope or Newsreader.

tests/test_font_faces.js checks the stylesheet side: every piece of
text has a face cut for its weight and size.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _project import Project, real_dist_fingerprint  # noqa: E402

PROJECT = None
_REAL_DIST = None

DOCS = ("Resume", "Cover_Letter")


def setUpModule():
    global PROJECT, _REAL_DIST
    _REAL_DIST = real_dist_fingerprint()
    PROJECT = Project("pdf-fonts-test")
    problem = PROJECT.build()
    if problem:
        PROJECT.remove()
        PROJECT = None
        raise unittest.SkipTest(f"could not build a copy of the project to test against ({problem})")


def tearDownModule():
    if PROJECT is not None:
        PROJECT.remove()
    assert real_dist_fingerprint() == _REAL_DIST, "this checkout's dist/ was written during the suite"


def _fonts(doc):
    """(page number, font dictionary) for every font the pages use."""
    import logging
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    found = list(PROJECT.dist.glob(f"*_{doc}.pdf"))
    assert len(found) == 1, found
    out = []
    for number, page in enumerate(PdfReader(str(found[0])).pages, 1):
        resources = page.get("/Resources") or {}
        fonts = resources.get_object().get("/Font") if resources else None
        for ref in (fonts.get_object().values() if fonts else []):
            out.append((number, ref.get_object()))
    return out


def _descriptor(font):
    """The font descriptor, looking through a Type 0 font to its descendant."""
    if font.get("/Subtype") == "/Type0":
        font = font["/DescendantFonts"][0].get_object()
    fd = font.get("/FontDescriptor")
    return fd.get_object() if fd is not None else None


class TestPdfFonts(unittest.TestCase):
    def test_fonts_are_found(self):
        for doc in DOCS:
            with self.subTest(document=doc):
                self.assertGreater(len(_fonts(doc)), 0, "no fonts found to check")

    def test_no_type3(self):
        for doc in DOCS:
            for page, font in _fonts(doc):
                with self.subTest(document=doc, page=page, font=str(font.get("/BaseFont"))):
                    self.assertNotEqual(font.get("/Subtype"), "/Type3")

    def test_embedded_as_truetype(self):
        for doc in DOCS:
            for page, font in _fonts(doc):
                with self.subTest(document=doc, page=page, font=str(font.get("/BaseFont"))):
                    fd = _descriptor(font)
                    self.assertIsNotNone(fd, "no font descriptor")
                    self.assertIn("/FontFile2", fd, "outlines are not an embedded TrueType program")

    def test_only_the_vendored_families(self):
        for doc in DOCS:
            for page, font in _fonts(doc):
                name = str(font.get("/BaseFont", ""))
                with self.subTest(document=doc, page=page, font=name):
                    # Subset fonts are named "ABCDEF+Family-..."
                    family = name.lstrip("/").split("+")[-1]
                    self.assertRegex(family, r"^(Manrope|Newsreader)[A-Za-z]*-")


if __name__ == "__main__":
    unittest.main()
