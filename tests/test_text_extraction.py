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

WHAT THIS CHECKS
────────────────
Both documents are built from the templates in a throwaway copy of the
project (tests/_project.py), then read with pypdf and pdfium — the two
extractors in requirements.txt. For each:

  • every word the page shows is in the text, as often as it is shown;
  • no ligature code points (U+FB00–FB06) appear;
  • the name and the role read as whole words.

pdfplumber and pdftotext, also checked when the fix was made, are not
project dependencies; they are used here when installed and skipped
otherwise.
"""

import html
import re
import shutil
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _project import Project, real_dist_fingerprint  # noqa: E402

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
        raise unittest.SkipTest(f"could not build a copy of the project to test against ({problem})")


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
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(path))
    try:
        out = []
        for i in range(len(doc)):
            tp = doc[i].get_textpage()
            out.append(tp.get_text_range())
            tp.close()
        return "\n".join(out)
    finally:
        doc.close()


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
    return subprocess.run([exe, "-layout", str(path), "-"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def _version(name):
    """The extractor's version, for a failure message."""
    try:
        if name == "pdftotext":
            r = subprocess.run([shutil.which("pdftotext"), "-v"], capture_output=True, text=True)
            return (r.stdout + r.stderr).strip().splitlines()[0]
        from importlib.metadata import version
        return version({"pdfium": "pypdfium2"}.get(name, name))
    except Exception:  # noqa: BLE001 — only ever decoration on a failure
        return "(version unknown)"


EXTRACTORS = {"pypdf": _pypdf, "pdfium": _pdfium,
              "pdfplumber": _pdfplumber, "pdftotext": _pdftotext}


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
            raise unittest.SkipTest(f"could not build the copy ({problem})")

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
