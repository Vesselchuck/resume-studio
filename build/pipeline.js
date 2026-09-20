/**
 * pipeline.js — The resume build phases, with no lifecycle of their own.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * These phases used to live inside resume.js, wrapped around a
 * top-level IIFE that launched Chromium, ran the sequence once and
 * exited. That shape is exactly right for a CLI and useless to a GUI,
 * which needs to run the same sequence hundreds of times against a
 * browser it keeps open.
 *
 * Copying the sequence into the GUI would have been the obvious move
 * and the wrong one: two implementations of "measure, solve, render,
 * print" drift, and when they drift the project stops being able to
 * promise that the same YAML produces the same PDF. So the phases moved
 * here, unchanged, and both drivers call them:
 *
 *     resume.js        cold CLI — one browser, one run, then exit
 *     build/engine.js  warm GUI engine — one browser, many runs
 *
 * This module owns no browser, no subprocess, no process lifetime. It
 * is handed a Playwright `page` and a `python` adapter and it runs
 * phases. That is the whole design.
 *
 * TWO DOCUMENTS, ONE PIPELINE
 * ---------------------------
 * `variant` picks which document the paths and the build phase refer
 * to. The resume needs the full measure -> solve -> final sequence
 * because it paginates across a two-column grid. The cover letter is
 * one flowing column, so it needs none of that: it builds, prints and
 * crops. The phases they share — Sass, navigation, printing, cropping —
 * are literally the same code, which is the point of putting them here
 * rather than leaving a second copy in letter.js.
 *
 * THE PYTHON ADAPTER
 * ------------------
 * The two drivers reach Python differently — the CLI spawns
 * `python -B build/build.py --mode=…` per call, the engine sends a
 * request to a warm worker — so the caller injects an adapter with two
 * named operations rather than a generic "run this command" function:
 *
 *     python.buildHtml(mode)                       -> void | Promise
 *     python.buildLetter()                         -> void | Promise
 *     python.cropPdf({input, output, meta, quiet}) -> void | Promise
 *
 * Both are awaited, so an adapter may be synchronous (the CLI's
 * execFileSync) or asynchronous (the engine's request/response to a
 * warm worker) without the phases caring which.
 *
 * Naming the operations instead of passing argv keeps the translation
 * explicit and small. Both adapters drive the same Python code;
 * tests/test_worker_equivalence.py asserts they produce identical
 * bytes.
 *
 * WHAT IS NOT HERE
 * ----------------
 * Unit tests (phase 0) and the snapshot diff (phase 10) stay in
 * resume.js. Both are CLI concerns: the GUI runs its own tests through
 * npm, and a live preview that pixel-diffed against a fixture on every
 * keystroke would be absurd. A GUI *Build* runs the snapshot by
 * shelling out to the CLI, which is the only path that should ever
 * write dist/*.pdf.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { pathToFileURL } = require('url');

const { checkLayoutInvariants } = require('./check_layout');
const { solveLayout } = require('./solve_layout');
const { extractMeasurements } = require('./measure_dom');
const { outputPaths } = require('./_output_name');
const c = require('./_console');


/**
 * Mark `err` as already-printed by a phase, so a driver's catch doesn't
 * re-emit it as "Unexpected error" with a stack trace. Phases throw
 * after calling c.err()/c.detail() (or after a subprocess whose stderr
 * is inherited has already streamed its diagnostics); wrapping the
 * throw with reported() conveys that through the call stack without
 * relying on error-message substring matching.
 */
function reported(err) {
  err.alreadyReported = true;
  return err;
}


/**
 * Build the phase set for one project root.
 *
 * @param {object}   opts
 * @param {string}   opts.root   — project root (the directory holding package.json)
 * @param {object}   opts.python — adapter: { buildHtml(mode), cropPdf({...}) }
 * @param {string}   opts.variant — 'resume' (default) or 'letter'; selects
 *   the document's paths and which build phase applies.
 * @param {string}   opts.navWait — how openDocument() decides the page is
 *   settled: 'networkidle' (default, what the CLI has always used) or
 *   'fonts'. See openDocument for why the faster option exists and what
 *   had to be true before it was allowed to.
 */
function createPipeline({ root, python, navWait = 'networkidle', variant = 'resume' }) {
  if (variant !== 'resume' && variant !== 'letter') {
    throw new Error(`unknown variant ${variant}; expected 'resume' or 'letter'`);
  }
  const isLetter = variant === 'letter';
  const htmlName = isLetter ? 'letter.html' : 'index.html';

  const paths = {
    root,
    variant,
    dist: path.join(root, 'dist'),
    html: path.join(root, 'dist', htmlName),
    htmlUrl: pathToFileURL(path.resolve(root, 'dist', htmlName)).href,
    styles: path.join(root, 'styles'),
    stylesEntry: path.join(root, 'styles', 'styles.scss'),
    css: path.join(root, 'dist', 'styles.css'),
    pdfMeta: path.join(root, 'dist', isLetter ? 'letter_meta.json' : 'pdf_meta.json'),
    placement: path.join(root, 'dist', 'placement.json'),

    // The PDFs are named after you — Gaius_Iulius_Resume.pdf, not
    // resume-color.pdf — and the name lives in the YAML, which only
    // Python reads. So these two are getters rather than strings:
    // the stem arrives in dist/pdf_meta.json, which build.py writes
    // during the measurement pass, and every read of these happens
    // after that (printing, pruning, the app's tray). Reading one
    // before any build has run yields 'Resume.pdf', which correctly
    // does not exist. See _output_name.js.
    get colorPdf() {
      return outputPaths(this.dist, this.pdfMeta, variant).colorPdf;
    },
    get grayscalePdf() {
      return outputPaths(this.dist, this.pdfMeta, variant).grayscalePdf;
    },
    get outputStem() {
      return outputPaths(this.dist, this.pdfMeta, variant).stem;
    },
  };

  /**
   * Read meta.maxPages from dist/pdf_meta.json (written by build.py).
   * Defaults to 10 if the field is missing for any reason — matches
   * the YAML default.
   */
  function getMaxPagesFromYaml() {
    try {
      const meta = JSON.parse(fs.readFileSync(paths.pdfMeta, 'utf-8'));
      if (Number.isInteger(meta.max_pages) && meta.max_pages > 0) {
        return meta.max_pages;
      }
    } catch {
      // Fall through to default.
    }
    return 10;
  }

  /* ─── Debug helpers ─────────────────────────────────────────── */

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
        const padBottom = parseFloat(cs.paddingBottom);
        const contentBottom = pageRect.bottom - padBottom;
        ['sidebar', 'main-col'].forEach((cls) => {
          const col = p.querySelector('.' + cls);
          if (!col) return;
          const colRect = col.getBoundingClientRect();
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

  /* ─── Phases ────────────────────────────────────────────────── */

  /**
   * Load dist/index.html and wait until it is safe to measure.
   *
   * 'networkidle' — the original strategy, and still the CLI's: wait
   * for 500 ms of network silence. Correct, and on a document whose
   * every asset is a local file, 500 ms of that wait is pure idling.
   * Two navigations per build made it the single largest cost in a
   * live render (measured: ~545 ms of a ~1530 ms render, twice over).
   *
   * 'fonts' — wait for `load`, then for `document.fonts.ready`. The
   * thing measurement actually depends on is the web fonts being laid
   * out; the fonts are vendored under fonts/ precisely so that no
   * network is involved. This is only sound because of that vendoring.
   * If a stylesheet ever pulls a font or an image from a CDN, this
   * strategy stops being safe and 'networkidle' has to come back.
   *
   * The CLI keeps 'networkidle' so a cold build's behavior is
   * unchanged. The engine opts into 'fonts' and its output is
   * pixel-compared against the CLI's to prove the two agree.
   */
  async function openDocument(page) {
    if (navWait === 'fonts') {
      await page.goto(paths.htmlUrl, { waitUntil: 'load' });
      await page.evaluate(() => document.fonts.ready);
      return;
    }
    await page.goto(paths.htmlUrl, { waitUntil: 'networkidle' });
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
      fs.mkdirSync(paths.dist, { recursive: true });
      const result = sass.compile(paths.stylesEntry, { sourceMap: false, style: 'expanded' });
      fs.writeFileSync(paths.css, result.css, 'utf-8');
      const cssKb = (Buffer.byteLength(result.css, 'utf-8') / 1024).toFixed(1);
      c.ok_pair('Compiled SCSS', `${path.join('dist', 'styles.css')} (${cssKb} KB)`);
    } catch (err) {
      c.err('Sass compilation failed');
      err.toString().split('\n').forEach(line => c.detail(line));
      c.detail('');
      c.detail('Is sass installed? Run:  npm install');
      throw reported(err);
    }
  }

  /**
   * True when dist/styles.css is missing or older than any .scss source.
   *
   * The CLI ignores this and always recompiles — a cold build should
   * never depend on mtimes it didn't set. The engine uses it to skip a
   * recompile between keystrokes that didn't touch a stylesheet.
   *
   * Deliberately conservative: any error reading mtimes returns true,
   * so an unreadable directory causes an extra compile rather than a
   * silently stale one. build.py's own check_stylesheet_freshness()
   * remains the backstop — if this ever returns a wrong `false`, the
   * build fails loudly instead of rendering against stale CSS.
   */
  function stylesAreStale() {
    try {
      const cssTime = fs.statSync(paths.css).mtimeMs;
      const sources = fs.readdirSync(paths.styles).filter(f => f.endsWith('.scss'));
      if (!sources.length) return true;
      return sources.some(f => fs.statSync(path.join(paths.styles, f)).mtimeMs > cssTime);
    } catch {
      return true;
    }
  }

  /**
   * Phase 2: Measurement-mode build.
   *
   * Produces dist/index.html with all content in a single flowing
   * column for the solver to measure. The final paginated build comes
   * later (phase 5) using the placement decided by the solver.
   */
  async function buildMeasurement() {
    await python.buildHtml('measurement');
  }

  /**
   * Phase 3: Extract per-element heights from the rendered measurement
   * HTML via Playwright.
   *
   * Requires the page to already have the measurement HTML loaded.
   * Returns the measurements object consumed by the solver. Honors
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
    fs.writeFileSync(paths.placement, JSON.stringify(placement, null, 2) + '\n', 'utf-8');
    const pagePlural = placement.pages.length === 1 ? 'page' : 'pages';
    c.ok_pair('Solved layout',
      `${placement.pages.length} ${pagePlural} → ${path.relative(root, paths.placement)}`);
    return placement;
  }

  /**
   * Phase 5: Final build using the solved placement.
   *
   * Consumes dist/placement.json, which was solved for one specific set
   * of job and sidebar ids. Always reached via measure → solve in the
   * sequence above; calling it against a placement solved for other
   * data fails loudly in the template rather than rendering a wrong
   * document. See op_build in build/worker.py.
   */
  async function buildFinal() {
    await python.buildHtml('final');
  }

  /**
   * The cover letter's only build phase.
   *
   * One flowing column: no measurement pass, no solver, no placement
   * file, and therefore none of the stale-placement hazard that makes
   * the resume's ordering matter. Build, print, crop.
   */
  async function buildLetter() {
    await python.buildLetter();
  }

  /**
   * Phases 6-7: Reload the final HTML and check layout invariants.
   *
   * Throws on invariant violation. The `Layout invariants: clean (N
   * pages)` success line uses the solver's expected page count for the
   * value text; on failure, prints actionable likely-causes.
   *
   * Honors DEBUG_MEASUREMENTS=1 for a per-page rendered-height dump
   * (useful when invariants fire and you want to compare against what
   * the solver thought would fit).
   */
  async function verifyInvariants(page, expectedPageCount) {
    // Reload the page so the invariant check runs against the FINAL
    // HTML (not the measurement HTML still loaded from phase 3).
    await openDocument(page);

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
   * Verify the cover letter fits on its one sheet.
   *
   * WHY THIS IS NOT COSMETIC
   * ────────────────────────
   * `.page` is a fixed 8.5 × 11in box with `overflow: hidden`, and
   * _print.scss does not unset that. The resume never runs into it
   * because the solver decides in advance what goes on each page. The
   * letter has no solver — it is one fixed sheet by design — so a
   * letter that runs long does not spill onto a second page and does
   * not warn. It is CLIPPED. The closing paragraph and your signature
   * come off the bottom and the build reports success.
   *
   * That is the failure this phase exists to catch, and it is why it
   * throws rather than warns: a cover letter missing its sign-off is
   * not a letter with a layout issue, it is the wrong document, and it
   * would be sent without anyone noticing. The message says how far
   * over it ran and roughly how much prose that is, because the fix is
   * always "cut a bit".
   *
   * WHAT IS MEASURED, AND WHAT IS NOT
   * ─────────────────────────────────
   * Not `.letter`. It is a flex item with `flex: 1 1 auto` and
   * `min-block-size: 0`, so it is free to shrink below its content:
   * its own bottom edge sits exactly on the page's content bottom
   * whether the letter is three paragraphs or thirty. Measured that
   * way the overflow is always precisely zero, which looks like a
   * passing check and is really no check at all.
   *
   * What overflows is the CONTENT inside it, so the probe is the
   * letter's last child — the signature — against the page's content
   * box (whose bottom padding is the --page-margin). That also names
   * the right thing in the error, since the signature is the first
   * casualty of a long letter. `scrollHeight - clientHeight` agrees
   * with it and would do as a cross-check; the child's rect is used
   * because it yields a distance rather than a boolean.
   *
   * A 1px tolerance absorbs sub-pixel rounding in the layout — a real
   * overflow is tens of pixels, never one.
   */
  async function verifyLetterFits(page) {
    const fit = await page.evaluate(() => {
      const sheet = document.querySelector('.page');
      const letter = document.querySelector('.letter');
      const last = letter && letter.lastElementChild;
      if (!sheet || !letter || !last) return null;

      const pad = parseFloat(window.getComputedStyle(sheet).paddingBottom);
      const limit = sheet.getBoundingClientRect().bottom - pad;

      // Leading of the prose, not of the scaffolding: the body is
      // what a writer would cut, so "about N lines" should be in the
      // body's lines.
      const body = letter.querySelector('.letter-body');
      const line = parseFloat(window.getComputedStyle(body || letter).lineHeight);

      return {
        overflowPx: last.getBoundingClientRect().bottom - limit,
        lineHeight: line || 16,
      };
    });

    if (!fit) {
      throw reported(new Error('Could not find .page/.letter to measure'));
    }

    if (process.env.DEBUG_MEASUREMENTS === '1') {
      console.log(`\n────── DEBUG: letter fit ──────\n`
        + `overflow: ${fit.overflowPx.toFixed(2)}px  `
        + `(negative = room to spare), line-height ${fit.lineHeight.toFixed(2)}px\n`);
    }

    if (fit.overflowPx > 1) {
      const lines = Math.ceil(fit.overflowPx / fit.lineHeight);
      const lineWord = lines === 1 ? 'line' : 'lines';
      c.err('The letter does not fit on one page');
      c.detail(
        `It runs ${Math.round(fit.overflowPx)}px past the bottom margin — `
        + `roughly ${lines} ${lineWord} of text.`);
      c.detail('');
      c.detail(
        'The sheet is a fixed 8.5 x 11in box that clips what overflows, '
        + 'so this would not have produced a two-page letter. It would '
        + 'have produced a one-page letter with the end missing.');
      c.detail('');
      c.detail('Shorten letter.body in your data file, then rebuild.');
      throw reported(new Error('Cover letter overflows its page'));
    }

    const room = Math.round(-fit.overflowPx);
    c.ok_pair('Fits the page', `${room}px of room left at the bottom`);
  }

  /**
   * Phases 8-9: Print the page to PDF(s), then crop each to true US
   * Letter.
   *
   * Chromium's `page.pdf()` quantizes page dimensions to a 0.12-pt
   * grid, producing pages slightly oversized. Post-process via
   * crop_pdf.py to get exact 8.5×11 in plus authoritative metadata
   * (and /Lang catalog entry) from dist/pdf_meta.json.
   *
   * Uses tmpdir-based intermediates so partially-written cropped PDFs
   * never overwrite known-good outputs if the crop step fails. Temp
   * files are unconditionally removed in the finally block — even when
   * the crop step throws.
   *
   * Grayscale variant: toggles `html.force-grayscale` on the page
   * before the second render. The class applies token-only color
   * overrides (--text-muted, --text-muted-soft, --border-rule, --accent
   * → see _print.scss for per-token rationale) and crucially does NOT
   * use `filter: grayscale(1)`: a CSS filter would force Chromium to
   * rasterize the page, producing a ~6× larger PDF (image-backed) with
   * sub-pixel layout drift relative to the color PDF. Token overrides
   * keep the output as pure vector text + strokes with byte-identical
   * layout geometry between the two variants. The class is removed
   * after the second render so the page state is clean for any
   * downstream consumers.
   *
   * @param {object}   page
   * @param {object}   targets
   * @param {?string}  targets.color     — where to write the color PDF, or null to skip
   * @param {?string}  targets.grayscale — where to write the grayscale PDF, or null to skip
   * @param {boolean}  targets.quiet     — suppress the "Wrote PDF" lines.
   *   A live preview prints to a throwaway file in the system temp
   *   directory; announcing "Wrote PDF (color): C:\Temp\studio-preview-
   *   resume-6844.pdf" on every keystroke is noise that also reads like
   *   the app is writing deliverables somewhere strange.
   *
   * The engine passes a temp path and `grayscale: null` for a live
   * preview: one variant, never into dist/. The CLI passes both real
   * dist/ paths, which is the only way dist/*.pdf is ever written.
   */
  async function printPdfs(page, targets) {
    const pdfOptions = {
      width: '8.5in',
      height: '11in',
      margin: { top: '0', bottom: '0', left: '0', right: '0' },
      printBackground: true,
    };

    let wroteOne = false;

    if (targets.color) {
      const tmp = path.join(os.tmpdir(), `resume-color-${process.pid}-${Date.now()}.tmp.pdf`);
      await page.pdf({ path: tmp, ...pdfOptions });
      try {
        await python.cropPdf({
          input: tmp, output: targets.color, meta: paths.pdfMeta, quiet: targets.quiet,
        });
      } finally {
        fs.rmSync(tmp, { force: true });
      }
      if (!targets.quiet) c.ok_pair('Wrote PDF (color)', path.relative(root, targets.color));
      wroteOne = true;
    }

    if (targets.grayscale) {
      await page.evaluate(() => document.documentElement.classList.add('force-grayscale'));
      const tmp = path.join(os.tmpdir(), `resume-grayscale-${process.pid}-${Date.now()}.tmp.pdf`);
      await page.pdf({ path: tmp, ...pdfOptions });
      await page.evaluate(() => document.documentElement.classList.remove('force-grayscale'));
      try {
        // Suppress the "Cropped" and "Stamped metadata" summary lines on
        // the second crop — they're identical to the first call's output
        // and only add noise. Errors and warnings still print. When the
        // color variant was skipped this is the only crop, so it prints
        // normally.
        await python.cropPdf({
          input: tmp, output: targets.grayscale, meta: paths.pdfMeta,
          quiet: targets.quiet || wroteOne,
        });
      } finally {
        fs.rmSync(tmp, { force: true });
      }
      if (!targets.quiet) {
        c.ok_pair('Wrote PDF (grayscale)', path.relative(root, targets.grayscale));
      }
    }
  }

  return {
    paths,
    // Exposed so a driver can reach its own adapter for operations
    // outside the phase set (the engine rasterizes through its warm
    // worker). Phases themselves always go through `python` directly.
    python,
    openDocument,
    compileSass,
    stylesAreStale,
    buildMeasurement,
    getMeasurements,
    solveAndWritePlacement,
    buildFinal,
    buildLetter,
    verifyInvariants,
    verifyLetterFits,
    printPdfs,
  };
}


module.exports = { createPipeline, reported };
