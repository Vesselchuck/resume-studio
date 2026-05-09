#!/usr/bin/env python3
"""
build.py — Generate dist/index.html from data/resume_default.yml.

Reads the resume data, validates its shape, computes derived fields
(page-2 jobs, continuation job, PDF metadata), and renders
templates/resume.j2 to dist/index.html.

Two modes:
  • --mode=final (default) — produces the paginated resume from a
    placement read from dist/placement.json. The placement is
    written by render.js after the layout solver runs.
  • --mode=measurement — produces a single-page flowing layout
    that the layout solver in render.js measures to decide
    page placement.

Custom Jinja filter:
  • md   — converts `**bold**` to <strong>bold</strong>. Used inside
           bullet text. Only spans on a single line are matched.

HTML escaping for special characters (& < >) is done via Jinja's
built-in `e` (escape) filter at the template call sites.

Run via `node render.js`, which calls this first; or directly with
`python3 scripts/build.py` (run from project root).
"""

import sys

# Suppress writing of __pycache__/ next to source files. Set this
# before any other (non-builtin) import so child imports also
# inherit the flag. Equivalent to running with `python -B` but
# enforces the no-cache rule even for direct invocations like
# `python scripts/build.py`.
sys.dont_write_bytecode = True

import os
import re
import json
import argparse
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape, StrictUndefined

# Local console helper (sibling module). Inserted at import time so
# the same `c.ok()` / `c.err()` API is available everywhere.
sys.path.insert(0, str(Path(__file__).parent))
import _console as c  # noqa: E402


ROOT = Path(__file__).parent.parent      # this script lives in scripts/
DATA_DIR = ROOT / "data"
TEMPLATES_DIR = ROOT / "templates"
OUT_FILE = ROOT / "dist" / "index.html"

# Data-file precedence: local (real, gitignored) wins over the
# committed placeholder. This lets the repo ship a template
# resume_default.yml while real resumes live in resume.local.yml outside
# version control.
DATA_FILE_LOCAL = DATA_DIR / "resume.local.yml"
DATA_FILE_DEFAULT = DATA_DIR / "resume_default.yml"
PDF_META_FILE = ROOT / "dist" / "pdf_meta.json"


def fail(msg: str) -> None:
    """
    Emit a coloured error headline (and any subsequent newline-
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

    Anything else is left as-is for the `typo` filter to handle. HTML
    metacharacters in the input are NOT auto-escaped here — that's the
    template's job (or the typo filter, for ampersands). The contract:
      • Input is plain text plus `**bold**` runs.
      • Output is HTML-safe to inject inside a <li> via `| safe`.
      • Any literal '<' or '>' in YAML source will pass through to the
        HTML and be interpreted as markup. Don't put HTML in YAML.

    Bold spans cannot cross newlines, cannot be empty (`****`), and the
    pattern is non-greedy so `**a** **b**` produces two distinct spans.
    """
    if text is None:
        return ""
    s = str(text)
    # Non-greedy match; require at least one non-asterisk character inside.
    return re.sub(r"\*\*([^*\n]+?)\*\*", r"<strong>\1</strong>", s)


def load_data():
    """Load resume data.

    Default behavior: prefer resume.local.yml over resume_default.yml.

    Override via RESUME_DATA_SOURCE env var:
      • RESUME_DATA_SOURCE=default → ignore resume.local.yml even if present
      • RESUME_DATA_SOURCE=local   → require resume.local.yml (error if missing)
      • unset (default) → original behavior (local if present, else default)

    The env var is consumed by snapshot_pdf.py --update-both to force a
    specific data source for each of the two builds it runs.

    Stamps `_data_source` on the returned dict so downstream consumers
    (notably the PDF metadata + snapshot test) can tell which file
    backed this build. Values: 'local' or 'default'.
    """
    override = os.environ.get('RESUME_DATA_SOURCE', '').strip().lower()
    if override == 'local':
        if not DATA_FILE_LOCAL.exists():
            fail(
                f"RESUME_DATA_SOURCE=local but no local data file at "
                f"{DATA_FILE_LOCAL.relative_to(ROOT)}"
            )
        path = DATA_FILE_LOCAL
        source = 'local'
    elif override == 'default':
        if not DATA_FILE_DEFAULT.exists():
            fail(
                f"RESUME_DATA_SOURCE=default but no default data file at "
                f"{DATA_FILE_DEFAULT.relative_to(ROOT)}"
            )
        path = DATA_FILE_DEFAULT
        source = 'default'
    elif override:
        fail(
            f"invalid RESUME_DATA_SOURCE={override!r}; "
            f"expected 'default', 'local', or unset"
        )
    elif DATA_FILE_LOCAL.exists():
        path = DATA_FILE_LOCAL
        source = 'local'
    elif DATA_FILE_DEFAULT.exists():
        path = DATA_FILE_DEFAULT
        source = 'default'
    else:
        fail(
            f"no data file found. Expected one of:\n"
            f"  {DATA_FILE_LOCAL.relative_to(ROOT)}\n"
            f"  {DATA_FILE_DEFAULT.relative_to(ROOT)}"
        )
    c.ok_pair("Loaded data", str(path.relative_to(ROOT)))
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        fail(
            f"{path.relative_to(ROOT)} is empty or not a YAML "
            f"mapping at the top level (parsed as {type(data).__name__})."
        )
    data['_data_source'] = source
    return data


def derive_pdf_metadata(data):
    """
    Derive authoritative PDF metadata from the resume data.

    Source of truth is `resume_default.yml` (or `resume.local.yml`); we don't
    duplicate. Title and author come from the name; subject from the
    description; keywords from the page-1 sidebar's "Key Skills" block.

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
        # BCP-47 language tag for the document. Optional in YAML;
        # defaults to en-US. Stamped into the PDF catalog as /Lang
        # by crop_pdf.py — assistive tech (screen readers, refresh-
        # able braille) reads this to pick pronunciation/voice.
        'lang':     (data.get('meta', {}).get('lang') or 'en-US').strip(),
        # Whether the build used the placeholder template data or a
        # local override. Consumed by snapshot_pdf.py so the visual
        # regression test compares against the matching fixture.
        'data_source': data.get('_data_source', 'default'),
        # maxPages cap from meta.maxPages — read by render.js to feed
        # the layout solver.
        'max_pages': data['meta']['maxPages'],
    }


VALID_SECTION_TYPES = {"summary", "experience", "education"}
VALID_SIDEBAR_BLOCK_TYPES = {"details", "list"}
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SchemaError(Exception):
    """Raised when the YAML data fails structural validation."""


def _validate_id(value, ctx):
    """Common id-field validation: required string, kebab-case."""
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

    Schema (Bunch 4):
      • Top-level keys: name, meta, sidebar, mainColumn — all required
      • name has 'first' and 'last' string fields
      • meta has 'description' (string) and 'maxPages' (positive int)
      • sidebar.blocks is a flat list; each block has a unique kebab-case 'id',
        a 'type' in VALID_SIDEBAR_BLOCK_TYPES, and a heading
      • mainColumn is a list of section dicts; each 'type' is in
        VALID_SECTION_TYPES; exactly one of each section type exists
      • experience.jobs is a list; each job has a unique kebab-case 'id'
        and either bullets (regular job) or gap=true (gap entry)
      • No 'bulletsPage1'/'bulletsPage2' keys (auto-flow handles bridging)

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

    # Experience.jobs: every job has a unique kebab-case id; no
    # legacy bulletsPage1/bulletsPage2 keys allowed.
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
        # Gap entries skip bullets entirely; regular jobs must have a list.
        if not job.get("gap"):
            if not isinstance(job.get("bullets"), list) or not job["bullets"]:
                raise SchemaError(
                    f"{ctx}: regular job {job['id']!r} must have a non-empty "
                    f"'bullets' list (or set 'gap: true' for a gap entry)"
                )
        # Reject legacy keys explicitly so the migration is explicit.
        for legacy in ("bulletsPage1", "bulletsPage2"):
            if legacy in job:
                raise SchemaError(
                    f"{ctx}: legacy key {legacy!r} is no longer supported; "
                    f"use a single 'bullets' list (auto-flow handles bridging)"
                )


def check_no_module_collisions():
    """
    Fail fast if any .py module name appears in both scripts/ and tests/.

    Python's import resolution gets confused when the same module name
    exists in two directories that are both reachable from sys.path,
    producing cryptic 'incorrectly imported' errors. This check surfaces
    such state before unittest tries to import anything.
    """
    scripts_dir = ROOT / "scripts"
    tests_dir = ROOT / "tests"
    if not scripts_dir.exists() or not tests_dir.exists():
        return
    scripts_modules = {p.stem for p in scripts_dir.glob("*.py")}
    tests_modules = {p.stem for p in tests_dir.glob("*.py")}
    overlap = scripts_modules & tests_modules
    if overlap:
        names = ", ".join(sorted(overlap))
        fail(
            f"module name collision between scripts/ and tests/: {names}\n"
            f"This breaks Python's import resolution. Delete the duplicate(s) "
            f"in whichever directory shouldn't have them."
        )


def build(mode='final'):
    """Build dist/index.html in the requested mode.

    mode='final': paginated layout consuming a placement.
    mode='measurement': single-page flowing layout for the solver.
    """
    if mode not in ('final', 'measurement'):
        fail(f"unknown build mode {mode!r}; expected 'final' or 'measurement'")

    check_no_module_collisions()
    data = load_data()
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

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(
            disabled_extensions=("j2",),
            default_for_string=False,
            default=False,
        ),
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
        )
    else:
        # Final paginated build. Requires dist/placement.json (written
        # by render.js after measurement + solving). Direct invocations
        # of this script in --mode=final without a prior render.js run
        # will fail with a clear message.
        placement_path = ROOT / "dist" / "placement.json"
        if not placement_path.exists():
            fail(
                f"{placement_path.relative_to(ROOT)} not found.\n"
                f"Final-mode build requires the layout solver's placement.\n"
                f"Run `node render.js` to produce it, or `python "
                f"{Path(__file__).relative_to(ROOT)} --mode=measurement` "
                f"to generate the measurement HTML for inspection."
            )
        placement = json.loads(placement_path.read_text(encoding='utf-8'))
        n_pages = len(placement.get('pages', []))
        page_word = 'page' if n_pages == 1 else 'pages'
        c.ok_pair("Loaded placement",
                  f"{placement_path.relative_to(ROOT)} "
                  f"({n_pages} {page_word})")
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
    # NOTE: dist/styles.css is produced by Sass (compiled from
    # assets/styles/styles.scss in render.js step 1), not copied here.
    # Direct invocations of build.py (`python scripts/build.py ...`)
    # without going through render.js will leave dist/styles.css
    # missing or stale; the rendered HTML will reference a missing
    # stylesheet. That's expected — direct invocations are a
    # debugging path, not the canonical build.
    # Emit PDF metadata manifest for crop_pdf.py to consume.
    # In measurement mode we still emit it so render.js can read
    # data_source consistently regardless of mode.
    pdf_meta = derive_pdf_metadata(data)
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
