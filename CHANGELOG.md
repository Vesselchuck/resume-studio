# Changelog

Every release of this project, newest first.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and version numbers follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html):
**MAJOR.MINOR.PATCH**.

The project is still in **initial development**, which SemVer marks
with a major version of **0**: the interface below can still change,
and nothing about it is promised yet. While the major version is 0,
this project follows the usual convention for that range:

- **MINOR** goes up when something you rely on stops working unless
  you change it,
- **PATCH** goes up for everything else — new features, fixes and
  adjustments that break nothing.

**1.0.0** will mean the interface is settled. From then on the rules
become the standard ones: a breaking change takes a new major version,
a new feature a minor one, and a fix a patch.

"Something you rely on" is this project's public interface, and it
has four parts:

1. **The data format** — the keys and structure of the YAML files you
   write.
2. **The commands** — what you run: the `node …` and `python …`
   invocations.
3. **The environment variables** the build reads.
4. **The outputs** — where the PDFs are written and what they are
   called.

A change to how the PDF *looks*, however large, is not a change to
that interface. Neither is an internal refactor.

Headings under each release: **Added** for new capability, **Changed**
for existing behavior that now works differently, **Removed** for what
is gone, **Fixed** for bugs, **Security** for what used to be exposed.
**Breaking** marks the changes that forced a new minor version.

---

## [0.4.1] — 2026-05-09

*New optional fields; every 0.4.0 file still builds unchanged.*

- **Added.** An optional `role` field, shown as a small uppercase
  accent-colored line under the name.
- **Added.** An optional `contact` block (`address` plus `rows` of
  `value` and optional `href`), shown on the right of the page header.
  The sidebar `details` block still works.
- **Added.** `{group: "…"}` entries in sidebar lists, shown as
  subheadings inside the list. They are left out of the PDF keywords.
- **Added.** `**bold**` in the summary.
- **Changed.** The header is two columns: name and role on the left,
  contact details on the right. The name is on one line and no longer
  in capitals.
- **Changed.** Section headings take the accent color, the summary is
  set as a lead paragraph, and date and location rows are larger. The
  sidebar is 2.3in wide instead of 2in.
- **Changed.** The CSS classes `.job-title` and `.edu-title` merge into
  `.entry-title`.

## [0.4.0] — 2026-05-08

*Breaking: the PDF changed from A4 to US Letter, and the styles moved.*

- **Changed — Breaking.** The page size is US Letter (8.5 × 11 in)
  instead of A4. This covers the page tokens, `@page`, printing,
  cropping and the layout checks. A 0.3.0 fixture fails until it is
  refreshed.
- **Changed — Breaking.** The Sass sources moved from `styles/` to
  `assets/styles/`. Anything still edited in the old folder is ignored.
- **Changed.** Several CSS custom properties and one class were
  renamed: `--ink` → `--text-primary`, `--rule` → `--border-rule`, and
  others; `.detail-label` → `.subgroup-label`.
- **Added.** Montserrat is included in the project and no longer loaded
  from the network. A system-installed copy had different glyph widths
  and changed where pages broke.
- **Added.** An optional `meta.lang` field, written into the PDF as
  `/Lang` so screen readers pick the right pronunciation. It defaults to
  `en-US`.
- **Added.** Matching console helpers in Python and Node:
  - status lines and phase banners
  - `NO_COLOR` / `FORCE_COLOR` support
- **Added.** More environment variables:
  - `STRICT_TESTS` fails the run if any test suite is skipped.
  - `DEBUG_MEASUREMENTS` prints what the solver measured.
- **Added.** `tests/test_check_layout.js`, and monochrome-print colors
  that hold up on black-and-white printers.
- **Changed.** Redesign:
  - a deep slate-green accent
  - thinner type
  - tighter section spacing
  - en-dash bullets
  - a middle dot between date and location
- **Changed.** `render.js` is split into named phases, and its log is
  grouped under banners.
- **Fixed.** The layout check always expected two pages, so any resume
  with a different page count failed.
- **Fixed.** Job heights were measured too short: two margins were
  never counted.
- **Fixed.** Both columns were charged the separator height of
  whichever came first.
- **Fixed.** If a PDF viewer had an output file open, the build crashed
  with a traceback. It now names the file to close.

## [0.3.0] — 2026-05-03

*The first release with an interface to depend on: a YAML data format,
a build command, environment variables and a fixed output path.*

- **Added.** One YAML file (`data/resume_default.yml`) holds all the resume
  content: `name`, `meta`, a `sidebar` of blocks and a `mainColumn` of
  sections. `build.py` checks the file and stops with a clear error
  when something is wrong.
- **Added.** Your own data file. `data/resume.local.yml` takes the place
  of the committed placeholder and is gitignored.
  `RESUME_DATA_SOURCE=default|local` forces one or the other.
- **Added.** `node render.js` runs the whole pipeline: unit tests, Sass,
  a measurement render, the layout solver, the final render, layout
  checks, printing, cropping and a snapshot comparison. Output is
  written to `print.pdf`.
- **Added.** Automatic page breaks. A solver works from measured heights
  and can split a job's bullets or a sidebar list across pages, keeping
  at least one bullet (or three list items) with its heading. The build
  fails if the content needs more than `meta.maxPages` pages.
- **Added.** Fixed-size pages, so the layout on screen and in print is
  the same, and a check that fails the build when anything overflows a
  page.
- **Added.** PDF cropping to the exact page size, and document metadata
  taken from the data: title, author, subject, and keywords from the
  Key Skills block.
- **Added.** A pixel-level snapshot test against committed fixtures,
  with diff images when it fails.
- **Added.** Unit tests, and automatic Python detection (`PYTHON`
  overrides it).
- **Changed.** The page is A4, and the layout follows the 0.1.0 design:
  contact details in a sidebar block.
- **Removed.** The hand-written `index.html`. Its content moved into
  YAML and Jinja templates.

## [0.2.0] — 2026-04-25

*A redesign of the same hand-written page.*

- **Changed.** The typefaces are Fraunces for the name and headings and
  Manrope for everything else, both from Google Fonts. They replace
  Montserrat.
- **Changed.** The paper size is US Letter with 0.5in margins instead
  of A4.
- **Changed.** The contact details move from a sidebar block into the
  page header, next to the name, which is now on one line. Languages
  show the name and the level side by side, and on screen the page
  sits on a warm off-white background.
- **Changed.** `print.pdf` is printed from the new page.

## [0.1.0] — 2026-04-20

*The starting point: the resume design, as a hand-written web page.*

- **Added.** `index.html`, with the resume written directly into the
  markup, and `styles.css`: a two-column A4 layout in Montserrat. The
  sidebar holds details, key skills and languages; the main column
  holds a summary, work experience, education and professional
  development. Section headings are uppercase with a short rule under
  them, and each job shows its location on the right. Below 780px the
  columns stack.
- **Added.** Print CSS for A4. The browser decides where pages break.
- **Added.** `print.pdf`, printed from the page.

The design first existed only as a PDF made with an online resume
builder. This version recreates it in HTML and CSS, so the history
starts from source files.

[0.4.1]: #041--2026-05-09
[0.4.0]: #040--2026-05-08
[0.3.0]: #030--2026-05-03
[0.2.0]: #020--2026-04-25
[0.1.0]: #010--2026-04-20
