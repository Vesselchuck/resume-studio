# Tests

Two kinds of verification: unit tests for the project's logic, and a
slow visual-regression snapshot test for the rendered PDF.

## Unit tests

Python and JavaScript test files live in `tests/`. Three are fast
pure-logic tests; one launches Chromium for in-browser assertions.

- `test_validate_data.py` — YAML schema validation in `build.py`
- `test_markdown_filter.py` — the `**bold**` filter for bullet text
- `test_solve_layout.js` — the layout solver in `scripts/solve_layout.js`
- `test_check_layout.js` — layout invariants in `scripts/check_layout.js`.
  Skips automatically if Chromium isn't available, so a fresh checkout
  without `npx playwright install` still gets coverage from the other
  three tests.

Run all of them via the cross-platform runner:

```
node scripts/run_tests.js
```

The runner invokes Python's `unittest discover` for `tests/test_*.py`
and runs each `tests/test_*.js` directly with Node. It picks the right
Python interpreter for the platform automatically (override with the
`PYTHON` env var if needed).

### Running a single test file

```
node scripts/run_tests.js test_validate_data            # Python
node scripts/run_tests.js test_solve_layout             # JavaScript
node scripts/run_tests.js test_validate_data.TestValidateData.test_good_data_passes
```

The runner picks the right runtime by file existence — if
`tests/test_<name>.js` exists, it runs that as Node; otherwise it
treats the argument as a Python `unittest` dotted path.

### Running tests without going through the build

The build pipeline (`node render.js`) runs all unit tests as step 0,
so any failure aborts the build. To run tests in isolation (e.g.,
during TDD):

```
node scripts/run_tests.js
```


## Snapshot test (slow, visual regression)

In `scripts/snapshot_pdf.py` — lives outside `tests/` so unittest
discovery doesn't try to import its heavy dependencies (`pypdfium2`,
`Pillow`).

The snapshot test runs automatically as part of `node render.js`
(step 10 of the pipeline). It rasterizes the freshly-built `print.pdf`,
compares it page-by-page to a committed fixture, and fails the build
if visible pixels changed beyond the configured tolerance.

### Dual fixtures

The build can use either of two data files:

- `data/resume_default.yml` — placeholder data, committed
- `data/resume.local.yml` — real resume data, gitignored

The snapshot tool reads `dist/pdf_meta.json` (written by `build.py`)
to learn which file backed the most recent build, and picks the
matching fixture:

| Data source | Fixture                                      | In git? |
|-------------|----------------------------------------------|---------|
| `default`   | `tests/fixtures/expected_print.pdf`          | yes     |
| `local`     | `tests/fixtures/expected_print.local.pdf`    | no      |

Each fixture matches the data file that produced it. There is no
"shared" fixture — that would mean comparing one data set's render
against another's pixels, which is meaningless.

### First build (no fixture yet)

```
node render.js
```

The snapshot step auto-bootstraps the matching fixture from the
current `print.pdf` and prints a notice. Subsequent builds diff
against this fixture. Inspect `print.pdf` visually before committing
the fixture.

### Routine build (fixture present)

```
node render.js
```

Snapshot runs automatically. On regression: side-by-side diff images
are written to `tests/fixtures/diff_pageN.png` and the build exits
non-zero.

### Refreshing after an intentional change

After tweaking CSS, content, or layout in a way that visibly changes
the output:

```
py scripts/snapshot_pdf.py --update          # Windows
python scripts/snapshot_pdf.py --update      # macOS / Linux
```

This updates the fixture matching the data source of the most recent
build (read from `dist/pdf_meta.json`). To refresh both fixtures in
one go (e.g., after a CSS change that affects both default and local
output):

```
py scripts/snapshot_pdf.py --update-both
```

`--update-both` runs the full build pipeline twice (once with default
data, once with local data if `data/resume.local.yml` exists),
refreshing the matching fixture each time. The intermediate snapshot
checks are skipped via `SKIP_SNAPSHOT=1` so the existing
about-to-be-replaced fixtures don't fail the build.

Commit the refreshed `expected_print.pdf` alongside whatever change
caused it. The local fixture (`expected_print.local.pdf`) stays
gitignored — it lives only on your machine.

### Running snapshot test on its own

```
py scripts/snapshot_pdf.py
```

Useful when iterating on tolerances or inspecting a regression
without rebuilding. Requires `print.pdf` to already exist in the
project root.


## Dependencies

All Python deps for both unit tests and snapshot tests are in
`requirements.txt`:

```
py -m pip install -r requirements.txt        # Windows
pip install -r requirements.txt              # macOS / Linux
```

The snapshot test deps (`pypdfium2`, `Pillow`) are imported lazily
inside `scripts/snapshot_pdf.py` so a missing dependency surfaces as
a clear "install with: pip install -r requirements.txt" message
rather than an `ImportError` traceback.
