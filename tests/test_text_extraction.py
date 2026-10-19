"""
Tests that the built PDFs read back as the words on the page.

WHY
───
A résumé is read by software before a person sees it, and that software
reads the PDF's text layer, not its pixels. Three things had broken it,
all invisible on screen:

  • Ligatures. With `liga` on, Chromium's PDF gave the tt / ff / ffl
    ligature glyphs no ToUnicode entry and no ActualText, so pypdf read
    "Mattis" as "Mais" and "efficitur" as "eicitur" (pdfplumber too).
    Fixed by `"liga" 0` in styles/_base.scss.
  • The role line's tracking. At 0.15em pdftotext and pdfium read the
    job title one letter at a time: "I M P E R AT O R …". Fixed by
    tracking it at 0.1em; once the fonts were static pdftotext split it
    at 0.1em again, and the 0.1em moved into the font's advance widths
    (styles/_components.scss, .name-role).
  • The name. Two spans with only markup whitespace between them read
    as "GaiusCaesar" in pypdf. Fixed by a real space inside the first.
  • Tracked headings and labels. Xpdf 4.06's pdftotext read the section
    headings "C E RT I F ICAT ION S", and poppler's read a details
    label "SECURIT Y CLEARANCE". Their 0.1em moved into the font too.

WHAT THIS CHECKS
────────────────
Both documents are built from the templates in a throwaway copy of the
project (tests/_project.py), then read with pypdf and pdfium — the two
extractors in requirements.txt. For each:

  • every word the page shows is in the text, as often as it is shown;
  • no ligature code points (U+FB00–FB06) appear;
  • the name and the role read as whole words;
  • no odd code points: spaces a reader may not split words on,
    zero-width characters, U+FFFD, private-use slots;
  • no capital letter stands alone (other than A and I), as tracked
    uppercase read before its tracking moved into the font;
  • no soft hyphen (U+00AD): a reader keeps it inside the word, so a
    search for the word misses it.

The template has no details block, so TestDetailLabels builds a copy
with one and checks that its labels read whole and its values each on
one line.

TestReadingOrder reads the résumé in the order its text is written into
the PDF, which is what pypdf, pdfium and `pdftotext -raw` give and what
many parsers read (Tika's defaults, for one). Chromium writes text in
CSS paint order, not DOM order: a positioned box paints after all the
in-flow content of its column. With `position: relative` on the bullets
(only there to anchor their dot), every page's bullets came after all
of that page's job titles, and page 2's after EDUCATION, so a reader in
stream order filed them under the wrong job; and the sidebar, first in
the DOM, came before the main column on every page, so page 1's sidebar
split the jobs of page 1 from those of page 2. It checks, against the
final HTML, that each bullet follows its own job's title and reads in
one piece, that the main column's headings come in order, that no
sidebar heading falls between the main column's first heading and its
last, and that the "Page 1 of 2" footer ends each page's text. The
footer, at z-index auto, had been written between the two columns.

TestLayoutOrder reads it in pdfminer.six's layout order instead, with
the default LAParams and with boxes_flow=None (top to bottom, then left
to right): the order parsers built on pdfminer.six read, pdfplumber
among them. It applies the checks that mean something for a reader
that orders text by position: each bullet follows its own job and reads
in one piece, and the main column's headings come in order.

TestParserConventions guards what résumé parsers document asking of
the text: each job's dates on one line, each date and its location as
two words with no symbol between them, the contact details before the
first section, each contact detail and link text whole on one line (so
a wrap can never split an email or a URL), and no word in the text
layer that the page does not show. The separator between a date and
its location was a middle dot (U+00B7) in the text, and RChilli
documents returning an empty date when a symbol that is not part of a
date appears in it; the dot is drawn now (styles/_components.scss,
.job-date::after). The rest the build always gave.

TestHiddenText guards against what hidden-text detectors look for
(arXiv 2605.28999), none of which the build does: no glyph under 4 pt,
none whose colour is within 15 RGB units of the white page (two of the
paper's rules), none drawn in an invisible render mode or outside
the crop box (tricks it names), and no /ActualText, which makes a
reader read text other than the glyphs shown. Chromium writes no
/ActualText for these documents; with ligatures on it wrote none for
the ligature glyphs either (see above).

pdfplumber and pdftotext, also checked when the fix was made, are not
project dependencies; they are used here when installed. pdfminer.six,
which pdfplumber is built on, is a test dependency (requirements.txt),
so its layout order is read on every run that installed them. A
missing extractor used to drop out without a word, so a run that never read the PDFs
with pdftotext looked the same as one that did. TestOptionalExtractors
now skips once for each one that is missing, and says which. The
pdftotext that matters most is Xpdf's, in the Windows CI job, which
fails before the tests if it has none.
"""

import html
import re
import shutil
import subprocess
import sys
import unicodedata
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _project import Project, build_error, real_dist_fingerprint  # noqa: E402

PROJECT = None
_REAL_DIST = None

DOCS = (("Resume", "index.html"), ("Cover_Letter", "letter.html"))


def setUpModule():
    global PROJECT, _REAL_DIST
    _REAL_DIST = real_dist_fingerprint()
    PROJECT = Project("text-extraction-test")
    problem = PROJECT.build()
    if problem:
        PROJECT.remove()
        PROJECT = None
        raise build_error(problem)


def tearDownModule():
    if PROJECT is not None:
        PROJECT.remove()
    assert real_dist_fingerprint() == _REAL_DIST, "this checkout's dist/ was written during the suite"


def _visible_words(html_text):
    h = re.sub(r"(?s)<(style|script|head)\b.*?</\1>", " ", html_text)
    return re.findall(r"[A-Za-z]+", html.unescape(re.sub(r"<[^>]+>", " ", h)))


def _pdf(doc):
    found = list(PROJECT.dist.glob(f"*_{doc}.pdf"))
    assert len(found) == 1, found
    return found[0]


def _pypdf(path):
    import logging
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)


def _pdfium(path):
    return "\n".join(_pdfium_pages(path))


def _pdfplumber(path):
    try:
        import pdfplumber
    except ImportError:
        return None
    import logging
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    with pdfplumber.open(str(path)) as d:
        return "\n".join(p.extract_text() or "" for p in d.pages)


def _pdftotext(path):
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    # Xpdf's pdftotext writes Latin-1 unless told otherwise, which turned
    # an en dash, or a ligature, into U+FFFD.
    return subprocess.run([exe, "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def _version(name):
    """The extractor's version, for a failure message."""
    try:
        if name == "pdftotext":
            r = subprocess.run([shutil.which("pdftotext"), "-v"], capture_output=True, text=True)
            return (r.stdout + r.stderr).strip().splitlines()[0]
        from importlib.metadata import version
        return version({"pdfium": "pypdfium2", "pdfminer": "pdfminer.six"}.get(name, name))
    except Exception:  # noqa: BLE001 — only ever decoration on a failure
        return "(version unknown)"


EXTRACTORS = {"pypdf": _pypdf, "pdfium": _pdfium,
              "pdfplumber": _pdfplumber, "pdftotext": _pdftotext}


class TestOptionalExtractors(unittest.TestCase):
    """The extractors this run may lack: pdfplumber and pdftotext, which
    are optional, and pdfminer.six, a test dependency.

    The other tests use whichever extractors are installed. These say
    which ones were not: a skip here means every check in this file ran
    without that extractor.
    """

    def test_pdfplumber_is_installed(self):
        try:
            import pdfplumber  # noqa: F401
        except ImportError:
            self.skipTest("pdfplumber is not installed, so the text layer "
                          "is not read with it (optional)")

    def test_pdfminer_is_installed(self):
        try:
            import pdfminer  # noqa: F401
        except ImportError:
            self.skipTest("pdfminer.six is not installed (requirements.txt), so "
                          "the text layer is not read in its layout order")

    def test_pdftotext_is_on_path(self):
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not on PATH, so the text layer "
                          "is not read with it (optional)")


class TestTextLayer(unittest.TestCase):
    def _texts(self, doc):
        path = _pdf(doc)
        out = {}
        for name, fn in EXTRACTORS.items():
            text = fn(path)
            if text is not None:
                out[name] = text
        return out

    def test_every_visible_word_is_extracted(self):
        for doc, page in DOCS:
            want = Counter(w.lower() for w in _visible_words(
                (PROJECT.dist / page).read_text(encoding="utf-8")))
            self.assertGreater(sum(want.values()), 50, f"{doc}: no text found in {page}")
            for name, text in self._texts(doc).items():
                with self.subTest(document=doc, extractor=name):
                    got = Counter(w.lower() for w in re.findall(r"[A-Za-z]+", text))
                    missing = {w: n - got[w] for w, n in want.items() if got[w] < n}
                    # What the extractor made of each lost word, and which
                    # extractor it was: a heading split into letters reads
                    # very differently from one dropped outright.
                    near = [line.strip() for line in text.splitlines()
                            if any(w in re.sub(r"[^a-z]", "", line.lower()) for w in missing)]
                    self.assertEqual(missing, {}, "words on the page the text layer lacks; "
                                     f"{name} ({_version(name)}) read them as {near[:6]!r}")

    def test_no_ligature_code_points(self):
        for doc, _ in DOCS:
            for name, text in self._texts(doc).items():
                with self.subTest(document=doc, extractor=name):
                    self.assertEqual(re.findall("[ﬀ-ﬆ]", text), [])

    # Spaces a reader may not split words on (U+00A0 and the narrow and
    # thin ones), zero-width characters, U+FFFD for a glyph that maps to
    # nothing, and private-use code points for one that maps to a font's
    # own slot. poppler's pdftotext turns U+00A0 into a space, so pypdf
    # and pdfium are the stricter readers here.
    ODD = re.compile("[     ​-‏⁠﻿�-]")

    def test_no_odd_code_points(self):
        for doc, _ in DOCS:
            for name, text in self._texts(doc).items():
                with self.subTest(document=doc, extractor=name):
                    found = Counter(f"U+{ord(c):04X}" for c in self.ODD.findall(text))
                    self.assertEqual(found, {}, f"{name} ({_version(name)})")

    def test_no_soft_hyphens(self):
        # Xpdf's pdftotext reads a soft hyphen in the data inside its
        # word ("Imper" U+00AD "ator" as "Impera" U+00AD " tor"), where a
        # search for the word misses it. pypdf and pdfium drop it.
        for doc, _ in DOCS:
            for name, text in self._texts(doc).items():
                with self.subTest(document=doc, extractor=name):
                    words = [w for w in text.split() if "\u00ad" in w]
                    self.assertEqual(words, [], f"{name} ({_version(name)}) reads soft hyphens")

    def test_no_word_reads_as_single_letters(self):
        # A capital letter standing alone, other than A and I: how tracked
        # uppercase read before its tracking moved into the font, "C E RT
        # I F ICAT ION S", "I M P E R AT O R", "SECURIT Y". The name, role
        # and labels are checked whole above; this covers every heading.
        lone = re.compile(r"(?<![^\s(])[B-HJ-Z](?![^\s,.;:)])")
        for doc, _ in DOCS:
            for name, text in self._texts(doc).items():
                with self.subTest(document=doc, extractor=name):
                    lines = [" ".join(line.split()) for line in text.splitlines() if lone.search(line)]
                    self.assertEqual(lines, [], f"{name} ({_version(name)}) splits these into letters")

    def test_the_name_and_role_read_whole(self):
        # Both documents carry the same header, role line included.
        for doc, _ in DOCS:
            for name, text in self._texts(doc).items():
                # The header's lines, for the failure message only.
                head = [" ".join(line.split()) for line in text.splitlines() if line.strip()][:3]
                with self.subTest(document=doc, extractor=name, field="name"):
                    self.assertIsNotNone(re.search(r"Gaius Caesar", text),
                                         f"the header reads as: {head}")
                with self.subTest(document=doc, extractor=name, field="role"):
                    self.assertIsNotNone(re.search(r"IMPERATOR\s+ROMANUS", text),
                                         f"the header reads as: {head}")


def _norm(text):
    # The PDF's text can curl a quote the HTML has straight.
    text = unicodedata.normalize("NFKC", text).replace(" ", " ")
    text = text.translate(str.maketrans("‘’“”", "''\"\""))
    return re.sub(r"\s+", " ", text).strip().lower()


class _Outline(HTMLParser):
    """The résumé's structure from its final HTML, in DOM order: one
    (page, column, kind, text) per section heading, job title and bullet,
    column "main" or "side", kind "heading", "title" or "bullet"."""

    VOID = {"meta", "link", "br", "img", "hr", "input", "source", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack, self.items, self.page, self.open = [], [], 0, None

    def _inside(self, cls):
        return any(cls in classes for _, classes in self.stack)

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        classes = (dict(attrs).get("class") or "").split()
        if tag == "article" and "page" in classes:
            self.page += 1
        kind = ("heading" if "section-heading" in classes
                else "title" if "entry-title" in classes and "list-group-heading" not in classes
                else "bullet" if tag == "li" and self._inside("bullets") else None)
        if kind and self.open is None:
            self.open = (kind, [], len(self.stack))
        self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        if tag in self.VOID or not self.stack:
            return
        self.stack.pop()
        if self.open and len(self.stack) == self.open[2]:
            kind, parts, _ = self.open
            text = _norm("".join(parts))
            if text:
                self.items.append((self.page, "side" if self._inside("sidebar") else "main", kind, text))
            self.open = None

    def handle_data(self, data):
        if self.open:
            self.open[1].append(data)


def _cells(text):
    """`text` on one line, and where each of its lines starts there."""
    flat, starts, at = [], [], 0
    for line in text.splitlines():
        cell = _norm(line)
        if cell:
            starts.append((at, cell))
            flat.append(cell)
            at += len(cell) + 1
    return " ".join(flat), starts


def _pypdf_pages(path):
    import logging
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    return [p.extract_text() or "" for p in PdfReader(str(path)).pages]


def _pdfium_pages(path):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(path))
    try:
        out = []
        for i in range(len(doc)):
            tp = doc[i].get_textpage()
            out.append(tp.get_text_range())
            tp.close()
        return out
    finally:
        doc.close()


def _pdftotext_raw_pages(path):
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    text = subprocess.run([exe, "-raw", "-enc", "UTF-8", str(path), "-"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout
    return text.split("\f")


# Extractors that give text in the order it is written into the PDF.
STREAM_ORDER = {"pypdf": _pypdf_pages, "pdfium": _pdfium_pages, "pdftotext -raw": _pdftotext_raw_pages}


def _pdfminer_pages(path, **laparams):
    try:
        from pdfminer.high_level import extract_text
        from pdfminer.layout import LAParams
    except ImportError:
        return None
    import logging
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    # One form feed after each page, the last one included.
    return extract_text(str(path), laparams=LAParams(**laparams)).split("\f")


# Extractors that order text by its position on the page: pdfminer.six's
# layout analysis, as it ships, and sorted only top to bottom, then left
# to right.
LAYOUT_ORDER = {"pdfminer": _pdfminer_pages,
                "pdfminer boxes_flow=None": lambda path: _pdfminer_pages(path, boxes_flow=None)}


class TestReadingOrder(unittest.TestCase):
    """The résumé's text, in the order it is written into the PDF,
    follows the document: each bullet after its own job, the main
    column's sections in order, and no sidebar heading between the
    main column's pages. See the module docstring for why it once
    did not."""

    ORDER = STREAM_ORDER

    @classmethod
    def setUpClass(cls):
        outline = _Outline()
        outline.feed((PROJECT.dist / "index.html").read_text(encoding="utf-8"))
        cls.items = outline.items
        cls.pages = {}
        for name, fn in cls.ORDER.items():
            pages = fn(_pdf("Resume"))
            if pages is not None:
                cls.pages[name] = pages

    def test_the_outline_is_found(self):
        kinds = Counter(kind for _, _, kind, _ in self.items)
        self.assertGreater(kinds["bullet"], 5, kinds)
        self.assertGreater(kinds["title"], 2, kinds)

    @staticmethod
    def _position(flat, starts, item):
        # A heading is a line of its own (its words also occur in prose);
        # a title or bullet is found by its first words, which may wrap.
        if item[2] == "heading":
            return next((at for at, cell in starts if cell == item[3]), -1)
        return flat.find(" ".join(item[3].split()[:6]))

    def test_each_bullet_follows_its_own_job(self):
        for name, pages in self.pages.items():
            flat, starts = _cells("\n".join(pages))
            pos = [self._position(flat, starts, it) for it in self.items]
            markers = [(p, i) for i, (it, p) in enumerate(zip(self.items, pos))
                       if it[1] == "main" and it[2] in ("title", "heading") and p >= 0]
            wrong, owner = [], None
            for i, it in enumerate(self.items):
                if it[1] == "main" and it[2] == "title":
                    owner = i
                elif it[1] == "main" and it[2] == "heading":
                    owner = None
                elif it[1] == "main" and it[2] == "bullet":
                    before = [m for m in markers if m[0] <= pos[i]]
                    if pos[i] < 0 or not before or max(before)[1] != owner:
                        wrong.append(it[3][:40])
            with self.subTest(extractor=name):
                self.assertEqual(wrong, [], f"{name} ({_version(name.split()[0])}) reads these bullets "
                                 "away from their job (the nearest title or heading before "
                                 "them is another one's)")

    def test_the_main_sections_come_in_order(self):
        for name, pages in self.pages.items():
            flat, starts = _cells("\n".join(pages))
            at = [self._position(flat, starts, it) for it in self.items
                  if it[1] == "main" and it[2] == "heading"]
            with self.subTest(extractor=name):
                self.assertTrue(all(a >= 0 for a in at) and at == sorted(at),
                                f"{name}: section headings at {at}")

    def test_the_main_column_reads_unbroken(self):
        self.assertGreater(max(it[0] for it in self.items), 1,
                           "the template fits on one page, so a page break is never read")
        for name, pages in self.pages.items():
            flat, starts = _cells("\n".join(pages))
            found = {"main": [], "side": []}
            for it in self.items:
                if it[2] in ("heading", "title"):
                    p = self._position(flat, starts, it)
                    if p >= 0:
                        found[it[1]].append((p, it[3]))
            with self.subTest(extractor=name):
                self.assertTrue(found["main"] and found["side"], found)
                lo, hi = min(found["main"])[0], max(found["main"])[0]
                inside = [text for p, text in sorted(found["side"]) if lo < p < hi]
                self.assertEqual(inside, [], f"{name}: sidebar headings read between "
                                 "the main column's first and last heading")

    def test_each_bullet_reads_whole(self):
        # Compared without whitespace: a line break inside a bullet, and a
        # hyphen at one ("Cambridge-" / "aligned"), are not breaks in it;
        # a sidebar line written between its lines is.
        squash = lambda text: re.sub(r"\s+", "", text)
        bullets = [it[3] for it in self.items if it[2] == "bullet"]
        for name, pages in self.pages.items():
            flat = squash(_cells("\n".join(pages))[0])
            broken = [b[:40] for b in bullets if squash(b) not in flat]
            with self.subTest(extractor=name):
                self.assertEqual(broken, [], f"{name}: these bullets do not read in one piece")

    def test_the_page_marker_reads_last(self):
        # The "Page 1 of 2" footer ends each page's text, outside every
        # section, not between two columns.
        count = max(it[0] for it in self.items)
        for name, pages in self.pages.items():
            for number, page in enumerate(pages[:count], 1):
                lines = [_norm(line) for line in page.splitlines() if line.strip()]
                with self.subTest(extractor=name, page=number):
                    self.assertEqual(lines[-1:], [f"page {number} of {count}"],
                                     f"{name}: page {number} ends with {lines[-3:]!r}")


class TestLayoutOrder(TestReadingOrder):
    """The résumé's text, in pdfminer.six's layout order, follows the
    document too: each bullet after its own job and in one piece, and
    the main column's sections in order. pdfminer.six is in
    requirements.txt; TestOptionalExtractors says when it is missing."""

    ORDER = LAYOUT_ORDER

    # Not checked here. The outline is the same HTML, checked above. A
    # reader that orders text by position reads each page's sidebar
    # beside its main column, so the sidebar falls between the main
    # column's pages by design; and nothing in its order promises the
    # footer comes last. Both are stream-order checks.
    test_the_outline_is_found = None
    test_the_main_column_reads_unbroken = None
    test_the_page_marker_reads_last = None


def _element_texts(html_text, pattern):
    """The text of every element `pattern` (a regex with one group for
    its inner HTML) matches, tags stripped and normalised."""
    return [_norm(html.unescape(re.sub(r"<[^>]+>", " ", inner)))
            for inner in re.findall(pattern, html_text, re.S)]


def _contact_values(html_text):
    """The header's contact details, the details blocks' values and the
    text of every link, from the final HTML, each once."""
    values = _element_texts(html_text, r'<span class="(?:header-contact-(?:address|item)|detail-value)">'
                                       r'(.*?)</span>')
    return list(dict.fromkeys(values + _element_texts(html_text, r"<a\b[^>]*>(.*?)</a>")))


def _not_on_one_line(text, values):
    """The `values` no single line of `text` holds whole."""
    lines = [_norm(line) for line in text.splitlines()]
    return [v for v in values if not any(v in line for line in lines)]


class TestParserConventions(unittest.TestCase):
    """What résumé parsers document asking of the text, and the build
    gives: each job's dates on one line (Textkernel's quality
    codes 224/225), each date and its location with no symbol between
    them (RChilli documents returning an empty date when a symbol that
    is not part of a date appears in it), the contact details first, as
    page text (Greenhouse and RChilli lose them from headers, footers
    and text boxes), each contact detail and link whole on one line (a
    parser matches an email or a URL within a line), and no text that
    is not on the page (hidden text is flagged as keyword stuffing;
    detectors compare the text layer with what is shown). These guard
    them. Only the symbol was ever broken: the separator was a middle
    dot in the text until it was drawn."""

    @classmethod
    def setUpClass(cls):
        cls.html = (PROJECT.dist / "index.html").read_text(encoding="utf-8")
        cls.texts = {}
        for name, fn in EXTRACTORS.items():
            text = fn(_pdf("Resume"))
            if text is not None:
                cls.texts[name] = text

    def test_each_job_date_reads_on_one_line(self):
        dates = _element_texts(self.html, r'<time class="job-date"[^>]*>(.*?)</time>')
        self.assertGreater(len(dates), 2, dates)
        for name, text in self.texts.items():
            lines = [_norm(line) for line in text.splitlines()]
            # Counted, not found: the template's dates are all alike.
            split = [d for d, n in Counter(dates).items()
                     if sum(line.count(d) for line in lines) < n]
            with self.subTest(extractor=name):
                self.assertEqual(split, [], f"{name} ({_version(name)}) splits these dates "
                                 "across lines")

    def test_each_date_and_location_read_as_two_words(self):
        # The separator is drawn. As a middle dot in the text, every
        # extractor read "date · location".
        metas = [(_norm(html.unescape(d)), _norm(html.unescape(loc))) for d, loc in re.findall(
            r'<time class="job-date"[^>]*>([^<]*)</time>\s*<span class="job-loc">([^<]*)</span>', self.html)]
        self.assertGreater(len(metas), 2, metas)
        texts = dict(self.texts)
        raw = _pdftotext_raw_pages(_pdf("Resume"))
        if raw is not None:
            texts["pdftotext -raw"] = "\n".join(raw)
        for name, text in texts.items():
            lines = [_norm(line) for line in text.splitlines()]
            # Counted, not found: the template's are all alike.
            apart = [m for m, n in Counter(metas).items()
                     if sum(line.count(f"{m[0]} {m[1]}") for line in lines) < n]
            near = [line for line in lines if any(d in line for d, _ in apart)]
            with self.subTest(extractor=name):
                self.assertEqual(apart, [], f"{name} ({_version(name.split()[0])}) does not read "
                                 f"these as the date, a space and the location; it read {near[:4]!r}")

    def test_the_contact_details_come_first(self):
        contact = _element_texts(self.html, r'<span class="header-contact-(?:address|item)">(.*?)</span>')
        self.assertGreater(len(contact), 1, contact)
        headings = _element_texts(self.html, r'<h2 class="section-heading[^"]*"[^>]*>(.*?)</h2>')
        self.assertTrue(headings, "no section heading found in the HTML")
        for name, text in self.texts.items():
            flat, starts = _cells(text)
            first = min((at for at, cell in starts if cell in headings), default=-1)
            with self.subTest(extractor=name):
                self.assertGreaterEqual(first, 0, f"{name}: no section heading on a line of its own")
                late = [c for c in contact if not 0 <= flat.find(c) < first]
                self.assertEqual(late, [], f"{name}: contact details missing or after "
                                 "the first section heading")

    def test_each_contact_detail_reads_on_one_line(self):
        # overflow-wrap breaks a word too wide for its column anywhere,
        # an email or a URL included (TestLongWords); this is where one
        # would show.
        values = _contact_values(self.html)
        self.assertGreater(len(values), 2, values)
        for name, text in self.texts.items():
            split = _not_on_one_line(text, values)
            near = [" ".join(line.split()) for line in text.splitlines()
                    if any(v[:6] in _norm(line) for v in split)]
            with self.subTest(extractor=name):
                self.assertEqual(split, [], f"{name} ({_version(name)}) does not read these "
                                 f"on one line; it read {near[:4]!r}")

    def test_no_text_that_is_not_on_the_page(self):
        shown = Counter(w.lower() for w in _visible_words(self.html))
        for name, text in self.texts.items():
            got = Counter(w.lower() for w in re.findall(r"[A-Za-z]+", text))
            extra = {w: n - shown[w] for w, n in got.items() if n > shown[w]}
            with self.subTest(extractor=name):
                self.assertEqual(extra, {}, f"{name} ({_version(name)}) reads words the "
                                 "page does not show")


def _hidden_glyphs(path):
    """Every glyph a hidden-text detector would flag, as (page, character,
    why), read with pdfium; spaces and the line breaks it adds are not
    glyphs."""
    import ctypes
    import math
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    # Modes that fill the glyph, so that its fill colour is what shows.
    filled = {raw.FPDF_TEXTRENDERMODE_FILL, raw.FPDF_TEXTRENDERMODE_FILL_STROKE,
              raw.FPDF_TEXTRENDERMODE_FILL_CLIP, raw.FPDF_TEXTRENDERMODE_FILL_STROKE_CLIP}
    found = []
    doc = pdfium.PdfDocument(str(path))
    try:
        for number in range(len(doc)):
            page = doc[number]
            left, bottom, right, top = page.get_cropbox()
            tp = page.get_textpage()
            matrix = raw.FS_MATRIX()
            r, g, b, a = (ctypes.c_uint() for _ in range(4))
            for i in range(tp.count_chars()):
                char = chr(raw.FPDFText_GetUnicode(tp, i))
                if char.isspace() or raw.FPDFText_IsGenerated(tp, i):
                    continue
                why = []
                # The size set by Tf, scaled by the text and page matrices
                # (Chromium sets it in CSS px under a 0.75 scale).
                raw.FPDFText_GetMatrix(tp, i, matrix)
                scale = math.sqrt(abs(matrix.a * matrix.d - matrix.b * matrix.c))
                size = raw.FPDFText_GetFontSize(tp, i) * scale
                if size < 4:
                    why.append(f"{size:.2f} pt")
                # The colour as it shows: blended over the white page by its
                # opacity, then compared with white by Euclidean distance.
                if raw.FPDFText_GetFillColor(tp, i, r, g, b, a):
                    shown = [(c.value * a.value + 255 * (255 - a.value)) / 255 for c in (r, g, b)]
                    if math.dist(shown, (255, 255, 255)) < 15:
                        why.append(f"colour {tuple(round(c) for c in shown)} on white")
                mode = raw.FPDFTextObj_GetTextRenderMode(raw.FPDFText_GetTextObject(tp, i))
                if mode not in filled:
                    why.append(f"render mode {mode}")
                x0, y0, x1, y1 = tp.get_charbox(i)
                if x0 < left or y0 < bottom or x1 > right or y1 > top:
                    why.append(f"at ({x0:.0f}, {y0:.0f}), outside the crop box")
                if why:
                    found.append((number + 1, char, ", ".join(why)))
            tp.close()
    finally:
        doc.close()
    return found


def _content_operations(path):
    """(where, operands, operator, resources) for every operation in the
    pages' content streams and in the form XObjects they draw, each form
    read once, read with pypdf."""
    import logging
    from pypdf import PdfReader
    from pypdf.generic import ContentStream
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = PdfReader(str(path))
    seen = set()

    def walk(stream, resources, where):
        resources = resources.get_object() if resources is not None else {}
        xobjects = resources.get("/XObject")
        for operands, op in ContentStream(stream, reader).operations:
            yield where, operands, op, resources
            if op == b"Do" and xobjects is not None:
                ref = xobjects.get_object().raw_get(operands[0])
                form = ref.get_object()
                key = getattr(ref, "idnum", id(form))
                if form.get("/Subtype") == "/Form" and key not in seen:
                    seen.add(key)
                    yield from walk(form, form.get("/Resources") or resources,
                                    f"{where}, form {operands[0]}")

    for number, page in enumerate(reader.pages, 1):
        contents = page.get_contents()
        if contents is not None:
            yield from walk(contents, page.get("/Resources"), f"page {number}")


class TestHiddenText(unittest.TestCase):
    """Nothing in either document is hidden the ways a hidden-text
    detector looks for (arXiv 2605.28999), and no /ActualText makes a
    reader read other text than the glyphs show. A résumé found doing
    either is flagged as keyword stuffing or prompt injection. These
    guard them; the build does none of them."""

    def test_no_glyph_is_hidden(self):
        # The paper's rules: text under 4 pt, and text within 15 units
        # (Euclidean, 0-255 RGB) of its background. The background here
        # is the white page (styles/_print.scss): the templates draw no
        # fill behind text, only rules. And two of the tricks it names:
        # a render mode that does not fill the glyph (3 draws nothing,
        # 7 only clips), and text outside the visible page.
        for doc, _ in DOCS:
            with self.subTest(document=doc):
                found = _hidden_glyphs(_pdf(doc))
                reasons = Counter(why for _, _, why in found)
                self.assertEqual(found, [], f"pdfium ({_version('pdfium')}) reads {len(found)} "
                                 f"hidden glyphs, as {dict(reasons.most_common(4))}")

    def test_no_invisible_render_mode_is_set(self):
        # The same render modes, read from the content streams by pypdf:
        # a second reader, and one that sees a `Tr` with no glyph after it.
        for doc, _ in DOCS:
            with self.subTest(document=doc):
                found = [f"{where}: {operands[0]} Tr"
                         for where, operands, op, _ in _content_operations(_pdf(doc))
                         if op == b"Tr" and int(operands[0]) in (3, 7)]
                self.assertEqual(found, [])

    def test_no_actual_text(self):
        # Chromium writes none for these documents, so any /ActualText
        # is new: a reader takes its text in place of the glyphs', which
        # is text the page does not show unless the two match. Checked
        # in marked-content properties both inline and named in the
        # resources' /Properties.
        for doc, _ in DOCS:
            found = []
            for where, operands, op, resources in _content_operations(_pdf(doc)):
                if op != b"BDC" or len(operands) < 2:
                    continue
                props = operands[1]
                if not hasattr(props, "keys"):
                    named = resources.get("/Properties")
                    props = named.get_object().get(props) if named is not None else None
                props = props.get_object() if props is not None else {}
                if "/ActualText" in props:
                    found.append(f"{where}: {operands[0]} /ActualText {str(props['/ActualText'])!r}")
            with self.subTest(document=doc):
                self.assertEqual(found, [])


class TestDetailLabels(unittest.TestCase):
    """The labels of a details block read as whole words, and its
    values each on one line.

    Built in a copy of its own, from the template with the details
    block its comments give as an example, first in the sidebar.
    """

    BLOCK = {"id": "online", "type": "details", "heading": "Online", "rows": [
        {"label": "LinkedIn", "value": "linkedin.com/in/gcaesar",
         "href": "https://linkedin.com/in/gcaesar"},
        {"label": "Security Clearance", "value": "Secret"},
    ]}

    @classmethod
    def setUpClass(cls):
        import yaml
        sys.path.insert(0, str(ROOT / "build"))
        import _yaml_loader
        cls.project = Project("detail-label-test")
        data = cls.project.root / "data"
        doc = _yaml_loader.load((data / "resume_default.yml").read_text(encoding="utf-8"))
        doc["sidebar"]["blocks"].insert(0, cls.BLOCK)
        # resume.yml wins over the template in the copy's CLI build.
        (data / "resume.yml").write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                                         encoding="utf-8")
        problem = cls.project.build()
        if problem:
            cls.project.remove()
            raise build_error(problem)

    @classmethod
    def tearDownClass(cls):
        cls.project.remove()

    def test_the_labels_read_whole(self):
        found = list(self.project.dist.glob("*_Resume.pdf"))
        self.assertEqual(len(found), 1, found)
        for name, fn in EXTRACTORS.items():
            text = fn(found[0])
            if text is None:
                continue
            for row in self.BLOCK["rows"]:
                label = row["label"].upper()
                with self.subTest(extractor=name, label=label):
                    near = [" ".join(line.split()) for line in text.splitlines()
                            if label.split()[0][:3] in line.replace(" ", "")]
                    self.assertIsNotNone(
                        re.search(r"\s+".join(map(re.escape, label.split())), text),
                        f"{name} ({_version(name)}) read it as {near[:3]!r}")

    def test_the_values_read_on_one_line(self):
        # As TestParserConventions checks the header's: the LinkedIn
        # value is a URL, and a link.
        values = _contact_values((self.project.dist / "index.html").read_text(encoding="utf-8"))
        for row in self.BLOCK["rows"]:
            self.assertIn(_norm(row["value"]), values)
        found = list(self.project.dist.glob("*_Resume.pdf"))
        self.assertEqual(len(found), 1, found)
        for name, fn in EXTRACTORS.items():
            text = fn(found[0])
            if text is None:
                continue
            with self.subTest(extractor=name):
                self.assertEqual(_not_on_one_line(text, values), [],
                                 f"{name} ({_version(name)}) does not read these on one line")


class TestLongWords(unittest.TestCase):
    """A word wider than its column wraps; it is not cut off or drawn
    over the other column.

    Nothing broke a word before (styles/_base.scss, overflow-wrap). A
    160-character URL in a bullet lost everything past the page margin
    from the PDF, pixels and text layer alike, and a long word in a
    sidebar list ran over the divider into the summary, while the layout
    check reported clean. Built in a copy of its own, from the template
    with one long word in each column.
    """

    LONG = "https://example.com/" + "x" * 160
    SIDE = "Supercalifragilisticexpialidocious" + "Pneumonoultramicroscopic"

    @classmethod
    def setUpClass(cls):
        import yaml
        sys.path.insert(0, str(ROOT / "build"))
        import _yaml_loader
        cls.project = Project("long-word-test")
        data = cls.project.root / "data"
        doc = _yaml_loader.load((data / "resume_default.yml").read_text(encoding="utf-8"))
        jobs = next(s for s in doc["mainColumn"] if s["type"] == "experience")["jobs"]
        jobs[0]["bullets"][0] = f"Built {cls.LONG} pipeline"
        doc["sidebar"]["blocks"][0]["items"][0] = cls.SIDE
        # resume.yml wins over the template in the copy's CLI build.
        (data / "resume.yml").write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                                         encoding="utf-8")
        problem = cls.project.build()
        if problem:
            cls.project.remove()
            raise build_error(problem)

    @classmethod
    def tearDownClass(cls):
        cls.project.remove()

    def _boxes(self, word):
        """(left, right) of every glyph of `word` on page 1, in points."""
        import pypdfium2 as pdfium
        found = list(self.project.dist.glob("*_Resume.pdf"))
        doc = pdfium.PdfDocument(str(found[0]))
        try:
            tp = doc[0].get_textpage()
            text = tp.get_text_range()
            flat = [(i, ch) for i, ch in enumerate(text) if ch not in "\r\n"]
            joined = "".join(ch for _, ch in flat)
            start = joined.find(word)
            self.assertGreaterEqual(start, 0, f"{word[:24]}… is not in the text layer whole")
            return [tp.get_charbox(i)[::2] for i, _ in flat[start:start + len(word)]]
        finally:
            doc.close()

    def test_a_long_word_in_the_main_column_stays_on_the_page(self):
        right = 612 - 36          # US Letter less the 0.5in margin
        boxes = self._boxes(self.LONG)
        self.assertLessEqual(max(r for _, r in boxes), right + 0.5)

    def test_a_long_word_in_the_sidebar_stays_in_the_sidebar(self):
        right = 36 + 2.3 * 72     # margin + --sidebar-w
        boxes = self._boxes(self.SIDE)
        self.assertLessEqual(max(r for _, r in boxes), right + 0.5)


if __name__ == "__main__":
    unittest.main()
