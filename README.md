# Resume

Version 0.4.1. See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

Single-source-of-truth resume pipeline. Edit YAML, get a pixel-faithful
US Letter PDF. Page placement is computed automatically — no manual
page-break management.

## What it does

1. Reads resume content from `data/resume_default.yml` (or
   `data/resume.local.yml` if present, for private data).
2. Validates the schema — clear errors for missing fields, duplicate ids, etc.
3. Renders a measurement-mode HTML page (everything in one flowing
   column) so a layout solver can read actual rendered heights.
4. Solves the layout: decides which sidebar blocks and which jobs go
   on which page, including bridging (a job's bullets split across
   pages, or a sidebar list split across pages).
5. Renders the final paginated HTML against the solved placement.
6. Prints to PDF via Playwright/Chromium, crops to exact US Letter
   (8.5×11 in), stamps PDF metadata.
7. Pixel-diffs the result against a committed snapshot to catch
   accidental visual changes.

The output: `print.pdf`.

## Quickstart

### macOS / Linux

```bash
# One-time setup
pip install -r requirements.txt
npm install
npx playwright install chromium

# Build
node render.js
```

### Windows (cmd.exe)

```cmd
:: One-time setup
py -m pip install -r requirements.txt
npm install
npx playwright install chromium

:: Build
node render.js
```

### Windows (PowerShell)

```powershell
# One-time setup
py -m pip install -r requirements.txt
npm install
npx playwright install chromium

# Build
node render.js
```

The build script auto-detects the right Python interpreter on each
platform. If detection fails (rare), override it explicitly:

| Shell           | How to override                            |
|-----------------|--------------------------------------------|
| bash / zsh      | `PYTHON=python3.12 node render.js`         |
| cmd.exe         | `set PYTHON=py && node render.js`          |
| PowerShell      | `$env:PYTHON="py"; node render.js`         |

## Editing your resume

The committed `data/resume_default.yml` is a fictional placeholder (Gaius
Caesar, with Latin filler text). To use
your own content without committing it, copy it to a private
`data/resume.local.yml` (which is gitignored) and edit there:

```bash
# macOS / Linux
cp data/resume_default.yml data/resume.local.yml
node render.js
```

```cmd
:: Windows (cmd.exe)
copy data\resume_default.yml data\resume.local.yml
node render.js
```

```powershell
# Windows (PowerShell)
Copy-Item data\resume_default.yml data\resume.local.yml
node render.js
```

The build prefers `resume.local.yml` over `resume_default.yml` if both exist.

## What you can change in the YAML

- **Personal info** — `name.first`, `name.last`, plus an optional
  `role` (a short line under the name).
- **Header contact** — optional `contact` block: an `address` line and
  `rows`, each with a `value` and an optional `href` (e.g. `mailto:` or
  `tel:` link). Shown on the right of the page header.
- **Sidebar blocks** — add, remove, reorder under `sidebar.blocks`.
  Each block has a kebab-case `id` (must be unique), a `type`
  (`details` or `list`), a `heading`, and content: `rows` of
  `label`/`value` (optional `href`) for `details`, or `items` for
  `list`. A list item can be a `{group: "…"}` entry, which renders as a
  subheading inside the list (group entries are left out of the PDF
  keywords).
- **Main column sections** — exactly one each of `summary`,
  `experience`, `education`. Add/remove jobs under
  `mainColumn[experience].jobs`. Each job has a unique kebab-case
  `id`, a `title`, `date`/`datetime`, an optional `location`, and
  `bullets`; set `gap: true` (no bullets needed) for a career-gap entry.
- **Bullets** — add or remove freely under each job's `bullets:` list.
  The layout solver decides where page breaks land. Wrap text in
  `**bold**` to emphasize it (works in bullets and the summary text).
- **Page cap** — `meta.maxPages` (required; the placeholder uses 10). Lower it to force
  tighter layouts; the build fails clearly if content can't fit.
- **PDF metadata** — `meta.description` (required) becomes the PDF
  subject; optional `meta.lang` (default `en-US`) is stamped as the
  PDF's `/Lang`.

You do **not** need to manage page breaks manually. If you write 20
bullets across your jobs, the solver figures out where to break.

## What gets generated

Running `node render.js` writes:

- `dist/index.html` — the rendered HTML (final mode by default)
- `dist/styles.css` — compiled from `assets/styles/styles.scss` via Sass
- `dist/pdf_meta.json` — derived PDF metadata + data source identifier
- `dist/placement.json` — the solver's per-page placement decisions
- `print.pdf` — the cropped US Letter PDF

Everything in `dist/` plus `print.pdf` is gitignored.

## Project layout

```
.
├── assets/                    Source assets compiled into the build
│   ├── styles/                Sass source — compiled to dist/styles.css
│   │   ├── styles.scss        Entry point (@use's the partials)
│   │   ├── _tokens.scss       CSS custom properties
│   │   ├── _fonts.scss        @font-face declarations (vendored Montserrat)
│   │   ├── _base.scss         Reset + body defaults
│   │   ├── _layout.scss       Page container, body grid, divider, hrs
│   │   ├── _components.scss   Name header, headings, sidebar, jobs
│   │   ├── _print.scss        @media print overrides
│   │   └── _measurement.scss  body.measurement-mode overrides
│   └── fonts/                 Vendored Montserrat (canonical Google
│       │                      Fonts version, served from disk for
│       │                      reproducible builds — see "Why vendored"
│       │                      below)
│       ├── Montserrat-VariableFont_wght.woff2          Upright (100-900)
│       ├── Montserrat-Italic-VariableFont_wght.woff2  Italic (100-900)
│       └── OFL.txt            Font license (OFL-1.1)
├── data/                      YAML resume content
│   ├── resume_default.yml     Placeholder, Gaius Caesar (committed)
│   └── resume.local.yml       Real data (gitignored)
├── scripts/
│   ├── build.py               YAML → HTML, two modes (final, measurement)
│   ├── crop_pdf.py            Trim Chromium's oversized PDF to true Letter
│   ├── snapshot_pdf.py        Visual regression test
│   ├── detect_python.js       Cross-platform Python interpreter detection
│   ├── run_tests.js           Test runner (Python + Node)
│   ├── check_layout.js        Post-build layout invariant checks
│   ├── measure_dom.js         Playwright DOM measurement extractor
│   ├── solve_layout.js        Pure-function layout solver
│   ├── _console.py            Shared console-output helper (Python)
│   └── _console.js            Shared console-output helper (Node)
├── templates/
│   ├── resume.j2              Final paginated template
│   ├── measurement.j2         Single-page flowing template (solver input)
│   └── _macros.j2             Shared rendering macros
├── tests/                     Unit tests + visual regression fixtures
│   ├── README.md              How to run tests
│   ├── test_validate_data.py     YAML schema validation tests
│   ├── test_markdown_filter.py   Bullet markdown filter tests
│   ├── test_solve_layout.js      Layout solver tests
│   ├── test_check_layout.js      Layout invariants tests (Playwright)
│   └── fixtures/
│       ├── expected_print.pdf       Snapshot for the placeholder data
│       ├── expected_print.local.pdf Snapshot for local data (gitignored)
│       └── diff_page*.png           Generated on snapshot failure
│                                    (gitignored)
├── render.js                  Build orchestrator (11-phase pipeline)
├── package.json               Node dependencies (playwright, sass)
├── requirements.txt           Python dependencies
├── CHANGELOG.md               Release history
└── LICENSE                    MIT
```

## Pipeline steps (`node render.js`)

```
0. Run all unit tests (Python + JS)
1. Compile Sass (assets/styles/ → dist/styles.css)
2. Build measurement HTML
3. Open it in Playwright; extract DOM measurements
4. Solve the layout; write dist/placement.json
5. Build final HTML against the placement
6. Reload the final HTML
7. Check layout invariants (page count, divider, rhythm, overflow)
8. Print to PDF
9. Crop to US Letter and stamp metadata
10. Snapshot test (auto-bootstraps fixture on first build)
```

Steps 0, 7, and 10 act as gates — the build stops if any of them fail.
This is what catches the case where the solver produces a placement
that doesn't actually fit (which would otherwise silently clip
content under `overflow: hidden`).

## Testing

See `tests/README.md` for the full guide. Quick summary:

```bash
# All tests (Python unittest + Node test files) — works everywhere
node scripts/run_tests.js

# A single test file
node scripts/run_tests.js test_validate_data
node scripts/run_tests.js test_solve_layout
```

To refresh the snapshot fixture after an intentional change:

```bash
# macOS / Linux
python scripts/snapshot_pdf.py --update            # current data source
python scripts/snapshot_pdf.py --update-both       # both placeholder and local
```

```cmd
:: Windows
py scripts\snapshot_pdf.py --update                :: current data source
py scripts\snapshot_pdf.py --update-both           :: both placeholder and local
```

## Configuration

Environment variables that affect the build:

- `PYTHON` — explicit Python interpreter (overrides auto-detection)
- `RESUME_DATA_SOURCE=default|local` — force which data file to use,
  ignoring the local-preferred-over-default logic. Used by
  `--update-both` to refresh both fixtures in one run.
- `STRICT_TESTS=1` — convert SKIP'd test suites into hard failures.
  By default a skipped suite (e.g. `test_check_layout` when the
  Playwright browser binary is missing) prints a yellow warning and
  the runner exits 0. With `STRICT_TESTS=1` set, the runner exits 1
  instead. Use this in CI to catch silently-bypassed test suites.
- `DEBUG_MEASUREMENTS=1` — dump the solver's input measurements and
  the final rendered column heights (developer diagnostic).
- `SKIP_SNAPSHOT=1` — skip the visual regression test (used internally
  by `snapshot_pdf.py --update-both`).
- `NO_COLOR=1` / `FORCE_COLOR=1` — control ANSI output (default:
  auto-detect from TTY).

## Requirements

- **Python** 3.10+ (tested on 3.10 and 3.12)
- **Node** 18+ (for Playwright)
- **Chromium** (installed via `npx playwright install chromium`)

## Why this architecture

The naive approach to a YAML-driven resume — flow content into a
fixed page, let the browser decide where to break — produces
unpredictable output across browsers, font versions, and rendering
quirks. The output of two builds on different machines wouldn't match.

This pipeline takes a different path:

1. **Each `<article class="page">` is a fixed 8.5×11 in box.** The
   screen layout IS the print layout. There is no native pagination;
   `@page` margins are zero, and Chromium just prints what it sees.
2. **A solver decides placement deterministically.** Given identical
   measurements (which Playwright produces consistently for a given
   font and CSS), the placement is identical.
3. **Multiple invariants prevent silent breakage.** The column
   divider must terminate at the page bottom margin; the section
   rhythm must be equal across columns; page count must match the
   solver's decision; no descendant can overflow its page's content
   area. Any failure aborts the build with a specific error.
4. **A pixel-diff snapshot test catches visual regressions.** Even
   if every invariant passes, the snapshot test will flag
   intentional or accidental visual changes.

The result: editing the YAML and re-running produces the same PDF
on any machine, and any visible change is caught by a test.

## Why vendored Montserrat

The project ships its own Montserrat font files under `assets/fonts/`
instead of loading from Google Fonts CDN. This isn't decorative — it
fixes a real reproducibility bug.

When the project loaded fonts from the CDN, three things could
silently change the rendered output:

1. **System-installed Montserrat overriding the web font.** If a
   contributor's OS had Montserrat installed locally, Chromium
   sometimes preferred the system version over the CDN-served file.
   Different system Montserrat versions render glyphs at slightly
   different widths — enough to change line-wrap decisions and
   therefore the layout solver's output.
2. **Different npm mirrors of Montserrat.** An earlier setup that
   loaded Montserrat from `@fontsource/montserrat` shipped subtly
   different outline files than the canonical Google Fonts version,
   even though their metrics tables matched. A 14.6% width divergence
   was measured on the same string between the two.
3. **CDN URL drift.** Google's `fonts.gstatic.com` woff2 hashes
   change when fonts are re-versioned. Pinning a specific URL would
   eventually 404; using the stable CSS endpoint would silently
   serve a different glyph file when Google updated the version.

The vendored woff2 files are the canonical Google Fonts release,
loaded directly from disk via `@font-face url('../assets/fonts/...')`
(relative to `dist/styles.css`). No
`local()` source is declared, so system Montserrat never overrides
the web font. The two woff2 files (~430 KB total) cover all weights
via the variable-font `wght` axis.

The Google Fonts `<link>` tag is still present in the templates as a
graceful-degradation fallback: if `assets/fonts/` is missing for any
reason, the browser falls through to the CDN. That fallback isn't
guaranteed to produce a matching layout (the system-Montserrat
override risk reappears), but it produces a readable PDF instead of
a fallback-Helvetica disaster.

## FAQ / common gotchas

**The build hangs at "Chromium launch" or fails with an install message.**
You probably haven't installed Playwright's browser binary. Run
`npx playwright install chromium` once. Subsequent builds reuse the
cached binary in `~/.cache/ms-playwright/` (Linux) or the platform
equivalent.

**The build complains about Python — "Microsoft Store alias" on Windows.**
Stock Windows aliases `python` and `python3` to a Microsoft Store
installer stub that exits non-zero. Either install real Python from
python.org and run `py` (the launcher), or set `PYTHON=py` explicitly:
`set PYTHON=py && node render.js` in cmd, or
`$env:PYTHON="py"; node render.js` in PowerShell.

**The snapshot test fails after a CSS edit and the diff image looks correct.**
That's the snapshot test doing its job: any visible change, intentional
or not, fires it. If your change is intentional, refresh the fixture
with `py scripts/snapshot_pdf.py --update`. The test will pass on the
next build.

**`--update` only updates one fixture, not both.**
By design. The snapshot tool reads `dist/pdf_meta.json` to learn which
data file (`resume_default.yml` or `resume.local.yml`) drove the most recent
build, and updates the matching fixture. If you want both refreshed in
one go, use `py scripts/snapshot_pdf.py --update-both` — it runs the
full pipeline twice with each data source forced.

**The build fails with "content-overflow" on a page.**
The solver produced a placement that doesn't actually fit. This usually
means a measurement is off — frequently due to a CSS change that altered
spacing unintentionally. The error message includes the offending
column, page, and culprit element id. If you can't find an obvious
cause, file a bug or report — this should not happen with reasonable
content; if it does, the solver or `measure_dom.js` has a bug.

**The build fails with "exceeds maxPages".**
Your content doesn't fit in the configured cap. Either increase
`meta.maxPages` in the YAML (the placeholder sets 10) or trim content. The error
identifies which column ran out of pages.

**`pip install -r requirements.txt` fails to install Pillow on a new Python version.**
The pin is `Pillow==10.3.0`, which doesn't have prebuilt wheels for very
new Python versions (3.13+). Bump the pin to a newer version (12.x is
fine), run `node render.js`, and refresh fixtures if anything changed
visually (it usually doesn't — Pillow upgrades rarely affect rasterization).

**I edited the YAML and the build silently dropped a section / job.**
Schema validation should catch every malformed entry with a clear
error. If you're seeing silent drops, please report — `validate_data`
in `build.py` is strict by design and shouldn't allow that.

**The solver placed something I disagree with — can I override?**
Not currently. The solver decides placement based on heights and the
fixed rules (header + ≥1 bullet on origin for jobs, heading + ≥3 items
for sidebar lists). If you want a different placement, edit content
to change the heights involved, or trim until the solver chooses what
you want. Future versions may add `splitAfter:` overrides on jobs but
this isn't currently implemented and adds complexity.

**The fonts look wrong on first build.**
Montserrat is vendored under `assets/fonts/` and loaded from disk —
no internet required at build time. If your render still produces
the wrong output, check that `assets/fonts/Montserrat-VariableFont_wght.woff2`
exists. The templates still link the Google Fonts CDN, but only as a
fallback for when the vendored files are missing; the build does not
rely on it (see "Why vendored Montserrat" above).

**Where do I put real resume data without committing it?**
Put it in `data/resume.local.yml`. That file is gitignored; the build
prefers it over `data/resume_default.yml` when both exist. Your private snapshot
fixture is `tests/fixtures/expected_print.local.pdf` (also gitignored).

## License

MIT. See [`LICENSE`](LICENSE).
