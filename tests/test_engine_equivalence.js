/**
 * test_engine_equivalence.js — the engine's shortcuts must render what
 * the plain path renders.
 *
 * WHAT THIS GUARDS
 * ----------------
 * The CLI and the engine both wait for `document.fonts.ready` after a
 * navigation instead of 500 ms of network silence. That is only sound
 * because the fonts are vendored under fonts/ and nothing in the
 * document is fetched over a network — exactly the kind of assumption
 * that quietly stops being true. The day someone adds a Google Fonts
 * @import, the build would measure an unstyled page. So this renders
 * each document both ways ('fonts' and the old 'networkidle') and
 * compares the pixels.
 *
 * The engine takes four more liberties, each checked here against the
 * plain path on real Chromium output:
 *
 *   • In-place loads (openDocument's `inPlace`): the measurements and
 *     the print after a swap must equal those after a navigation —
 *     exactly, many times over, since the failure this guards against
 *     (a stylesheet briefly detached) was intermittent.
 *   • The in-memory crop: rasterizing Chromium's raw print with the
 *     crop applied in memory must give the pixels of rasterizing the
 *     file crop_pdf.py writes.
 *   • Page keys: a page whose PDF-level key did not change is not
 *     rendered again. For an edit on page 1, one on page 2 and one that
 *     adds a glyph, the result must equal a full render.
 *   • The early cutoff: an unchanged render is not redone — and a change
 *     to the data, the stylesheet or the fonts must never be answered
 *     from it.
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
 *   2. The browser-side steps produce the same document. Covered here.
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
 * browser, seconds apart.
 *
 * Pixels rather than bytes, deliberately: Chromium stamps /CreationDate
 * into every PDF it prints, so two renders are never byte-identical.
 * That is why snapshot_pdf.py is a pixel diff and not a checksum, and
 * the comparisons here run through build/worker.py (its `compare` op,
 * or its raster op's pixel hashes) so they use that suite's own
 * rasterizer — rather than this project taking on a PNG-decoding
 * dependency in Node purely to compare two images.
 *
 * REQUIREMENTS
 * ------------
 * Playwright's Chromium. Without it the suite prints the runner's
 * `SKIP <suite>: <reason>` marker and exits 0, the same way
 * test_check_layout.js does — a contributor without browsers installed
 * should not see a red suite, but does see a yellow one, and
 * STRICT_TESTS=1 turns it red.
 *
 * That is the ONLY skip. The engine failing to load or to start (a
 * broken require, a Python worker that will not boot) is exactly the
 * regression this suite exists to catch, so it is a failure. An earlier
 * version skipped on those too, printed "0 passed, 0 failed", and the
 * runner showed a green tick for a suite that had tested nothing.
 *
 * No built dist/ is needed: renderTo() compiles the stylesheet when it
 * is stale (pipeline.stylesAreStale() is true when dist/styles.css is
 * missing), as does engine.renderPreview(). The resume data used by the
 * later checks is a temporary copy of data/resume_default.yml, named
 * through RESUME_DATA_FILE, so nothing in data/ is touched.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { assertEq, assertTrue, fail, report } = require('./_framework');

const ROOT = path.join(__dirname, '..');
const DIST = path.join(ROOT, 'dist');
const RASTER_SCALE = 2.0;

// dist/ intermediates both passes overwrite. The PDFs are never touched
// — renders here print to the system temp directory — but index.html
// and placement.json are shared scratch, and a test should hand them
// back as it found them.
const SCRATCH = [
  path.join(DIST, 'styles.css'),
  path.join(DIST, 'index.html'),
  path.join(DIST, 'placement.json'),
  path.join(DIST, 'pdf_meta.json'),
  path.join(DIST, 'letter.html'),
  path.join(DIST, 'letter_meta.json'),
];


const SUITE = 'test_engine_equivalence';

/** The runner's skip marker (see build/run_tests.js). Chromium only. */
function skip(reason) {
  console.log(`SKIP ${SUITE}: ${reason}`);
  process.exitCode = 0;
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


/* ─── Helpers for the engine's shortcuts ──────────────────────── */

/** Page hashes of a PDF, rendered in full (no slot: nothing skipped). */
async function hashes(worker, pdf, crop) {
  const r = await worker.raster({ pdfPath: pdf, scale: RASTER_SCALE, crop });
  return r.images.map(im => im.hash);
}

/** Replace `from` with `to` in a file, insisting that it was there. */
function edit(file, from, to) {
  const text = fs.readFileSync(file, 'utf-8');
  if (!text.includes(from)) throw new Error(`fixture: "${from}" not in ${file}`);
  fs.writeFileSync(file, text.replace(from, to));
}

/**
 * In-place loads against navigations, on the measurement and the final
 * HTML alternately: the measurements and the print must be identical.
 */
async function swapMatchesGoto(pipeline, page, worker, temps) {
  if (pipeline.stylesAreStale()) pipeline.compileSass();
  await pipeline.buildMeasurement();
  assertEq(await pipeline.openDocument(page), 'goto', 'swap: the first load navigates');
  const measured = await pipeline.getMeasurements(page);
  const placement = pipeline.solveAndWritePlacement(measured);
  await pipeline.buildFinal();
  await pipeline.openDocument(page);
  const gotoPdf = path.join(os.tmpdir(), `equiv-goto-${process.pid}.pdf`);
  temps.push(gotoPdf);
  await pipeline.printPreviewPdf(page, gotoPdf);
  const want = await hashes(worker, gotoPdf, 'letter');

  const ROUNDS = 40;
  let swaps = 0;
  let sameMeasurements = 0;
  let samePrints = 0;
  for (let i = 0; i < ROUNDS; i++) {
    await pipeline.buildMeasurement();
    if (await pipeline.openDocument(page, { inPlace: true }) === 'swap') swaps++;
    if (JSON.stringify(await pipeline.getMeasurements(page)) === JSON.stringify(measured)) {
      sameMeasurements++;
    }
    pipeline.solveAndWritePlacement(measured);
    await pipeline.buildFinal();
    if (await pipeline.verifyInvariants(page, placement.pages.length, { inPlace: true }) === 'swap') swaps++;
    if (i % 8 === 0) {
      const swapPdf = path.join(os.tmpdir(), `equiv-swap-${process.pid}-${i}.pdf`);
      temps.push(swapPdf);
      await pipeline.printPreviewPdf(page, swapPdf);
      if (JSON.stringify(await hashes(worker, swapPdf, 'letter')) === JSON.stringify(want)) samePrints++;
    }
  }
  assertEq(swaps, ROUNDS * 2, 'swap: every in-place load was done in place');
  assertEq(sameMeasurements, ROUNDS, 'swap: measurements after an in-place load equal a navigation\'s');
  assertEq(samePrints, Math.ceil(ROUNDS / 8), 'swap: the print after an in-place load equals a navigation\'s');

  // Another document in the page: the pipeline navigates, never swaps.
  await page.goto('about:blank');
  assertEq(await pipeline.openDocument(page, { inPlace: true }), 'goto',
    'swap: a page showing another document is navigated');
}

/** Raster with the in-memory crop == raster of crop_pdf.py's file. */
async function inMemoryCropMatchesFile(pipeline, page, worker, temps) {
  const raw = path.join(os.tmpdir(), `equiv-raw-${pipeline.paths.variant}-${process.pid}.pdf`);
  const cropped = path.join(os.tmpdir(), `equiv-cropped-${pipeline.paths.variant}-${process.pid}.pdf`);
  temps.push(raw, cropped);
  await pipeline.printPreviewPdf(page, raw);
  await worker.cropPdf({ input: raw, output: cropped, meta: pipeline.paths.pdfMeta, quiet: true });
  const inMemory = await hashes(worker, raw, 'letter');
  const fromFile = await hashes(worker, cropped, null);
  assertTrue(inMemory.length > 0, `crop: ${pipeline.paths.variant} printed pages`);
  assertEq(inMemory, fromFile,
    `crop: ${pipeline.paths.variant} — the in-memory crop rasterizes like crop_pdf.py's file`);
}

/**
 * Page keys on real prints: after each edit, the raster that skips
 * unchanged pages must equal a full render of the same print.
 */
async function pageKeysOnRealEdits(pipeline, page, worker, dataFile, temps) {
  const slot = `equiv-test-${process.pid}`;
  const printAndRaster = async (label) => {
    await pipeline.buildMeasurement();
    await pipeline.openDocument(page);
    const placement = pipeline.solveAndWritePlacement(await pipeline.getMeasurements(page));
    await pipeline.buildFinal();
    await pipeline.verifyInvariants(page, placement.pages.length);
    const pdf = path.join(os.tmpdir(), `equiv-keys-${label}-${process.pid}.pdf`);
    temps.push(pdf);
    await pipeline.printPreviewPdf(page, pdf);
    const skipping = await worker.raster({ pdfPath: pdf, scale: RASTER_SCALE, crop: 'letter', slot });
    const full = await hashes(worker, pdf, 'letter');
    return { skipping, full };
  };

  const base = await printAndRaster('base');
  assertEq(base.skipping.rendered, base.full.length, 'keys: the first render renders every page');
  assertTrue(base.full.length >= 2, 'keys: the fixture resume has at least two pages');

  const cases = [
    ['a bullet on page 1', 'Phasellus scelerisque magna', 'Phasellus scelerisque magnum', 0],
    ['a bullet on page 2', 'Sed finibus accumsan', 'Sed finibus accumsam', 1],
    ['a new glyph', 'Aliquam mattis', 'Aliquam Žmattis', null],
  ];
  let before = base.full;
  for (const [label, from, to, onlyPage] of cases) {
    edit(dataFile, from, to);
    const r = await printAndRaster(label.replace(/\W+/g, '-'));
    assertEq(r.skipping.images.map(im => im.hash), r.full,
      `keys: after ${label}, the pages equal a full render`);
    if (onlyPage !== null) {
      const changed = r.full.map((h, i) => h !== before[i]);
      assertTrue(changed[onlyPage], `keys: ${label} changed that page's pixels`);
      assertTrue(r.skipping.rendered < r.full.length,
        `keys: ${label} left an unchanged page unrendered (${r.skipping.rendered} of ${r.full.length} rendered)`);
    }
    before = r.full;
  }
}

/** The early cutoff answers only a render whose inputs did not change. */
async function earlyCutoff(engine, dataFile) {
  const env = { RESUME_DATA_FILE: dataFile, RESUME_DATA_SOURCE: null };
  const render = () => engine.renderPreview({ doc: 'resume', env });
  const hashesOf = r => r.images.map(im => im.hash);

  const first = await render();
  const same = await render();
  assertEq(same.timings.cached, 'render', 'cutoff: an unchanged render is not redone');
  assertEq(hashesOf(same), hashesOf(first), 'cutoff: ...and returns the same pages');
  assertTrue(same.images.every(im => im.png), 'cutoff: ...each with its PNG');

  edit(dataFile, '    jobs:\n', '    jobs:\n      # a comment only\n');
  const comment = await render();
  assertEq(comment.timings.cached, 'render', 'cutoff: a comment-only edit is not re-rendered');

  edit(dataFile, 'Donec a venenatis', 'Donec e venenatis');
  const real = await render();
  assertTrue(real.timings.cached === undefined, 'cutoff: a real edit is rendered');
  assertTrue(hashesOf(real)[0] !== hashesOf(first)[0], 'cutoff: ...and page 1 shows it');
  edit(dataFile, 'Donec e venenatis', 'Donec a venenatis');
  const back = await render();
  assertEq(hashesOf(back), hashesOf(first), 'cutoff: reverting the edit gives the first pages back');

  // The stylesheet changes under the engine (a Build recompiles it, say).
  const css = path.join(DIST, 'styles.css');
  const cssBefore = fs.readFileSync(css);
  fs.writeFileSync(css, Buffer.concat([cssBefore, Buffer.from('\n.cutoff-test { color: red; }\n')]));
  const styled = await render();
  assertTrue(styled.timings.cached === undefined, 'cutoff: a changed styles.css is rendered');
  assertEq((styled.timings.loads || [])[0], 'goto', 'cutoff: ...after a real navigation');
  fs.writeFileSync(css, cssBefore);
  const unstyled = await render();
  assertTrue(unstyled.timings.cached === undefined, 'cutoff: restoring styles.css is rendered');
  assertEq(hashesOf(unstyled), hashesOf(first), 'cutoff: ...and matches the first render');

  // A font file replaced (same name, new time): a real navigation.
  const font = fs.readdirSync(path.join(ROOT, 'fonts')).find(f => f.endsWith('.woff2'));
  const fontPath = path.join(ROOT, 'fonts', font);
  const st = fs.statSync(fontPath);
  try {
    // Seconds as numbers, not Dates, so the restore below keeps the
    // original time to well under a millisecond.
    fs.utimesSync(fontPath, st.atimeMs / 1000, st.mtimeMs / 1000 + 5);
    const refonted = await render();
    assertTrue(refonted.timings.cached === undefined, 'cutoff: a changed font file is rendered');
    assertEq((refonted.timings.loads || [])[0], 'goto', 'cutoff: ...after a real navigation');
    assertEq(hashesOf(refonted), hashesOf(first), 'cutoff: ...with the same pages');
  } finally {
    fs.utimesSync(fontPath, st.atimeMs / 1000, st.mtimeMs / 1000);
  }

  // A Sass compile forgets everything.
  const recompiled = await engine.renderPreview({ doc: 'resume', env, recompileStyles: true });
  assertTrue(recompiled.timings.cached === undefined, 'cutoff: a Sass recompile forgets the last render');
  assertEq((recompiled.timings.loads || [])[0], 'goto', 'cutoff: ...and navigates');

  // The caller's own images: unchanged pages come back without a PNG.
  const known = Object.fromEntries(first.images.map(im => [im.page, im.hash]));
  const again = await engine.renderPreview({ doc: 'resume', env, known });
  assertEq(again.timings.cached, 'render', 'cutoff: a caller holding the pages gets the cached render');
  assertTrue(again.images.every(im => im.unchanged && !im.png), 'cutoff: ...as {hash, unchanged}');
}


(async () => {
  // Chromium first: its absence is the one condition that is a skip,
  // and checking it before the engine means nothing is started only to
  // be torn down again.
  let chromium;
  try {
    ({ chromium } = require('playwright'));
  } catch (err) {
    return skip(`playwright not installed (${err.message.split('\n')[0]})`);
  }

  let browser;
  try {
    browser = await chromium.launch();
  } catch (err) {
    return skip(`chromium launch failed (${err.message.split('\n')[0]})`);
  }

  // From here on, anything that goes wrong is a failure.
  let createEngine, createPipeline;
  try {
    ({ createEngine } = require('../build/engine'));
    ({ createPipeline } = require('../build/pipeline'));
  } catch (err) {
    fail('the engine and pipeline modules load', { error: err.stack || err.message });
    try { await browser.close(); } catch { /* already gone */ }
    return report();
  }

  let engine;
  try {
    engine = await createEngine({ root: ROOT });
  } catch (err) {
    fail('the engine starts (Python worker boots)', { error: err.stack || err.message });
    try { await browser.close(); } catch { /* already gone */ }
    return report();
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

      // Same worker, same page, same data — only navWait differs. The
      // CLI and the engine both use 'fonts' (the default) now;
      // 'networkidle' is the wait they both used to rely on.
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
              `The fonts-ready navigation (the CLI's and the engine's) may no longer be safe — ` +
              `check whether a stylesheet started loading a remote asset.` +
              (result.note ? ` ${result.note}` : ''),
          });
        }
      }
    }

    // The engine's shortcuts, on the real template data (a copy).
    const dataFile = path.join(os.tmpdir(), `equiv-resume-${process.pid}.yml`);
    temps.push(dataFile);
    fs.copyFileSync(path.join(ROOT, 'data', 'resume_default.yml'), dataFile);
    const resumeWorker = engine.pipelines.resume.python;
    resumeWorker.buildEnv = { RESUME_DATA_FILE: dataFile, RESUME_DATA_SOURCE: null };
    try {
      await swapMatchesGoto(engine.pipelines.resume, page, resumeWorker, temps);
      await inMemoryCropMatchesFile(engine.pipelines.resume, page, resumeWorker, temps);
      await pageKeysOnRealEdits(engine.pipelines.resume, page, resumeWorker, dataFile, temps);
    } finally {
      resumeWorker.buildEnv = null;
    }
    await engine.pipelines.letter.buildLetter();
    await engine.pipelines.letter.openDocument(page);
    await inMemoryCropMatchesFile(engine.pipelines.letter, page, resumeWorker, temps);

    fs.copyFileSync(path.join(ROOT, 'data', 'resume_default.yml'), dataFile);
    await earlyCutoff(engine, dataFile);

    // An unchanged page is not re-encoded: a second preview of the same
    // document reuses the first one's image, byte for byte.
    const first = await engine.renderPreview({ doc: 'letter' });
    const again = await engine.renderPreview({ doc: 'letter' });
    assertTrue(again.timings.reusedPages === again.images.length,
      'a preview of an unchanged document reuses every page image');
    assertTrue(again.images.every((im, i) => im.png === first.images[i].png && im.hash === first.images[i].hash),
      'reused page images are identical to the ones they replace');
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
