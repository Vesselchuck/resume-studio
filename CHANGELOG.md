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
2. **The commands** — what you run: npm scripts, `node …` and
   `python …` invocations, the `.bat` launchers.
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

## [0.7.1] — 2026-09-21

*The Studio no longer loses a save made during a render, shows each
save sooner, and escapes dropped file names. Nothing you rely on
changed.*

- **Added.** `SECURITY.md`: how to report a vulnerability privately,
  through GitHub's private vulnerability reporting.
- **Fixed.** A file saved while the Studio was still rendering the
  previous save was ignored, so the preview could show an older version
  until the next save. The Studio now renders once more as soon as the
  current render finishes.
- **Changed.** The Studio shows a saved change sooner. It waits 75 ms
  after a save instead of 250 ms before rendering, encodes the page
  images side by side, and does not re-encode or re-send a page whose
  pixels did not change — editing page 1 of the resume leaves page 2's
  image as it is. In testing, the time from saving the resume to seeing
  the updated page went from about 1.0 s to about 0.8 s.
- **Security.** Dropping a file whose name contains HTML no longer
  inserts that markup into the "already exists" dialog. The two buttons
  that repeat the file name now escape it, as the rest of the app
  already did. Found by GitHub code scanning (CodeQL).

---

## [0.7.0] — 2026-09-21

*Breaking: the Studio is now the only way to build. `build.bat`,
`ui.bat` and the `resume`, `build` and `letter` npm scripts are gone.
Styles compile faster and the Studio opens sooner.*

### Upgrading from 0.6.x

1. Run `npm ci`. The Sass compiler changed package.
2. Build in the Studio: double-click `studio.bat`, or run
   `npm run studio` (`npm run ui` for the browser version).
3. If you ran `npm run resume` or `npm run letter` in scripts of your
   own, run `node resume.js` or `node letter.js` instead.

### Changes

- **Removed — Breaking.** Every way to build other than the Studio:
  `build.bat`, `ui.bat`, and the npm scripts `resume`, `build` and
  `letter`. Open the Studio with `studio.bat` or `npm run studio`
  (`npm run ui` still opens it in a browser). For debugging, the build
  scripts the Studio runs can still be run directly:
  `node resume.js` and `node letter.js`.
- **Changed.** The README is reorganized around the Studio: setup, then
  opening and using the app, with the command line moved to a
  "Debugging from the command line" section.
- **Changed.** Styles are compiled with `sass-embedded`, the native build
  of the Dart Sass compiler, instead of the pure-JavaScript `sass`
  package. The CSS is identical. A build spends about half as long on
  Sass, and in Studio a style change recompiles in about 10 ms instead of
  about 55 ms, because the compiler stays running between changes.
  Run `npm ci` after pulling this change.
- **Changed.** Studio starts its Python worker, Chromium and the Sass
  compiler at the same time when it opens, and compiles the stylesheet
  before the first preview needs it. The first preview appears about
  0.4 s sooner.

---

## [0.6.1] — 2026-09-21

*Security updates for two Python packages, tests that run on GitHub,
and README screenshots for the dark theme. Nothing you rely on
changed.*

- **Added.** Tests run on GitHub (GitHub Actions) on every push to `main`
  and every pull request: the workflow builds the resume and the cover
  letter from the shipped templates and runs `npm test`. The README
  shows the result as a badge, next to the latest release and the
  license.
- **Changed.** The README's screenshots of the documents, of color and
  grayscale pages, and of the Studio with the cover letter selected now
  switch with GitHub's light and dark theme, as the main Studio
  screenshot already did.
- **Security.** pypdf, which crops the PDFs and writes their metadata,
  is updated from 5.9.0 to 6.16.1 for its security fixes. The PDFs it
  produces are unchanged.
- **Security.** Pillow, which the snapshot test and the Studio preview
  use to compare page images, is updated from 10.3.0 to 12.3.0 for its
  security fixes. Comparisons give the same results as before.

---

## [0.6.0] — 2026-09-20

*Breaking: `node render.js` is gone, your data file and the
`RESUME_DATA_SOURCE` values were renamed, YAML is read with different
typing rules, and the PDFs have new names. All four parts of the
interface changed.*

### Upgrading from 0.5.x

1. Build with `npm run resume` (or `node resume.js`) instead of
   `node render.js`.
2. Rename your data file: `data/resume.local.yml` → `data/resume.yml`.
   The shipped placeholder keeps its name, `data/resume_default.yml`.
3. Change `RESUME_DATA_SOURCE=local` to `RESUME_DATA_SOURCE=mine`.
4. Look for your PDFs under their new names:
   `dist/<First>_<Last>_Resume.pdf` and
   `dist/<First>_<Last>_Resume_Grayscale.pdf`.
5. Write booleans as `true`/`false`. `yes`, `no`, `on` and `off` are now
   plain strings.
6. If you relied on the snapshot test failing the build, set
   `RESUME_SNAPSHOT=strict`. It is now off unless you turn it on.

### Commands

- **Removed — Breaking.** `render.js`, the only build command in 0.5.x.
  `resume.js` replaces it and keeps the same phases. The phases now
  live in `build/pipeline.js`, which `resume.js` drives.
- **Added.** npm scripts. There were none before:

  | script | runs |
  | --- | --- |
  | `resume`, `build` | `node resume.js` |
  | `letter` | `node letter.js` |
  | `test` | `node build/run_tests.js` |
  | `ui` | Resume Studio in your browser |
  | `ui:serve` | the Studio server without opening a browser |
  | `studio` | Resume Studio as a desktop app (`tauri dev`) |
  | `studio:build` | the desktop installer (`tauri build`) |

- **Added.** Windows launchers. `build.bat` shows a menu (resume, cover
  letter, or both) and also takes `resume`, `letter` or `both` as an
  argument. `studio.bat` runs `npm run studio` and `ui.bat` runs
  `npm run ui`.

### Cover letter

- **Added.** A one-page cover letter, built with `npm run letter`. It
  reuses the resume's header, top rule, page sheet, type and color
  tokens, and its color/grayscale printing, so the two read as a set.
  There is no layout solver because a letter is one flowing column.
  The files are `letter.js`, `build/build_letter.py`,
  `templates/letter.j2` and `styles/_letter.scss`.
- **Added.** Letter data lives in `data/letter.yml` (yours, gitignored),
  with `data/letter_default.yml` as the shipped template. It has two
  fields:
  - `letter.body` is the whole letter, greeting and sign-off included.
    Write it as one block, with blank lines between paragraphs, or as a
    list with one paragraph per entry.
  - `letter.recipient` is the address block. Write it as a pasted block
    or as a list. A block splits on every line, not on blank lines, and
    blank lines are dropped.
- **Added.** The date is stamped when the letter is built. How it is
  written depends on `meta.lang`:
  - `September 20, 2026` for `en-US`
  - `20 September 2026` for day-first English: `en-GB`, `en-AU`,
    `en-IE`, `en-IN` and others
  - ISO `2026-09-20` for any other language

  The month names are written out in the code rather than read from
  the system locale, so the same file produces the same letter on any
  machine. The HTML also carries the date as
  `<time datetime="2026-09-20">`.
- **Added.** The name under the sign-off is filled in from `name`, which
  the shared profile provides.
- **Added.** The build fails if the letter does not fit on one page, and
  says roughly how many lines too long it is. `.page` hides whatever
  overflows it, so without this check an over-long letter would lose
  its sign-off without any warning.
- **Added.** Letter typography:
  - The prose is limited to 6.5in, about 85 characters per line.
  - The greeting and sign-off are set in the scaffolding type, 10pt at
    weights 500 and 400, against the 11pt/350 body.
  - Gaps: 16px after the greeting, 22px before the sign-off, and 27px
    between the date and the address.
  - `hyphens: none`, `text-wrap: pretty`, and at least two lines kept
    together at a page break.
  - The signature is never the first line on a page.

  All of this is set for the letter only. The resume's layout solver
  measures rendered line heights, so changing how the resume breaks
  lines would change its page layout and its snapshot fixtures.
- **Changed.** Earlier development versions of the letter had `date`,
  `salutation`, `closing` and `signature` fields. A file that still has
  one is rejected with an error that says what to write instead.
  Ignoring the field would drop that line from the letter without
  saying so.

### Resume Studio

- **Added.** Resume Studio shows the printed PDF and updates it while
  you edit the YAML in your own editor. It runs in a browser
  (`npm run ui`) or as a Tauri desktop app (`npm run studio`), and the
  desktop window opens maximized. The server listens only on
  127.0.0.1, on a random port unless you set `STUDIO_PORT`.
  `STUDIO_NODE` chooses which `node` the desktop app starts.
- **Added.** Live preview. Changes in `data/` and `styles/` re-render the
  page after a short delay. If the YAML has an error, the preview keeps
  the last good render and shows the error beside it.
- **Added.** A card for each document, each with its own **Build** button:
  - **Color** and **Grayscale** checkboxes pick which PDFs to build.
  - On the resume card only, **Compare against snapshot** and **Run
    tests** control whether those checks run. Tests are off in the app,
    so a build takes about a second instead of ten.
  - After a build, the preview shows the PDF that was just written
    rather than rendering the page a second time.
- **Added.** The build tray has one row per PDF, labeled **Color** and
  **Grayscale**. **Show (217 KB)** opens that PDF's folder in the file
  manager.
- **Added.** Drop a `.yml` on the window and Studio reads its top-level
  keys to tell whether it is a resume or a cover letter, then opens it
  on the matching card. It asks only when the file fits both or
  neither. A dropped file is opened where it is; nothing in `data/` is
  replaced.
- **Added.** The data-file picker is split by document. The Resume card
  never offers a letter file, and the Letter card never offers a
  resume file.
- **Added.** A warm engine (`build/engine.js`) that keeps a Chromium
  page and a Python worker (`build/worker.py`) running between
  previews, so a preview doesn't wait for either to start. Builds
  still run the command-line script, so there is only one way to
  produce a PDF.
- **Security.** The desktop installer bundles only the three
  `*_default.yml` templates from `data/`, never your own files.

### Data files

- **Changed — Breaking.** Your data file is renamed so the short name is
  yours; the template already had the suffixed name:

  | 0.5.x | 0.6.0 |
  | --- | --- |
  | `data/resume.local.yml` | `data/resume.yml` |

  New in this release: `data/letter.yml` / `data/letter_default.yml`
  and `data/_profile.yml` / `data/_profile_default.yml`.
- **Changed — Breaking.** `RESUME_DATA_SOURCE` takes `default` or `mine`.
  `local` is no longer accepted.
- **Changed — Breaking.** YAML is read with YAML 1.2 core typing
  instead of PyYAML's YAML 1.1 rules. Only `true` and `false` are
  booleans. `22:30` stays a string instead of becoming 1350. A value
  that looks like a date stays a string. `3.90` keeps its trailing
  zero.
- **Changed.** YAML is parsed with libyaml when it is available. On the
  resume, parsing went from about 12.6ms to about 0.8ms.
- **Added.** A shared profile. `data/_profile.yml` holds what is the
  same in every application: `name`, `contact`, `meta.lang` and
  `meta.maxPages`. It is merged under every document you build.
  - The document's own value wins over the profile's.
  - Lists are replaced, not combined.
  - The build log shows which values came from the profile.

  `role` and `meta.description` stay in each document because they
  name the job you are applying for. The shipped templates use the
  template profile, `_profile_default.yml`, and never yours (see
  Privacy).
- **Added.** `RESUME_DATA_FILE` and `LETTER_DATA_FILE` build from
  any file you point them at, and that file is read where it is.
- **Added.** JSON Schemas for all three kinds of data file in
  `schemas/`. `.vscode/settings.json` connects them to the Red Hat YAML
  extension, which then offers completion, hover help and inline
  errors. SchemaStore is turned off there, because `resume.yml` is
  also the standard JSON Resume filename and VS Code was checking your
  file against the JSON Resume schema.
- **Changed.** `gap` must be a real boolean.
- **Changed.** The template's `meta.maxPages` is 4, down from 10. If the
  value is missing entirely, the build still uses 10.

### Output files

- **Changed — Breaking.** PDFs are named after the person, using
  `name.first` and `name.last`:

  ```
  dist/Gaius_Caesar_Resume.pdf
  dist/Gaius_Caesar_Resume_Grayscale.pdf
  dist/Gaius_Caesar_Cover_Letter.pdf
  dist/Gaius_Caesar_Cover_Letter_Grayscale.pdf
  ```

  In 0.5.x they were `dist/resume-color.pdf` and
  `dist/resume-grayscale.pdf`. The color PDF gets the name without a
  suffix because it is the one you send.
- **Added.** How names become filenames (`build/_output_name.py`):
  - Accents are removed and the letter kept: José → Jose.
  - Letters with no plain-ASCII form are spelled out: ß → ss, ø → o.
  - Apostrophes are dropped: O'Brien → OBrien.
  - Any other run of characters that aren't letters or digits becomes
    one underscore.
  - A name with no ASCII spelling at all is kept in its original
    characters.

  The Node code reads the finished name from the build metadata
  (`output_stem`) instead of working it out a second time.
- **Added.** Each build deletes older PDFs from `dist/` that it did not
  just write: files with the 0.5.x names, files from a brief
  `-grayscale` spelling used during development, and files left over
  from an earlier spelling of your name. It only touches files that
  match this project's own naming pattern, and it prints every file
  it removes.
- **Added.** `RESUME_VARIANTS` chooses which PDFs a build writes: `color`,
  `grayscale`, or both, separated by a comma. If it is unset, both are
  built, as before. When a variant is left out, its old PDF is deleted
  so it can't be mistaken for a fresh one.
- **Changed.** Snapshot fixtures keep fixed names:
  `expected_resume-color.pdf`, and `expected_resume-color.mine.pdf` for
  your data (0.5.x used `.local.pdf`). A committed test file named after
  whoever last built it would change with every build and would put a
  real name into the repository.

### Build and snapshot test

- **Changed — Breaking.** The snapshot test is off by default.
  `RESUME_SNAPSHOT=on` reports differences without failing the build,
  and `RESUME_SNAPSHOT=strict` fails the build as 0.5.x did. The PDFs are
  written before the comparison runs, so a difference tells you
  something changed; it is not a reason to withhold the file.
- **Added.** `RESUME_TESTS`. The unit tests still run first from the
  command line; `off` skips them.
- **Changed.** The snapshot test compares only the variants the last
  build produced. `--update-all` always builds both.
- **Fixed.** If `dist/pdf_meta.json` held a `data_source` value the
  snapshot test did not recognize, it compared against the committed
  fixture without saying so. Your resume could then be checked against
  the placeholder. It now stops with an error.
- **Fixed.** The rasterizer left the PDF open after reading it, which
  on Windows kept the file locked and stopped it being deleted.

### Privacy

- **Security.** `data/` in `.gitignore` is now an allowlist. Only the
  three templates are tracked, so a new personal file stays private by
  default. In 0.5.x it was a list of named private files, and every new
  one had to be added to it. The pattern covers subfolders too, so
  files kept in a folder such as `data/legacy/` stay private.
- **Security.** A template document merges the template profile, never
  yours. The committed snapshot fixtures are rendered from the
  template, so if the template borrowed a field from your real profile,
  your details would end up in a file that is committed.
  `build.profile_for` rules this out.
- **Added.** `tests/test_anonymized.py` checks everything that would be
  committed, including the text of the committed PDFs. It fails if it
  finds your name, contact details, employers or places from your own
  data files, including any kept in subfolders of `data/`. In a git
  checkout it asks git which files would be committed, so gitignored
  files are never reported. In a checkout without your data it skips.
- **Changed.** Personal details were removed from committable files:
  the examples in the schemas, README and tests use invented places and
  values. The only personal detail left is the copyright holder's name
  in `LICENSE`, which the privacy test deliberately allows.
- **Changed.** `.gitignore` also covers files an AI assistant saves into
  the project folder (`Claude outputs/`), since previews rendered from
  your data show your contact details. It also covers snapshot
  fixtures under their 0.5.x names (`*.local.pdf`).

### Tests

- **Added.** `test_yaml_typing.py`, `test_profile_merge.py`,
  `test_letter_data.py`, `test_output_name.py`,
  `test_output_name.js`, `test_detect_doc.js`,
  `test_worker_equivalence.py`, `test_engine_equivalence.js` and
  `test_anonymized.py`.
- **Added.** `jsonschema` in `requirements.txt`. The schema tests use it
  and skip if it is missing.
- **Changed.** `build/run_tests.js` prints why each skipped test was
  skipped.
- **Changed.** The unit tests run with `RESUME_DATA_SOURCE`,
  `RESUME_DATA_FILE` and `LETTER_DATA_FILE` cleared, so a
  variable set in the shell can't change their results.

### Documentation and language

- **Changed.** The README was rewritten for this release. It has new
  sections on Resume Studio, the shared profile, editing in VS Code,
  YAML typing, output names and the cover letter.
- **Changed.** All comments, messages and documentation use American
  English. Two file-private functions were renamed along with their
  call sites: `colourEnabled` is now `colorEnabled`, and
  `_colour_enabled` is now `_color_enabled`.
- **Added.** This changelog.
- **Added.** Screenshots in `docs/screenshots/`, shown in the README:
  both documents, the color and grayscale variants, and the Studio in
  light and dark themes. They use the shipped placeholder data.

---

## [0.5.1] — 2026-05-16

*A new visible element, and no change to the interface.*

- **Added.** A "Page N of M" footer at the bottom right of every page when
  the resume runs to more than one page. It sits in the page's bottom
  margin, so the layout solver doesn't have to allow room for it.

## [0.5.0] — 2026-05-16

*Breaking: the PDF moved and was renamed, the build scripts moved, and a
snapshot flag was renamed.*

- **Changed — Breaking.** The PDF is no longer written to `print.pdf` in
  the project root. Each build now writes two PDFs:
  `dist/resume-color.pdf` and `dist/resume-grayscale.pdf`.
- **Changed — Breaking.** The build scripts moved from `scripts/` to
  `build/`, so every documented command changed with them, for example
  `python build/snapshot_pdf.py`.
- **Changed — Breaking.** `snapshot_pdf.py --update-both` is now
  `--update-all`. `--update` now refreshes both variants for the current
  data source.
- **Changed — Breaking.** The styles moved from `assets/styles/` to
  `styles/`, and the fonts from `assets/fonts/` to `fonts/`.
- **Changed.** The snapshot fixtures are renamed to
  `expected_resume-{color,grayscale}.pdf`, with a `.local.pdf` pair for
  your data. There are four fixtures now; a missing one is created
  automatically.
- **Added.** A grayscale PDF. It is printed with four color tokens
  swapped rather than with a CSS `filter`. A filter would turn the page
  into one large image, about six times the file size, and shift the
  layout.
- **Added.** `build/_constants.json`, holding values the Python and Node
  code share: console styling and environment variable names.
- **Added.** `RESUME_PIPELINE_SUFFIX`, a label that `--update-all` adds to
  the first log banner of each build it runs.
- **Added.** The build fails if `dist/styles.css` is older than the Sass
  it was compiled from. Running `python build/build.py` on its own skips
  the Sass step, so that is when this catches a stale stylesheet.
- **Added.** `dist/favicon.svg` showing the person's initials in the
  accent color; Open Graph, `theme-color` and `robots noindex` tags; and,
  on screens 1728px wide or more, pages shown side by side.
- **Added.** Validation for `role`, `contact` and `meta.lang`.
- **Added.** `crop_pdf.py --quiet`.
- **Added.** Tests for the data loader, PDF metadata, accent reading and
  cropping, plus a shared JavaScript test helper.
- **Changed.** Jinja escapes HTML characters in every template.
  Previously a few places were missed, including the page title, the
  meta tags and the name.
- **Changed.** `<html lang>` now comes from `meta.lang`. It used to be
  hard-coded as `en`, so the HTML and the PDF could report different
  languages.
- **Changed.** `***triple asterisks***` are left as typed instead of
  becoming bold.
- **Changed.** Visual:
  - All rules are 1pt.
  - The top rule is gray.
  - Bullets are small dots.
  - Monochrome printing darkens more tokens.
- **Changed.** `playwright` and `sass` are pinned to exact versions, and
  setup uses `npm ci`.
- **Changed.** `measurement.j2` uses the same macros as the final
  template, so the heights the solver measures and the page it lays out
  come from the same markup.
- **Removed.** `tests/README.md`. Its content moved into the README.
- **Fixed.** Sidebar link addresses are now HTML-escaped.

## [0.4.3] — 2026-05-10

*Visual and accessibility adjustments; nothing new, nothing broken.*

- **Changed.** `--size-small` (8.5pt) and `--size-meta` (9pt) merge into
  one `--size-caption` (9pt). A difference of half a point was too small
  to see.
- **Changed.** The column divider is a 1pt gray hairline instead of a
  2pt accent-green rule, which competed with the content. The accent
  color is kept for the top rule.
- **Changed.** Slightly more space between consecutive jobs.
- **Fixed.** Gap entries no longer use `opacity: 0.7`, which dropped
  their meta text to about 3.6:1 contrast, below WCAG AA. They now use a
  solid color at 5.7:1.
- **Changed.** Placeholder content was reorganized. Only keys that
  already existed are used.

## [0.4.2] — 2026-05-10

*New typefaces and a screen-reader improvement; the interface is
unchanged.*

- **Changed.** Montserrat is replaced by Manrope for text and Newsreader
  for the name and section headings. Both are included in the project
  under the SIL Open Font License 1.1.
- **Changed.** Section headings are weight 700. Body line height goes
  from 1.35 to 1.4 to suit Manrope's taller lowercase letters.
- **Changed.** Date and location rows use a new 9pt `--size-meta`.
- **Added.** The role line and section headings are output twice: once
  as hidden text for screen readers and once as visible text hidden
  from them. With wide letter-spacing, some screen readers spell a word
  out letter by letter.
- **Removed.** The Google Fonts `<link>`. The fonts ship with the project.
- **Fixed.** The measurement page didn't render `{group: …}` list items
  as group headings, so the heights it measured didn't match the final
  page.

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

---

## Version history

Before this changelog, versions were kept as folders with informal
numbers. The archive folders are now named by version; this table
records what each one used to be called, so older notes that mention
the old names can still be followed.

| version | previously | what the number records |
| --- | --- | --- |
| 0.1.0 | `0.1.pdf` | the design, recreated as a web page |
| 0.2.0 | `0.2` | a hand-written web page |
| 0.3.0 | `1.0` | the build pipeline |
| 0.4.0 | `2.0` | breaking: A4 → US Letter, styles moved |
| 0.4.1 | `3.0` | new optional fields |
| 0.4.2 | `3.5` | new typefaces |
| 0.4.3 | `4.0` | adjustments and a contrast fix |
| 0.5.0 | `5.0` | breaking: the PDF and the scripts moved |
| 0.5.1 | the May snapshot | the page-number footer |
| 0.6.0 | — | breaking: `render.js` removed, files renamed |
| 0.6.1 | — | security updates, tests on GitHub |
| 0.7.0 | — | breaking: the Studio is the only way to build |
| 0.7.1 | — | a lost-save fix, faster previews, a security fix |

[0.7.1]: #071--2026-09-21
[0.7.0]: #070--2026-09-21
[0.6.1]: #061--2026-09-21
[0.6.0]: #060--2026-09-20
[0.5.1]: #051--2026-05-16
[0.5.0]: #050--2026-05-16
[0.4.3]: #043--2026-05-10
[0.4.2]: #042--2026-05-10
[0.4.1]: #041--2026-05-09
[0.4.0]: #040--2026-05-08
[0.3.0]: #030--2026-05-03
[0.2.0]: #020--2026-04-25
[0.1.0]: #010--2026-04-20
