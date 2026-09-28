"""
Tests that the built PDFs keep what a screen reader and a PDF/UA-1
validator need.

WHY
───
veraPDF (PDF/UA-1, `-f ua1`) failed both documents on four counts, all
fixed together (CHANGELOG, "PDF/UA"):

  • the section headings were not in the structure tree at all, and
    the role line was untagged. Each was written twice: a visible copy
    marked aria-hidden, which Chromium leaves out of the tags, and an
    off-screen copy for screen readers, which Chromium never draws, so
    it left no content to tag either. The templates now write each
    once, visible and tagged;
  • link annotations had no /Contents (build/crop_pdf.py,
    describe_links);
  • drawing Chromium does not tag — the page background, the rules,
    the page footer — was neither tagged nor an /Artifact
    (mark_untagged_as_artifacts);
  • the XMP stream had no /Type /Metadata /Subtype /XML (apply_xmp).

After the fix veraPDF passes every PDF/UA-1 rule on both documents
but one: the documents do not claim PDF/UA in their XMP, on purpose.
veraPDF is not a project dependency, so this suite checks the same
things directly, on both documents built from the templates in a
throwaway copy of the project (tests/_project.py).

Reading order is checked too. Chromium tags in document order: page
by page, sidebar before main column, which put page 2's continued jobs
under page 1's sidebar headings. templates/resume.j2 reorders the tags
with aria-owns (every main column, then every sidebar); the structure
tree must list every job and degree before the first sidebar heading.

The artifact marking would also hide a regression: text that loses its
tag would simply become an artifact. So the HTML is checked too: the
only elements kept from screen readers are the decorative rule and the
page footer.
"""

import re
import sys
from html.parser import HTMLParser
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _project import Project, real_dist_fingerprint  # noqa: E402

PROJECT = None
_REAL_DIST = None

DOCS = (("Resume", "index.html"), ("Cover_Letter", "letter.html"))

DRAWING = {b"f", b"F", b"f*", b"S", b"s", b"B", b"B*", b"b", b"b*",
           b"Tj", b"TJ", b"'", b'"', b"Do", b"sh", b"INLINE IMAGE"}


def setUpModule():
    global PROJECT, _REAL_DIST
    _REAL_DIST = real_dist_fingerprint()
    PROJECT = Project("pdf-accessibility-test")
    problem = PROJECT.build()
    if problem:
        PROJECT.remove()
        PROJECT = None
        raise unittest.SkipTest(f"could not build a copy of the project to test against ({problem})")


def tearDownModule():
    if PROJECT is not None:
        PROJECT.remove()
    assert real_dist_fingerprint() == _REAL_DIST, "this checkout's dist/ was written during the suite"


def _reader(doc):
    import logging
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    found = list(PROJECT.dist.glob(f"*_{doc}.pdf"))
    assert len(found) == 1, found
    return PdfReader(str(found[0]))


def _structure_types(reader):
    """Every structure element's type, in document order."""
    out = []

    def walk(node):
        node = node.get_object()
        if not hasattr(node, "get"):
            return
        if node.get("/S"):
            out.append(str(node["/S"]))
        kids = node.get("/K")
        for kid in kids if isinstance(kids, list) else ([kids] if kids is not None else []):
            try:
                kid = kid.get_object()
            except AttributeError:
                pass
            if hasattr(kid, "get"):
                walk(kid)

    walk(reader.trailer["/Root"]["/StructTreeRoot"])
    return out


class _ColumnHeadings(HTMLParser):
    """Counts the section headings in the main columns and the sidebars."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.main = 0
        self.side = 0

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        column = "main" if "main-col" in classes else "side" if "sidebar" in classes else None
        self.stack.append((tag, column))
        if tag == "h2" and "section-heading" in classes:
            columns = [c for _, c in self.stack if c]
            if columns and columns[-1] == "main":
                self.main += 1
            elif columns:
                self.side += 1

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break


class TestPdfAccessibility(unittest.TestCase):
    def test_headings_are_tagged_in_order(self):
        for doc, page in DOCS:
            with self.subTest(document=doc):
                html = (PROJECT.dist / page).read_text(encoding="utf-8")
                headings = [t for t in _structure_types(_reader(doc)) if re.fullmatch(r"/H[1-6]", t)]
                self.assertEqual(headings[:1], ["/H1"], "the name comes first, as H1")
                self.assertEqual(headings.count("/H2"), html.count('<h2 class="section-heading"'),
                                 "every section heading is in the structure tree")
                levels = [int(h[2]) for h in headings]
                skips = [(a, b) for a, b in zip(levels, levels[1:]) if b > a + 1]
                self.assertEqual(skips, [], "no heading level is skipped going down")

    def test_the_name_is_one_heading(self):
        # Written as two spans, NVDA with Acrobat read two level-1
        # headings, "Gaius" and "Caesar", and left the name out of its
        # heading list. One text run gives the H1 one piece of content.
        for doc, _ in DOCS:
            with self.subTest(document=doc):
                h1 = []

                def walk(node):
                    node = node.get_object()
                    if not hasattr(node, "get"):
                        return
                    if node.get("/S") == "/H1":
                        h1.append(node)
                        return
                    kids = node.get("/K")
                    for kid in kids if isinstance(kids, list) else ([kids] if kids is not None else []):
                        try:
                            kid = kid.get_object()
                        except AttributeError:
                            pass
                        if hasattr(kid, "get"):
                            walk(kid)

                walk(_reader(doc).trailer["/Root"]["/StructTreeRoot"])
                self.assertEqual(len(h1), 1)
                kids = h1[0].get("/K")
                self.assertEqual(len(kids) if isinstance(kids, list) else 1, 1,
                                 "the name is a single piece of content")

    def test_main_column_is_read_before_the_sidebars(self):
        parser = _ColumnHeadings()
        parser.feed((PROJECT.dist / "index.html").read_text(encoding="utf-8"))
        self.assertGreater(parser.side, 0, "the template has sidebar headings")
        headings = [t for t in _structure_types(_reader("Resume")) if re.fullmatch(r"/H[1-6]", t)]
        h2 = [i for i, t in enumerate(headings) if t == "/H2"]
        first_sidebar = h2[parser.main]
        h3 = [i for i, t in enumerate(headings) if t == "/H3"]
        self.assertTrue(h3, "the template has job titles")
        self.assertLess(max(h3), first_sidebar,
                        "every job and degree comes before the first sidebar heading")
        self.assertEqual(len(h2) - parser.main, parser.side,
                         "and the sidebar headings all come after them")

    def test_only_decoration_is_hidden_from_readers(self):
        for doc, page in DOCS:
            with self.subTest(document=doc):
                html = (PROJECT.dist / page).read_text(encoding="utf-8")
                self.assertNotIn('class="visually-hidden"', html,
                                 "an off-screen copy is never drawn, so never tagged")
                hidden = re.findall(r"<(\w+)([^>]*)\baria-hidden=\"true\"", html)
                allowed = [(tag, attrs) for tag, attrs in hidden
                           if (tag == "hr" and "top-rule" in attrs)
                           or (tag == "footer" and "page-footer" in attrs)]
                self.assertEqual(hidden, allowed,
                                 "text marked aria-hidden is left untagged, and would end up an artifact")

    def test_everything_drawn_is_tagged_or_an_artifact(self):
        from pypdf.generic import ContentStream
        for doc, _ in DOCS:
            reader = _reader(doc)
            for number, page in enumerate(reader.pages, 1):
                with self.subTest(document=doc, page=number):
                    depth, loose = 0, []
                    for _, op in ContentStream(page.get_contents(), reader).operations:
                        if op in (b"BDC", b"BMC"):
                            depth += 1
                        elif op == b"EMC":
                            depth -= 1
                        elif op in DRAWING and depth == 0:
                            loose.append(op.decode("latin-1"))
                    self.assertEqual(loose, [])

    def test_links_are_described(self):
        for doc, _ in DOCS:
            reader = _reader(doc)
            links = [a.get_object() for p in reader.pages for a in (p.get("/Annots") or [])
                     if a.get_object().get("/Subtype") == "/Link"]
            with self.subTest(document=doc):
                self.assertGreater(len(links), 0, "the templates have an email and a web link")
                self.assertTrue(all(str(a.get("/Contents") or "").strip() for a in links))

    def test_xmp_stream_is_labelled(self):
        for doc, _ in DOCS:
            with self.subTest(document=doc):
                stream = _reader(doc).trailer["/Root"]["/Metadata"].get_object()
                self.assertEqual(stream.get("/Type"), "/Metadata")
                self.assertEqual(stream.get("/Subtype"), "/XML")


if __name__ == "__main__":
    unittest.main()
