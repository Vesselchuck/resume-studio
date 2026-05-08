/**
 * render.js — Build script for the resume.
 *
 * Pipeline:
 *   0. Run unit tests (Python + JS via scripts/run_tests.js).
 *      Fail-fast on any logic regression before producing artifacts.
 *   1. Compile Sass: assets/styles/styles.scss → dist/styles.css
 *      via the `sass` npm package. Source maps are disabled.
 *   2. Measurement-mode build (build.py --mode=measurement):
 *      writes dist/index.html with all content in a single flowing
 *      column for the solver to measure.
 *   3. Open the measurement HTML in Playwright and extract heights
 *      from every [data-measure] element.
 *   4. Run the layout solver (scripts/solve_layout.js) on the
 *      measurements; write dist/placement.json with the per-page
 *      placement.
 *   5. Final build (build.py --mode=final): reads placement.json
 *      and renders the paginated dist/index.html.
 *   6. Reload the final HTML in Playwright.
 *   7. Assert layout invariants (column divider terminates at the
 *      page bottom margin; section-rhythm equal across sidebar
 *      and main column; expected page count = solver's count;
 *      no descendant overflows its page's content area).
 *   8. Generate a PDF via Playwright.
 *   9. Crop the PDF to exact US Letter (8.5×11 in) and stamp
 *      authoritative metadata from dist/pdf_meta.json.
 *  10. Snapshot test: pixel-diff print.pdf against the committed
 *      fixture. Auto-bootstraps on first build.
 *
 * Run with: node render.js
 *
 * The script auto-detects Python by trying platform-appropriate
 * candidates. To override, set the PYTHON env var:
 *   bash/zsh:    PYTHON=python3.12 node render.js
 *   cmd.exe:     set PYTHON=py && node render.js
 *   PowerShell:  $env:PYTHON="py"; node render.js
 *
 * Requires:
 *   • Node:    playwright, sass   (`npm install`)
 *   • Python:  see requirements.txt
 *              (`pip install -r requirements.txt`,
 *               or `py -m pip install -r requirements.txt` on Windows)
 *
 * Internal structure
 * ──────────────────
 * The pipeline is split into named phase functions, each
 * responsible for one logical step. Phase functions throw on
 * failure; the orchestrator (the IIFE at the bottom) catches once
 * and exits non-zero. This keeps the orchestrator small and each
 * phase independently readable.
 *
 *   runTests()             — phase 0
 *   compileSass()          — phase 1
 *   buildMeasurement()     — phase 2
 *   getMeasurements(page)  — phase 3
 *   solveAndWritePlacement(measurements) — phase 4
 *   buildFinal()           — phase 5
 *   verifyInvariants(...)  — phases 6-7
 *   printAndCropPDF(page)  — phases 8-9
 *   runSnapshot()          — phase 10
 */

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { execFileSync } = require('child_process');
const { pathToFileURL } = require('url');
const { checkLayoutInvariants } = require('./scripts/check_layout');
const { detectPython } = require('./scripts/detect_python');
const { solveLayout } = require('./scripts/solve_layout');
const { extractMeasurements } = require('./scripts/measure_dom');
const c = require('./scripts/_console');


/* ─── Constants ───────────────────────────────────────────────── */

const ROOT = __dirname;
const HTML_PATH = pathToFileURL(path.resolve(ROOT, 'dist/index.html')).href;
const PDF_META_PATH = path.join(ROOT, 'dist', 'pdf_meta.json');
const PLACEMENT_PATH = path.join(ROOT, 'dist', 'placement.json');
const FINAL_PDF_PATH = path.join(ROOT, 'print.pdf');

// Detect Python via the shared detect_python module. Done once at
// startup so all subprocess calls share the same interpreter.
const PYTHON = detectPython();


/* ─── Helpers ─────────────────────────────────────────────────── */

/**
 * Build the env passed to subprocesses (build.py, crop_pdf.py,
 * snapshot_pdf.py). When render.js's own stdout is a TTY, forward
 * that signal via FORCE_COLOR=1 so subprocesses keep their ANSI
 * codes — otherwise execFileSync's pipe-captured stdout would look
 * like non-TTY to them and they'd suppress colour. NO_COLOR
 * passthrough is automatic since process.env is inherited.
 */
function subprocessEnv() {
  const env = { ...process.env };
  if (process.stdout.isTTY) {
    env.FORCE_COLOR = '1';
  }
  return env;
}


/**
 * Read meta.maxPages from dist/pdf_meta.json (written by build.py).
 * Defaults to 10 if the field is missing for any reason — matches
 * the YAML default.
 */
function getMaxPagesFromYaml() {
  try {
    const meta = JSON.parse(fs.readFileSync(PDF_META_PATH, 'utf-8'));
    if (Number.isInteger(meta.max_pages) && meta.max_pages > 0) {
      return meta.max_pages;
    }
  } catch {
    // Fall through to default.
  }
  return 10;
}


/**
 * Run a Python script as a subprocess, handling stdio + errors uniformly.
 *
 * Centralises the four call sites (build.py × 2, crop_pdf.py,
 * snapshot_pdf.py) that were previously inline try/catch boilerplate.
 * The `-B` flag is added unconditionally to suppress __pycache__/
 * creation; the cwd and FORCE_COLOR-forwarding env are set the same
 * way every time.
 *
 * On success, the captured stdout is written through to the parent's
 * stdout so the subprocess's _console output appears in order.
 *
 * On failure:
 *   • The subprocess's stderr was already auto-streamed to the parent's
 *     stderr by execFileSync (Node behaviour), so failure markers from
 *     _console.err() etc. have already reached the user.
 *   • We flush captured stdout (which is NOT auto-streamed) so any
 *     in-progress success markers don't get lost.
 *   • If the subprocess emitted nothing to either stream (rare —
 *     usually means execFileSync itself failed before the script
 *     ran), we emit a fallback _console error.
 *   • We re-throw so the orchestrator can decide the exit policy.
 *
 * @param {string[]} scriptArgs — args after `-B`, typically [script_path, ...]
 * @param {string}   fallbackLabel — error label used if the subprocess
 *                                   produced no output of its own
 */
function runPython(scriptArgs, fallbackLabel) {
  try {
    const out = execFileSync(
      PYTHON,
      ['-B', ...scriptArgs],
      { cwd: ROOT, encoding: 'utf-8', env: subprocessEnv() },
    );
    process.stdout.write(out);
  } catch (err) {
    if (err.stdout) process.stdout.write(err.stdout);
    if (!err.stdout && !err.stderr) {
      c.err(`${fallbackLabel}: ${err.message}`);
    }
    throw err;
  }
}


/* ─── Debug helpers ───────────────────────────────────────────── */

/**
 * Dump the solver's view of page geometry and per-block/section/job
 * measured heights. Enabled via DEBUG_MEASUREMENTS=1.
 *
 * Intentionally uses `console.log` (not _console) — this is raw
 * developer-facing diagnostic output, not pipeline status.
 */
function dumpMeasurementsDebug(measurements) {
  console.log('\n────── DEBUG: measurements ──────');
  console.log('pageGeometry:', JSON.stringify(measurements.pageGeometry, null, 2));
  console.log('\nsidebar blocks:');
  for (const b of measurements.sidebar) {
    console.log(`  ${b.id} (${b.type}): total=${b.totalHeight.toFixed(2)}px,`,
      `heading=${b.headingHeight.toFixed(2)}, items=${b.items.length},`,
      `itemGap=${b.itemGap.toFixed(2)}`);
  }
  console.log('\nmainColumn sections:');
  for (const s of measurements.mainColumn) {
    if (s.kind === 'experience') {
      console.log(`  experience: total=${s.totalHeight.toFixed(2)}px,`,
        `heading=${s.headingHeight.toFixed(2)}, jobs=${s.jobs.length}`);
      for (const j of s.jobs) {
        console.log(`    ${j.id}: total=${j.totalHeight.toFixed(2)}px,`,
          `header=${j.headerHeight.toFixed(2)},`,
          `bullets=[${j.bullets.map(b => b.height.toFixed(1)).join(', ')}]`);
      }
    } else {
      console.log(`  ${s.kind}: total=${s.totalHeight.toFixed(2)}px,`,
        `heading=${s.headingHeight.toFixed(2)}`);
    }
  }
  console.log('────────────────────────────────\n');
}


/**
 * Dump actual rendered heights of the per-page columns from the
 * final HTML so we can compare against what the solver thought
 * would fit. Enabled via DEBUG_MEASUREMENTS=1.
 */
async function dumpFinalLayoutDebug(page) {
  const dump = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll('.page').forEach((p, i) => {
      const pageRect = p.getBoundingClientRect();
      const cs = window.getComputedStyle(p);
      const padTop = parseFloat(cs.paddingTop);
      const padBottom = parseFloat(cs.paddingBottom);
      const contentBottom = pageRect.bottom - padBottom;
      ['sidebar', 'main-col'].forEach((cls) => {
        const col = p.querySelector('.' + cls);
        if (!col) return;
        const colRect = col.getBoundingClientRect();
        // Find lowest descendant
        let lowestBottom = -Infinity, lowestEl = null;
        col.querySelectorAll('*').forEach((el) => {
          const r = el.getBoundingClientRect();
          if (r.bottom > lowestBottom) {
            lowestBottom = r.bottom;
            lowestEl = el;
          }
        });
        out.push({
          page: i + 1,
          column: cls,
          col_top: colRect.top.toFixed(2),
          col_bottom: colRect.bottom.toFixed(2),
          content_bottom_limit: contentBottom.toFixed(2),
          lowest_descendant_bottom: lowestBottom.toFixed(2),
          col_height_used: (lowestBottom - colRect.top).toFixed(2),
          col_capacity: (contentBottom - colRect.top).toFixed(2),
          overflow_px: (lowestBottom - contentBottom).toFixed(2),
          culprit: lowestEl?.tagName + '.' + String(lowestEl?.className || '').split(' ')[0],
        });
      });
    });
    return out;
  });
  console.log('\n────── DEBUG: rendered final layout ──────');
  for (const row of dump) {
    console.log(`  P${row.page} ${row.column}: used ${row.col_height_used}px / ${row.col_capacity}px capacity (overflow ${row.overflow_px}px) | culprit: ${row.culprit}`);
  }
  console.log('──────────────────────────────────────────\n');
}


/* ─── Phase functions ─────────────────────────────────────────── */

/**
 * Phase 0: Run unit tests via scripts/run_tests.js.
 *
 * Tests are cheap and a failed unit test almost always indicates a
 * problem that would also break rendered output. Fail fast before
 * producing any artifacts.
 *
 * Uses stdio:'inherit' so the runner's output (banners + per-suite
 * lines) flows directly to the user's terminal in real time.
 */
function runTests() {
  c.banner('Tests');
  c.ok_pair('Detected Python', PYTHON);
  try {
    execFileSync(
      process.execPath,
      [path.join(ROOT, 'scripts', 'run_tests.js')],
      { cwd: ROOT, stdio: 'inherit' },
    );
  } catch {
    // run_tests.js already printed its own failure markers; we just
    // need to abort the pipeline.
    throw new Error('Unit tests failed');
  }
}


/**
 * Phase 1: Compile assets/styles/styles.scss → dist/styles.css.
 *
 * Uses sass's programmatic API rather than spawning the `sass.cmd` /
 * `sass` binary. The binary approach is brittle on Windows: Node 20+
 * refuses to execFile .cmd/.bat files without `shell: true`
 * (CVE-2024-27980), and `shell: true` reintroduces quoting issues for
 * paths containing spaces. Programmatic API sidesteps both problems
 * and is identical on every platform.
 */
function compileSass() {
  try {
    const sass = require('sass');
    const distDir = path.join(ROOT, 'dist');
    fs.mkdirSync(distDir, { recursive: true });
    const result = sass.compile(
      path.join(ROOT, 'assets', 'styles', 'styles.scss'),
      { sourceMap: false, style: 'expanded' },
    );
    fs.writeFileSync(path.join(distDir, 'styles.css'), result.css, 'utf-8');
    const cssBytes = Buffer.byteLength(result.css, 'utf-8');
    const cssKb = (cssBytes / 1024).toFixed(1);
    c.ok_pair('Compiled SCSS', `${path.join('dist', 'styles.css')} (${cssKb} KB)`);
  } catch (err) {
    c.err('Sass compilation failed');
    // sass errors have nicely formatted .toString() output that
    // identifies the file + line + the offending source.
    err.toString().split('\n').forEach(line => c.detail(line));
    c.detail('');
    c.detail('Is sass installed? Run:  npm install');
    throw err;
  }
}


/**
 * Phase 2: Measurement-mode build.
 *
 * Produces dist/index.html with all content in a single flowing
 * column for the solver to measure. The final paginated build comes
 * later (phase 5) using the placement decided by the solver.
 */
function buildMeasurement() {
  runPython(
    [path.join(ROOT, 'scripts', 'build.py'), '--mode=measurement'],
    'Measurement build failed',
  );
}


/**
 * Phase 3: Extract per-element heights from the rendered measurement
 * HTML via Playwright.
 *
 * Requires the page to already have HTML_PATH loaded. Returns the
 * measurements object consumed by the solver. Honours
 * DEBUG_MEASUREMENTS=1 for diagnostic output.
 */
async function getMeasurements(page) {
  let measurements;
  try {
    measurements = await extractMeasurements(page, getMaxPagesFromYaml());
  } catch (err) {
    c.err('Measurement extraction failed');
    err.message.split('\n').forEach(line => c.detail(line));
    throw err;
  }
  if (process.env.DEBUG_MEASUREMENTS === '1') {
    dumpMeasurementsDebug(measurements);
  }
  return measurements;
}


/**
 * Phase 4: Run the pure solver on measurements; write placement.json.
 *
 * The solver is pure (no I/O); we capture its result and persist it
 * for the final-mode build. Any solver-thrown SolverError carries
 * `column`, `block_id`, `job_id` for actionable diagnostics.
 */
function solveAndWritePlacement(measurements) {
  let placement;
  try {
    placement = solveLayout(measurements);
  } catch (err) {
    c.err('Layout solver failed');
    err.message.split('\n').forEach(line => c.detail(line));
    if (err.column) c.detail(`Column: ${err.column}`);
    if (err.block_id) c.detail(`Block:  ${err.block_id}`);
    if (err.job_id)   c.detail(`Job:    ${err.job_id}`);
    c.detail('');
    c.detail('If content is too dense for meta.maxPages, either:');
    c.detail('  • Increase meta.maxPages in your resume YAML');
    c.detail('  • Trim content (shorter bullets, fewer skills, etc.)');
    throw err;
  }
  fs.writeFileSync(
    PLACEMENT_PATH,
    JSON.stringify(placement, null, 2) + '\n',
    'utf-8',
  );
  const pagePlural = placement.pages.length === 1 ? 'page' : 'pages';
  c.ok_pair('Solved layout',
    `${placement.pages.length} ${pagePlural} → ${path.relative(ROOT, PLACEMENT_PATH)}`);
  return placement;
}


/**
 * Phase 5: Final build using the solved placement.
 */
function buildFinal() {
  runPython(
    [path.join(ROOT, 'scripts', 'build.py'), '--mode=final'],
    'Final build failed',
  );
}


/**
 * Phases 6-7: Reload the final HTML and check layout invariants.
 *
 * Throws on invariant violation. The `Layout invariants: clean (N
 * pages)` success line uses the solver's expected page count for
 * the value text; on failure, prints actionable likely-causes.
 *
 * Honours DEBUG_MEASUREMENTS=1 for a per-page rendered-height dump
 * (useful when invariants fire and you want to compare against what
 * the solver thought would fit).
 */
async function verifyInvariants(page, expectedPageCount) {
  // Reload the page so the invariant check runs against the FINAL
  // HTML (not the measurement HTML still loaded from phase 3).
  await page.goto(HTML_PATH, { waitUntil: 'networkidle' });

  const result = await checkLayoutInvariants(page, { expectedPageCount });

  if (process.env.DEBUG_MEASUREMENTS === '1') {
    await dumpFinalLayoutDebug(page);
  }

  if (!result.ok) {
    c.err('Layout invariant violated');
    c.detail(`Violations: ${JSON.stringify(result.violations, null, 2)}`);
    if (result.rhythmMeasurements.length) {
      c.detail(`Rhythm measurements: ${JSON.stringify(result.rhythmMeasurements, null, 2)}`);
    }
    c.detail('');
    c.detail('Likely causes:');
    c.detail('  • .body-grid lost its `flex: 1 1 auto`');
    c.detail('  • A new flex-grow sibling was added inside .page');
    c.detail('  • .page padding changed without updating --page-margin');
    c.detail('  • --section-rhythm or its derived margins were changed');
    c.detail('  • A .page was added or removed without updating EXPECTED_PAGE_COUNT');
    throw new Error('Layout invariant violated');
  }

  const pagesWord = expectedPageCount === 1 ? 'page' : 'pages';
  c.ok_pair('Layout invariants', `clean (${expectedPageCount} ${pagesWord})`);
}


/**
 * Phases 8-9: Print the page to PDF, then crop to true US Letter.
 *
 * Chromium's `page.pdf()` quantizes page dimensions to a 0.12-pt
 * grid, producing pages slightly oversized. Post-process via
 * crop_pdf.py to get exact 8.5×11 in plus authoritative metadata
 * (and /Lang catalog entry) from dist/pdf_meta.json.
 *
 * Uses a tmpdir-based intermediate so a partially-written cropped
 * PDF never overwrites a known-good print.pdf if the crop step
 * fails. The temp file is unconditionally removed in the finally
 * block — even when the crop step throws.
 */
async function printAndCropPDF(page) {
  c.banner('PDF');
  const tmpPdf = path.join(os.tmpdir(), `resume-print-${process.pid}.tmp.pdf`);
  await page.pdf({
    path: tmpPdf,
    width: '8.5in',
    height: '11in',
    margin: { top: '0', bottom: '0', left: '0', right: '0' },
    printBackground: true,
  });

  try {
    runPython(
      [
        path.join(ROOT, 'scripts', 'crop_pdf.py'),
        tmpPdf,
        FINAL_PDF_PATH,
        '--meta', PDF_META_PATH,
      ],
      'PDF crop failed',
    );
  } finally {
    fs.rmSync(tmpPdf, { force: true });
  }

  c.ok_pair('Wrote PDF', 'print.pdf');
}


/**
 * Phase 10: Snapshot test.
 *
 * Pixel-diff print.pdf against the committed fixture. Auto-bootstraps
 * the fixture on first build (when it doesn't exist yet); on subsequent
 * builds, a regression here fails the build.
 *
 * Skipped when SKIP_SNAPSHOT=1 (set by snapshot_pdf.py --update-both,
 * which is itself going to overwrite the fixture next).
 */
function runSnapshot() {
  c.banner('Snapshot');
  if (process.env.SKIP_SNAPSHOT === '1') {
    c.info('Snapshot test skipped (SKIP_SNAPSHOT=1)');
    return;
  }
  try {
    runPython(
      [path.join(ROOT, 'scripts', 'snapshot_pdf.py'), '--auto-bootstrap'],
      'Snapshot test failed',
    );
  } catch (err) {
    // The subprocess already emitted its diff lines via _console;
    // append the refresh-fixture hint and re-throw so the
    // orchestrator sets a non-zero exit.
    c.detail('');
    c.detail('To refresh the fixture if the change was intentional:');
    c.detail('  python scripts/snapshot_pdf.py --update');
    throw err;
  }
}


/* ─── Pipeline orchestrator ───────────────────────────────────── */

(async () => {
  let browser;
  let exitCode = 0;
  try {
    runTests();

    c.banner('Build (measurement)');
    compileSass();
    buildMeasurement();

    // Browser-using phases share a single Playwright instance; the
    // orchestrator owns its lifecycle (created here, closed in the
    // finally block at the bottom).
    try {
      browser = await chromium.launch();
    } catch (err) {
      c.err('Chromium launch failed');
      err.message.split('\n').forEach(line => c.detail(line));
      c.detail('');
      c.detail('Is Chromium installed? Run:  npx playwright install chromium');
      throw err;
    }
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto(HTML_PATH, { waitUntil: 'networkidle' });

    const measurements = await getMeasurements(page);
    const placement = solveAndWritePlacement(measurements);

    c.banner('Build (final)');
    buildFinal();

    await verifyInvariants(page, placement.pages.length);
    await printAndCropPDF(page);
    runSnapshot();
  } catch (err) {
    // Phase functions throw on failure; they've already printed
    // their own diagnostics via _console. Treat unrecognized errors
    // (no .stdout/.stderr — i.e. not from a subprocess) as
    // unexpected and surface a stack trace for debugging.
    if (!err.stdout && !err.stderr && !err.message?.includes('failed')
        && !err.message?.includes('violated')) {
      c.err('Unexpected error');
      (err.stack || err.message).split('\n').forEach(line => c.detail(line));
    }
    exitCode = 1;
  } finally {
    if (browser) {
      try {
        await browser.close();
      } catch (closeErr) {
        // Don't mask the original error; just log.
        c.detail(`(also: browser.close() failed: ${closeErr.message})`);
      }
    }
    process.exit(exitCode);
  }
})();
