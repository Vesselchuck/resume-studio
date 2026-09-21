#!/usr/bin/env python3
"""
build_letter.py — Generate dist/letter.html from
                        data/letter.yml (yours), falling back to
                        data/letter_default.yml.

Sibling of build/build.py for the cover letter. The cover letter is a
single-column flowing document, so this builder is much simpler than
the resume's: no measurement mode, no layout solver, no placement.
It reads the data, validates its shape, resolves the same header
fields the resume uses (name / role / contact / lang), and renders
templates/letter.j2 to dist/letter.html.

Shared logic is imported from build.py rather than duplicated:
markdown_filter (the `**bold**` → <strong> filter), read_accent,
resolve_lang, write_favicon, check_stylesheet_freshness,
check_no_module_collisions, SchemaError, and fail(). Importing build
is side-effect-free (it only defines functions/constants and inserts
build/ on sys.path).

Data-file precedence mirrors the resume's and reuses the SAME
RESUME_DATA_SOURCE env var (the project's single "mine vs default"
selector):
  • RESUME_DATA_SOURCE=default → require data/letter_default.yml
  • RESUME_DATA_SOURCE=mine    → require data/letter.yml
  • unset (default)            → yours if present, else the template

Run via `node letter.js`, which compiles the SCSS first; or
directly with `python3 build/build_letter.py` (run from project
root) once dist/styles.css exists.
"""

import datetime
import sys

# Suppress __pycache__/ next to source files. Set before any non-
# builtin import, matching build.py / crop_pdf.py.
sys.dont_write_bytecode = True

import json
import os
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

# Local sibling modules.
sys.path.insert(0, str(Path(__file__).parent))
import _console as c  # noqa: E402
import _yaml_loader  # noqa: E402  (libyaml + YAML 1.2 core typing)
import _output_name  # noqa: E402  (derives the PDF filename from your name)
import build  # noqa: E402  (shared helpers; import is side-effect-free)
from _env_contract import (  # noqa: E402
    ENV_RESUME_DATA_SOURCE,
    ENV_LETTER_DATA_FILE,
)

# Reuse the resume builder's paths so both documents share dist/,
# templates/, and the SCSS token file (for the accent color).
ROOT = build.ROOT
DATA_DIR = build.DATA_DIR
TEMPLATES_DIR = build.TEMPLATES_DIR
OUT_FILE = ROOT / "dist" / "letter.html"
FAVICON_FILE = ROOT / "dist" / "favicon.svg"
PDF_META_FILE = ROOT / "dist" / "letter_meta.json"

# Mirrors build.py: the unsuffixed name is your real letter and is
# gitignored; the *_default.yml placeholder is what the repo ships.
# Everything you type or run says "letter": letter.yml,
# letter_default.yml, letter.js, `node letter.js`. Only the OUTPUTS
# spell it out in full (<Your_Name>_Cover_Letter.pdf), because a
# recruiter reads them.
DATA_FILE_MINE = DATA_DIR / "letter.yml"
DATA_FILE_DEFAULT = DATA_DIR / "letter_default.yml"


def load_data():
    """Load letter data with the same precedence as build.load_data.

    Returns a (data, source) tuple where `source` is 'mine' or
    'default'. See module docstring for the RESUME_DATA_SOURCE rules.
    """
    # An explicit file wins over everything else — see build.load_data
    # for why this exists rather than a tool copying files around.
    explicit = os.environ.get(ENV_LETTER_DATA_FILE, '').strip()
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            build.fail(
                f"{ENV_LETTER_DATA_FILE} points at a file that does not "
                f"exist:\n  {path}"
            )
        try:
            shown = path.relative_to(ROOT)
        except ValueError:
            shown = path
        c.ok_pair("Loaded data", str(shown))
        with path.open(encoding="utf-8") as f:
            data = _yaml_loader.load(f)
        if not isinstance(data, dict):
            build.fail(
                f"{shown} is empty or not a YAML mapping at the top "
                f"level (parsed as {type(data).__name__})."
            )
        return build.apply_profile(data, path), 'mine'

    override = os.environ.get(ENV_RESUME_DATA_SOURCE, '').strip().lower()
    if override == 'mine':
        if not DATA_FILE_MINE.exists():
            build.fail(
                f"{ENV_RESUME_DATA_SOURCE}=mine but no letter data "
                f"file at {DATA_FILE_MINE.relative_to(ROOT)}"
            )
        path, source = DATA_FILE_MINE, 'mine'
    elif override == 'default':
        if not DATA_FILE_DEFAULT.exists():
            build.fail(
                f"{ENV_RESUME_DATA_SOURCE}=default but no letter template "
                f"at {DATA_FILE_DEFAULT.relative_to(ROOT)}"
            )
        path, source = DATA_FILE_DEFAULT, 'default'
    elif override:
        build.fail(
            f"invalid {ENV_RESUME_DATA_SOURCE}={override!r}; "
            f"expected 'default', 'mine', or unset"
        )
    elif DATA_FILE_MINE.exists():
        path, source = DATA_FILE_MINE, 'mine'
    elif DATA_FILE_DEFAULT.exists():
        path, source = DATA_FILE_DEFAULT, 'default'
    else:
        build.fail(
            f"no letter data file found. Expected one of:\n"
            f"  {DATA_FILE_MINE.relative_to(ROOT)}\n"
            f"  {DATA_FILE_DEFAULT.relative_to(ROOT)}"
        )
    c.ok_pair("Loaded data", str(path.relative_to(ROOT)))
    with path.open(encoding="utf-8") as f:
        data = _yaml_loader.load(f)
    if not isinstance(data, dict):
        build.fail(
            f"{path.relative_to(ROOT)} is empty or not a YAML mapping at the "
            f"top level (parsed as {type(data).__name__})."
        )
    return build.apply_profile(data, path), source


def _validate_contact(data):
    """Validate the optional `contact` block.

    Mirrors the contact rules in build.validate_data so the cover
    letter accepts exactly the same contact shape as the resume (the
    header template iterating `contact.rows` is identical). Kept as a
    local copy rather than importing build.validate_data, which
    validates the whole resume schema (sidebar, mainColumn, …) that a
    cover letter doesn't have. If the resume's contact rules change,
    update both sites.
    """
    if "contact" in data and data["contact"] is not None:
        contact = data["contact"]
        if not isinstance(contact, dict):
            raise build.SchemaError("'contact' must be a mapping")
        if "address" in contact and not isinstance(contact["address"], str):
            raise build.SchemaError("'contact.address' must be a string")
        rows = contact.get("rows")
        if not isinstance(rows, list):
            raise build.SchemaError(
                "'contact.rows' must be a list (use an empty list if you want "
                "only an address)"
            )
        for i, row in enumerate(rows):
            ctx = f"contact.rows[{i}]"
            if not isinstance(row, dict):
                raise build.SchemaError(
                    f"{ctx}: must be a mapping with 'value' (and optional 'href')"
                )
            if not isinstance(row.get("value"), str) or not row["value"]:
                raise build.SchemaError(f"{ctx}: 'value' must be a non-empty string")
            if "href" in row and not isinstance(row["href"], str):
                raise build.SchemaError(f"{ctx}: 'href' must be a string if provided")


def validate_data(data):
    """Validate the letter data shape up-front.

    Schema:
      • Top-level required keys: name, letter
      • Top-level optional keys: role (string), contact (mapping),
        meta (mapping)
      • name has 'first' and 'last' string fields
      • role, if provided, is a string
      • contact, if provided, follows the resume's contact shape
      • meta, if provided, is a mapping; meta.description and meta.lang
        are optional strings
      • letter is a mapping with a required non-empty 'body' list of
        non-empty strings; optional 'recipient' (a list of non-empty
        strings, or one block of text, one line per entry)
      • 'date', 'salutation', 'closing' and 'signature' are REJECTED.
        The date is stamped by the build, the greeting and sign-off are
        ordinary paragraphs of 'body', and the name under them comes
        from 'name'.

    Raises build.SchemaError on the first violation found.
    """
    for key in ("name", "letter"):
        if key not in data:
            raise build.SchemaError(f"missing top-level key: {key!r}")

    # Name.
    if not isinstance(data["name"], dict):
        raise build.SchemaError("'name' must be a mapping")
    for key in ("first", "last"):
        if not isinstance(data["name"].get(key), str):
            raise build.SchemaError(f"'name.{key}' must be a string")

    # Role (optional).
    if "role" in data and not isinstance(data["role"], str):
        raise build.SchemaError("'role' must be a string if provided")

    # Contact (optional) — same shape as the resume.
    _validate_contact(data)

    # Meta (optional). Only description + lang are consumed.
    if "meta" in data and data["meta"] is not None:
        meta = data["meta"]
        if not isinstance(meta, dict):
            raise build.SchemaError("'meta' must be a mapping if provided")
        if "description" in meta and meta["description"] is not None \
                and not isinstance(meta["description"], str):
            raise build.SchemaError("'meta.description' must be a string if provided")
        if "lang" in meta and not isinstance(meta["lang"], str):
            raise build.SchemaError("'meta.lang' must be a string if provided")

    # Letter.
    letter = data["letter"]
    if not isinstance(letter, dict):
        raise build.SchemaError("'letter' must be a mapping")
    body = letter.get("body")
    # body accepts EITHER a single block string (paragraphs separated by
    # blank lines — the easy paste-in form) OR a list of paragraph
    # strings (one entry per paragraph). resolve_letter() normalizes
    # both to a list before rendering.
    if isinstance(body, str):
        if not body.strip():
            raise build.SchemaError("'letter.body' string must not be blank")
    elif isinstance(body, list):
        if not body:
            raise build.SchemaError(
                "'letter.body' must be a non-empty list of paragraphs"
            )
        for i, para in enumerate(body):
            if not isinstance(para, str) or not para.strip():
                raise build.SchemaError(
                    f"letter.body[{i}] must be a non-empty string"
                )
    else:
        raise build.SchemaError(
            "'letter.body' must be a non-empty list of paragraphs, or a "
            "single block string with paragraphs separated by blank lines"
        )
    # `letter.date` used to be free text you typed. It is now stamped
    # by the build from the system clock, so the field is not optional
    # — it is gone. Rejecting it rather than ignoring it is deliberate:
    # a silently ignored date would leave someone editing a line that
    # has no effect, and the failure would only show up as a letter
    # dated something other than what the file says.
    if "date" in letter:
        raise build.SchemaError(
            "'letter.date' is no longer part of the data — the build "
            "stamps today's date when it runs. Delete the line."
        )
    if "recipient" in letter and letter["recipient"] is not None:
        recipient = letter["recipient"]
        if isinstance(recipient, str):
            if not recipient.strip():
                raise build.SchemaError(
                    "'letter.recipient' must not be empty if provided"
                )
        elif isinstance(recipient, list):
            for i, line in enumerate(recipient):
                if not isinstance(line, str) or not line.strip():
                    raise build.SchemaError(
                        f"letter.recipient[{i}] must be a non-empty string"
                    )
        else:
            raise build.SchemaError(
                "'letter.recipient' must be a list of lines, or a single "
                "block of text with one line per entry"
            )

    # The sign-off fields are gone. Each is rejected by name, with the
    # instruction that replaces it, because these are fields people
    # have in their files today — a bare "additional property" error
    # would tell them something is wrong without telling them what to
    # do, and silently ignoring the field would drop the greeting off
    # the letter without a word.
    for field, instead in (
        ("salutation", "write it as the first paragraph of 'letter.body'"),
        ("closing", "write it as the last paragraph of 'letter.body'"),
        ("signature", "it is taken from 'name' (which the profile supplies)"),
    ):
        if field in letter:
            raise build.SchemaError(
                f"'letter.{field}' is no longer part of the data — "
                f"{instead}. Delete the line."
            )


# Month names, written out rather than taken from strftime("%B").
# strftime consults LC_TIME, so on a machine with a non-English locale
# the same data file would build a letter with a month name in another
# language — a silent, environment-dependent difference in the one line
# of the document that is supposed to be a fact. These are fixed.
_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")

# English-speaking regions that write the day first. Not an exhaustive
# list of the world's date conventions — just the ones where writing
# "September 20, 2026" to a reader of English would look wrong.
_DAY_FIRST_REGIONS = frozenset({
    "GB", "IE", "AU", "NZ", "ZA", "IN", "PK", "BD", "LK",
    "KE", "NG", "GH", "TZ", "UG", "ZW", "MT", "SG", "MY", "HK",
})


def format_date(today, lang):
    """Render `today` the way a reader of `lang` expects to see it.

    Three cases, in order of how much this project can honestly claim
    to know:

      en-US and friends  →  September 20, 2026
      en-GB and friends  →  20 September 2026
      anything else      →  2026-09-20

    The ISO fallback is not laziness. Printing "September" to a reader
    of German or Japanese would be worse than printing a format that
    every locale reads correctly, and this project has no translation
    of the month names and no business inventing one. ISO 8601 is
    unambiguous everywhere — it is the one date format that cannot be
    misread as month-first or day-first.

    `today` is a datetime.date; `lang` is the BCP-47 tag already
    resolved by build.resolve_lang (so it is never None here).
    """
    subtags = [s for s in str(lang or "").split("-") if s]
    language = subtags[0].lower() if subtags else ""
    region = next((s.upper() for s in subtags[1:] if len(s) == 2), "")

    if language != "en":
        return today.isoformat()

    month = _MONTHS[today.month - 1]
    if region in _DAY_FIRST_REGIONS:
        return f"{today.day} {month} {today.year}"
    return f"{month} {today.day}, {today.year}"


def _split_paragraphs(body):
    """Normalize `letter.body` to a list of paragraph strings.

    Accepts the two authoring forms validate_data allows:
      • a single block string (e.g. YAML `body: |`) — split into
        paragraphs on blank lines, so you can paste prose and separate
        paragraphs with an empty line instead of writing `- >-` before
        each one. Soft line breaks WITHIN a paragraph are kept as-is;
        HTML collapses them to spaces when the <p> is rendered.
      • a list of paragraph strings — returned as-is.
    """
    if isinstance(body, str):
        parts = re.split(r"\n\s*\n", body.strip())
        return [p.strip() for p in parts if p.strip()]
    return list(body)


def _split_lines(recipient):
    """Normalize `letter.recipient` to a list of address lines.

    Accepts the same two authoring forms as `body`, for the same
    reason: you paste an address out of a job posting rather than
    retyping it as a YAML list.

    The split rule is NOT the same, though, and the difference is the
    point. `body` splits on BLANK lines, because a paragraph is a run
    of lines. An address is the opposite — every line is its own
    entry, and blank lines in a pasted address block are accidents of
    copying rather than structure. So this splits on every newline and
    drops the empties.
    """
    if isinstance(recipient, str):
        return [line.strip() for line in recipient.splitlines() if line.strip()]
    return list(recipient)


def resolve_letter(data, lang=None, today=None):
    """Return the letter dict with body normalized, the date stamped,
    and sign-off defaults filled in.

    `body` is normalized to a list of paragraphs (see _split_paragraphs)
    and `recipient` to a list of lines (see _split_lines). Optional
    parts that are absent stay absent so the template's `{% if %}`
    guards close the layout gap. The input dict is not mutated — a
    shallow copy is returned.

    THE GREETING AND SIGN-OFF ARE JUST PARAGRAPHS
    ─────────────────────────────────────────────
    "Dear Hiring Team," and "Sincerely," used to be their own fields.
    They are prose, they are always in the same two places, and having
    them as fields meant three places to edit a letter instead of one.
    They are now simply the first and last paragraphs of `body`, which
    is what you would type anyway if nothing told you otherwise.

    The name under the sign-off is NOT prose — it is your name, which
    the profile already knows — so it is not in the letter data at all.
    It is filled in here from `name`.

    THE DATE IS NOT IN THE DATA
    ───────────────────────────
    It is stamped here, from the system clock, every time the letter is
    built. A cover letter's date is the day you send it, which means a
    date in the YAML is wrong by default: it is right on the day you
    typed it and silently wrong every day after, and the one reader who
    notices is the one deciding whether to interview you.

    Two fields come out of it. `date` is what gets printed, formatted
    for `lang` (see format_date). `date_iso` is the same day in ISO
    8601, which the template puts in the <time datetime> attribute so
    the printed form stays human while the machine-readable one stays
    unambiguous.

    `lang` defaults to build.resolve_lang(data) and `today` to the
    local date; both are parameters so the tests can pin them.
    """
    letter = dict(data["letter"])
    letter["body"] = _split_paragraphs(letter.get("body"))
    if letter.get("recipient") is not None:
        letter["recipient"] = _split_lines(letter["recipient"])

    if today is None:
        today = datetime.date.today()
    if lang is None:
        lang = build.resolve_lang(data)
    letter["date"] = format_date(today, lang)
    letter["date_iso"] = today.isoformat()
    letter["signature"] = f"{data['name']['first']} {data['name']['last']}"
    return letter


def derive_pdf_metadata(data, lang, data_source):
    """Derive PDF metadata for crop_pdf.py to stamp.

    Title is "{name} — Cover Letter"; author is the name; subject is
    meta.description (stripped); `output_stem` is the name folded into
    a filename. Cover letters have no skills block, so keywords are
    empty. `lang` and `data_source` are passed in by the
    caller, matching build.derive_pdf_metadata's contract.
    """
    name = f"{data['name']['first']} {data['name']['last']}"
    description = ((data.get('meta') or {}).get('description') or '').strip()
    return {
        'title':       f"{name} — Cover Letter",
        'author':      name,
        'subject':     description,
        'keywords':    '',
        'lang':        lang,
        'data_source': data_source,
        # Shared filename stem for both PDF variants — see
        # _output_name.py. Derived the same way as the resume's, from
        # the same profile, so the two documents always agree.
        'output_stem': _output_name.stem(data['name'], 'letter'),
    }


def build_letter():
    """Build dist/letter.html from the letter data."""
    build.check_no_module_collisions()
    build.check_stylesheet_freshness()
    data, data_source = load_data()
    try:
        validate_data(data)
    except build.SchemaError as e:
        build.fail(f"invalid letter data — {e}")

    accent = build.read_accent()
    lang = build.resolve_lang(data)  # reads meta.lang, defaults en-US
    letter = resolve_letter(data, lang=lang)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.filters["md"] = build.markdown_filter

    template = env.get_template("letter.j2")
    rendered = template.render(
        name=data["name"],
        role=data.get("role"),
        contact=data.get("contact"),
        meta=data.get("meta") or {},
        letter=letter,
        accent=accent,
        lang=lang,
    )

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        OUT_FILE.write_text(rendered, encoding="utf-8")
    except PermissionError:
        build.fail(
            f"Permission denied writing {OUT_FILE}.\n"
            f"Another program is holding the file open (most likely a browser "
            f"tab previewing the rendered HTML). Close it and re-run."
        )

    pdf_meta = derive_pdf_metadata(data, lang, data_source)
    try:
        PDF_META_FILE.write_text(
            json.dumps(pdf_meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except PermissionError:
        build.fail(
            f"Permission denied writing {PDF_META_FILE}.\n"
            f"Close any program holding the file open and re-run."
        )

    kb = len(rendered.encode('utf-8')) / 1024
    c.ok_pair("Wrote HTML", f"{OUT_FILE.relative_to(ROOT)} ({kb:.1f} KB)")
    # Favicon — shared with the resume (same initials + accent).
    build.write_favicon(data, FAVICON_FILE, accent)
    c.ok_pair("Wrote metadata", str(PDF_META_FILE.relative_to(ROOT)))


def main():
    build_letter()


if __name__ == "__main__":
    main()
