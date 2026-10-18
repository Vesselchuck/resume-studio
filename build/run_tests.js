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
 *   Every JS SKIP and every suite that ran 0 tests fails. A Python
 *   skip fails unless its reason is one of EXPECTED_SKIPS below: the
 *   skips that are right on a machine like CI's (a test for the other
 *   platform, no private data files on GitHub, an optional tool not
 *   installed). Without that list strict mode could never pass
 *   anywhere, which is why CI ran without it and a suite skipped there
 *   stayed green.
 *
 * This checkout's dist/
 * ─────────────────────
 * No suite may write this checkout's dist/: the ones that build work in
 * a temp copy (tests/_project.js, tests/_project.py) and each checks
 * that dist/ is unchanged after it. The runner checks the whole run as
 * well, by content and not only by mtime, and names what changed. A
 * change is a warning, or a failure under STRICT_TESTS. See
 * distFingerprint().
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
 *   • Arg matching a JS file (tests/test_<arg>.js exists) → runs
 *     only that JS test. The JS file wins when both exist: the Python
 *     twin of a same-named suite is reached as tests.<name>
 *     (`test_output_name` runs the JS suite, `tests.test_output_name`
 *     the Python one).
 *   • Arg naming a Python module (tests/<module>.py exists; a dotted
 *     class or test after it is passed on) → runs only that Python test.
 *   • Arg matching neither → exits 2 with an error.
 *
 * Exits 0 on success, 1 on any test failure (or skip when
 * STRICT_TESTS=1), 2 on runner/usage error.
 */

const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { execFileSync, spawnSync } = require('child_process');
const { detectPython } = require('./detect_python');
const c = require('./_console');

const ROOT = path.resolve(__dirname, '..');
const TESTS_DIR = path.join(ROOT, 'tests');
const DIST = path.join(ROOT, 'dist');
const PYTHON = detectPython();

const userArg = process.argv[2];
const strictTests = envFlag('STRICT_TESTS');

/**
 * Limits on a captured suite.
 *
 * spawnSync's default maxBuffer is 1 MB: a suite printing more (a
 * failure dumping a long diff, a verbose -v run as the suite grows)
 * was killed with ENOBUFS and reported as "could not run", its real
 * output gone. The whole Python run prints ~60 KB today, so 256 MB is
 * a ceiling that only a runaway loop reaches.
 *
 * There was no timeout, so a suite that hung (a Chromium that never
 * answers, a worker waiting on a pipe) hung `npm test`, and with it
 * every `node resume.js`, whose build waits behind the suites. Fifteen
 * minutes is ~15× the slowest suite in a Linux sandbox.
 */
const MAX_OUTPUT_BYTES = 256 * 1024 * 1024;
const SUITE_TIMEOUT_MS = 15 * 60 * 1000;

/**
 * The Python skips STRICT_TESTS lets through, matched against the whole
 * reason a test gives. Each is an environment that is legitimately
 * different, not something broken:
 *
 *   test_power.py           each platform skips the other's test, and
 *                           only the other's: "Windows only" is
 *                           expected off Windows, the no-op path's skip
 *                           on it
 *   test_anonymized.py      no private data files, only on GitHub
 *                           (GITHUB_ACTIONS set). On any other machine
 *                           that skip means _real_files() found nothing,
 *                           and every anonymity check behind it skipped
 *   test_text_extraction.py the optional extractors (the Windows CI job
 *                           fails earlier on its own if pdftotext is gone)
 *
 * Tesseract is not here: CI never reaches that skip (the OCR test skips
 * on "no real data files" first), and on a machine with real data a
 * missing Tesseract means the screenshots were not read.
 *
 * Anything else that skips under STRICT_TESTS fails the run. Add a
 * reason here only for a skip that is right on CI as well; a skip that
 * means "this could not be checked" belongs out of this list.
 */
const ON_WINDOWS = process.platform === 'win32';
const ON_GITHUB = Boolean(process.env.GITHUB_ACTIONS);
const EXPECTED_SKIPS = [
  { reason: /^Windows only$/, when: !ON_WINDOWS },
  { reason: /^the no-op path is for other platforms$/, when: ON_WINDOWS },
  { reason: /^no real data files in data\/ — nothing of yours to leak$/, when: ON_GITHUB },
  { reason: /^no data\/_profile\.yml$/, when: ON_GITHUB },
  { reason: /^pdfplumber is not installed, so the text layer is not read with it \(optional\)$/, when: true },
  { reason: /^pdftotext is not on PATH, so the text layer is not read with it \(optional\)$/, when: true },
].filter(s => s.when).map(s => s.reason);

/**
 * This checkout's dist/, so the run can tell whether it was written.
 *
 * Each suite that builds checks dist/ itself, by name, size and mtime
 * (realDistFingerprint in tests/_project.*), and only around its own
 * work. A full run once left the real PDFs, styles.css, .lock and
 * .studio-prefs.json rewritten with every suite passing, and it could
 * not be reproduced. Likely, though not shown: a Studio left open, or a
 * `node resume.js` (or a Studio Build with tests on) running at the
 * same time, which write there between suites. So the runner takes
 * this before and after the whole run, with a content hash of every
 * file up to SMALL_FILE_BYTES (so a rewrite with the same bytes shows
 * as that), and names each difference.
 *
 * The lock files are left out of the comparison and reported on their
 * own: dist/.lock names whoever holds dist/ (its pid and host), which
 * is the likeliest answer to "who wrote this".
 */
const SMALL_FILE_BYTES = 1024 * 1024;
const LOCK_FILES = new Set(['.lock', '.lock.next', '.lock.break']);

function distFingerprint(dist) {
  const out = new Map();
  const walk = (dir) => {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      const p = path.join(dir, e.name);
      const rel = path.relative(dist, p).split(path.sep).join('/');
      if (e.isDirectory()) walk(p);
      else if (e.isFile() && !LOCK_FILES.has(rel)) {
        try {
          const st = fs.statSync(p);
          const hash = st.size <= SMALL_FILE_BYTES
            ? crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex') : null;
          out.set(rel, { size: st.size, mtimeMs: st.mtimeMs, hash });
        } catch { /* removed between the listing and the read */ }
      }
    }
  };
  walk(dist);
  return out;
}

/** What dist/.lock says (its holder), or null when there is none. */
function lockHolder(dist) {
  try {
    return fs.readFileSync(path.join(dist, '.lock'), 'utf-8').trim() || '(empty)';
  } catch {
    return null;
  }
}

/** One line per file that differs between two distFingerprint()s. */
function distChanges(before, after) {
  const lines = [];
  for (const [rel, now] of after) {
    const was = before.get(rel);
    if (!was) lines.push(`${rel}: added`);
    else if (was.size !== now.size || was.hash !== now.hash) lines.push(`${rel}: changed`);
    else if (was.mtimeMs !== now.mtimeMs) {
      lines.push(now.hash ? `${rel}: rewritten with the same bytes` : `${rel}: modified time changed`);
    }
  }
  for (const rel of before.keys()) {
    if (!after.has(rel)) lines.push(`${rel}: removed`);
  }
  return lines.sort();
}

/**
 * Report a difference in dist/ over the run, and return whether there
 * was one: a warning, or an error under STRICT_TESTS (the caller then
 * fails the run).
 */
function reportDist(before, lockBefore) {
  const changes = distChanges(before, distFingerprint(DIST));
  const lockAfter = lockHolder(DIST);
  if (!changes.length) return false;
  const say = strictTests ? c.err : c.warn;
  say(`this checkout's dist/ changed during the test run (${changes.length} `
    + `file${changes.length === 1 ? '' : 's'}); no suite should write there`);
  for (const line of changes) c.detail(line);
  c.detail(`dist/.lock before the run: ${lockBefore === null ? 'none' : `held: ${lockBefore}`}`);
  c.detail(`dist/.lock after the run: ${lockAfter === null ? 'none' : `held: ${lockAfter}`}`);
  c.detail('A Studio left open, or a `node resume.js` / `node letter.js` or a Studio '
    + 'Build running at the same time, writes there: close it or let it finish, and run again.');
  return true;
}

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
  if (r.error && r.error.code === 'ETIMEDOUT') {
    return `${cmd} did not finish within ${SUITE_TIMEOUT_MS / 60000} minutes and was stopped`;
  }
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
 * exited 0 but reported a skip that is not in EXPECTED_SKIPS
 * (unittest prints "OK (skipped=N…)" in that case), or ran 0 tests.
 * The dispatcher uses `skipped` to honor STRICT_TESTS.
 */
function runPythonCaptured(args, label) {
  const start = Date.now();
  const r = spawnSync(PYTHON, args, {
    cwd: ROOT, encoding: 'utf-8', env: pythonEnv(),
    maxBuffer: MAX_OUTPUT_BYTES, timeout: SUITE_TIMEOUT_MS,
  });
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
  // unittest prints e.g. "OK (skipped=2)" when tests were skipped, and
  // other counts after it when there are any ("OK (skipped=1, expected
  // failures=1)"), which an earlier, closed match missed: that run
  // showed as a plain pass.
  const skipMatch = combined.match(/OK\s*\([^)]*\bskipped=(\d+)/);
  if (skipMatch) {
    const n = Number(skipMatch[1]);
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
    // nothing for every documented test, which is the majority. A skip
    // raised in setUpModule or setUpClass can come with no label at all,
    // a bare "skipped '<reason>'" line, so the label is optional too.
    //
    // Under STRICT_TESTS each reason is checked against EXPECTED_SKIPS,
    // and a skip whose reason could not be read at all counts as
    // unexpected: strict mode cannot vouch for what it did not see.
    const reasons = [...combined.matchAll(
      /^(?:(.*?) \.\.\. )?skipped (['"])(.+)\2\s*$/gm)];
    let unexpected = Math.max(0, n - reasons.length);
    for (const [, label_, , why] of reasons) {
      const expected = EXPECTED_SKIPS.some((re) => re.test(why));
      if (!expected) unexpected++;
      const reason = why.length > 110 ? `${why.slice(0, 107)}...` : why;
      const mark = strictTests && !expected ? ' (not an expected skip)' : '';
      c.detail(`${(label_ || 'setUpModule / setUpClass').trim()} — ${reason}${mark}`);
    }
    return { exitCode: 0, skipped: unexpected > 0 };
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
  const r = spawnSync(process.execPath, [jsFile], {
    cwd: ROOT, encoding: 'utf-8',
    maxBuffer: MAX_OUTPUT_BYTES, timeout: SUITE_TIMEOUT_MS,
  });
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

  // Parse "N passed, M failed" out of stdout. The LAST such line: it is
  // what report() prints at the very end.
  const summaries = [...stdout.matchAll(/^(\d+)\s+passed,\s+(\d+)\s+failed\s*$/gm)];
  const m = summaries.length ? summaries[summaries.length - 1] : null;

  // A suite that says it failed has failed, whatever its exit code.
  // report() sets process.exitCode, and anything that sets it again
  // afterwards (a cleanup path's `process.exitCode = 0`, an explicit
  // process.exit(0)) used to turn "3 passed, 2 failed" into a ✅.
  if (m && Number(m[2]) > 0) {
    c.err_pair(label, `${m[2]} failed (the suite exited 0 anyway)`);
    if (stdout) process.stdout.write(stdout);
    if (stderr) process.stderr.write(stderr);
    return { exitCode: 1, skipped: false };
  }

  // Detect SKIP marker. Test files print "SKIP <name>: <reason>" on
  // stdout when they bail out (e.g. missing Playwright browser). The
  // suite exits 0, so we have to inspect the output to distinguish
  // a skip from a true pass. The reason can be long (e.g. a full
  // Playwright error), so we put it on an indented detail line.
  //
  // Some suites skip only a part and still run the rest
  // (test_worker_restarts.js's end-to-end half): that is still a skip,
  // so STRICT_TESTS still fails it, but the count of what did pass is
  // shown rather than dropped.
  const skipMatch = stdout.match(/^SKIP\s+\S+:\s*(.+)$/m);
  if (skipMatch) {
    c.warn_pair(label, m && Number(m[1]) > 0
      ? `${m[1]} passed, part skipped (in ${elapsed}s)` : 'skipped');
    c.detail(skipMatch[1].trim());
    return { exitCode: 0, skipped: true };
  }

  // A suite that exits 0 having passed nothing is not a pass: it bailed
  // out before its assertions (an early return, a swallowed error) or
  // never called report(). It gets a warning and counts as a skip —
  // STRICT_TESTS then fails it — rather than a green tick that would
  // hide it. test_engine_equivalence.js once did exactly this: every
  // engine startup failure printed "0 passed, 0 failed" and showed ✅.
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
const distBefore = distFingerprint(DIST);
const lockBefore = lockHolder(DIST);

if (userArg) {
  // Single-target mode. Use pass-through (native) output: the user
  // is iterating on a specific test and wants the runner's full,
  // unsummarized output (verbose names, traceback, etc.).
  const jsCandidate = path.join(TESTS_DIR,
    userArg.endsWith('.js') ? userArg : `${userArg}.js`);
  // The module is checked here, not left to unittest: a module that
  // does not exist is an ImportError there, exit 1, which reads as a
  // test failure rather than the usage error it is.
  const dotted = userArg.startsWith('tests.') ? userArg : `tests.${userArg}`;
  const pyCandidate = path.join(TESTS_DIR, `${dotted.split('.')[1]}.py`);
  if (fs.existsSync(jsCandidate)) {
    exitCode = runNodeInherit(jsCandidate);
  } else if (!userArg.endsWith('.js') && fs.existsSync(pyCandidate)) {
    exitCode = runPythonInherit(['-B', '-m', 'unittest', dotted]);
  } else {
    c.err(`no test named ${userArg}: neither ${path.relative(ROOT, jsCandidate)} `
      + `nor ${path.relative(ROOT, pyCandidate)} exists`);
    exitCode = 2;
  }
} else {
  // Discover-all mode: capture + summarize each runner.
  // Python first since it's faster.
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

  // STRICT_TESTS converts any skipped suite into a hard failure,
  // Python's expected skips aside (EXPECTED_SKIPS). Used in CI to catch
  // silent skips (e.g. missing browser binary that would otherwise let
  // invariant tests slip past).
  if (anySkipped && strictTests && exitCode === 0) {
    c.err('STRICT_TESTS is on — skipped suites, suites that ran 0 tests and '
      + 'unexpected Python skips are treated as failures');
    exitCode = 1;
  }
}

// After the suites, whatever they returned: a run that failed for
// another reason still says whether dist/ was written.
if (reportDist(distBefore, lockBefore) && strictTests && exitCode === 0) exitCode = 1;

process.exit(exitCode);
