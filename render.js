/**
 * render.js — Build script for the resume.
 *
 * Pipeline:
 *   0. Run unit tests (Python + JS via build/run_tests.js).
 *      Fail-fast on any logic regression before producing artifacts.
 *   1. Compile Sass: styles/styles.scss → dist/styles.css
 *      via the `sass` npm package. Source maps are disabled.
 *   2. Measurement-mode build (build.py --mode=measurement):
 *      writes dist/index.html with all content in a single flowing
 *      column for the solver to measure.
 *   3. Open the measurement HTML in Playwright and extract heights
 *      from every [data-measure] element.
 *   4. Run the layout solver (build/solve_layout.js) on the
 *      measurements; write dist/placement.json with the per-page
 *      placement.
 *   5. Final build (build.py --mode=final): reads placement.json
 *      and renders the paginated dist/index.html.
 *   6. Reload the final HTML in Playwright.
 *   7. Assert layout invariants (column divider terminates at the
 *      page bottom margin; section-rhythm equal across sidebar
 *      and main column; expected page count = solver's count;
 *      no descendant overflows its page's content area).
 *   8. Generate TWO PDFs via Playwright — color, then grayscale
 *      (rendered by toggling html.force-grayscale on the page).
 *   9. Crop each PDF to exact US Letter (8.5×11 in) and stamp
 *      authoritative metadata from dist/pdf_meta.json.
 *  10. Snapshot test: pixel-diff dist/resume-color.pdf and
 *      dist/resume-grayscale.pdf against committed fixtures.
 *      Auto-bootstraps on first build.
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
const { checkLayoutInvariants } = require('./build/check_layout');
const { detectPython } = require('./build/detect_python');
const { solveLayout } = require('./build/solve_layout');
const { extractMeasurements } = require('./build/measure_dom');
const {
  ENV_SKIP_SNAPSHOT,
  ENV_RESUME_PIPELINE_SUFFIX,
} = require('./build/_env_contract');
const c = require('./build/_console');


/* ─── Constants ───────────────────────────────────────────────── */

const ROOT = __dirname;
const HTML_PATH = pathToFileURL(path.resolve(ROOT, 'dist/index.html')).href;
const PDF_META_PATH = path.join(ROOT, 'dist', 'pdf_meta.json');
const PLACEMENT_PATH = path.join(ROOT, 'dist', 'placement.json');
const COLOR_PDF_PATH = path.join(ROOT, 'dist', 'resume-color.pdf');
const GRAYSCALE_PDF_PATH = path.join(ROOT, 'dist', 'resume-grayscale.pdf');

// Detect Python via the shared detect_python module. Done once at
// startup so all subprocess calls share the same interpreter.
const PYTHON = detectPython();


/* ─── Helpers ─────────────────────────────────────────────────── */

/**
 * Mark `err` as already-printed by a phase function, so the
 * orchestrator's catch doesn't re-emit it as "Unexpected error"
 * with a stack trace. Phase functions throw errors after calling
 * c.err()/c.detail() (or after a subprocess whose stderr is
 * inherited has already streamed its diagnostics); wrapping the
 * throw with reported() conveys that fact through the call stack
 * without relying on error-message substring matching.
 *
 * Returns the same err for use in `throw reported(err)` patterns,
 * including `throw reported(new Error('…'))` for synthesized errors.
 */
function reported(err) {
  err.alreadyReported = true;
  return err;
}


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
      {
        cwd: ROOT,
        encoding: 'utf-8',
        env: subprocessEnv(),
        // Explicit: stdin ignored, stdout captured into `out` for
        // ordered replay below, stderr inherited so subprocess
        // diagnostics stream live. Without this, the implicit Node
        // default (which inherits stderr) happens to do the right
        // thing, but a future stdio override would silently break
        // the streaming-diagnostics contract.
        stdio: ['ignore', 'pipe', 'inherit'],
      },
    );
    process.stdout.write(out);
  } catch (err) {
    if (err.stdout) process.stdout.write(err.stdout);
    if (!err.stdout && !err.stderr) {
      c.err(`${fallbackLabel}: ${err.message}`);
    }
    throw reported(err);
  }
}


/**
 * Symmetric counterpart for spawning a Node subprocess with INHERITED
 * stdio. Used when the child's output (banners, per-suite status lines,
 * progress dots) should stream directly to the user's terminal in real
 * time, rather than being captured and replayed after exit.
 *
 * Differences from runPython:
 *   • stdio:'inherit' — child writes straight to parent's stdout/stderr
 *   • No captured output to replay on failure — the child already
 *     printed everything by the time execFileSync returns/throws
 *   • Fallback label is wrapped in a fresh Error rather than re-thrown
 *     from the subprocess error, since the captured-output handling is
 *     irrelevant here
 *
 * Both helpers mark thrown errors via `reported()` so the orchestrator's
 * catch knows not to re-emit them.
 *
 * @param {string} scriptPath      — path to the JS file to execute
 * @param {string} fallbackMessage — message for the synthesized Error
 *                                   if the child exits non-zero
 */
function runNodeInherit(scriptPath, fallbackMessage) {
  try {
    execFileSync(process.execPath, [scriptPath],
      { cwd: ROOT, stdio: 'inherit', env: subprocessEnv() });
  } catch {
    // The child already printed its own failure markers via _console
    // (since stdio was inherited); just abort the pipeline.
    throw reported(new Error(fallbackMessage));
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
 * Phase 0: Run unit tests via build/run_tests.js.
 *
 * Tests are cheap and a failed unit test almost always indicates a
 * problem that would also break rendered output. Fail fast before
 * producing any artifacts.
 *
 * Uses stdio:'inherit' so the runner's output (banners + per-suite
 * lines) flows directly to the user's terminal in real time.
 */
function runTests() {
  // When invoked as a subprocess of snapshot_pdf.py --update-all, the
  // parent sets ENV_RESUME_PIPELINE_SUFFIX to label which data source
  // is being built ("default data" or "local data"). The suffix attaches
  // to the FIRST banner only — subsequent phase banners stay plain
  // because the data-source context is anchored at the top.
  const suffix = process.env[ENV_RESUME_PIPELINE_SUFFIX];
  c.banner(suffix ? `Tests (${suffix})` : 'Tests');
  c.ok_pair('Detected Python', PYTHON);
  runNodeInherit(path.join(ROOT, 'build', 'run_tests.js'), 'Unit tests failed');
}


/**
 * Phase 1: Compile styles/styles.scss → dist/styles.css.
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
      path.join(ROOT, 'styles', 'styles.scss'),
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
    throw reported(err);
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
    [path.join(ROOT, 'build', 'build.py'), '--mode=measurement'],
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
    throw reported(err);
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
    throw reported(err);
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
    [path.join(ROOT, 'build', 'build.py'), '--mode=final'],
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
    throw reported(new Error('Layout invariant violated'));
  }

  const pagesWord = expectedPageCount === 1 ? 'page' : 'pages';
  c.ok_pair('Layout invariants', `clean (${expectedPageCount} ${pagesWord})`);
}


/**
 * Phases 8-9: Print the page to TWO PDFs (color + grayscale), then
 * crop each to true US Letter.
 *
 * Chromium's `page.pdf()` quantizes page dimensions to a 0.12-pt
 * grid, producing pages slightly oversized. Post-process via
 * crop_pdf.py to get exact 8.5×11 in plus authoritative metadata
 * (and /Lang catalog entry) from dist/pdf_meta.json.
 *
 * Uses tmpdir-based intermediates so partially-written cropped
 * PDFs never overwrite known-good outputs if the crop step fails.
 * Temp files are unconditionally removed in the finally block —
 * even when the crop step throws.
 *
 * Grayscale variant: toggles `html.force-grayscale` on the page
 * before the second render. The class applies token-only colour
 * overrides (--text-muted, --text-muted-soft, --border-rule,
 * --accent → see _print.scss for per-token rationale) and crucially
 * does NOT use `filter: grayscale(1)`: a CSS filter would force
 * Chromium to rasterize the page, producing a ~6× larger PDF (image-
 * backed) with sub-pixel layout drift relative to the colour PDF.
 * Token overrides keep the output as pure vector text + strokes with
 * byte-identical layout geometry between the two variants. The class
 * is removed after the second render so the page state is clean for
 * any downstream consumers.
 */
async function printAndCropPDF(page) {
  c.banner('PDF');

  // Pass 1 — color PDF.
  const tmpColorPdf = path.join(os.tmpdir(), `resume-color-${process.pid}.tmp.pdf`);
  await page.pdf({
    path: tmpColorPdf,
    width: '8.5in',
    height: '11in',
    margin: { top: '0', bottom: '0', left: '0', right: '0' },
    printBackground: true,
  });
  try {
    runPython(
      [
        path.join(ROOT, 'build', 'crop_pdf.py'),
        tmpColorPdf,
        COLOR_PDF_PATH,
        '--meta', PDF_META_PATH,
      ],
      'PDF crop (color) failed',
    );
  } finally {
    fs.rmSync(tmpColorPdf, { force: true });
  }
  c.ok_pair('Wrote PDF (color)', path.relative(ROOT, COLOR_PDF_PATH));

  // Pass 2 — grayscale PDF. Toggle the class, re-render, restore.
  await page.evaluate(() => document.documentElement.classList.add('force-grayscale'));
  const tmpGrayPdf = path.join(os.tmpdir(), `resume-grayscale-${process.pid}.tmp.pdf`);
  await page.pdf({
    path: tmpGrayPdf,
    width: '8.5in',
    height: '11in',
    margin: { top: '0', bottom: '0', left: '0', right: '0' },
    printBackground: true,
  });
  await page.evaluate(() => document.documentElement.classList.remove('force-grayscale'));
  try {
    runPython(
      [
        path.join(ROOT, 'build', 'crop_pdf.py'),
        tmpGrayPdf,
        GRAYSCALE_PDF_PATH,
        '--meta', PDF_META_PATH,
        // Suppress the "Cropped" and "Stamped metadata" summary lines
        // on the second crop — they're identical to the first call's
        // output and only add noise. Errors and warnings still print.
        '--quiet',
      ],
      'PDF crop (grayscale) failed',
    );
  } finally {
    fs.rmSync(tmpGrayPdf, { force: true });
  }
  c.ok_pair('Wrote PDF (grayscale)', path.relative(ROOT, GRAYSCALE_PDF_PATH));
}


/**
 * Phase 10: Snapshot test.
 *
 * Pixel-diff both PDF variants (color + grayscale) against committed
 * fixtures. Auto-bootstraps any missing fixture on first build (when
 * it doesn't exist yet); on subsequent builds, a regression here
 * fails the build.
 *
 * Skipped when ENV_SKIP_SNAPSHOT='1' (set by snapshot_pdf.py
 * --update-all, which is itself going to overwrite the fixture next).
 */
function runSnapshot() {
  if (process.env[ENV_SKIP_SNAPSHOT] === '1') {
    c.banner('Snapshot (skipped)');
    return;
  }
  c.banner('Snapshot');
  try {
    runPython(
      [path.join(ROOT, 'build', 'snapshot_pdf.py'), '--auto-bootstrap'],
      'Snapshot test failed',
    );
  } catch (err) {
    // The subprocess already emitted its diff lines via _console;
    // append the refresh-fixture hint and re-throw so the
    // orchestrator sets a non-zero exit.
    c.detail('To refresh the fixture if the change was intentional:');
    c.detail('  python build/snapshot_pdf.py --update');
    throw reported(err);
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
      throw reported(err);
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
    // Phase functions throw on failure; the ones that reported their
    // own diagnostics via _console mark the error with reported().
    // Anything reaching here without that mark is a true unexpected
    // error (e.g. a bug, an unhandled rejection from inside a phase,
    // a non-Error throw) — surface a stack trace so it's debuggable.
    if (!err.alreadyReported) {
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
    // Set exitCode and let Node drain naturally rather than forcing
    // process.exit() — the latter can truncate the final stderr write
    // on Windows when the orchestrator finishes during an error path.
    // No handles remain pending after browser.close(), so the IIFE
    // resolves and the process exits with the right code.
    process.exitCode = exitCode;
  }
})();
