/**
 * resume.js — Build script for the resume.
 *
 * Pipeline:
 *   0. Run unit tests (Python + JS via build/run_tests.js).
 *      Fail-fast on any logic regression before producing artifacts.
 *   1. Compile Sass: styles/styles.scss → dist/styles.css
 *      via the `sass-embedded` npm package (native Dart Sass).
 *      Source maps are disabled.
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
 *  10. Snapshot test: pixel-diff both built PDFs against committed
 *      fixtures. Auto-bootstraps on first build.
 *
 * The PDFs are named after you, from `name.first` / `name.last` in
 * the profile — dist/Gaius_Caesar_Resume.pdf and
 * dist/Gaius_Caesar_Resume_Grayscale.pdf. See build/_output_name.py.
 *
 * Run with: node resume.js
 *
 * The script auto-detects Python by trying platform-appropriate
 * candidates. To override, set the PYTHON env var:
 *   bash/zsh:    PYTHON=python3.12 node resume.js
 *   cmd.exe:     set "PYTHON=py" && node resume.js
 *                (the quotes keep cmd.exe from putting the space before
 *                && into the value; detect_python trims it anyway)
 *   PowerShell:  $env:PYTHON="py"; node resume.js
 *
 * Requires:
 *   • Node:    playwright, sass-embedded   (`npm install`)
 *   • Python:  see requirements.txt
 *              (`pip install -r requirements.txt`,
 *               or `py -m pip install -r requirements.txt` on Windows)
 *
 * Internal structure
 * ──────────────────
 * Phases 1 and 2-9 live in build/pipeline.js, which owns no browser
 * and no process lifetime — it is handed a Playwright page and a
 * Python adapter and runs phases. This file is one of its two
 * drivers: the cold CLI, which launches Chromium, runs the sequence
 * once and exits. The other is build/engine.js, the warm engine the
 * desktop app uses to re-render without paying startup on every edit.
 *
 * Both drivers call the same phase functions on purpose. Two copies
 * of "measure, solve, render, print" would drift, and the moment they
 * drift this project can no longer promise that the same YAML
 * produces the same PDF. tests/test_worker_equivalence.py asserts the
 * two Python adapters produce byte-identical output.
 *
 * What stays here rather than in the pipeline: phase 0 (unit tests)
 * and phase 10 (the snapshot diff). Both are CLI concerns — a live
 * preview that pixel-diffed against a fixture on every keystroke
 * would be absurd — and this remains the only path that writes the
 * resume PDFs in dist/.
 *
 *   runTests()      — phase 0   (here)
 *   pipeline.*      — phases 1-9 (build/pipeline.js)
 *   runSnapshot()   — phase 10  (here)
 */

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');
const { detectPython } = require('./build/detect_python');
const { createPipeline, reported } = require('./build/pipeline');
const { pruneStale } = require('./build/_output_name');
const {
  ENV_SKIP_SNAPSHOT,
  ENV_RESUME_VARIANTS,
  ENV_RESUME_SNAPSHOT,
  ENV_RESUME_TESTS,
  ENV_RESUME_DATA_SOURCE,
  ENV_RESUME_DATA_FILE,
  ENV_LETTER_DATA_FILE,
  ENV_RESUME_PIPELINE_SUFFIX,
} = require('./build/_env_contract');
const c = require('./build/_console');


/* ─── Constants ───────────────────────────────────────────────── */

const ROOT = __dirname;

// Detect Python via the shared detect_python module. Done once at
// startup so all subprocess calls share the same interpreter.
const PYTHON = detectPython();


/* ─── Helpers ─────────────────────────────────────────────────── */

/**
 * Build the env passed to subprocesses (build.py, crop_pdf.py,
 * snapshot_pdf.py, the test runner). When resume.js would itself print
 * color (its stdout is a TTY, or FORCE_COLOR asks for it), forward
 * that via FORCE_COLOR=1 so subprocesses keep their ANSI codes —
 * otherwise execFileSync's pipe-captured stdout would look like
 * non-TTY to them and they'd suppress color. Asking colorEnabled()
 * rather than isTTY is what keeps a user's FORCE_COLOR=0 from being
 * overwritten with 1. NO_COLOR passthrough is automatic since
 * process.env is inherited.
 *
 * PYTHONUTF8 / PYTHONIOENCODING: the Python children print emoji (the
 * _console symbols). With stdout captured into a pipe, Windows Python
 * encodes to the ANSI code page (cp1252), where ✅ raises
 * UnicodeEncodeError and the build dies on its first status line.
 * build/engine.js sets the same two for its worker.
 */
function subprocessEnv() {
  const env = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
  if (c.colorEnabled(process.stdout)) {
    env.FORCE_COLOR = '1';
  }
  return env;
}


/**
 * Run a Python script as a subprocess, handling stdio + errors uniformly.
 *
 * The `-B` flag is added unconditionally to suppress __pycache__/
 * creation; the cwd and FORCE_COLOR-forwarding env are set the same
 * way every time.
 *
 * On success, the captured stdout is written through to the parent's
 * stdout so the subprocess's _console output appears in order.
 *
 * On failure:
 *   • The subprocess's stderr was already auto-streamed to the parent's
 *     stderr by execFileSync (Node behavior), so failure markers from
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
    // A null status means the script never ran to an exit — spawn
    // failed (a bad PYTHON) or a signal killed it — so whatever it
    // printed, it did not print why it stopped.
    if ((!err.stdout && !err.stderr) || typeof err.status !== 'number') {
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
function runNodeInherit(scriptPath, fallbackMessage, env = subprocessEnv()) {
  try {
    execFileSync(process.execPath, [scriptPath],
      { cwd: ROOT, stdio: 'inherit', env });
  } catch {
    // The child already printed its own failure markers via _console
    // (since stdio was inherited); just abort the pipeline.
    throw reported(new Error(fallbackMessage));
  }
}


/* ─── The cold Python adapter ─────────────────────────────────── */

/**
 * One subprocess per operation — the CLI's way of reaching Python.
 *
 * build/engine.js supplies a different adapter with the same two
 * methods, backed by a warm build/worker.py process. Everything in
 * build/pipeline.js is written against this shape and cannot tell the
 * difference.
 */
const coldPython = {
  buildHtml(mode) {
    runPython(
      [path.join(ROOT, 'build', 'build.py'), `--mode=${mode}`],
      mode === 'measurement' ? 'Measurement build failed' : 'Final build failed',
    );
  },

  cropPdf({ input, output, meta, quiet }) {
    const args = [path.join(ROOT, 'build', 'crop_pdf.py'), input, output];
    if (meta) args.push('--meta', meta);
    if (quiet) args.push('--quiet');
    runPython(args, 'PDF crop failed');
  },
};

const pipeline = createPipeline({ root: ROOT, python: coldPython });


/* ─── CLI-only phases ─────────────────────────────────────────── */

/**
 * Phase 0: Run unit tests via build/run_tests.js.
 *
 * A failed unit test almost always indicates a problem that would
 * also break rendered output, so this runs first and fails fast,
 * before any artifacts exist.
 *
 * Skippable via RESUME_TESTS=off — see the body for why that is safe
 * and why the default is nevertheless 'on'.
 *
 * Uses stdio:'inherit' so the runner's output (banners + per-suite
 * lines) flows directly to the user's terminal in real time.
 */
function runTests() {
  // Opt out, for the case where the code has not changed.
  //
  // These suites test the PIPELINE — the solver, the loader, the warm
  // engine's equivalence to the cold CLI. None of that changes when
  // you edit a bullet, and on the reference machine they cost ~9s,
  // most of it engine_equivalence launching Chromium to render the
  // document twice. Paying that on every save is a tax on writing.
  //
  // What validates YOUR DATA is not here and always runs: build.py's
  // validate_data, the layout invariants, the maxPages ceiling. So
  // skipping this cannot produce a wrong document — only an unchecked
  // pipeline, which is the right thing to leave to the terminal.
  //
  // Hence the default: unset means 'on', so `node resume.js` keeps
  // running them. Studio passes 'off' unless you tick the box.
  const mode = (process.env[ENV_RESUME_TESTS] || 'on').trim().toLowerCase();
  if (mode === 'off') {
    c.banner('Tests (off)');
    c.detail(`Set ${ENV_RESUME_TESTS}=on to run them before building.`,
             { stream: process.stdout });
    return;
  }
  if (mode !== 'on') {
    c.warn_pair('Unknown tests mode', `${mode} — treating as 'on'`);
  }

  // When invoked as a subprocess of snapshot_pdf.py --update-all, the
  // parent sets ENV_RESUME_PIPELINE_SUFFIX to label which data source
  // is being built ("default data" or "local data"). The suffix attaches
  // to the FIRST banner only — subsequent phase banners stay plain
  // because the data-source context is anchored at the top.
  const suffix = process.env[ENV_RESUME_PIPELINE_SUFFIX];
  c.banner(suffix ? `Tests (${suffix})` : 'Tests');
  c.ok_pair('Detected Python', PYTHON);

  // The unit tests get a clean environment, not this build's.
  //
  // tests/test_load_data.py exercises every branch of the loader by
  // setting these itself. Inheriting a value the caller happened to set
  // — Studio sets RESUME_DATA_FILE whenever you pick a data file —
  // makes the loader take a different path than the test is testing,
  // and the whole suite reports failures that have nothing to do with
  // the code under test.
  const testEnv = subprocessEnv();
  for (const name of [ENV_RESUME_DATA_SOURCE, ENV_RESUME_DATA_FILE,
                      ENV_LETTER_DATA_FILE]) {
    delete testEnv[name];
  }
  runNodeInherit(path.join(ROOT, 'build', 'run_tests.js'), 'Unit tests failed', testEnv);
}


/**
 * Phase 10: Snapshot test — off unless asked for.
 *
 * WHY IT IS OPT-IN
 * ----------------
 * The fixtures describe one particular version of one particular
 * document. That is exactly right for a template whose look should not
 * drift, and wrong for a resume, which changes because the person it
 * describes changed jobs. Every genuine edit fires this, and a check
 * that cries wolf on every real edit gets dismissed on the one build
 * where it was telling the truth.
 *
 * WHY A DIFFERENCE NO LONGER FAILS THE BUILD
 * ------------------------------------------
 * By the time this phase runs, both PDFs are already written. Exiting
 * non-zero does not un-write them — it just reports "build failed"
 * about a build that produced its output correctly. So 'on' reports
 * loudly and exits 0.
 *
 * 'strict' keeps the original fail-the-build behavior for anyone who
 * wants this in CI, where "the document changed and nobody said so"
 * should stop the pipeline.
 *
 *   RESUME_SNAPSHOT unset / 'off'  → skip (default)
 *                   'on'           → check, report, continue
 *                   'strict'       → check, fail on a difference
 *
 * SKIP_SNAPSHOT=1 still forces a skip regardless — snapshot_pdf.py
 * --update-all sets it while rebuilding the fixtures it is about to
 * replace.
 */
function runSnapshot() {
  if (process.env[ENV_SKIP_SNAPSHOT] === '1') {
    c.banner('Snapshot (skipped)');
    return;
  }

  const mode = (process.env[ENV_RESUME_SNAPSHOT] || 'off').trim().toLowerCase();
  if (mode !== 'on' && mode !== 'strict') {
    if (mode !== 'off') {
      c.warn_pair('Unknown snapshot mode', `${mode} — treating as 'off'`);
    }
    c.banner('Snapshot (off)');
    // stdout, because this continues the banner above it. _console
    // sends detail to stderr by default, which is right when it follows
    // an error and wrong here: the two streams are drained separately,
    // so the hint raced ahead of its own banner and printed before it.
    c.detail(`Set ${ENV_RESUME_SNAPSHOT}=on to compare against the fixtures.`,
             { stream: process.stdout });
    return;
  }

  // A file named through RESUME_DATA_FILE that is neither the template
  // nor data/resume.yml has no fixture to compare against, so there is
  // nothing to check — say so rather than report it as a difference.
  if (readDataSource() === 'explicit') {
    c.banner('Snapshot (skipped)');
    c.detail('Fixtures exist only for data/resume_default.yml and data/resume.yml;',
             { stream: process.stdout });
    c.detail('this build read a different file.', { stream: process.stdout });
    return;
  }

  c.banner('Snapshot');
  try {
    runPython(
      [path.join(ROOT, 'build', 'snapshot_pdf.py'), '--auto-bootstrap'],
      'Snapshot test failed',
    );
  } catch (err) {
    // Exit 1 is a visible difference; anything else (2: a missing
    // dependency, fixture or metadata file) means the comparison never
    // ran, and the subprocess has already said why.
    const differs = err.status === 1;
    if (differs) {
      // The subprocess already printed its per-page diff lines.
      c.detail('To accept this as the new baseline:');
      c.detail('  python build/snapshot_pdf.py --update');
    }
    if (mode === 'strict') {
      throw reported(err);
    }
    if (differs) {
      c.warn_pair('Snapshot differs',
        'the PDFs above were still written — see the diff files listed');
    } else {
      c.warn_pair('Snapshot not checked',
        'the PDFs above were still written — see the message above');
    }
  }
}


/** data_source from dist/pdf_meta.json, or null if it can't be read. */
function readDataSource() {
  try {
    const meta = JSON.parse(
      fs.readFileSync(path.join(ROOT, 'dist', 'pdf_meta.json'), 'utf-8'));
    return typeof meta.data_source === 'string' ? meta.data_source : null;
  } catch {
    return null;
  }
}


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
    name => c.info_pair('Removed stale PDF', `${path.join('dist', name)} (not this build's name)`),
    (name, why) => c.warn_pair('Could not remove', `${path.join('dist', name)} — ${why}`),
  );
}


/* ─── Pipeline orchestrator ───────────────────────────────────── */

(async () => {
  let browser;
  let exitCode = 0;
  try {
    // Validated first: a typo in RESUME_VARIANTS is known before any
    // work starts, and used to surface only after the tests, the Sass
    // compile, the measurement pass and the browser had all run.
    const variants = selectedVariants();

    runTests();

    c.banner('Build (measurement)');
    pipeline.compileSass();
    await pipeline.buildMeasurement();

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
    await pipeline.openDocument(page);

    const measurements = await pipeline.getMeasurements(page);
    const placement = pipeline.solveAndWritePlacement(measurements);

    c.banner('Build (final)');
    await pipeline.buildFinal();

    await pipeline.verifyInvariants(page, placement.pages.length);

    c.banner('PDF');
    if (!variants.grayscale) dropUnbuiltVariant(pipeline.paths.grayscalePdf, 'grayscale');
    if (!variants.color) dropUnbuiltVariant(pipeline.paths.colorPdf, 'color');
    await pipeline.printPdfs(page, {
      color: variants.color ? pipeline.paths.colorPdf : null,
      grayscale: variants.grayscale ? pipeline.paths.grayscalePdf : null,
    });
    pruneStaleOutputs('resume', [
      ...(variants.color ? [pipeline.paths.colorPdf] : []),
      ...(variants.grayscale ? [pipeline.paths.grayscalePdf] : []),
    ]);

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
