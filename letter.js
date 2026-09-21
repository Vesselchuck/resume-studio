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
 *   3. Open the HTML in Playwright and print TWO PDFs — color, then
 *      grayscale (via html.force-grayscale, exactly like resume.js).
 *   4. Crop each PDF to exact US Letter (8.5×11 in) and stamp metadata
 *      from dist/letter_meta.json via build/crop_pdf.py.
 *
 * Output: dist/<Your_Name>_Cover_Letter.pdf and
 *         dist/<Your_Name>_Cover_Letter_Grayscale.pdf, named from
 *         the profile's name — see build/_output_name.py.
 *         (The intermediate HTML stays dist/letter.html — it is
 *         build_letter.py's to name, and it is not a deliverable.)
 *
 * Run with: npm run letter
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
 * Sass compile, the navigation, the two print passes and the cropping
 * are the same code the resume runs — previously this file carried its
 * own copy of all four, and a fix to one (the temp-file naming, say, or
 * the grayscale toggle) had to be remembered in two places.
 *
 * This file is one of that module's drivers: the cold CLI. The other is
 * build/engine.js, which keeps a browser and a Python worker warm so
 * the desktop app can re-render the letter as you edit it.
 */

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');
const { detectPython } = require('./build/detect_python');
const { createPipeline, reported } = require('./build/pipeline');
const { pruneStale } = require('./build/_output_name');
const { ENV_RESUME_VARIANTS } = require('./build/_env_contract');
const c = require('./build/_console');


/* ─── Constants ───────────────────────────────────────────────── */

const ROOT = __dirname;
const PYTHON = detectPython();


/* ─── Helpers (mirrors resume.js's, trimmed) ──────────────────── */

/** Forward TTY-ness to subprocesses so crop_pdf.py keeps its color. */
function subprocessEnv() {
  const env = { ...process.env };
  if (process.stdout.isTTY) env.FORCE_COLOR = '1';
  return env;
}

/**
 * Run a Python script, streaming stderr live and replaying captured
 * stdout in order. Identical contract to resume.js's runPython.
 */
function runPython(scriptArgs, fallbackLabel) {
  try {
    const out = execFileSync(
      PYTHON,
      ['-B', ...scriptArgs],
      { cwd: ROOT, encoding: 'utf-8', env: subprocessEnv(),
        stdio: ['ignore', 'pipe', 'inherit'] },
    );
    process.stdout.write(out);
  } catch (err) {
    if (err.stdout) process.stdout.write(err.stdout);
    if (!err.stdout && !err.stderr) c.err(`${fallbackLabel}: ${err.message}`);
    throw reported(err);
  }
}


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

const pipeline = createPipeline({ root: ROOT, python: coldPython, variant: 'letter' });


/* ─── Variant selection ───────────────────────────────────────── */

/**
 * Which PDF variants this run should produce.
 *
 * Unset means both, which is what this script has always done and what
 * the committed fixtures assume — so nothing changes for anyone who
 * does not set the variable.
 *
 * A variant that is NOT selected has its dist/ file removed. That is
 * deliberate: leaving last run's grayscale PDF sitting there would let
 * the snapshot test compare a fixture against a file this build never
 * wrote, which is the kind of stale-artifact bug that passes for weeks
 * and then fails for reasons nobody can reconstruct. Absent means
 * absent.
 */
function selectedVariants() {
  const raw = (process.env[ENV_RESUME_VARIANTS] || '').trim();
  if (!raw) return { color: true, grayscale: true };

  const wanted = new Set(
    raw.split(',').map(v => v.trim().toLowerCase()).filter(Boolean),
  );
  const unknown = [...wanted].filter(v => v !== 'color' && v !== 'grayscale');
  if (unknown.length) {
    c.err(`${ENV_RESUME_VARIANTS}: unknown variant ${unknown.join(', ')}`);
    c.detail("Expected a comma-separated subset of 'color' and 'grayscale'.");
    throw reported(new Error('bad variant selection'));
  }
  if (!wanted.size) {
    c.err(`${ENV_RESUME_VARIANTS} selected no variants`);
    c.detail("Set it to 'color', 'grayscale' or both, or leave it unset.");
    throw reported(new Error('bad variant selection'));
  }
  return { color: wanted.has('color'), grayscale: wanted.has('grayscale') };
}

/** Remove the output of a variant this run is not producing. */
function dropUnbuiltVariant(target, label) {
  if (!fs.existsSync(target)) return;
  try {
    fs.rmSync(target, { force: true });
    c.info_pair('Removed stale PDF', `${path.relative(ROOT, target)} (${label} not selected)`);
  } catch (err) {
    c.warn_pair('Could not remove', `${path.relative(ROOT, target)} — ${err.message}`);
  }
}


/**
 * Clear this document's outputs from earlier builds that this one did
 * not overwrite.
 *
 * The PDFs are named after you, so the set of filenames a build
 * occupies moves when `name.first` / `name.last` does — and it moved
 * once already for everyone, when outputs stopped being called
 * {DOC}-color.pdf. Left alone, dist/ accumulates complete,
 * plausible-looking resumes under names that are no longer current,
 * in the exact directory you open when you need to attach one.
 *
 * Runs after printPdfs, so `keep` is what actually landed on disk.
 * _output_name.pruneStale does the deleting and is scoped there; this
 * only supplies the reporting.
 */
function pruneStaleOutputs(variant, keep) {
  pruneStale(
    path.join(ROOT, 'dist'),
    variant,
    keep,
    name => c.info_pair('Removed stale PDF', `dist/${name} (not this build's name)`),
    (name, why) => c.warn_pair('Could not remove', `dist/${name} — ${why}`),
  );
}


/* ─── Orchestrator ────────────────────────────────────────────── */

(async () => {
  let browser;
  let exitCode = 0;
  try {
    c.banner('Cover letter');
    c.ok_pair('Detected Python', PYTHON);
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
    const variants = selectedVariants();
    if (!variants.grayscale) dropUnbuiltVariant(pipeline.paths.grayscalePdf, 'grayscale');
    if (!variants.color) dropUnbuiltVariant(pipeline.paths.colorPdf, 'color');
    await pipeline.printPdfs(page, {
      color: variants.color ? pipeline.paths.colorPdf : null,
      grayscale: variants.grayscale ? pipeline.paths.grayscalePdf : null,
    });
    pruneStaleOutputs('letter', [
      ...(variants.color ? [pipeline.paths.colorPdf] : []),
      ...(variants.grayscale ? [pipeline.paths.grayscalePdf] : []),
    ]);
  } catch (err) {
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
        c.detail(`(also: browser.close() failed: ${closeErr.message})`);
      }
    }
    process.exitCode = exitCode;
  }
})();
