/**
 * render.js — Build script for the resume.
 *
 * Pipeline:
 *   0. Run unit tests (Python + JS via scripts/run_tests.js).
 *      Fail-fast on any logic regression before producing artifacts.
 *   1. Compile Sass: styles/styles.scss → dist/styles.css
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
 *   9. Crop the PDF to exact ISO A4 (210×297mm) and stamp
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

const ROOT = __dirname;
const HTML_PATH = pathToFileURL(path.resolve(ROOT, 'dist/index.html')).href;
const PDF_META_PATH = path.join(ROOT, 'dist', 'pdf_meta.json');

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
 * Detect Python via the shared detect_python module.
 */
const PYTHON = detectPython();

/**
 * Wraps checkLayoutInvariants() with the same console diagnostics
 * the build pipeline has always printed. Returns boolean ok.
 */
async function assertLayoutInvariant(page) {
  const result = await checkLayoutInvariants(page);
  if (!result.ok) {
    console.error('\n❌ LAYOUT INVARIANT VIOLATED');
    console.error('Violations:', JSON.stringify(result.violations, null, 2));
    if (result.rhythmMeasurements.length) {
      console.error('Rhythm measurements:',
        JSON.stringify(result.rhythmMeasurements, null, 2));
    }
    console.error('\nLikely causes:');
    console.error('  • .body-grid lost its `flex: 1 1 auto`');
    console.error('  • A new flex-grow sibling was added inside .page');
    console.error('  • .page padding changed without updating --page-margin');
    console.error('  • --section-rhythm or its derived margins were changed');
    console.error('  • A .page was added or removed without updating EXPECTED_PAGE_COUNT');
    return false;
  }
  console.log('✓ Layout invariants OK on all pages');
  return true;
}

/* ─── Pipeline ────────────────────────────────────────────────── */
(async () => {
  let browser;
  let exitCode = 0;
  try {
    console.log(`✓ Using Python: ${PYTHON}`);

    // 0. Run unit tests. Fail fast on any logic regression — tests are
    //    cheap, and a failed unit test almost always indicates a problem
    //    that would also break rendered output.
    //    Delegates to scripts/run_tests.js so JS solver tests run too.
    try {
      execFileSync(process.execPath, [path.join(ROOT, 'scripts', 'run_tests.js')], {
        cwd: ROOT,
        stdio: 'inherit',
      });
    } catch (err) {
      console.error('\n❌ UNIT TESTS FAILED');
      exitCode = 1;
      return;
    }

    // 1. Compile Sass to dist/styles.css. The source partials live in
    //    styles/ with styles.scss as the entry. Source maps are
    //    disabled — the compiled CSS is a build artifact, not authored.
    //
    //    We use sass's programmatic API rather than spawning the
    //    `sass.cmd` / `sass` binary. The binary approach is brittle on
    //    Windows: Node 20+ refuses to execFile .cmd/.bat files without
    //    `shell: true` (CVE-2024-27980), and `shell: true` reintroduces
    //    quoting issues for paths containing spaces. Programmatic API
    //    sidesteps both problems and is identical on every platform.
    try {
      const sass = require('sass');
      const distDir = path.join(ROOT, 'dist');
      fs.mkdirSync(distDir, { recursive: true });
      const result = sass.compile(
        path.join(ROOT, 'styles', 'styles.scss'),
        { sourceMap: false, style: 'expanded' },
      );
      fs.writeFileSync(path.join(distDir, 'styles.css'), result.css, 'utf-8');
      console.log('✓ Compiled styles/styles.scss → dist/styles.css');
    } catch (err) {
      console.error('\n❌ SASS COMPILATION FAILED');
      // sass errors have nicely formatted .toString() output that
      // identifies the file + line + the offending source.
      console.error(err.toString());
      console.error('\nIs sass installed? Run:  npm install');
      exitCode = 1;
      return;
    }

    // 2. Measurement-mode build: produces dist/index.html with all
    //    content in a single flowing column for the solver to measure.
    //    The final paginated build comes later (step 4) using the
    //    placement decided by the solver.
    try {
      const out = execFileSync(
        PYTHON,
        [path.join(ROOT, 'scripts', 'build.py'), '--mode=measurement'],
        { cwd: ROOT, encoding: 'utf-8' },
      );
      process.stdout.write(out);
    } catch (err) {
      console.error('\n❌ MEASUREMENT BUILD STEP FAILED');
      console.error(err.stdout || err.message);
      exitCode = 1;
      return;
    }

    try {
      browser = await chromium.launch();
    } catch (err) {
      console.error('\n❌ CHROMIUM LAUNCH FAILED');
      console.error(err.message);
      console.error('\nIs Chromium installed? Run:  npx playwright install chromium');
      exitCode = 1;
      return;
    }

    const ctx = await browser.newContext();
    const page = await ctx.newPage();

    // 3. Open the measurement HTML and extract heights.
    await page.goto(HTML_PATH, { waitUntil: 'networkidle' });
    let measurements;
    try {
      measurements = await extractMeasurements(page, getMaxPagesFromYaml());
    } catch (err) {
      console.error('\n❌ MEASUREMENT EXTRACTION FAILED');
      console.error(err.message);
      exitCode = 1;
      return;
    }

    // 4. Run the layout solver and write dist/placement.json.
    let placement;
    try {
      placement = solveLayout(measurements);
    } catch (err) {
      console.error('\n❌ LAYOUT SOLVER FAILED');
      console.error(err.message);
      if (err.column) console.error(`  Column: ${err.column}`);
      if (err.block_id) console.error(`  Block:  ${err.block_id}`);
      if (err.job_id)   console.error(`  Job:    ${err.job_id}`);
      console.error('\nIf content is too dense for meta.maxPages, either:');
      console.error('  • Increase meta.maxPages in your resume YAML');
      console.error('  • Trim content (shorter bullets, fewer skills, etc.)');
      exitCode = 1;
      return;
    }
    const placementPath = path.join(ROOT, 'dist', 'placement.json');
    fs.writeFileSync(
      placementPath,
      JSON.stringify(placement, null, 2) + '\n',
      'utf-8',
    );
    console.log(`✓ Solved layout: ${placement.pages.length} page(s) → `
      + `${path.relative(ROOT, placementPath)}`);

    // 5. Final build using the solved placement.
    try {
      const out = execFileSync(
        PYTHON,
        [path.join(ROOT, 'scripts', 'build.py'), '--mode=final'],
        { cwd: ROOT, encoding: 'utf-8' },
      );
      process.stdout.write(out);
    } catch (err) {
      console.error('\n❌ FINAL BUILD STEP FAILED');
      console.error(err.stdout || err.message);
      exitCode = 1;
      return;
    }

    // 6. Reload the page so the layout invariant check runs against
    //    the FINAL HTML (not the measurement HTML).
    await page.goto(HTML_PATH, { waitUntil: 'networkidle' });

    // 7. Run invariant checks. Bail before producing artifacts if broken.
    const ok = await assertLayoutInvariant(page,
      { expectedPageCount: placement.pages.length });
    if (!ok) {
      exitCode = 1;
      return;
    }

    // 8. Print PDF to a temp path. Chromium's `page.pdf()` quantizes
    //    page dimensions to a 0.12-pt grid, producing pages ~0.23 mm
    //    oversized. Post-process via crop_pdf.py to get exact ISO A4.
    const tmpPdf = path.join(os.tmpdir(), `resume-print-${process.pid}.tmp.pdf`);
    const finalPdf = path.join(ROOT, 'print.pdf');
    await page.pdf({
      path: tmpPdf,
      width: '210mm',
      height: '297mm',
      margin: { top: '0', bottom: '0', left: '0', right: '0' },
      printBackground: true,
    });

    // 9. Crop the temp PDF to true A4 dimensions and stamp authoritative
    //    metadata from dist/pdf_meta.json (written by build.py).
    const pdfMetaPath = path.join(ROOT, 'dist', 'pdf_meta.json');
    try {
      const out = execFileSync(
        PYTHON,
        [
          path.join(ROOT, 'scripts', 'crop_pdf.py'),
          tmpPdf,
          finalPdf,
          '--meta', pdfMetaPath,
        ],
        { cwd: ROOT, encoding: 'utf-8' },
      );
      process.stdout.write(out);
    } catch (err) {
      console.error('\n❌ PDF CROP STEP FAILED');
      console.error(err.stdout || err.message);
      exitCode = 1;
      return;
    } finally {
      fs.rmSync(tmpPdf, { force: true });
    }

    // 10. Snapshot test: pixel-diff the cropped PDF against the
    //    committed fixture. Auto-bootstraps the fixture on first
    //    build (when it doesn't exist yet); on subsequent builds,
    //    a regression here fails the build.
    //    Skipped when SKIP_SNAPSHOT=1 (set by snapshot_pdf.py --update-both,
    //    which is itself going to overwrite the fixture next).
    if (process.env.SKIP_SNAPSHOT === '1') {
      console.log('ℹ  Snapshot test skipped (SKIP_SNAPSHOT=1)');
    } else {
      try {
        const out = execFileSync(
          PYTHON,
          [path.join(ROOT, 'scripts', 'snapshot_pdf.py'), '--auto-bootstrap'],
          { cwd: ROOT, encoding: 'utf-8' },
        );
        process.stdout.write(out);
      } catch (err) {
        console.error('\n❌ SNAPSHOT TEST FAILED — visual regression detected');
        console.error(err.stdout || err.message);
        console.error('\nIf this change was intentional, refresh the fixture:');
        console.error('  python scripts/snapshot_pdf.py --update');
        exitCode = 1;
        return;
      }
    }

    console.log('✓ Wrote print.pdf');
  } catch (err) {
    console.error('\n❌ UNEXPECTED ERROR');
    console.error(err.stack || err.message);
    exitCode = 1;
  } finally {
    if (browser) {
      try {
        await browser.close();
      } catch (closeErr) {
        // Don't mask the original error; just log.
        console.error('(also: browser.close() failed:', closeErr.message, ')');
      }
    }
    process.exit(exitCode);
  }
})();
