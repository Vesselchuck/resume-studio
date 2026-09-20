#!/usr/bin/env python3
"""
build.py — Generate dist/index.html from data/resume.yml.
           (data/resume_default.yml is the shipped placeholder.)

Reads the resume data, validates its shape, computes derived fields
(page-2 jobs, continuation job, PDF metadata), and renders
templates/resume.j2 to dist/index.html.

Two modes:
  • --mode=final (default) — produces the paginated resume from a
    placement read from dist/placement.json. The placement is
    written by resume.js after the layout solver runs.
  • --mode=measurement — produces a single-page flowing layout
    that the layout solver in resume.js measures to decide
    page placement.

Custom Jinja filter:
  • md   — converts `**bold**` to <strong>bold</strong>. Used inside
           bullet text. Only spans on a single line are matched.

HTML escaping for special characters (& < >) is done via Jinja's
built-in `e` (escape) filter at the template call sites.

Run via `npm run resume`, which calls this first; or directly with
`python3 build/build.py` (run from project root).
"""

import sys

# Suppress writing of __pycache__/ next to source files. Set this
# before any other (non-builtin) import so child imports also
# inherit the flag. Equivalent to running with `python -B` but
# enforces the no-cache rule even for direct invocations like
# `python build/build.py`.
sys.dont_write_bytecode = True

import os
import re
import json
import argparse
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

# Local sibling modules. Inserted at import time so the same `c.ok()`
# / `c.err()` API and shared constants are available everywhere.
sys.path.insert(0, str(Path(__file__).parent))
import _console as c  # noqa: E402
import _yaml_loader  # noqa: E402  (libyaml + YAML 1.2 core typing)
import _output_name  # noqa: E402  (derives the PDF filename from your name)
from _env_contract import ENV_RESUME_DATA_SOURCE, ENV_RESUME_DATA_FILE  # noqa: E402


ROOT = Path(__file__).parent.parent      # this script lives in build/
DATA_DIR = ROOT / "data"
TEMPLATES_DIR = ROOT / "templates"
OUT_FILE = ROOT / "dist" / "index.html"
FAVICON_FILE = ROOT / "dist" / "favicon.svg"
TOKENS_FILE = ROOT / "styles" / "_tokens.scss"

# Data-file precedence: your resume wins over the shipped template.
#
# data/resume.yml is yours and is gitignored. data/resume_default.yml
# is the Caesar placeholder the repo ships so the project builds for
# someone who has just cloned it.
#
# The unsuffixed name belongs to the real document on purpose: the
# file you edit every week should have the obvious name, and the one
# you touch once should carry the qualifier. .gitignore is written as
# an allowlist to match — everything in data/ is private except the
# three *_default.yml templates — so a new file you drop in there is
# private by default rather than exposed until someone remembers to
# add a line for it.
DATA_FILE_MINE = DATA_DIR / "resume.yml"
DATA_FILE_DEFAULT = DATA_DIR / "resume_default.yml"
PDF_META_FILE = ROOT / "dist" / "pdf_meta.json"


def fail(msg: str) -> None:
    """
    Emit a colored error headline (and any subsequent newline-
    separated detail lines) via _console, then exit non-zero.

    Replaces the old `sys.exit('ERROR: ...')` pattern. The message
    string can be multi-line; the first line becomes the headline,
    further lines become indented detail.
    """
    lines = msg.splitlines()
    if lines:
        c.err(lines[0])
        for line in lines[1:]:
            c.detail(line)
    sys.exit(1)


def markdown_filter(text):
    """
    Minimal markdown for resume bullets: `**bold**` → `<strong>bold</strong>`.

    Anything else is left as-is. HTML metacharacters in the input are NOT
    auto-escaped here — that's the template's job (Jinja's `e` filter runs
    before `md` in the chain `bullet | e | md | safe`). The contract:
      • Input is plain text plus `**bold**` runs.
      • Output is HTML-safe to inject inside a <li> via `| safe`.
      • Any literal '<' or '>' in YAML source will pass through to the
        HTML and be interpreted as markup. Don't put HTML in YAML.

    Bold spans cannot cross newlines, cannot be empty (`****`), and the
    pattern is non-greedy so `**a** **b**` produces two distinct spans.

    The negative lookarounds `(?<!\\*)` and `(?!\\*)` reject delimiters
    that are immediately adjacent to a third asterisk. This stops
    `***triple***` from bleeding to `*<strong>triple</strong>*` — three
    asterisks are ambiguous (no convention for what they mean here) and
    the safe thing is to leave the literal alone rather than emit
    half-rendered output.
    """
    if text is None:
        return ""
    s = str(text)
    # Non-greedy match; require at least one non-asterisk character inside;
    # reject runs of 3+ asterisks via lookarounds.
    return re.sub(r"(?<!\*)\*\*([^*\n]+?)\*\*(?!\*)", r"<strong>\1</strong>", s)


# ─── The shared profile ──────────────────────────────────────────

#: A file of values common to every document, merged underneath each
#: one. Named with a leading underscore so it sorts to the top of
#: data/ and so the Studio app has a clean rule for keeping it out of
#: the document picker: it is not a document.
PROFILE_NAME = "_profile.yml"

#: The profile the shipped templates merge instead. See profile_for.
DEFAULT_PROFILE_NAME = "_profile_default.yml"


def profile_for(doc_path):
    """
    The profile a document merges: the one beside it, from its own world.

    There are two worlds in data/, and they must not mix:

      yours      resume.yml, letter.yml      → _profile.yml
      template   resume_default.yml,         → _profile_default.yml
                 letter_default.yml

    A template document merges the template profile, never yours. That
    matters more than it looks, because the template is what the
    COMMITTED snapshot fixtures are rendered from
    (`snapshot_pdf.py --update-all` builds it and writes
    tests/fixtures/expected_resume-*.pdf, which git tracks). If the
    template merged your real profile, any key it left out — delete its
    `contact` block and let the profile "fill it in" — would put your
    real phone number into a file that goes to the repository. Keeping
    the worlds apart makes that impossible by construction rather than
    by the template happening to be complete.

    A document is a template when its file name ends in `_default`
    before the extension, which is the naming the project already uses
    for everything it ships.
    """
    doc = Path(doc_path)
    name = DEFAULT_PROFILE_NAME if doc.stem.endswith("_default") else PROFILE_NAME
    return doc.parent / name


def deep_merge(base, override):
    """
    Merge `override` onto `base`, recursing into nested mappings.

    Mappings merge key by key; everything else — scalars and lists —
    is replaced wholesale by `override`.

    Lists replace rather than concatenate, and that is the important
    decision here. `contact.rows` is the case that settles it: if a
    document listed one row and the profile listed three, a
    concatenating merge would produce four rows in an order nobody
    chose, and "override one contact row" would be impossible to
    express. Replacing means a document that mentions `rows` at all
    owns the whole list, which is easy to predict and easy to undo.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def apply_profile(data, doc_path):
    """
    Merge the shared profile sitting beside `doc_path` under `data`.

    Which profile — yours or the template's — is decided by
    profile_for, and the template never sees yours.

    Returns the merged mapping. The document always wins: the profile
    supplies a key only where the document has not.

    WHY THE PROFILE LIVES NEXT TO THE DOCUMENT
    ──────────────────────────────────────────
    The path is derived from the document's own directory rather than
    from a module-level constant pointing at data/. That keeps the
    rule simple to state — a profile applies to the files beside it —
    and it means a document loaded from somewhere else entirely via
    RESUME_DATA_FILE picks up the profile next to *it*, or none at
    all, rather than silently inheriting this project's.

    WHY THIS IS SAFE TO ADD TO AN EXISTING SETUP
    ────────────────────────────────────────────
    No profile file, no change. A profile that duplicates what the
    documents already say, no change either — the document's own
    values win. So the file can appear first and the duplicated blocks
    can be deleted from each document later, one at a time, with every
    intermediate state producing the same PDF.

    The merge is announced in the build log. That is the whole reason
    it is allowed to be implicit: a value can reach the PDF from a
    file the document never mentions, so the build has to say so out
    loud, every time, rather than leaving you to wonder where a phone
    number came from.
    """
    profile_path = profile_for(doc_path)
    if not profile_path.exists():
        return data

    with profile_path.open(encoding="utf-8") as f:
        profile = _yaml_loader.load(f)

    # An empty file is a reasonable thing to leave lying around while
    # you decide what to put in it; a list or a string is a mistake.
    if profile is None:
        return data
    if not isinstance(profile, dict):
        fail(
            f"{profile_path.name} must be a YAML mapping at the top level "
            f"(parsed as {type(profile).__name__}).\n"
            f"  {profile_path}"
        )

    try:
        shown = profile_path.relative_to(ROOT)
    except ValueError:
        shown = profile_path
    supplied = _profile_contributions(profile, data)
    c.ok_pair(
        "Shared profile",
        f"{shown} → {', '.join(supplied) if supplied else 'nothing new'}",
    )
    return deep_merge(profile, data)


def _profile_contributions(profile, data, prefix=""):
    """
    The dotted paths the profile actually fills in, for the build log.

    Recurses so the log says 'meta.lang' rather than nothing at all
    when the document has its own `meta` block but no `lang` inside
    it — which is the common case, since `meta.description` belongs
    to the document and `meta.lang` does not.
    """
    found = []
    for key, value in profile.items():
        path = f"{prefix}{key}"
        if key not in data:
            found.append(path)
        elif isinstance(value, dict) and isinstance(data[key], dict):
            found.extend(_profile_contributions(value, data[key], f"{path}."))
    return sorted(found)


def load_data():
    """Load resume data.

    Default behavior: prefer resume.yml over resume_default.yml.

    Override via RESUME_DATA_SOURCE env var:
      • RESUME_DATA_SOURCE=default → ignore resume.yml even if present
      • RESUME_DATA_SOURCE=mine    → require resume.yml (error if missing)
      • unset (default) → yours if present, else the shipped template

    The env var is consumed by snapshot_pdf.py --update-all to force a
    specific data source for each of the two builds it runs.

    Returns a (data, source) tuple where `source` is 'mine' or
    'default'. Callers thread `source` explicitly into the PDF metadata
    manifest. Previously this function stamped a `_data_source` field
    on the returned dict, which (a) mutated the user-data dict so
    iteration over its keys saw a phantom underscore-prefixed entry,
    and (b) made the function's effect on its return value implicit.
    Returning a tuple keeps the data dict clean and the contract
    explicit.
    """
    # An explicit file wins over everything else.
    #
    # This exists so a tool can render an arbitrary YAML file without
    # copying it over data/resume.yml first. Reading a file and
    # replacing someone's file are very different operations, and the
    # absence of this option made a preview feature reach for the
    # destructive one.
    explicit = os.environ.get(ENV_RESUME_DATA_FILE, '').strip()
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            fail(f"{ENV_RESUME_DATA_FILE} points at a file that does not exist:\n  {path}")
        try:
            shown = path.relative_to(ROOT)
        except ValueError:
            shown = path
        c.ok_pair("Loaded data", str(shown))
        with path.open(encoding="utf-8") as f:
            data = _yaml_loader.load(f)
        if not isinstance(data, dict):
            fail(
                f"{shown} is empty or not a YAML mapping at the top "
                f"level (parsed as {type(data).__name__})."
            )
        # 'mine' for metadata purposes: it is not the shipped template,
        # so the snapshot machinery should treat it as private data.
        return apply_profile(data, path), 'mine'

    override = os.environ.get(ENV_RESUME_DATA_SOURCE, '').strip().lower()
    if override == 'mine':
        if not DATA_FILE_MINE.exists():
            fail(
                f"{ENV_RESUME_DATA_SOURCE}=mine but no data file at "
                f"{DATA_FILE_MINE.relative_to(ROOT)}"
            )
        path = DATA_FILE_MINE
        source = 'mine'
    elif override == 'default':
        if not DATA_FILE_DEFAULT.exists():
            fail(
                f"{ENV_RESUME_DATA_SOURCE}=default but no default data file at "
                f"{DATA_FILE_DEFAULT.relative_to(ROOT)}"
            )
        path = DATA_FILE_DEFAULT
        source = 'default'
    elif override:
        fail(
            f"invalid {ENV_RESUME_DATA_SOURCE}={override!r}; "
            f"expected 'default', 'mine', or unset"
        )
    elif DATA_FILE_MINE.exists():
        path = DATA_FILE_MINE
        source = 'mine'
    elif DATA_FILE_DEFAULT.exists():
        path = DATA_FILE_DEFAULT
        source = 'default'
    else:
        fail(
            f"no data file found. Expected one of:\n"
            f"  {DATA_FILE_MINE.relative_to(ROOT)}\n"
            f"  {DATA_FILE_DEFAULT.relative_to(ROOT)}"
        )
    c.ok_pair("Loaded data", str(path.relative_to(ROOT)))
    with path.open(encoding="utf-8") as f:
        data = _yaml_loader.load(f)
    if not isinstance(data, dict):
        fail(
            f"{path.relative_to(ROOT)} is empty or not a YAML "
            f"mapping at the top level (parsed as {type(data).__name__})."
        )
    return apply_profile(data, path), source


def derive_pdf_metadata(data, lang, data_source):
    """
    Derive authoritative PDF metadata from the resume data.

    Source of truth is `resume.yml` (or `resume_default.yml`); we don't
    duplicate. Title and author come from the name; subject from the
    description; keywords from the page-1 sidebar's "Key Skills" block;
    `output_stem` from the name again, this time folded into something
    a filename can hold.

    `lang` and `data_source` are passed in explicitly by the caller
    (build()) — both are resolved once at the top of the build and
    threaded through every consumer, so there is exactly one place
    that decides them.

    Returned values are written to `dist/pdf_meta.json` and consumed
    by `crop_pdf.py`, which stamps them into the final PDF's /Info
    dictionary. This avoids relying on Chromium's inconsistent
    metadata defaults (Title is usually OK from <title>, but Author,
    Subject, and Keywords are typically empty or wrong).
    """
    name = f"{data['name']['first']} {data['name']['last']}"
    description = (data.get('meta', {}).get('description') or '').strip()

    # Pull keywords from the sidebar's "Key Skills" block (id='key-skills'
    # if the convention holds; falls back to heading match).
    # Items may be plain strings or {group: "..."} dicts — skip dicts.
    # Cap at 10: PDF /Keywords has no hard limit, but viewer UIs and
    # search indexers truncate long lists aggressively; the first 10
    # skills are what we care about being searchable.
    keywords = []
    try:
        for block in data['sidebar']['blocks']:
            if block.get('id') == 'key-skills' or \
               block.get('heading', '').strip().lower() == 'key skills':
                keywords = [
                    item for item in block.get('items', [])
                    if isinstance(item, str)
                ][:10]
                break
    except (KeyError, TypeError):
        # Schema didn't match — fall through with empty keywords.
        pass

    return {
        'title':    f"{name} — Resume",
        'author':   name,
        'subject':  description,
        'keywords': ', '.join(keywords),
        # BCP-47 language tag for the document. Resolved once at the
        # top of build() and passed in. Stamped into the PDF catalog
        # as /Lang by crop_pdf.py — assistive tech (screen readers,
        # refreshable braille) reads this to pick pronunciation/voice.
        'lang':     lang,
        # Whether the build used the placeholder template data or a
        # local override. Consumed by snapshot_pdf.py so the visual
        # regression test compares against the matching fixture.
        'data_source': data_source,
        # maxPages cap from meta.maxPages — read by resume.js to feed
        # the layout solver.
        'max_pages': data['meta']['maxPages'],
        # The filename stem both PDF variants share, derived from the
        # name above. Node cannot read the YAML this came from, so the
        # derivation happens here and travels in this file; see
        # _output_name.py for the folding rules and _output_name.js
        # for who reads it back.
        'output_stem': _output_name.stem(data['name'], 'resume'),
    }


def resolve_lang(data: dict) -> str:
    """
    Resolve the document's BCP-47 language tag.

    Single source of truth for the en-US fallback used when meta.lang is
    absent or empty. Called exactly once per build, from build(), which
    then threads the resolved value into:
      • template.render(lang=…) — drives <html lang="…">
      • derive_pdf_metadata(data, lang, …) — written into the PDF /Lang
                                              catalog entry by crop_pdf.py

    Keeping the default in one place — and resolving it exactly once
    per build — ensures the HTML and PDF never disagree on the
    document language.
    """
    return (data.get('meta', {}).get('lang') or 'en-US').strip()


def read_accent() -> str:
    """
    Read the canonical --accent color from styles/_tokens.scss.

    Single source of truth: the SCSS token file is the design-system
    canon for the accent. Other consumers that need the same value
    (favicon SVG background, template's theme-color meta tag) read it
    from here at build time instead of duplicating the literal.

    This eliminates a drift surface that previously existed: before
    centralization, `#2d4a3e` was hardcoded in three places; after
    centralization there is exactly one literal in the codebase,
    inside the SCSS declaration itself.

    Returns the hex value with leading '#' (e.g. "#2d4a3e"). Fails
    with a clear remediation message if the token file is missing or
    doesn't define --accent.
    """
    if not TOKENS_FILE.exists():
        fail(
            f"{TOKENS_FILE.relative_to(ROOT)} not found — "
            f"cannot determine accent color for favicon and template."
        )
    source = TOKENS_FILE.read_text(encoding='utf-8')
    # Match only the :root declaration (the first --accent encountered).
    # Subsequent overrides under @media print and (monochrome) and
    # html.force-grayscale are intentional context-specific re-definitions
    # — the canonical value for non-CSS consumers (favicon SVG, theme-color)
    # is the chromatic :root value, not the grayscale fallback.
    m = re.search(r'--accent:\s*(#[0-9a-fA-F]+)\s*;', source)
    if not m:
        fail(
            f"{TOKENS_FILE.relative_to(ROOT)} does not declare --accent "
            f"with a literal hex value.\n"
            f"Add `--accent: #xxxxxx;` to :root (or use a literal hex "
            f"value at the canonical declaration site) and retry."
        )
    return m.group(1)


def write_favicon(data, out_path, accent_hex):
    """
    Generate a minimal SVG favicon with the person's initials and
    write it to `out_path`.

    Initials are derived from `name.first` and `name.last` — first
    letter of each, uppercased. For "Gaius Caesar" → "GC"; for
    "Marcus Antonius" → "MA". Falls back to "?" if either name part
    is missing or empty (shouldn't happen given schema validation
    requires both, but guards anyway).

    The SVG background is `accent_hex`, threaded in by the caller from
    read_accent() so the favicon visually matches the document's
    section-heading color without duplicating the hex literal here.
    """
    first = (data.get('name', {}).get('first') or '').strip()
    last  = (data.get('name', {}).get('last')  or '').strip()
    initials = (first[:1] + last[:1]).upper() or '?'
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        f'<rect width="16" height="16" rx="2" fill="{accent_hex}"/>'
        f'<text x="8" y="12" font-family="system-ui, sans-serif" '
        f'font-size="9" font-weight="600" fill="white" '
        f'text-anchor="middle">{initials}</text>'
        '</svg>\n'
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_path.write_text(svg, encoding='utf-8')
    except PermissionError:
        fail(
            f"Permission denied writing {out_path}.\n"
            f"Close any program holding the file open and re-run."
        )


VALID_SECTION_TYPES = {"summary", "experience", "education"}
VALID_SIDEBAR_BLOCK_TYPES = {"details", "list"}
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SchemaError(Exception):
    """Raised when the YAML data fails structural validation."""


def _validate_id(value, ctx):
    """Validate id is a non-empty kebab-case string.

    Called for sidebar block ids and job ids. The kebab-case constraint
    is stylistic, not load-bearing: ids surface as HTML `id` attribute
    values on rendered <article>/<section> elements and as keys in
    placement.json for resolving placement entries back to source data,
    but neither use technically requires the shape. Underscores,
    camelCase, or other patterns would work mechanically — the rule
    keeps ids consistent and URL-fragment-friendly without escaping
    concerns. Relax it if a stronger reason emerges; nothing downstream
    will break.

    `ctx` is a human-readable path prefix (e.g. 'sidebar.blocks[3]')
    used in any SchemaError to locate the offending field.
    """
    if not isinstance(value, str) or not value:
        raise SchemaError(f"{ctx}: 'id' must be a non-empty string")
    if not ID_PATTERN.match(value):
        raise SchemaError(
            f"{ctx}: id {value!r} must be kebab-case (lowercase letters/digits, "
            f"separated by single hyphens)"
        )


def validate_data(data):
    """
    Validate the resume data shape up-front so failures surface
    with clear messages instead of as KeyError / StopIteration deep
    in template rendering.

    Schema:
      • Top-level required keys: name, meta, sidebar, mainColumn
      • Top-level optional keys: role (string), contact (mapping)
      • name has 'first' and 'last' string fields
      • role, if provided, is a string
      • contact, if provided, is a mapping with a required `rows` list
        (each row: non-empty `value`, optional `href`) and an optional
        string `address`
      • meta has 'description' (string), 'maxPages' (positive int),
        and an optional 'lang' (string)
      • sidebar.blocks is a flat list; each block has a unique kebab-case 'id',
        a 'type' in VALID_SIDEBAR_BLOCK_TYPES, and a heading
      • mainColumn is a list of section dicts; each 'type' is in
        VALID_SECTION_TYPES; exactly one of each section type exists
      • experience.jobs is a list; each job has a unique kebab-case 'id'
        and either bullets (regular job) or gap=true (gap entry).
        'gap', when present, must be an unquoted boolean

    Raises SchemaError on the first violation found.
    """
    # Top-level structure.
    for key in ("name", "meta", "sidebar", "mainColumn"):
        if key not in data:
            raise SchemaError(f"missing top-level key: {key!r}")

    # Name.
    if not isinstance(data["name"], dict):
        raise SchemaError("'name' must be a mapping")
    for key in ("first", "last"):
        if not isinstance(data["name"].get(key), str):
            raise SchemaError(f"'name.{key}' must be a string")

    # Role (optional). Free-form subtitle rendered under the name; if
    # provided must be a string. A non-string would render via Python's
    # default str() conversion inside the page header — usually weird.
    if "role" in data and not isinstance(data["role"], str):
        raise SchemaError("'role' must be a string if provided")

    # Contact (optional). If provided must be a mapping. The header
    # template iterates `contact.rows` unconditionally once the contact
    # block opens, so `rows` is required as soon as you opt in (use an
    # empty list if you only want an address). Each row needs a non-
    # empty `value`; `href` is optional but must be a string when set.
    if "contact" in data and data["contact"] is not None:
        contact = data["contact"]
        if not isinstance(contact, dict):
            raise SchemaError("'contact' must be a mapping")
        if "address" in contact and not isinstance(contact["address"], str):
            raise SchemaError("'contact.address' must be a string")
        rows = contact.get("rows")
        if not isinstance(rows, list):
            raise SchemaError(
                "'contact.rows' must be a list (use an empty list if "
                "you want only an address)"
            )
        for i, row in enumerate(rows):
            ctx = f"contact.rows[{i}]"
            if not isinstance(row, dict):
                raise SchemaError(
                    f"{ctx}: must be a mapping with 'value' (and optional 'href')"
                )
            if not isinstance(row.get("value"), str) or not row["value"]:
                raise SchemaError(f"{ctx}: 'value' must be a non-empty string")
            if "href" in row and not isinstance(row["href"], str):
                raise SchemaError(f"{ctx}: 'href' must be a string if provided")

    # Meta.
    if not isinstance(data["meta"], dict):
        raise SchemaError("'meta' must be a mapping")
    if not isinstance(data["meta"].get("description"), str):
        raise SchemaError("'meta.description' must be a string")
    max_pages = data["meta"].get("maxPages")
    if not isinstance(max_pages, int) or max_pages < 1:
        raise SchemaError(
            f"'meta.maxPages' must be a positive integer; got {max_pages!r}"
        )
    # meta.lang (optional). If provided must be a string — resolve_lang()
    # calls .strip() on it, which would AttributeError on a non-string.
    if "lang" in data["meta"] and not isinstance(data["meta"]["lang"], str):
        raise SchemaError("'meta.lang' must be a string if provided")

    # Sidebar.
    if not isinstance(data["sidebar"], dict):
        raise SchemaError("'sidebar' must be a mapping with a 'blocks' list")
    blocks = data["sidebar"].get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise SchemaError("'sidebar.blocks' must be a non-empty list")
    seen_block_ids = set()
    for i, block in enumerate(blocks, start=1):
        ctx = f"sidebar.blocks[{i - 1}]"
        if not isinstance(block, dict):
            raise SchemaError(f"{ctx}: must be a mapping")
        _validate_id(block.get("id"), ctx)
        if block["id"] in seen_block_ids:
            raise SchemaError(f"{ctx}: duplicate sidebar block id {block['id']!r}")
        seen_block_ids.add(block["id"])
        btype = block.get("type")
        if btype not in VALID_SIDEBAR_BLOCK_TYPES:
            raise SchemaError(
                f"{ctx}: type {btype!r} not in {sorted(VALID_SIDEBAR_BLOCK_TYPES)}"
            )
        if not isinstance(block.get("heading"), str) or not block["heading"]:
            raise SchemaError(f"{ctx}: 'heading' must be a non-empty string")

    # Main column.
    if not isinstance(data["mainColumn"], list):
        raise SchemaError("'mainColumn' must be a list")
    seen_types = []
    for i, section in enumerate(data["mainColumn"], start=1):
        if not isinstance(section, dict):
            raise SchemaError(f"mainColumn entry {i} must be a mapping")
        stype = section.get("type")
        if stype not in VALID_SECTION_TYPES:
            raise SchemaError(
                f"mainColumn entry {i}: type {stype!r} not in "
                f"{sorted(VALID_SECTION_TYPES)}"
            )
        if stype in seen_types:
            raise SchemaError(
                f"mainColumn has multiple {stype!r} sections; current template "
                f"supports exactly one of each"
            )
        seen_types.append(stype)
    for required in ("summary", "experience", "education"):
        if required not in seen_types:
            raise SchemaError(f"mainColumn is missing required section type: {required!r}")

    # Experience.jobs: every job has a unique kebab-case id; gap
    # entries skip bullets, regular jobs require a non-empty list.
    experience = next(s for s in data["mainColumn"] if s["type"] == "experience")
    if not isinstance(experience.get("jobs"), list) or not experience["jobs"]:
        raise SchemaError("'experience.jobs' must be a non-empty list")
    seen_job_ids = set()
    for i, job in enumerate(experience["jobs"], start=1):
        ctx = f"experience.jobs[{i - 1}]"
        if not isinstance(job, dict):
            raise SchemaError(f"{ctx}: must be a mapping")
        _validate_id(job.get("id"), ctx)
        if job["id"] in seen_job_ids:
            raise SchemaError(f"{ctx}: duplicate job id {job['id']!r}")
        seen_job_ids.add(job["id"])
        # 'gap' must be a real boolean, not a string that looks like one.
        #
        # build/_yaml_loader.py resolves an unquoted `gap: true` to True,
        # but `gap: "true"` and `gap: "false"` both arrive as non-empty
        # strings — and every non-empty string is truthy in Python, so
        # the quoted form would mark a gap entry while appearing to say
        # the opposite. Checking the type costs nothing; debugging a job
        # whose bullets silently vanished costs an afternoon.
        if "gap" in job and not isinstance(job["gap"], bool):
            raise SchemaError(
                f"{ctx}: 'gap' must be true or false, unquoted; got "
                f"{job['gap']!r}"
            )
        # Gap entries skip bullets entirely; regular jobs must have a list.
        if not job.get("gap"):
            if not isinstance(job.get("bullets"), list) or not job["bullets"]:
                raise SchemaError(
                    f"{ctx}: regular job {job['id']!r} must have a non-empty "
                    f"'bullets' list (or set 'gap: true' for a gap entry)"
                )


def check_no_module_collisions():
    """
    Fail fast if any .py module name appears in both build/ and tests/.

    Python's import resolution gets confused when the same module name
    exists in two directories that are both reachable from sys.path,
    producing cryptic 'incorrectly imported' errors. This check surfaces
    such state before unittest tries to import anything.
    """
    build_dir = ROOT / "build"
    tests_dir = ROOT / "tests"
    if not build_dir.exists() or not tests_dir.exists():
        return
    build_modules = {p.stem for p in build_dir.glob("*.py")}
    tests_modules = {p.stem for p in tests_dir.glob("*.py")}
    overlap = build_modules & tests_modules
    if overlap:
        names = ", ".join(sorted(overlap))
        fail(
            f"module name collision between build/ and tests/: {names}\n"
            f"This breaks Python's import resolution. Delete the duplicate(s) "
            f"in whichever directory shouldn't have them."
        )


# Tolerance window (in seconds) for check_stylesheet_freshness's mtime
# comparison. 2s covers FAT32's worst-case mtime granularity plus
# inter-process clock jitter and short cloud-sync delays, while a real
# forgot-to-compile stale stylesheet is off by minutes to days and
# trips the check regardless. See the function's docstring for the
# full rationale.
FRESHNESS_TOLERANCE_SECONDS = 2.0


def check_stylesheet_freshness():
    """
    Fail fast if dist/styles.css is missing or significantly older
    than its SCSS sources.

    The build pipeline compiles SCSS in resume.js step 1, before this
    script runs — so the canonical `npm run resume` path always passes
    this check immediately. Direct invocations of build.py
    (`python build/build.py ...`) skip the Sass step entirely; without
    this check, they'd render an HTML that silently references a missing
    or stale stylesheet and produce visually broken output that's
    indistinguishable from a real layout bug.

    Tolerance window
    ────────────────
    Mtime comparison uses a small tolerance (FRESHNESS_TOLERANCE_SECONDS)
    because a freshly-written CSS can legitimately appear microseconds
    OLDER than an SCSS source under several common conditions:
      • FAT32 has 2-second mtime granularity (worst common case).
      • ext3 has 1-second; NTFS is 100ns but writes through Node's
        fs.writeFileSync don't always commit at full precision.
      • Cloud sync agents (OneDrive, Dropbox, iCloud Drive) bump
        mtimes of synced files at sync-completion time, which can
        fall AFTER an unrelated write to the same volume.
      • Some editors and IDE indexers touch files post-save.
    A real "I forgot to recompile" stale CSS is off by minutes to days,
    well past any tolerance worth setting, so the check still catches
    its actual target while ignoring sub-second filesystem noise.

    The check is two stat() calls per .scss file — negligible cost.
    """
    css_file = ROOT / "dist" / "styles.css"
    styles_dir = ROOT / "styles"
    sass_cmd = (
        f"npx sass {styles_dir.relative_to(ROOT)}/styles.scss "
        f"{css_file.relative_to(ROOT)}"
    )
    if not css_file.exists():
        fail(
            f"{css_file.relative_to(ROOT)} not found.\n"
            f"build.py does not compile SCSS — `npm run resume` does that as step 1.\n"
            f"Run `npm run resume` for the canonical flow, or compile manually:\n"
            f"  {sass_cmd}"
        )
    css_mtime = css_file.stat().st_mtime
    # Record (path, skew_seconds) for each genuinely-stale source so the
    # error message can report the magnitude — small skews (< 1s) reveal
    # a precision/sync artifact, large ones (minutes+) reveal real
    # forgot-to-compile staleness.
    stale = sorted(
        (
            (p.relative_to(ROOT), p.stat().st_mtime - css_mtime)
            for p in styles_dir.glob("*.scss")
            if p.stat().st_mtime - css_mtime > FRESHNESS_TOLERANCE_SECONDS
        ),
        key=lambda pair: -pair[1],  # largest skew first
    )
    if stale:
        listing = "\n".join(f"  {p}  (+{skew:.1f}s)" for p, skew in stale)
        fail(
            f"{css_file.relative_to(ROOT)} is older than its SCSS sources:\n"
            f"{listing}\n"
            f"Recompile with `npm run resume`, or:\n"
            f"  {sass_cmd}"
        )


def build(mode='final'):
    """Build dist/index.html in the requested mode.

    mode='final': paginated layout consuming a placement.
    mode='measurement': single-page flowing layout for the solver.
    """
    if mode not in ('final', 'measurement'):
        fail(f"unknown build mode {mode!r}; expected 'final' or 'measurement'")

    check_no_module_collisions()
    check_stylesheet_freshness()
    data, data_source = load_data()
    try:
        validate_data(data)
    except SchemaError as e:
        fail(f"invalid resume data — {e}")

    # Resolve named sections — schema guarantees exactly one of each.
    sections_by_type = {s["type"]: s for s in data["mainColumn"]}
    summary = sections_by_type["summary"]
    experience = sections_by_type["experience"]
    education = sections_by_type["education"]

    # Build lookup tables (id → object) so the placement-driven template
    # can reference jobs and sidebar blocks by id without searching.
    block_by_id = {b["id"]: b for b in data["sidebar"]["blocks"]}
    job_by_id = {j["id"]: j for j in experience["jobs"]}

    # Canonical accent color, read from the SCSS token file. Threaded
    # into the template context (theme-color meta tag) and the favicon
    # SVG so neither has to duplicate the hex literal. See read_accent's
    # docstring for the centralization rationale.
    accent = read_accent()

    # Document language (BCP-47). Same single-source-of-truth pattern as
    # accent: resolved once here, passed into both templates' <html lang>
    # attribute and into derive_pdf_metadata for the PDF /Lang catalog
    # entry. Without this, the HTML hardcoded "en" while the PDF used
    # meta.lang, so a French user got an English-tagged HTML and a
    # French-tagged PDF.
    lang = resolve_lang(data)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        # Autoescape is ON for every template extension. This is the
        # single defense against an injected '&', '<', or '"' in the
        # YAML data (resume names, descriptions, URLs) producing
        # malformed HTML in <title>, <meta content="…">, or <a href="…">.
        # Before the flip every `{{ var }}` had to be hand-written as
        # `{{ var | e }}`, and several sites had been missed (<title>,
        # meta author/description, og:* properties, the .name-first /
        # .name-last spans). Flipping the default closes that whole
        # category.
        #
        # Filter sites that need to emit literal HTML (the markdown
        # filter expands `**bold**` into `<strong>` tags) MUST end the
        # chain with `| safe` so autoescape doesn't re-escape the tags.
        # In practice the only such chain is `bullet | e | md | safe`:
        # the leading `| e` does real work (escapes '&' etc. before
        # markdown_filter sees the text, which it would otherwise pass
        # through raw), and `| safe` blocks the auto-escape that would
        # otherwise hit `<strong>` and produce `&lt;strong&gt;`.
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.filters["md"] = markdown_filter

    if mode == 'measurement':
        # Render a single-page flowing layout. The solver opens this
        # in Playwright, measures every job/bullet/block/item, and
        # produces the placement that drives the final build.
        template = env.get_template("measurement.j2")
        rendered = template.render(
            name=data["name"],
            role=data.get("role"),
            contact=data.get("contact"),
            meta=data["meta"],
            summary=summary,
            experience=experience,
            education=education,
            sidebar_blocks=data["sidebar"]["blocks"],
            accent=accent,
            lang=lang,
        )
    else:
        # Final paginated build. Requires dist/placement.json (written
        # by resume.js after measurement + solving). Direct invocations
        # of this script in --mode=final without a prior resume.js run
        # will fail with a clear message.
        placement_path = ROOT / "dist" / "placement.json"
        if not placement_path.exists():
            fail(
                f"{placement_path.relative_to(ROOT)} not found.\n"
                f"Final-mode build requires the layout solver's placement.\n"
                f"Run `npm run resume` to produce it, or `python "
                f"{Path(__file__).relative_to(ROOT)} --mode=measurement` "
                f"to generate the measurement HTML for inspection."
            )
        placement = json.loads(placement_path.read_text(encoding='utf-8'))
        n_pages = len(placement.get('pages', []))
        page_word = 'page' if n_pages == 1 else 'pages'
        c.ok_pair("Loaded placement",
                  f"{placement_path.relative_to(ROOT)} "
                  f"({n_pages} {page_word})")
        # Per-page sidebar aria-label. Each page lists the headings of
        # the (non-continuation) blocks it contains, joined by " and ".
        # Computed here in plain Python instead of inside the template
        # (where it lived as a side-effecting `headings.append()` loop).
        for page in placement.get('pages', []):
            page['sidebar_aria'] = ' and '.join(
                block_by_id[entry['block_id']]['heading']
                for entry in page.get('sidebar_blocks', [])
                if not entry.get('continuation', False)
            )
        template = env.get_template("resume.j2")
        rendered = template.render(
            name=data["name"],
            role=data.get("role"),
            contact=data.get("contact"),
            meta=data["meta"],
            summary=summary,
            experience=experience,
            education=education,
            placement=placement,
            block_by_id=block_by_id,
            job_by_id=job_by_id,
            accent=accent,
            lang=lang,
        )

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        OUT_FILE.write_text(rendered, encoding="utf-8")
    except PermissionError:
        fail(
            f"Permission denied writing {OUT_FILE}.\n"
            f"Another program is holding the file open (most likely a browser tab\n"
            f"or editor previewing the rendered HTML). Close it and re-run."
        )
    # dist/styles.css is produced by Sass (compiled from
    # styles/styles.scss in resume.js step 1). The freshness
    # of that file is verified up-front by check_stylesheet_freshness().
    # Emit PDF metadata manifest for crop_pdf.py to consume.
    # In measurement mode we still emit it so resume.js can read
    # data_source consistently regardless of mode.
    pdf_meta = derive_pdf_metadata(data, lang, data_source)
    try:
        PDF_META_FILE.write_text(
            json.dumps(pdf_meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except PermissionError:
        fail(
            f"Permission denied writing {PDF_META_FILE}.\n"
            f"Close any program holding the file open and re-run."
        )
    kb = len(rendered.encode('utf-8')) / 1024
    c.ok_pair("Wrote HTML", f"{OUT_FILE.relative_to(ROOT)} ({kb:.1f} KB)")
    # Favicon — generated from the person's initials. Written in both
    # measurement and final modes since the rendered HTML's <head>
    # references it; opening dist/index.html with a missing favicon
    # would show a 404 in dev tools.
    write_favicon(data, FAVICON_FILE, accent)
    # Measurement mode is the first pass that produces pdf_meta.json;
    # final mode rewrites it (typically with identical content). Use
    # different verbs so the user can tell them apart in the log.
    metadata_label = "Wrote metadata" if mode == 'measurement' else "Refreshed metadata"
    c.ok_pair(metadata_label, str(PDF_META_FILE.relative_to(ROOT)))


def main():
    parser = argparse.ArgumentParser(description="Build the resume HTML.")
    parser.add_argument(
        "--mode",
        choices=("final", "measurement"),
        default="final",
        help="final (default): paginated output. "
             "measurement: single-page flowing layout for the layout solver.",
    )
    args = parser.parse_args()
    build(mode=args.mode)


if __name__ == "__main__":
    main()
