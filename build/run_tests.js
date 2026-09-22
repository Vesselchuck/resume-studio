/**
 * run_tests.js — Cross-platform unit test runner.
 *
 * Runs both Python unittest tests under tests/test_*.py and Node
 * test files under tests/test_*.js. Picks the right Python via the
 * shared detect_python module so the python3-vs-py difference on
 * Windows is invisible.
 *
 * Output format
 * ─────────────
 * In discover-all mode (no arg), the runner suppresses the per-test
 * dots from unittest and the JS suites and prints a single c.ok()
 * line per runner with the test count and elapsed time, matching
 * the rest of the pipeline's output style. Failures are streamed
 * verbatim from the underlying runner so the diagnostic detail
 * (assertion text, stack traces) reaches the user unchanged.
 *
 * In single-target mode (arg given), the underlying runner's
 * native output is used as-is — useful when iterating on a specific
 * test file and you want the raw `unittest -v` or test-suite output.
 *
 * Skipped suites
 * ──────────────
 * A JS test file may exit 0 after printing "SKIP <name>: <reason>"
 * (e.g. test_check_layout.js when Playwright's browser binary is
 * missing). The runner detects SKIP markers and shows them as
 * yellow warnings via c.warn_pair(), distinct from passing suites.
 *
 * A suite that exits 0 but reports zero passed tests (or no parseable
 * summary at all) is never shown as a pass: it is a yellow "ran 0
 * tests" warning and counts as a skip. The same holds for Python's
 * "Ran 0 tests".
 *
 *   STRICT_TESTS=1 converts SKIP into a hard failure (exit code 1).
 *   Use this in CI to ensure no suite is silently bypassed. Accepts
 *   1/true/on/yes (any case); anything else, including 0/false, is off.
 *
 * Run with:  node build/run_tests.js
 *
 * Optional argument: a specific test path. For Python this is a
 * dotted path passed to unittest. For JS, pass the bare file stem;
 * the runner matches it against tests/test_*.js.
 *
 *   node build/run_tests.js
 *   node build/run_tests.js test_validate_data
 *   node build/run_tests.js test_solve_layout
 *
 * Behavior:
 *   • No arg → runs ALL Python tests, then ALL JS tests. Exits with
 *     code 1 if any test failed, 0 if all passed.
 *   • Arg matching a Python module → runs only that Python test.
 *   • Arg matching a JS file (tests/test_<arg>.js exists) → runs
 *     only that JS test.
 *   • Arg matching neither → exits 2 with an error.
 *
 * Exits 0 on success, 1 on any test failure (or skip when
 * STRICT_TESTS=1), 2 on runner/usage error.
 */

const path = require('path');
const fs = require('fs');
const { execFileSync, spawnSync } = require('child_process');
const { detectPython } = require('./detect_python');
const c = require('./_console');

const ROOT = path.resolve(__dirname, '..');
const TESTS_DIR = path.join(ROOT, 'tests');
const PYTHON = detectPython();

const userArg = process.argv[2];

/**
 * Parse a boolean-ish env var: 1/true/on/yes (trimmed, any case) is on,
 * anything else — including "0", "false", "off" and the empty string —
 * is off. `Boolean(process.env.X)` is the trap this avoids: every
 * non-empty string is truthy, so STRICT_TESTS=0 used to turn strict
 * mode ON.
 */
function envFlag(name) {
  const raw = process.env[name];
  if (raw === undefined) return false;
  return ['1', 'true', 'on', 'yes'].includes(String(raw).trim().toLowerCase());
}

/**
 * The env for Python children. Test output and the modules under test
 * print emoji (the _console symbols); on Windows a captured pipe
 * defaults to the ANSI code page (cp1252), where encoding ✅ raises
 * UnicodeEncodeError and the suite dies for reasons unrelated to what
 * it tests. build/engine.js sets the same two for its worker.
 */
function pythonEnv() {
  return { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
}

/**
 * Describe why a child produced no exit status. spawnSync reports a
 * spawn failure (ENOENT for a misspelled PYTHON, EACCES, a timeout) in
 * r.error and leaves r.status null; a signal kill sets r.signal. Either
 * way the child printed nothing, so without this the user sees
 * "failures (exit 2)" and no reason.
 */
function describeNoStatus(r, cmd) {
  if (r.error) return `could not run ${cmd}: ${r.error.message}`;
  if (r.signal) return `${cmd} was killed by ${r.signal}`;
  return `${cmd} exited without a status`;
}

/* ──────────────────────────────────────────────────────────────
   Single-target mode: pass-through runners (preserve native output)
   ────────────────────────────────────────────────────────────── */

function runPythonInherit(args) {
  try {
    execFileSync(PYTHON, args, { cwd: ROOT, stdio: 'inherit', env: pythonEnv() });
    return 0;
  } catch (err) {
    if (typeof err.status === 'number') return err.status;
    c.err(`could not run ${PYTHON}: ${err.message}`);
    return 2;
  }
}

function runNodeInherit(jsFile) {
  try {
    execFileSync(process.execPath, [jsFile], { cwd: ROOT, stdio: 'inherit' });
    return 0;
  } catch (err) {
    if (typeof err.status === 'number') return err.status;
    c.err(`could not run ${path.basename(jsFile)}: ${err.message}`);
    return 2;
  }
}

/* ──────────────────────────────────────────────────────────────
   Discover-all mode: capture + summarize
   ────────────────────────────────────────────────────────────── */

/**
 * Run python -m unittest and emit a summary line.
 *
 * unittest writes its dots and the summary line ("Ran N tests in
 * X.XXXs") to stderr, plus an "OK" or failure block. We capture
 * everything; on success print a one-liner; on failure stream the
 * captured output so the user sees the assertion details.
 *
 * Returns { exitCode, skipped }. `skipped` is true if the suite
 * exited 0 but reported skipped tests (unittest prints
 * "OK (skipped=N)" in that case). The dispatcher uses `skipped`
 * to honor STRICT_TESTS.
 */
function runPythonCaptured(args, label) {
  const start = Date.now();
  const r = spawnSync(PYTHON, args, { cwd: ROOT, encoding: 'utf-8', env: pythonEnv() });
  const elapsed = ((Date.now() - start) / 1000).toFixed(3);
  const code = typeof r.status === 'number' ? r.status : 2;

  const combined = (r.stdout || '') + (r.stderr || '');

  // "Ran 0 tests" is not a pass. unittest exits 0 on it before Python
  // 3.12 and 5 ("NO TESTS RAN") from 3.12 on; either way discovery found
  // nothing — a broken import path, a renamed directory — and a green
  // tick would say the opposite. Reported as a skip, so STRICT_TESTS
  // turns it into a failure.
  const ran = combined.match(/Ran\s+(\d+)\s+tests?\s+in\s+/);
  if ((code === 0 || code === 5) && ran && Number(ran[1]) === 0) {
    c.warn_pair(label, `ran 0 tests (in ${elapsed}s)`);
    c.detail('unittest discovered no tests — check tests/ and its imports');
    return { exitCode: 0, skipped: true };
  }

  if (code !== 0) {
    c.err_pair(label, `failures (exit ${code})`);
    if (r.status === null) c.detail(describeNoStatus(r, PYTHON));
    else if (!combined.trim()) c.detail(`${PYTHON} exited ${code} without printing anything`);
    process.stdout.write(combined);
    return { exitCode: code, skipped: false };
  }

  // Detect skipped tests in the unittest summary block.
  // unittest prints e.g. "OK (skipped=2)" when tests were skipped.
  const skipMatch = combined.match(/OK\s*\(skipped=(\d+)\)/);
  if (skipMatch) {
    const n = skipMatch[1];
    c.warn_pair(label, `${n} skipped (in ${elapsed}s)`);
    // Say WHICH and WHY, not just how many.
    //
    // A bare "1 skipped" is a warning you cannot act on: it could be
    // a missing optional dependency, a fixture that isn't built yet,
    // or a test quietly disabled by a rename. Every skip in this
    // suite carries a reason written to be read by a person, so
    // print them — a skip nobody can explain is one nobody fixes.
    //
    // -v is what makes the reasons available at all: unittest prints
    // them per test only in verbose mode. The verbose stream stays
    // captured, so this costs nothing in normal output.
    //
    // The label is whatever precedes " ... skipped", deliberately
    // loose: unittest prints the test's id normally, but substitutes
    // the first line of its docstring when it has one, and most of
    // these do. Matching either is what makes this work at all — an
    // earlier version anchored on the id form and silently printed
    // nothing for every documented test, which is the majority.
    for (const [, label_, why] of combined.matchAll(
      /^(.*?) \.\.\. skipped ['"](.+?)['"]\s*$/gm)) {
      const reason = why.length > 110 ? `${why.slice(0, 107)}...` : why;
      c.detail(`${label_.trim()} — ${reason}`);
    }
    return { exitCode: 0, skipped: true };
  }

  // "Ran N tests in X.YYYs" is the count. Without it the run cannot be
  // shown as a pass: exit 0 with no summary means unittest never got as
  // far as running anything we can vouch for.
  if (!ran) {
    c.warn_pair(label, `ran 0 tests (no unittest summary, in ${elapsed}s)`);
    return { exitCode: 0, skipped: true };
  }
  c.ok_pair(label, `${ran[1]} passed in ${elapsed}s`);
  return { exitCode: 0, skipped: false };
}

/**
 * Run a JS test file and emit a summary line.
 *
 * The custom JS test files print dots-as-progress + a final "N
 * passed, M failed" line. We capture the whole stream; on success
 * we extract the summary numbers; on failure we replay the captured
 * output so the failure block (which includes "Failures:" detail)
 * reaches the user.
 *
 * A test file may also exit 0 after printing
 * "SKIP <suite_name>: <reason>" — used when the suite cannot run
 * in the current environment (e.g. missing browser binary). We
 * detect this and report it as a yellow warning, distinct from
 * a normal pass.
 *
 * Returns { exitCode, skipped }. `skipped` is true on detected SKIP.
 */
function runNodeCaptured(jsFile, label) {
  const start = Date.now();
  const r = spawnSync(process.execPath, [jsFile], { cwd: ROOT, encoding: 'utf-8' });
  const elapsed = ((Date.now() - start) / 1000).toFixed(3);
  const code = typeof r.status === 'number' ? r.status : 2;

  const stdout = r.stdout || '';
  const stderr = r.stderr || '';

  if (code !== 0) {
    c.err_pair(label, `failures (exit ${code})`);
    if (r.status === null) c.detail(describeNoStatus(r, `node ${path.basename(jsFile)}`));
    if (stdout) process.stdout.write(stdout);
    if (stderr) process.stderr.write(stderr);
    return { exitCode: code, skipped: false };
  }

  // Detect SKIP marker. Test files print "SKIP <name>: <reason>" on
  // stdout when they bail out (e.g. missing Playwright browser). The
  // suite exits 0, so we have to inspect the output to distinguish
  // a skip from a true pass. The reason can be long (e.g. a full
  // Playwright error), so we put it on an indented detail line.
  const skipMatch = stdout.match(/^SKIP\s+\S+:\s*(.+)$/m);
  if (skipMatch) {
    c.warn_pair(label, 'skipped');
    c.detail(skipMatch[1].trim());
    return { exitCode: 0, skipped: true };
  }

  // Parse "N passed, M failed" out of stdout.
  //
  // A suite that exits 0 having passed nothing is not a pass: it bailed
  // out before its assertions (an early return, a swallowed error) or
  // never called report(). It gets a warning and counts as a skip —
  // STRICT_TESTS then fails it — rather than a green tick that would
  // hide it. test_engine_equivalence.js once did exactly this: every
  // engine startup failure printed "0 passed, 0 failed" and showed ✅.
  const m = stdout.match(/(\d+)\s+passed,\s+(\d+)\s+failed/);
  if (!m || Number(m[1]) === 0) {
    c.warn_pair(label, m ? `ran 0 tests (in ${elapsed}s)`
                         : `ran 0 tests (no "N passed" summary, in ${elapsed}s)`);
    const lastLine = stdout.trim().split('\n').filter(Boolean).pop();
    if (lastLine) c.detail(lastLine.trim());
    return { exitCode: 0, skipped: true };
  }
  c.ok_pair(label, `${m[1]} passed in ${elapsed}s`);
  return { exitCode: 0, skipped: false };
}

function listJsTests() {
  if (!fs.existsSync(TESTS_DIR)) return [];
  return fs.readdirSync(TESTS_DIR)
    .filter((f) => f.startsWith('test_') && f.endsWith('.js'))
    .map((f) => path.join(TESTS_DIR, f));
}

/* ──────────────────────────────────────────────────────────────
   Dispatch
   ────────────────────────────────────────────────────────────── */

let exitCode = 0;

if (userArg) {
  // Single-target mode. Use pass-through (native) output: the user
  // is iterating on a specific test and wants the runner's full,
  // unsummarized output (verbose names, traceback, etc.).
  const jsCandidate = path.join(TESTS_DIR,
    userArg.endsWith('.js') ? userArg : `${userArg}.js`);
  if (fs.existsSync(jsCandidate)) {
    exitCode = runNodeInherit(jsCandidate);
  } else {
    const dotted = userArg.startsWith('tests.') ? userArg : `tests.${userArg}`;
    exitCode = runPythonInherit(['-B', '-m', 'unittest', dotted]);
  }
} else {
  // Discover-all mode: capture + summarize each runner.
  // Python first since it's faster.
  const strictTests = envFlag('STRICT_TESTS');
  let anySkipped = false;

  /** Fold a runner's structured result into the running tally. */
  function applyResult(result) {
    if (result.skipped) anySkipped = true;
    if (result.exitCode !== 0) {
      exitCode = Math.max(exitCode, result.exitCode);
    }
  }

  applyResult(runPythonCaptured(
    ['-B', '-m', 'unittest', 'discover', '-v', 'tests/'],
    'Python',
  ));
  for (const jsFile of listJsTests()) {
    // Derive a friendly label from the file stem ("test_solve_layout"
    // → "solve_layout") to match what users actually call the suite.
    const stem = path.basename(jsFile, '.js').replace(/^test_/, '');
    applyResult(runNodeCaptured(jsFile, stem));
  }

  // STRICT_TESTS converts any skipped suite into a hard failure.
  // Used in CI to catch silent skips (e.g. missing browser binary
  // that would otherwise let invariant tests slip past).
  if (anySkipped && strictTests && exitCode === 0) {
    c.err('STRICT_TESTS is on — skipped suites (and suites that ran 0 tests) are treated as failures');
    exitCode = 1;
  }
}

process.exit(exitCode);
