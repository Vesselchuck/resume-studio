# Resume

Version 0.5.1. See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

Single-source-of-truth resume pipeline. Edit YAML, get a pixel-faithful
US Letter PDF in two variants (full-color and grayscale). Page
placement is computed automatically — no manual page-break management.
Multi-page resumes get a "Page N of M" footer at the bottom right of
each page.

## What it does

1. Reads resume content from `data/resume_default.yml` (or
   `data/resume.local.yml` if present, for private data).
2. Validates the schema — clear errors for missing fields, duplicate
   ids, malformed bullets, etc.
3. Renders a measurement-mode HTML page (everything in one flowing
   column) so a layout solver can read actual rendered heights.
4. Solves the layout: decides which sidebar blocks and which jobs go
   on which page, including bridging (a job's bullets split across
   pages, or a sidebar list split across pages).
5. Renders the final paginated HTML against the solved placement.
6. Prints a color and a grayscale PDF via Playwright/Chromium, crops
   each to exact US Letter (8.5×11 in), and stamps PDF metadata and
   language for accessibility.
7. Pixel-diffs each variant against a committed snapshot to catch
   accidental visual changes.

The outputs: `dist/resume-color.pdf` and `dist/resume-grayscale.pdf`.

## Quickstart

### macOS / Linux

```bash
# One-time setup
pip install -r requirements.txt
npm ci
npx playwright install chromium

# Build
node render.js
```

### Windows (cmd.exe)

```cmd
:: One-time setup
py -m pip install -r requirements.txt
npm ci
npx playwright install chromium

:: Build
node render.js
```

### Windows (PowerShell)

```powershell
# One-time setup
py -m pip install -r requirements.txt
npm ci
npx playwright install chromium

# Build
node render.js
```

`npm ci` (not `npm install`) is the recommended installer — both
`package.json` and `requirements.txt` exact-pin every dependency, and
`npm ci` reproduces the lockfile exactly without drift.

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
The PDF metadata manifest records which data source was used, so the
snapshot test picks the matching fixtures.

## What you can change in the YAML

- **Personal info** — `name.first`, `name.last`, an optional `role`
  line under the name, and an optional `contact` block (`address` plus
  `rows` of `value` and optional `href`) shown in the page header.
- **Sidebar blocks** — add, remove, reorder under `sidebar.blocks`.
  Each block has a kebab-case `id` (must be unique), a `type`
  (`details` or `list`), a `heading`, and content (`rows` for
  `details`, `items` for `list`). `list` items can include
  `- group: "..."` entries, shown as subheadings. The first block
  with `id: key-skills` (or heading "Key Skills") drives the PDF's
  /Keywords metadata.
- **Main column sections** — exactly one each of `summary`,
  `experience`, `education`. Add/remove jobs under
  `mainColumn[experience].jobs`. A job with `gap: true` (and no
  `location` or `bullets`) marks a non-employment period.
- **Bullets** — add or remove freely under each job's `bullets:` list.
  Bullets (and the summary text) support `**bold**` markdown; everything else is treated as
  plain text. The layout solver decides where page breaks land.
- **Page cap** — `meta.maxPages: 10` is the default. Lower it to force
  tighter layouts; the build fails clearly if content can't fit.
- **Language** — `meta.lang: en-US` (BCP-47). Drives the document's
  `<html lang>` and the PDF's `/Lang` catalog entry.

You do **not** need to manage page breaks manually. If you write 20
bullets across your jobs, the solver figures out where to break.

## What gets generated

Running `node render.js` writes everything to `dist/`:

- `dist/index.html` — the rendered HTML (final mode by default)
- `dist/styles.css` — compiled from `styles/styles.scss` via Sass
- `dist/favicon.svg` — favicon with the initials in the accent color
- `dist/pdf_meta.json` — derived PDF metadata + data source identifier
- `dist/placement.json` — the solver's per-page placement decisions
- `dist/resume-color.pdf` — final color PDF (US Letter)
- `dist/resume-grayscale.pdf` — final grayscale PDF (US Letter)

All of `dist/` is gitignored.

## Project layout

```
.
├── .gitignore                Ignores dist/, node_modules/, __pycache__/,
│                             tests/fixtures/diff_*.png, and the local
│                             data file + its private fixtures.
├── LICENSE                   MIT license for project code (font files
│                             under fonts/ are OFL 1.1 — see License section).
├── CHANGELOG.md              Release history.
├── README.md                 This file.
├── package.json              Node dependencies (playwright, sass).
├── package-lock.json         Exact-pinned lockfile for npm ci.
├── render.js                 Build orchestrator (11-phase pipeline, 0–10).
├── requirements.txt          Python dependencies (exact-pinned).
├── data/
│   ├── resume_default.yml    Placeholder data (committed).
│   └── resume.local.yml      Real data (gitignored; absent until you
│                             create it — see "Editing your resume").
├── styles/                   Sass source — compiled to dist/styles.css.
│   ├── styles.scss           Entry point (@use's the partials).
│   ├── _tokens.scss          CSS custom properties (geometry, colors).
│   ├── _fonts.scss           @font-face declarations.
│   ├── _base.scss            Reset + body defaults.
│   ├── _layout.scss          Page container, body grid, divider, hrs.
│   ├── _components.scss      Name header, headings, sidebar, jobs.
│   ├── _print.scss           @media print overrides.
│   └── _measurement.scss     body.measurement-mode overrides.
├── fonts/                    Vendored variable WOFF2 fonts.
│   ├── Manrope.woff2         Body text (variable wght 100–900).
│   ├── Manrope-OFL.txt       SIL OFL 1.1 license (required to keep).
│   ├── Newsreader.woff2      Display text (variable wght 200–800).
│   └── Newsreader-OFL.txt    SIL OFL 1.1 license (required to keep).
├── templates/
│   ├── resume.j2             Final paginated template.
│   ├── measurement.j2        Single-page flowing template (solver input).
│   └── _macros.j2            Shared rendering macros.
├── build/                    Build pipeline (Python + Node modules).
│   ├── build.py              YAML → HTML (modes: final, measurement).
│   ├── crop_pdf.py           Trim Chromium's PDF to true US Letter.
│   ├── snapshot_pdf.py       Visual regression test.
│   ├── solve_layout.js       Pure-function layout solver.
│   ├── measure_dom.js        Playwright DOM measurement extractor.
│   ├── check_layout.js       Post-build layout invariant checks.
│   ├── run_tests.js          Test runner (Python + Node).
│   ├── detect_python.js      Cross-platform Python interpreter detect.
│   ├── _constants.json       Single source for cross-language constants.
│   ├── _console.{py,js}      Shared console-output helpers (read _constants.json).
│   └── _env_contract.{py,js} Shared environment-variable names (read _constants.json).
└── tests/                    Unit tests + visual regression fixtures.
    ├── _framework.js                    Tiny JS test harness.
    ├── test_validate_data.py            YAML schema validation.
    ├── test_load_data.py                Data loader + env-var precedence.
    ├── test_markdown_filter.py          Bullet markdown filter.
    ├── test_derive_pdf_metadata.py      PDF metadata derivation.
    ├── test_read_accent.py              Accent-color extractor.
    ├── test_crop_pdf.py                 PDF cropping + /Lang stamping.
    ├── test_solve_layout.js             Layout solver.
    ├── test_check_layout.js             Layout invariants (Playwright).
    └── fixtures/                        Snapshot fixtures; a missing one is
                                         created on the next build.
        ├── expected_resume-color.pdf            Snapshot (placeholder data, committed).
        ├── expected_resume-grayscale.pdf        Snapshot (placeholder data, committed).
        ├── expected_resume-color.local.pdf      Snapshot (local data, gitignored).
        ├── expected_resume-grayscale.local.pdf  Snapshot (local data, gitignored).
        └── diff_*_pageN.png                     Generated on failure (gitignored).
```

## Pipeline steps (`node render.js`)

```
0.  Run all unit tests (Python + JS)
1.  Compile Sass (styles/ → dist/styles.css)
2.  Build measurement HTML
3.  Open it in Playwright; extract DOM measurements
4.  Solve the layout; write dist/placement.json
5.  Build final HTML against the placement
6.  Reload the final HTML
7.  Check layout invariants (page count, divider, rhythm, overflow)
8.  Print to PDF (color, then grayscale)
9.  Crop each PDF to US Letter, stamp metadata + /Lang
10. Snapshot test (auto-bootstraps missing fixtures on first build)
```

Steps 0, 7, and 10 act as gates — the build stops if any of them fail.
Step 7 in particular catches the case where the solver produces a
placement that doesn't actually fit (which would otherwise silently
clip content under `overflow: hidden`).

## Testing

Two kinds of verification: unit tests for the project's logic, and a
slow visual-regression snapshot test for the rendered PDFs.

### Unit tests

Python and JavaScript test files live in `tests/`. Seven are fast
pure-logic tests (sub-second total); one launches Chromium for
in-browser DOM assertions.

| Test                          | What it covers                              |
|-------------------------------|---------------------------------------------|
| `test_validate_data.py`       | YAML schema validation in `build.py`        |
| `test_load_data.py`           | Data loader + `RESUME_DATA_SOURCE` env var  |
| `test_markdown_filter.py`     | The `**bold**` filter for bullet text       |
| `test_derive_pdf_metadata.py` | PDF metadata derivation from YAML           |
| `test_read_accent.py`         | Accent color parsing from `_tokens.scss`    |
| `test_crop_pdf.py`            | PDF cropping, metadata, and `/Lang`         |
| `test_solve_layout.js`        | The layout solver (`build/solve_layout.js`) |
| `test_check_layout.js`        | Layout invariants in a real browser         |

`test_check_layout.js` skips automatically if Chromium isn't available,
so a fresh checkout without `npx playwright install` still gets
coverage from the other seven tests.

Run all of them via the cross-platform runner:

```
node build/run_tests.js
```

The runner invokes Python's `unittest discover` for `tests/test_*.py`
and runs each `tests/test_*.js` directly with Node. It picks the right
Python interpreter for the platform automatically (override with the
`PYTHON` env var if needed).

#### Running a single test

```
node build/run_tests.js test_validate_data            # Python module
node build/run_tests.js test_solve_layout             # JavaScript file
node build/run_tests.js test_validate_data.TestValidateData.test_good_data_passes
```

The runner picks the right runtime by file existence — if
`tests/test_<name>.js` exists, it runs that as Node; otherwise it
treats the argument as a Python `unittest` dotted path.

#### Running tests without going through the full build

The build pipeline (`node render.js`) runs all unit tests as step 0,
so any failure aborts the build. During TDD or quick iteration, skip
the build and run the tests directly:

```
node build/run_tests.js
```

### Snapshot test

`build/snapshot_pdf.py` lives outside `tests/` so unittest discovery
doesn't try to import its heavy dependencies (`pypdfium2`, `Pillow`).
It runs automatically as part of `node render.js` (step 10). It
rasterizes the freshly-built `dist/resume-color.pdf` and
`dist/resume-grayscale.pdf`, compares each page-by-page to a committed
fixture, and fails the build if visible pixels changed beyond the
configured tolerance in either variant.

#### Four fixtures: two variants × two data sources

Render produces two PDFs per build (color and grayscale). The build
can use either of two data files (`resume_default.yml` or `resume.local.yml`).
The snapshot tool reads `dist/pdf_meta.json` (written by `build.py`)
to learn which file backed the most recent build, and picks the
matching fixture for each variant:

| Data source | Variant   | Fixture                                              | In git? |
|-------------|-----------|------------------------------------------------------|---------|
| `default`   | color     | `tests/fixtures/expected_resume-color.pdf`           | yes     |
| `default`   | grayscale | `tests/fixtures/expected_resume-grayscale.pdf`       | yes     |
| `local`     | color     | `tests/fixtures/expected_resume-color.local.pdf`     | no      |
| `local`     | grayscale | `tests/fixtures/expected_resume-grayscale.local.pdf` | no      |

Each fixture matches the data file that produced it. There is no
"shared" fixture — that would mean comparing one data set's render
against another's pixels, which is meaningless.

#### First build (no fixtures yet)

```
node render.js
```

The snapshot step auto-bootstraps any missing fixture from the current
`dist/resume-*.pdf` and prints a notice. Subsequent builds diff against
the fixtures. Inspect the PDFs visually before committing them.

#### Refreshing after an intentional change

After tweaking CSS, content, or layout in a way that visibly changes
the output:

```bash
# macOS / Linux
python build/snapshot_pdf.py --update          # current data source only
python build/snapshot_pdf.py --update-all      # both default and local
```

```cmd
:: Windows
py build\snapshot_pdf.py --update              :: current data source only
py build\snapshot_pdf.py --update-all          :: both default and local
```

`--update` refreshes BOTH variants (color + grayscale) of the fixture
matching the current data source. `--update-all` runs the full build
pipeline twice (once with default data, once with local data if
`data/resume.local.yml` exists), refreshing all four fixtures. The
intermediate snapshot checks are skipped via `SKIP_SNAPSHOT=1` so the
existing about-to-be-replaced fixtures don't fail the build.

Commit the refreshed `expected_resume-color.pdf` and
`expected_resume-grayscale.pdf` alongside whatever change caused them.
The local fixtures stay gitignored — they live only on your machine.

#### Running snapshot test on its own

```
py build/snapshot_pdf.py
```

Useful when iterating on tolerances or inspecting a regression without
rebuilding. Requires both `dist/resume-color.pdf` and
`dist/resume-grayscale.pdf` to already exist.

## Configuration

Environment variables that affect the build:

| Variable                   | Purpose                                                |
|----------------------------|--------------------------------------------------------|
| `PYTHON`                   | Explicit Python interpreter (overrides auto-detect).   |
| `RESUME_DATA_SOURCE`       | `default` or `local` — force which data file to use,   |
|                            | ignoring the local-preferred-over-default logic. Used  |
|                            | internally by `--update-all`.                          |
| `STRICT_TESTS=1`           | Convert SKIP'd test suites into hard failures. Without |
|                            | it, a skipped suite (e.g. `test_check_layout` when the |
|                            | Playwright browser binary is missing) prints a yellow  |
|                            | warning and the runner exits 0. With `STRICT_TESTS=1`  |
|                            | the runner exits 1. Use in CI to catch silently-       |
|                            | bypassed suites.                                       |
| `DEBUG_MEASUREMENTS=1`     | Dump the solver's input measurements and the final     |
|                            | rendered column heights (developer diagnostic).        |
| `SKIP_SNAPSHOT=1`          | Skip the visual regression step (used internally by    |
|                            | `--update-all`).                                       |
| `RESUME_PIPELINE_SUFFIX`   | Label appended to the first phase heading during       |
|                            | `--update-all` so you can see which data source is     |
|                            | being processed.                                       |
| `NO_COLOR` / `FORCE_COLOR` | Control ANSI output (default: auto-detect from TTY).   |

## Requirements

- **Python** 3.10+ (tested on 3.10 and 3.12).
- **Node** 18+ (for Playwright 1.60).
- **Chromium** (installed via `npx playwright install chromium`).
- Dependencies are exact-pinned in `requirements.txt` and
  `package.json`. Use `npm ci` (not `npm install`) to reproduce the
  lockfile exactly.

## Why this architecture

The naive approach to a YAML-driven resume — flow content into a fixed
page, let the browser decide where to break — produces unpredictable
output across browsers, font versions, and rendering quirks. Two
builds on different machines wouldn't match.

This pipeline takes a different path:

1. **Each `<article class="page">` is a fixed 8.5×11 in box.** The
   screen layout IS the print layout. There is no native pagination;
   `@page` margins are zero, and Chromium prints what it sees.
2. **A solver decides placement deterministically.** Given identical
   measurements (which Playwright produces consistently for a given
   font and CSS), the placement is identical.
3. **Multiple invariants prevent silent breakage.** The column
   divider must terminate at the page bottom margin; the section
   rhythm must be equal across columns; page count must match the
   solver's decision; no descendant can overflow its page's content
   area. Any failure aborts the build with a specific error.
4. **A pixel-diff snapshot test catches visual regressions.** Even
   if every invariant passes, the snapshot test fires on any
   intentional or accidental visual change.

The result: editing the YAML and re-running produces the same PDFs on
any machine, and any visible change is caught by a test.

## Why vendored fonts

The project ships its own Manrope and Newsreader font files under
`fonts/` instead of loading from Google Fonts CDN. This isn't
decorative — it fixes a real reproducibility problem.

When a project loads fonts from a CDN, three things can silently
change the rendered output:

1. **System-installed font overriding the web font.** If a contributor's
   OS has Manrope or Newsreader installed locally, Chromium sometimes
   prefers the system version over the CDN-served file. Different
   system font versions render glyphs at slightly different widths —
   enough to change line-wrap decisions and therefore the solver's
   output.
2. **Different npm or CDN mirrors of the same font.** Distributions
   that bill themselves as "the same font" can ship subtly different
   outline files, even when their metrics tables match.
3. **CDN URL drift.** Google's `fonts.gstatic.com` woff2 hashes change
   when fonts are re-versioned. Pinning a specific URL would eventually
   404; the stable CSS endpoint silently serves a different glyph file
   when Google updates the version.

The vendored woff2 files are the canonical Google Fonts release of
each face. They're loaded directly from disk via
`@font-face url('../fonts/...')` with no `local()` source, so a
system-installed font of the same name can't take over. Each is a
variable WOFF2 covering its full weight range; total weight on disk
is well under 1 MB.

The OFL license texts (`Manrope-OFL.txt`, `Newsreader-OFL.txt`) sit
alongside the woff2 binaries. Keep them. Removing them while keeping
the woff2 files would put the project in violation of the SIL Open
Font License, which requires the license travel with the font.

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
That's the snapshot test doing its job — any visible change, intentional
or not, fires it. If your change is intentional, refresh the fixtures
with `python build/snapshot_pdf.py --update` (or `--update-all` for
both data sources). The test will pass on the next build.

**`--update` only updates two fixtures, not all four.**
By design. The snapshot tool reads `dist/pdf_meta.json` to learn which
data file (`resume_default.yml` or `resume.local.yml`) drove the most recent
build, and updates both variant fixtures (color + grayscale) for that
data source. If you want all four refreshed in one go, use
`--update-all` — it runs the full pipeline twice with each data source
forced.

**The build fails with "content-overflow" on a page.**
The solver produced a placement that doesn't actually fit. This usually
means a measurement is off — frequently due to a CSS change that
altered spacing without obvious indication. The error message includes
the offending column, page, and culprit element id. If you can't find
an obvious cause, the solver or `measure_dom.js` has a bug; this
should not happen with reasonable content.

**The build fails with "exceeds maxPages".**
Your content doesn't fit in the configured cap. Either increase
`meta.maxPages` in the YAML (default is 10) or trim content. The error
identifies which column ran out of pages.

**`pip install -r requirements.txt` fails to install Pillow on a new Python version.**
The pin is `Pillow==10.3.0`, which doesn't have prebuilt wheels for
very new Python versions (3.13+). Bump the pin in `requirements.txt`
to a newer version (12.x is fine), run `node render.js`, and refresh
fixtures if anything changed visually (it usually doesn't — Pillow
upgrades rarely affect rasterization).

**I edited the YAML and the build silently dropped a section / job.**
Schema validation should catch every malformed entry with a clear
error. If you're seeing silent drops, please report — `validate_data`
in `build/build.py` is strict by design and shouldn't allow that.

**The solver placed something I disagree with — can I override?**
Not currently. The solver decides placement based on measured heights
and fixed rules (header + ≥1 bullet on origin for jobs, heading + ≥3
items for sidebar lists). If you want a different placement, edit
content to change the heights involved, or trim until the solver
chooses what you want.

**The fonts look wrong on first build.**
Manrope and Newsreader are vendored under `fonts/` and loaded from
disk — no internet required at build time. If your render still
produces the wrong output, check that the woff2 files exist and that
the templates haven't been edited to reference an external font URL.
The build deliberately does NOT use Google Fonts CDN (see "Why
vendored fonts" above).

**Where do I put real resume data without committing it?**
Put it in `data/resume.local.yml`. That file is gitignored; the build
prefers it over `data/resume_default.yml` when both exist. Your private
snapshot fixtures (`expected_resume-color.local.pdf` and
`expected_resume-grayscale.local.pdf`) are also gitignored.

**Why `npm ci` and not `npm install`?**
`npm ci` installs exactly what `package-lock.json` specifies and fails
fast if `package.json` and the lockfile disagree. `npm install` is the
wrong tool for reproducible builds — even with exact-pinned direct
dependencies, some npm versions can resolve newer transitive deps in
ways that drift from the committed lockfile.

## License

MIT. See [`LICENSE`](LICENSE).

The vendored fonts under `fonts/` are licensed under the SIL Open Font
License 1.1; see `Manrope-OFL.txt` and `Newsreader-OFL.txt`.
