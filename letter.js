/**
 * letter.js — Build script for the cover letter.
 *
 * Sibling of resume.js for the single-page cover letter. Because the
 * cover letter is one flowing column (no two-column grid, no content
 * that bridges pages), it does NOT need the resume's measurement pass,
 * layout solver, or layout-invariant checks. The pipeline is therefore
 * short:
 *
 *   1. Compile Sass: styles/styles.scss → dist/styles.css
 *      (the same stylesheet the resume uses; it now also contains the
 *      letter partial).
 *   2. Build HTML: build/build_letter.py → dist/letter.html
 *      (+ dist/letter_meta.json, dist/favicon.svg).
 *   3. Open the HTML in Playwright and print the PDF.
 *   4. Crop it to exact US Letter (8.5×11 in) and stamp metadata
 *      from dist/letter_meta.json via build/crop_pdf.py.
 *
 * Output: dist/<Your_Name>_Cover_Letter.pdf, named from the profile's
 *         name — see build/_output_name.py.
 *         (The intermediate HTML stays dist/letter.html — it is
 *         build_letter.py's to name, and it is not a deliverable.)
 *
 * Run with: node letter.js
 *
 * Python is auto-detected (or set the PYTHON env var) the same way as
 * resume.js — both use build/detect_python.js.
 *
 * Requires: playwright + sass-embedded (`npm install`) and the Python deps in
 * requirements.txt. Shares all of those with the resume build; no new
 * dependencies are introduced.
 *
 * Internal structure
 * ──────────────────
 * The phases live in build/pipeline.js, created with variant 'letter',
 * which selects this document's paths and its single build step. The
 * Sass compile, the navigation, the print and the cropping are the same
 * code the resume runs — previously this file carried its own copy of
 * all four, and a fix to one (the temp-file naming, say) had to be
 * remembered in two places.
 *
 * This file is one of that module's drivers: the cold CLI. The other is
 * build/engine.js, which keeps a browser and a Python worker warm so
 * the desktop app can re-render the letter as you edit it.
 */

// Before anything heavy is required: V8 reuses the compiled form of
// every module loaded after this line. require('playwright') is most of
// this script's start-up, and the Studio's Build runs this script as a
// fresh process every time. Same cache as the Studio's own; see
// build/_compile_cache.js.
require('./build/_compile_cache').enable();

const { chromium } = require('playwright');
const path = require('path');
const { detectPython } = require('./build/detect_python');
const { createPipeline, reported } = require('./build/pipeline');
const { recordBuilt, seedRecord } = require('./build/_output_name');
const { pythonRunner, pruneStaleOutputs, readDataSource } = require('./build/_cli');
const distLock = require('./build/_dist_lock');
const c = require('./build/_console');


/* ─── Constants ───────────────────────────────────────────────── */

const ROOT = __dirname;
const PYTHON = detectPython();
const runPython = pythonRunner(ROOT, PYTHON);


/* ─── The cold Python adapter ─────────────────────────────────── */

/**
 * One subprocess per operation. build/engine.js supplies a different
 * adapter with the same methods, backed by a warm build/worker.py.
 *
 * buildHtml is present but unused here: the letter has no measurement
 * or final mode. It is included so the adapter satisfies the same
 * shape the resume driver passes, and throws rather than silently
 * doing nothing if a future phase ever reaches for it on a letter.
 */
const coldPython = {
  buildHtml() {
    c.err('Internal error: the cover letter has no measurement/final modes');
    throw reported(new Error('the cover letter has no measurement/final modes'));
  },

  buildLetter() {
    runPython(
      [path.join(ROOT, 'build', 'build_letter.py')],
      'Letter HTML build failed',
    );
  },

  cropPdf({ input, output, meta, quiet }) {
    const args = [path.join(ROOT, 'build', 'crop_pdf.py'), input, output];
    if (meta) args.push('--meta', meta);
    if (quiet) args.push('--quiet');
    runPython(args, 'PDF crop failed');
  },
};

// navWait 'fonts': load + document.fonts.ready, as in resume.js and the
// Studio's engine. See openDocument in build/pipeline.js.
const pipeline = createPipeline({
  root: ROOT, python: coldPython, variant: 'letter', navWait: 'fonts',
});


/* ─── Orchestrator ────────────────────────────────────────────── */

(async () => {
  let browser;
  let exitCode = 0;
  let releaseDist = () => {};
  try {
    c.banner('Cover letter');
    c.ok_pair('Detected Python', PYTHON);
    // This build owns dist/ until its PDF is written and the stale ones
    // pruned; see build/_dist_lock.js.
    releaseDist = await distLock.acquire(path.join(ROOT, 'dist'), {
      owner: 'node letter.js',
      blocking: true,     // execFileSync steps: heartbeat from a thread
      onWait: who => c.info_pair('Waiting for dist/', `in use by ${who}`),
    });
    seedRecord(pipeline.paths.dist, 'letter', pipeline.paths.pdfMeta);
    pipeline.compileSass();
    await pipeline.buildLetter();

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
    await pipeline.openDocument(page);

    await pipeline.verifyLetterFits(page);

    c.banner('PDF');
    // Read once: `paths.pdf` re-reads the metadata on every access.
    const output = pipeline.paths.pdf;
    await pipeline.printPdfs(page, { output });
    recordBuilt(pipeline.paths.dist, 'letter', output, { dataSource: readDataSource(pipeline.paths.pdfMeta) });
    pruneStaleOutputs(ROOT, 'letter', [output]);
  } catch (err) {
    if (!err.alreadyReported) {
      c.err('Unexpected error');
      (err.stack || err.message).split('\n').forEach(line => c.detail(line));
    }
    exitCode = 1;
  } finally {
    releaseDist();
    if (browser) {
      try {
        await browser.close();
      } catch (closeErr) {
        c.detail(`(also: browser.close() failed: ${closeErr.message})`);
      }
    }
    process.exitCode = exitCode;
  }
})();
