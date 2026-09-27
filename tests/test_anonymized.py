"""
Nothing that goes to the repository may carry you.

The project holds two kinds of file. Yours — data/resume.yml,
data/letter.yml, data/_profile.yml, the *.mine.pdf fixtures, dist/ —
are gitignored and never leave this machine. Everything else is meant
to be committed: code, templates, schemas, docs, the shipped *_default
templates and the snapshot fixtures rendered from them.

The failure this test exists for is quiet. Nobody commits their phone
number on purpose; it arrives as an "example" in a schema description,
a realistic value in a test, a sample path in the README, or — the
serious one — a committed fixture rendered from a template that
borrowed a field from your real profile. Each of those looks harmless
in the diff that introduces it.

So the test works from your real files outward. On a machine that has
them, it reads the values that identify you — your name, your contact
details, where you worked and studied — and fails if any of them
appears in a file that git would commit, including the text of the
committed PDFs. In a clean checkout there are no real files to read,
and it skips: there is nothing of yours to leak.

It deliberately does NOT look for generic words that also appear in
your data ("Summary", "Hiring Team", "Sincerely"): only values that
point at a person, and only ones the shipped templates do not also use.

Images are read too, because docs/screenshots/ is the likeliest place
a picture of your real résumé would land. Their embedded metadata is
always searched. What they show is read with Tesseract OCR when it is
installed; without it that one test skips and says so. It is not a
Python package and not in requirements.txt, because only a machine
with your real files ever runs this.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import _yaml_loader  # noqa: E402

DATA = ROOT / "data"

# The shipped templates. Every other data file, at any depth under
# data/ (resume.yml, letter.yml, _profile.yml, resume.yml.old, old
# versions kept in data/legacy/ …), is yours and a source of what
# must not leak.
TEMPLATE_FILES = ("_profile_default.yml", "resume_default.yml", "letter_default.yml")
DATA_SUFFIXES = (".yml", ".yaml", ".yml.old", ".yml.bak")


def _real_files():
    return sorted(
        p for p in DATA.rglob("*")
        if p.is_file()
        and p.name.endswith(DATA_SUFFIXES)
        and not (p.parent == DATA and p.name in TEMPLATE_FILES))

# Mirrors .gitignore: the places that are private by design. A file
# under one of these is allowed to contain you; nothing else is.
PRIVATE_DIRS = {"dist", "node_modules", "Claude outputs", "__pycache__",
                ".git", "target", "gen"}
PRIVATE_PATTERNS = (
    re.compile(r"^data/(?!(resume_default|letter_default|_profile_default)\.yml$)"),
    re.compile(r"^tests/fixtures/.*\.(mine|local)\.pdf$"),
    re.compile(r"^tests/fixtures/diff_.*\.png$"),
    re.compile(r"(^|/)[^/]*\.bak$"),
    re.compile(r"(^|/)[^/]*\.log$"),
    re.compile(r"^\.vscode/(?!settings\.json$)"),
)
# Files allowed to name you. LICENSE states who holds the copyright;
# that is the one place a real name belongs, and it is there on purpose.
ALLOWED = {"LICENSE"}

# Content that cannot be searched as text.
BINARY = (".png", ".ico", ".woff2", ".woff", ".ttf", ".jpg", ".jpeg", ".gif")
# The part of BINARY that can show text: searched by the image tests.
# Icons are left out; at icon size there is nothing legible to read.
IMAGES = (".png", ".jpg", ".jpeg", ".gif")

# Where the Windows installer (UB Mannheim's build, the one tessdoc
# points to) puts Tesseract. It does not add itself to PATH by default.
WINDOWS_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")

# Images are enlarged before OCR. docs/screenshots/documents*.png shows
# whole pages at about 85 dpi, where 9pt type is too small for Tesseract:
# on a render of a real résumé at that size it missed 3 of the 25 details
# printed on the page, and at 2× it found all 25.
OCR_UPSCALE = 2


def _load(path):
    try:
        return _yaml_loader.load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _identifying_values(doc):
    """The values in one real file that point at a person."""
    out = set()

    def add(value):
        if isinstance(value, str):
            value = value.strip()
            if value:
                out.add(value)

    name = doc.get("name") or {}
    if isinstance(name, dict):
        first, last = name.get("first"), name.get("last")
        add(first)
        add(last)
        if isinstance(first, str) and isinstance(last, str):
            add(f"{first} {last}")

    def add_place(value):
        # The whole value, and the city on its own: "Springfield, IL
        # 62701" in a schema example gives away "Springfield, Illinois"
        # just as well, and would not match the full string.
        add(value)
        if isinstance(value, str) and "," in value:
            add(value.split(",", 1)[0])

    contact = doc.get("contact") or {}
    if isinstance(contact, dict):
        add_place(contact.get("address"))
        for row in contact.get("rows") or []:
            if isinstance(row, dict):
                add(row.get("value"))
                href = row.get("href")
                if isinstance(href, str):
                    # mailto:/tel: carry the same value in another form.
                    add(re.sub(r"^(mailto|tel):", "", href))

    for section in doc.get("mainColumn") or []:
        if not isinstance(section, dict):
            continue
        for job in section.get("jobs") or []:
            if isinstance(job, dict):
                add_place(job.get("location"))
                title = job.get("title")
                if isinstance(title, str) and "," in title:
                    add(title.split(",", 1)[1])       # the employer
        for item in section.get("items") or []:
            if isinstance(item, dict):
                add(item.get("institution"))

    # Phone numbers and emails wherever they occur, in any field.
    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            for m in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", node):
                add(m)
            for m in re.findall(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", node):
                if not re.fullmatch(r"\(?0{3}\)?[\s.-]?0{3}[\s.-]?0{4}", m):
                    add(m)
    walk(doc)
    return out


def _needles():
    real = _real_files()
    if not real:
        return set()
    template_text = "\n".join(
        (DATA / f).read_text(encoding="utf-8").lower()
        for f in TEMPLATE_FILES if (DATA / f).exists())
    needles = set()
    for path in real:
        for value in _identifying_values(_load(path)):
            # Too short to mean anything, or shared with the templates.
            if len(value) < 4 or value.lower() in template_text:
                continue
            needles.add(value)
    return needles


def _git_committable():
    """The files git itself would commit, or None outside a repository.

    Asking git is exact: it applies .gitignore, nested ignore files and
    the global excludes, so a gitignored build log that mentions you is
    not reported as a leak, and a file that IS tracked is always seen.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in out.decode("utf-8").split("\0") if p]


def _all_committable():
    listed = _git_committable()
    if listed is not None:
        for rel in listed:
            path = ROOT / rel
            if path.is_file():
                yield rel, path
        return
    # Not a git checkout yet: approximate .gitignore by hand.
    # os.walk with pruning, not rglob: node_modules alone is thousands
    # of files that would be listed only to be thrown away.
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in PRIVATE_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(ROOT).as_posix()
            if any(p.search(rel) for p in PRIVATE_PATTERNS):
                continue
            yield rel, path


def _committable_files():
    """Committable files that can be searched as text."""
    for rel, path in _all_committable():
        if path.suffix.lower() not in BINARY:
            yield rel, path


def _committable_images():
    for rel, path in _all_committable():
        if path.suffix.lower() in IMAGES and rel not in ALLOWED:
            yield rel, path


def _matches(text, needles):
    """The needles that occur in `text`, ignoring case.

    PDF text extraction drops the space between name parts
    ("GaiusCaesar"), and OCR drops or adds spaces around letter-spaced
    type, so each needle is also compared with all whitespace removed.
    """
    low, squashed = text.lower(), re.sub(r"\s+", "", text.lower())
    return sorted(n for n in needles
                  if n.lower() in low or re.sub(r"\s+", "", n.lower()) in squashed)


def _image_metadata(path):
    """The text an image carries besides its pixels (PNG text chunks,
    EXIF, comments), which a screenshot tool can fill with a window
    title, a file path or a user name."""
    from PIL import Image
    with Image.open(path) as im:
        parts = [str(v) for v in im.info.values() if isinstance(v, (str, bytes))]
        parts += [str(v) for v in getattr(im, "text", {}).values()]
        parts += [str(v) for v in im.getexif().values()]
    return "\n".join(p.decode("utf-8", "replace") if isinstance(p, bytes) else p
                     for p in parts)


def _find_tesseract():
    found = shutil.which("tesseract")
    if found:
        return found
    if os.name == "nt" and WINDOWS_TESSERACT.is_file():
        return str(WINDOWS_TESSERACT)
    return None


def _ocr(tesseract, path, workdir):
    """What Tesseract reads in one image, enlarged by OCR_UPSCALE.

    Tesseract is told to use one thread. It runs one process per image
    instead, and its own threading made it slower, not faster: 15.4 s
    for one screenshot against 2.6 s single-threaded, same text (Linux,
    2 cores).
    """
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im = im.resize((im.width * OCR_UPSCALE, im.height * OCR_UPSCALE),
                       Image.LANCZOS)
        scaled = Path(workdir) / f"{path.stem}.{os.getpid()}.{id(im)}.png"
        im.save(scaled, compress_level=1)
    r = subprocess.run([tesseract, str(scaled), "-"], capture_output=True,
                       env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    if r.returncode != 0:
        raise RuntimeError(f"tesseract failed on {path.name} (exit {r.returncode}): "
                           + r.stderr.decode("utf-8", "replace").strip()[-300:])
    return r.stdout.decode("utf-8", "replace")


def _text_of(path):
    if path.suffix.lower() == ".pdf":
        try:
            import pypdf
        except ImportError:
            return None
        reader = pypdf.PdfReader(str(path))
        parts = [page.extract_text() or "" for page in reader.pages]
        parts += [str(v) for v in (reader.metadata or {}).values()]
        return "\n".join(parts)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


class TestNothingCommittableCarriesYou(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.needles = _needles()

    def test_no_committable_file_contains_your_details(self):
        if not self.needles:
            self.skipTest("no real data files in data/ — nothing of yours to leak")

        found = []
        for rel, path in _committable_files():
            if rel in ALLOWED:
                continue
            text = _text_of(path)
            if text is None:
                continue
            found += [f"{rel}: {n!r}" for n in _matches(text, self.needles)]

        self.assertEqual(
            found, [],
            "Files that would be committed contain details from your real "
            "data files. Replace them with placeholders:\n  "
            + "\n  ".join(sorted(found)))

    def test_no_committable_image_carries_your_details_in_metadata(self):
        if not self.needles:
            self.skipTest("no real data files in data/ — nothing of yours to leak")
        found = []
        for rel, path in _committable_images():
            found += [f"{rel}: {n!r}"
                      for n in _matches(_image_metadata(path), self.needles)]
        self.assertEqual(
            found, [],
            "Images that would be committed carry details from your real "
            "data files in their metadata. Strip it or retake them:\n  "
            + "\n  ".join(sorted(found)))

    def test_no_committable_image_shows_your_details(self):
        """Screenshots show no details from your real data files (OCR)."""
        if not self.needles:
            self.skipTest("no real data files in data/ — nothing of yours to leak")
        tesseract = _find_tesseract()
        if not tesseract:
            self.skipTest("Tesseract OCR not found on PATH or in its default "
                          "Windows folder — screenshots are not read")
        images = list(_committable_images())

        with tempfile.TemporaryDirectory() as work:
            # Guard against passing by reading nothing: a broken install
            # or a missing language file returns no text, and every image
            # would come back clean.
            from PIL import Image, ImageDraw, ImageFont
            canary = Path(work) / "canary.png"
            im = Image.new("RGB", (900, 120), "white")
            ImageDraw.Draw(im).text((20, 30), "Gaius Julius Caesar",
                                    fill="black", font=ImageFont.load_default(48))
            im.save(canary)
            self.assertIn("gaius julius caesar", _ocr(tesseract, canary, work).lower(),
                          "Tesseract ran but could not read a line of plain text")

            with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
                texts = list(pool.map(lambda item: _ocr(tesseract, item[1], work),
                                      images))

        found = []
        for (rel, _), text in zip(images, texts):
            found += [f"{rel}: {n!r}" for n in _matches(text, self.needles)]
        self.assertEqual(
            found, [],
            "Images that would be committed show details from your real "
            "data files. Retake them from the shipped template data:\n  "
            + "\n  ".join(sorted(found)))

    def test_the_needles_are_real(self):
        # Guard against this passing by searching for nothing: on a
        # machine with a real profile, at least the name must be in play.
        if not (DATA / "_profile.yml").exists():
            self.skipTest("no data/_profile.yml")
        profile = _load(DATA / "_profile.yml")
        name = profile.get("name") or {}
        last = name.get("last") if isinstance(name, dict) else None
        if not isinstance(last, str) or len(last) < 4:
            self.skipTest("profile has no usable surname")
        self.assertIn(last, self.needles)


if __name__ == "__main__":
    unittest.main()
