"""
Tests for tests/_marked_content.py, the checker the artifact-marking
suites (test_crop_pdf, test_tagged_groups, test_pdf_accessibility) rely
on to say a tagged PDF's content is right.

WHY
───
A checker that misses a case passes the marking that gets it wrong.
This one had four blind spots, each pinned here on a small synthetic
PDF:

  • drawing inside a marked-content sequence that is neither an
    /Artifact nor tagged (optional content, /OC; a bare /Span) counted
    as covered, though it is neither tagged nor an artifact;
  • an MCID given through a named property list (/P /MC0 BDC, with /MC0
    in /Properties) was not seen, so tagged content hidden inside an
    /Artifact that way passed;
  • an unbalanced BMC/EMC, q/Q or BT/ET was never reported;
  • an MCID was never checked against the structure tree: its stream's
    /StructParents, the ParentTree entry, and the element listing it.

Chromium's output has none of these (test_pdf_accessibility checks the
built documents with the same checker); the cases are built by hand.
"""

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from pypdf import PdfReader, PdfWriter  # noqa: E402
from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject,  # noqa: E402
                           DictionaryObject, FloatObject, NameObject, NumberObject)

import _marked_content  # noqa: E402

TEXT = b"BT /F1 9 Tf 72 640 Td (Text) Tj ET"


def entries(**values):
    return DictionaryObject({NameObject(f"/{k}"): v for k, v in values.items()})


def make_pdf(content, form=None, page_parent=True, kids=None, form_kid="stm",
             properties=None):
    """A one-page tagged PDF whose page draws `content`. With `form`, a
    form XObject /X1 with that content and /StructParents 1. The
    ParentTree maps the page's MCID 0 to a /P that lists `kids` (default
    MCID 0 of the page), and the form's MCID 0 to a /Span whose MCR
    points at the form ("stm"), or is a bare 0 ("int"). `properties`
    becomes the page's (and the form's) /Properties. Read back."""
    w = PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    font = w._add_object(entries(Type=NameObject("/Font"), Subtype=NameObject("/Type1"),
                                 BaseFont=NameObject("/Helvetica")))
    page[NameObject("/Resources")] = entries(Font=entries(F1=font))
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = w._add_object(stream)
    if page_parent:
        page[NameObject("/StructParents")] = NumberObject(0)
    nums = []
    tree = w._add_object(entries(Type=NameObject("/StructTreeRoot")))
    p = w._add_object(entries(
        Type=NameObject("/StructElem"), S=NameObject("/P"), P=tree,
        Pg=page.indirect_reference,
        K=NumberObject(0) if kids is None else kids))
    nums += [NumberObject(0), ArrayObject([p])]
    tree_kids = [p]
    if form is not None:
        x = DecodedStreamObject()
        x.set_data(form)
        x.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(FloatObject(v) for v in (0, 0, 100, 20)),
            NameObject("/Resources"): entries(Font=entries(F1=font)),
            NameObject("/StructParents"): NumberObject(1),
        })
        ref = w._add_object(x)
        page["/Resources"][NameObject("/XObject")] = entries(X1=ref)
        kid = (entries(Type=NameObject("/MCR"), MCID=NumberObject(0), Stm=ref)
               if form_kid == "stm" else NumberObject(0))
        span = w._add_object(entries(Type=NameObject("/StructElem"), S=NameObject("/Span"),
                                     P=tree, Pg=page.indirect_reference, K=kid))
        nums += [NumberObject(1), ArrayObject([span])]
        tree_kids.append(span)
        if properties is not None:
            x["/Resources"][NameObject("/Properties")] = properties
    if properties is not None:
        page["/Resources"][NameObject("/Properties")] = properties
    root = tree.get_object()
    root[NameObject("/K")] = ArrayObject(tree_kids)
    root[NameObject("/ParentTree")] = w._add_object(entries(Nums=ArrayObject(nums)))
    w.root_object[NameObject("/StructTreeRoot")] = tree
    w.root_object[NameObject("/MarkInfo")] = entries(Marked=BooleanObject(True))
    buf = io.BytesIO()
    w.write(buf)
    buf.seek(0)
    return PdfReader(buf)


class TestWhatCoversDrawing(unittest.TestCase):
    def test_tagged_and_artifact_drawing_is_covered(self):
        pdf = make_pdf(b"/P <</MCID 0>> BDC " + TEXT + b" EMC\n"
                       b"/Artifact BMC 0 0 10 10 re f EMC\n")
        self.assertEqual(_marked_content.check(pdf), ([], [], [], []))

    def test_other_marked_content_does_not_cover_drawing(self):
        pdf = make_pdf(b"/OC /oc1 BDC 0 0 10 10 re f EMC\n"
                       b"/Span BMC " + TEXT + b" EMC\n"
                       b"/P <</MCID 0>> BDC /Span BMC " + TEXT + b" EMC EMC\n")
        self.assertEqual(_marked_content.problems(pdf), (["page 1: f", "page 1: Tj"], []))

    def test_an_mcid_in_a_named_property_list_covers_drawing(self):
        props = entries(MC0=entries(MCID=NumberObject(0)), MC1=entries(Foo=NumberObject(1)))
        pdf = make_pdf(b"/P /MC0 BDC " + TEXT + b" EMC\n/P /MC1 BDC 0 0 1 1 re f EMC\n",
                       properties=props)
        self.assertEqual(_marked_content.check(pdf), (["page 1: f"], [], [], []))


class TestHidden(unittest.TestCase):
    def test_an_mcid_inside_an_artifact(self):
        pdf = make_pdf(b"/Artifact BMC /P <</MCID 0>> BDC " + TEXT + b" EMC EMC\n")
        self.assertEqual(_marked_content.problems(pdf), ([], ["page 1: MCID 0"]))

    def test_a_named_mcid_inside_an_artifact(self):
        props = entries(MC0=entries(MCID=NumberObject(0)))
        pdf = make_pdf(b"/Artifact BMC /P /MC0 BDC " + TEXT + b" EMC EMC\n",
                       properties=props)
        self.assertEqual(_marked_content.problems(pdf), ([], ["page 1: MCID 0"]))

    def test_an_artifact_left_open_around_a_form(self):
        # The sequence open at the `Do` holds the form's content.
        pdf = make_pdf(b"/P <</MCID 0>> BDC EMC /Artifact BMC /X1 Do EMC\n",
                       form=b"/Span <</MCID 0>> BDC " + TEXT + b" EMC\n")
        self.assertEqual(_marked_content.problems(pdf), ([], ["page 1 /X1: MCID 0"]))


class TestBalance(unittest.TestCase):
    def test_unbalanced_operators_are_reported(self):
        cases = {
            b"/Artifact BMC 0 0 1 1 re f\n": "1 marked-content sequence(s) left open",
            b"/Artifact BMC 0 0 1 1 re f EMC EMC\n": "EMC with no BMC or BDC open",
            b"q /Artifact BMC 0 0 1 1 re f EMC\n": "1 q left open",
            b"/Artifact BMC 0 0 1 1 re f EMC Q\n": "Q with no q open",
            b"/Artifact BMC BT ET ET EMC\n": "ET with no BT open",
            b"/Artifact BMC BT BT ET EMC\n": "BT inside BT",
            b"/Artifact BMC BT EMC\n": "BT left open",
        }
        for content, want in cases.items():
            with self.subTest(content=content):
                self.assertEqual(_marked_content.check(make_pdf(content)).unbalanced,
                                 [f"page 1: {want}"])

    def test_a_form_cannot_close_what_its_drawer_opened(self):
        pdf = make_pdf(b"/Artifact BMC /X1 Do EMC\n", form=b"EMC\n")
        self.assertEqual(_marked_content.check(pdf).unbalanced,
                         ["page 1 /X1: EMC with no BMC or BDC open"])

    def test_chromium_closing_a_sequence_after_its_q_is_balanced(self):
        # Chromium opens a BDC inside q … Q and closes it after the Q.
        pdf = make_pdf(b"q /P <</MCID 0>> BDC " + TEXT + b" Q EMC\n")
        self.assertEqual(_marked_content.check(pdf), ([], [], [], []))


class TestOrphans(unittest.TestCase):
    def test_mcids_the_structure_tree_leads_back_to(self):
        pdf = make_pdf(b"/P <</MCID 0>> BDC " + TEXT + b" EMC /X1 Do\n",
                       form=b"/Span <</MCID 0>> BDC " + TEXT + b" EMC\n")
        self.assertEqual(_marked_content.check(pdf), ([], [], [], []))

    def test_mcids_it_does_not(self):
        mcr = entries(Type=NameObject("/MCR"), MCID=NumberObject(0))
        cases = [
            (dict(content=b"/P <</MCID 0>> BDC " + TEXT + b" EMC\n", page_parent=False),
             "page 1: MCID 0, in a stream without /StructParents"),
            (dict(content=b"/P <</MCID 3>> BDC " + TEXT + b" EMC\n"),
             "page 1: MCID 3, with no structure element at ParentTree 0"),
            (dict(content=b"/P <</MCID 0>> BDC " + TEXT + b" EMC\n", kids=NumberObject(1)),
             "page 1: MCID 0, which its structure element (/P) does not list"),
            # An MCR that names no stream is the page's MCID 0, not the form's.
            (dict(content=b"/X1 Do\n", form=b"/Span <</MCID 0>> BDC " + TEXT + b" EMC\n",
                  form_kid="int"),
             "page 1 /X1: MCID 0, which its structure element (/Span) does not list"),
            (dict(content=b"/P <</MCID 0>> BDC " + TEXT + b" EMC\n",
                  kids=ArrayObject([mcr])), None),
        ]
        for kwargs, want in cases:
            with self.subTest(want=want):
                self.assertEqual(_marked_content.check(make_pdf(**kwargs)).orphans,
                                 [want] if want else [])


if __name__ == "__main__":
    unittest.main()
