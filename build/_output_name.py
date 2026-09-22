"""
_output_name.py — what the finished PDFs are called.

Outputs used to be named after the document: dist/resume-color.pdf,
dist/letter-grayscale.pdf. That is a fine name for a build artifact and
a bad name for the file you attach to an application, where the
recipient sees the filename before they see the document. So the name
is now yours:

    dist/Gaius_Caesar_Resume.pdf
    dist/Gaius_Caesar_Resume_Grayscale.pdf
    dist/Gaius_Caesar_Cover_Letter.pdf
    dist/Gaius_Caesar_Cover_Letter_Grayscale.pdf

The color variant takes the bare stem because it is the one you send;
the grayscale variant is suffixed. Both come from `name.first` and
`name.last`, which since the profile split live in data/_profile.yml
for every document at once — so the two documents always agree.

WHY THE DERIVATION LIVES HERE AND ONLY HERE
───────────────────────────────────────────
Node needs these paths too: the pipeline writes the PDFs, resume.js
prunes the variants it did not build, and the Studio tray offers them
for opening. It does NOT recompute them. The name depends on the YAML,
and Node has no YAML parser in this project — that is precisely why
studio_server's document detection is a column-anchored regex rather
than a parse.

So the flow is one-way: build.py derives the stem, writes it into
dist/pdf_meta.json as `output_stem`, and every Node consumer reads it
back through _output_name.js. There is no second implementation to
drift out of step, and the fallback when the metadata is missing is the
bare DOC_SUFFIX ('Resume.pdf') rather than a guess at your name.

THE FOLDING RULES, AND WHY EACH ONE IS THERE
────────────────────────────────────────────
A filename crosses more boundaries than a document does: an email
attachment, an HR portal's upload form, an ATS that may still be
parsing bytes as Latin-1, a recruiter's Windows Downloads folder. So
the stem is reduced to ASCII letters, digits and underscores:

  • Accents are folded, not stripped — José → Jose, not Jos. Losing a
    letter changes the name; folding it keeps it legible.
  • Letters with no decomposition (ø, ß, æ, ł, þ …) are transliterated
    explicitly, because NFKD does nothing for them and they would
    otherwise vanish. ß → ss, æ → ae.
  • Apostrophes are DELETED rather than replaced: O'Brien → OBrien,
    not O_Brien.
  • Everything else outside [A-Za-z0-9] collapses to a single
    underscore, which covers spaces, hyphens, periods and the
    characters Windows forbids outright (\\ / : * ? " < > |).
  • A name part that would LOSE LETTERS to folding — a purely CJK name,
    or a mixed-script one like "Zoë 山田" or "Petrov-Смирнов", where the
    ASCII pass would keep "Zoe" or "Petrov" and silently drop the rest —
    falls back to the raw characters with only what the filesystem
    forbids removed. Modern filesystems take them; producing
    "Resume.pdf" for such a person, or a file named after half of
    their name, would not be acceptable. The part is kept whole, not
    half folded, so the name reads the way it was written.
  • Failing even that, the stem is the document suffix alone. The build
    always has somewhere to write.

Consumed by:
  • build/build.py               — writes output_stem into pdf_meta.json
  • build/build_letter.py  — same, into letter_meta.json
  • build/snapshot_pdf.py        — locates the PDFs it must compare
  • build/_output_name.js        — the Node mirror (reads, never derives)
"""

import json
import re
import unicodedata
from pathlib import Path

_CONSTANTS = json.loads(
    (Path(__file__).parent / "_constants.json").read_text(encoding="utf-8")
)["output_name"]

DOC_SUFFIX = _CONSTANTS["DOC_SUFFIX"]
SEPARATOR = _CONSTANTS["SEPARATOR"]
GRAYSCALE_SUFFIX = _CONSTANTS["GRAYSCALE_SUFFIX"]
MAX_PART = _CONSTANTS["MAX_PART"]
LEGACY = _CONSTANTS["LEGACY"]

# Letters NFKD will not decompose, because they are atomic code points
# rather than a base plus a combining mark. Without this table they
# would be dropped entirely by the "keep ASCII only" pass.
_TRANSLITERATE = {
    "ß": "ss", "ẞ": "Ss",
    "æ": "ae", "Æ": "Ae",
    "œ": "oe", "Œ": "Oe",
    "ø": "o",  "Ø": "O",
    "đ": "d",  "Đ": "D",
    "ð": "d",  "Ð": "D",
    "þ": "th", "Þ": "Th",
    "ł": "l",  "Ł": "L",
    "ı": "i",  "İ": "I",
    "ŋ": "n",  "Ŋ": "N",
    "ħ": "h",  "Ħ": "H",
    "ĸ": "k",
    "ŉ": "n",
}

# Deleted outright rather than collapsed to a separator, so that
# O'Brien becomes OBrien and not O_Brien.
_DROPPED = re.compile(r"['‘’ʼ´`“”\"]")

# Characters Windows refuses in a filename, plus control codes. Used
# only on the non-ASCII fallback path, where the goal is a usable name
# rather than an ASCII one.
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def fold_to_ascii(text: str) -> str:
    """Reduce `text` to unaccented ASCII, keeping every letter legible.

    Transliterates the atomic letters NFKD cannot decompose, then
    decomposes the rest and drops the combining marks. Characters with
    no ASCII reading at all (CJK, for instance) survive this step and
    are removed by the caller's character-class filter — see the
    module docstring for what happens to a name made only of those.
    """
    text = "".join(_TRANSLITERATE.get(ch, ch) for ch in text)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _loses_letters(folded: str) -> bool:
    """True if `folded` still holds letters the ASCII filter would drop.

    fold_to_ascii leaves behind exactly the letters it has no ASCII
    reading for (CJK, Cyrillic, Greek, Arabic, …); anything else
    non-ASCII at that point is punctuation or a symbol, which the
    convention is happy to turn into a separator.
    """
    return any(ord(ch) > 127 and unicodedata.category(ch).startswith("L")
               for ch in folded)


def slug_part(raw) -> str:
    """Turn one name part into a filename-safe fragment.

    Returns '' for anything that is not a non-empty string, so a
    missing `name.last` simply drops out of the stem instead of
    producing a stray separator.
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""

    folded = fold_to_ascii(_DROPPED.sub("", raw))
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", folded).strip("_")

    if not cleaned or _loses_letters(folded):
        # The ASCII filter would drop letters — all of them, or the
        # non-Latin half of a mixed-script name. Keep the name rather
        # than the convention: strip what the filesystem forbids,
        # collapse whitespace, and let the characters through.
        cleaned = _FORBIDDEN.sub("", _DROPPED.sub("", raw))
        cleaned = re.sub(r"\s+", SEPARATOR, cleaned).strip("_. ")

    # Trailing separators can reappear after the cap lands mid-run.
    return cleaned[:MAX_PART].strip("_")


def stem(name, variant: str) -> str:
    """The shared filename stem for one document's PDFs.

    `name` is the data file's `name` mapping ({first, last}); `variant`
    is the pipeline's 'resume' or 'letter'. Both variants of a document
    share this stem — only the grayscale one is suffixed.
    """
    if variant not in DOC_SUFFIX:
        raise ValueError(
            f"unknown variant {variant!r}; expected one of {sorted(DOC_SUFFIX)}"
        )

    suffix = DOC_SUFFIX[variant]
    if not isinstance(name, dict):
        return suffix

    parts = [p for p in (slug_part(name.get("first")),
                         slug_part(name.get("last"))) if p]
    if not parts:
        return suffix
    return SEPARATOR.join(parts + [suffix])


def color_pdf(dist, output_stem: str) -> Path:
    """Path of the color PDF — the bare stem."""
    return Path(dist) / f"{output_stem}.pdf"


def grayscale_pdf(dist, output_stem: str) -> Path:
    """Path of the black-and-white PDF — the stem plus GRAYSCALE_SUFFIX."""
    return Path(dist) / f"{output_stem}{GRAYSCALE_SUFFIX}.pdf"


def stem_from_meta(meta_file, variant: str) -> str:
    """Read `output_stem` back out of a build's metadata JSON.

    A missing or unreadable file yields the bare document suffix. That
    is the bootstrap case — nothing has been built yet — and in it the
    PDFs do not exist under any name, so the fallback only ever names
    a file that is correctly reported as absent.
    """
    try:
        meta = json.loads(Path(meta_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DOC_SUFFIX[variant]

    value = meta.get("output_stem")
    return value if isinstance(value, str) and value else DOC_SUFFIX[variant]


def legacy_pdfs(dist, variant: str):
    """The pre-rename filenames for `variant`, as paths under `dist`."""
    return [Path(dist) / name for name in LEGACY[variant]]
