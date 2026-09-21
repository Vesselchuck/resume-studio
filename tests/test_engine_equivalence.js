/**
 * test_engine_equivalence.js — the engine's fast navigation must render
 * what the CLI's careful one renders.
 *
 * WHAT THIS GUARDS
 * ----------------
 * build/engine.js takes exactly one liberty the CLI does not: it waits
 * for `document.fonts.ready` after a navigation instead of 500 ms of
 * network silence. That single change took a live render from ~1530 ms
 * to ~540 ms, and it is only sound because the fonts are vendored under
 * fonts/ and nothing in the document is fetched over a network.
 *
 * "Only sound because of an assumption about the stylesheets" is
 * exactly the kind of optimization that quietly stops being true. The
 * day someone adds a Google Fonts @import, the engine would measure an
 * unstyled page and the app would show a layout the PDF does not have.
 * So this renders each document both ways and compares the pixels.
 *
 * HOW THE CLAIM IS DECOMPOSED
 * ---------------------------
 * "The engine renders what the CLI prints" is two independent claims:
 *
 *   1. The warm Python worker produces what the CLI's subprocesses do.
 *      Covered by tests/test_worker_equivalence.py, and covered more
 *      strictly than here — byte equality, because build.py's HTML and
 *      crop_pdf.py's output for a given input are deterministic.
 *
 *   2. The fonts-ready navigation produces what networkidle does.
 *      Covered here.
 *
 * Testing them separately is what lets each one be exact. An earlier
 * version of this file tried to prove both at once by comparing a live
 * render against whatever built PDF happened to be sitting in dist/ —
 * a PDF from some *previous* build, of possibly different data. It
 * passed when the preceding build happened to be fresh and failed
 * otherwise. A test that depends on ambient state it did not establish
 * is not a test.
 *
 * Both sides here are produced now, from the same data, in the same
 * browser, seconds apart. The only variable is the navigation strategy.
 *
 * Pixels rather than bytes, deliberately: Chromium stamps /CreationDate
 * into every PDF it prints, so two renders are never byte-identical.
 * That is why snapshot_pdf.py is a pixel diff and not a checksum, and
 * the comparison here runs through build/worker.py's `compare` op so it
 * uses that suite's own rasterizer, tolerance and threshold — rather
 * than this project taking on a PNG-decoding dependency in Node purely
 * to compare two images.
 *
 * REQUIREMENTS
 * ------------
 * Playwright's Chromium and a compiled dist/styles.css. Skips cleanly
 * without either, the same way test_check_layout.js does — a
 * contributor without browsers installed should not see a red suite.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { assertTrue, fail, report } = require('./_framework');

const ROOT = path.join(__dirname, '..');
const DIST = path.join(ROOT, 'dist');
const RASTER_SCALE = 2.0;

// dist/ intermediates both passes overwrite. The PDFs are never touched
// — renders here print to the system temp directory — but index.html
// and placement.json are shared scratch, and a test should hand them
// back as it found them.
const SCRATCH = [
  path.join(DIST, 'index.html'),
  path.join(DIST, 'placement.json'),
  path.join(DIST, 'pdf_meta.json'),
  path.join(DIST, 'letter.html'),
  path.join(DIST, 'letter_meta.json'),
];


function skip(reason) {
  console.log(`\n  (skipped: ${reason})`);
  report();
}

function snapshot(paths) {
  return paths.map(p => [p, fs.existsSync(p) ? fs.readFileSync(p) : null]);
}

function restore(saved) {
  for (const [p, data] of saved) {
    try {
      if (data === null) fs.rmSync(p, { force: true });
      else fs.writeFileSync(p, data);
    } catch { /* cleanup, not an assertion */ }
  }
}

/**
 * Run one document's full sequence on `page` and print a color PDF.
 *
 * Drives the pipeline's phases rather than the engine's renderPreview,
 * so the identical code path can be pointed at either navigation
 * strategy — which is the whole comparison.
 */
async function renderTo(pipeline, page, outPath, variant) {
  if (pipeline.stylesAreStale()) pipeline.compileSass();

  if (variant === 'letter') {
    await pipeline.buildLetter();
    await pipeline.openDocument(page);
  } else {
    await pipeline.buildMeasurement();
    await pipeline.openDocument(page);
    const measurements = await pipeline.getMeasurements(page);
    const placement = pipeline.solveAndWritePlacement(measurements);
    await pipeline.buildFinal();
    await pipeline.verifyInvariants(page, placement.pages.length);
  }

  await pipeline.printPdfs(page, { color: outPath, grayscale: null, quiet: true });
}


(async () => {
  if (!fs.existsSync(path.join(DIST, 'styles.css'))) {
    return skip('dist/ not built — run `node resume.js` first');
  }

  let createEngine, createPipeline, chromium;
  try {
    ({ createEngine } = require('../build/engine'));
    ({ createPipeline } = require('../build/pipeline'));
    ({ chromium } = require('playwright'));
  } catch (err) {
    return skip(`engine unavailable (${err.message})`);
  }

  let engine;
  try {
    engine = await createEngine({ root: ROOT });
  } catch (err) {
    return skip(`engine failed to start (${err.message})`);
  }

  let browser;
  try {
    browser = await chromium.launch();
  } catch (err) {
    // The condition test_check_layout.js skips on: no browser binary.
    await engine.dispose();
    return skip(`Chromium unavailable (${err.message.split('\n')[0]})`);
  }

  const saved = snapshot(SCRATCH);
  const temps = [];

  try {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();

    for (const variant of ['resume', 'letter']) {
      const label = variant === 'resume' ? 'Resume' : 'Cover Letter';
      const worker = engine.pipelines[variant].python;

      const fastPdf = path.join(os.tmpdir(), `equiv-fast-${variant}-${process.pid}.pdf`);
      const slowPdf = path.join(os.tmpdir(), `equiv-slow-${variant}-${process.pid}.pdf`);
      temps.push(fastPdf, slowPdf);

      // Same worker, same page, same data — only navWait differs.
      const slowPipeline = createPipeline({
        root: ROOT, python: worker, variant, navWait: 'networkidle',
      });

      await renderTo(engine.pipelines[variant], page, fastPdf, variant);
      await renderTo(slowPipeline, page, slowPdf, variant);

      assertTrue(fs.existsSync(fastPdf), `${label}: fonts-ready render produced a PDF`);
      assertTrue(fs.existsSync(slowPdf), `${label}: networkidle render produced a PDF`);

      const cmp = await worker.compare({ a: fastPdf, b: slowPdf, scale: RASTER_SCALE });

      if (!cmp.pageCountMatches) {
        fail(`${label}: both strategies agree on page count`, {
          actual: `${cmp.aPages} pages (fonts-ready)`,
          expected: `${cmp.bPages} pages (networkidle)`,
        });
        continue;
      }

      assertTrue(cmp.pages.length > 0, `${label}: comparison covered at least one page`);

      for (const result of cmp.pages) {
        if (result.pass) {
          assertTrue(true, `${label} page ${result.page} matches the CLI's navigation`);
        } else {
          fail(`${label} page ${result.page} matches the CLI's navigation`, {
            error:
              `${(result.fraction * 100).toFixed(4)}% of pixels differ ` +
              `(threshold ${(cmp.threshold * 100).toFixed(1)}%). ` +
              `The engine's fonts-ready navigation may no longer be safe — ` +
              `check whether a stylesheet started loading a remote asset.` +
              (result.note ? ` ${result.note}` : ''),
          });
        }
      }
    }
  } catch (err) {
    fail('engine equivalence run', { error: err.stack || err.message });
  } finally {
    for (const t of temps) {
      try { fs.rmSync(t, { force: true }); } catch { /* best effort */ }
    }
    restore(saved);
    try { await browser.close(); } catch { /* already gone */ }
    await engine.dispose();
  }

  report();
})();
