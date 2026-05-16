/**
 * _framework.js — Tiny shared test harness for JS test files.
 *
 * Extracted from the verbatim copies that lived in test_solve_layout.js
 * and test_check_layout.js. The two files used the same accumulator and
 * the same `assertEq`, but each had its own subset of additional helpers
 * (`assertThrows` + `test` in the solver tests; `assertTrue` + an inline
 * `findViolation` in the layout tests). This module exposes the union so
 * both test files can drop their copies and import what they need.
 *
 * Why not Jest / Mocha / Vitest?
 * ──────────────────────────────
 * The project deliberately stays dependency-light: Playwright + Sass on
 * the Node side, nothing more. A 50-line homegrown harness costs less in
 * maintenance than a real framework's transitive deps, and the test
 * files already commit to its style (dots-as-progress, "N passed, M
 * failed" summary, indented failure block). Switching frameworks would
 * be a Phase-N+1 decision, not part of dedup.
 *
 * Output format
 * ─────────────
 * Each assertion writes one character to stdout:
 *   .   pass
 *   F   assertion failed
 *   E   exception thrown from inside test(name, fn)
 *
 * At the end, call `report()` to print "N passed, M failed", followed by
 * an indented per-failure block if anything failed, then exit with code
 * 0 or 1. The shape of that summary is what `build/run_tests.js`'s
 * captured-output parser keys off ("N passed, M failed").
 *
 * SKIP support
 * ────────────
 * If a suite needs to bail out before any assertions (e.g. Chromium not
 * installed), it prints "SKIP <suite>: <reason>" on stdout directly and
 * `process.exit(0)` — no framework call needed. `build/run_tests.js`
 * detects the SKIP marker via regex. This module doesn't get involved.
 */

const state = {
  passed: 0,
  failed: 0,
  failures: [],
};


/**
 * Assert deep-equality via JSON.stringify. Both args must be JSON-safe
 * (no functions, no circular refs, no NaN — none of which appear in
 * the test inputs this project uses).
 */
function assertEq(actual, expected, name) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    state.passed++;
    process.stdout.write('.');
  } else {
    state.failed++;
    state.failures.push({ name, actual: a, expected: e });
    process.stdout.write('F');
  }
}


/**
 * Assert that `cond` is truthy.
 */
function assertTrue(cond, name) {
  if (cond) {
    state.passed++;
    process.stdout.write('.');
  } else {
    state.failed++;
    state.failures.push({ name, error: 'expected truthy' });
    process.stdout.write('F');
  }
}


/**
 * Assert that `fn()` throws an error matching `predicate(err) === true`.
 *
 * Distinguishes three failure modes in the recorded message:
 *   • fn() didn't throw at all                — "expected throw, got success"
 *   • fn() threw but predicate returned false — "predicate failed" + err.message
 *   • fn() threw and predicate returned true  — pass
 */
function assertThrows(fn, predicate, name) {
  try {
    fn();
  } catch (err) {
    if (predicate(err)) {
      state.passed++;
      process.stdout.write('.');
      return;
    }
    state.failed++;
    state.failures.push({ name, error: 'predicate failed', actual: err.message });
    process.stdout.write('F');
    return;
  }
  state.failed++;
  state.failures.push({ name, error: 'expected throw, got success' });
  process.stdout.write('F');
}


/**
 * Wrap a test body so a thrown exception is recorded as an 'E' rather
 * than aborting the whole suite. Use for tests that exercise the
 * happy path of a function (where a throw means the test setup is
 * wrong, not the assertion); use assertThrows for tests that EXPECT
 * a throw.
 */
function test(name, fn) {
  try {
    fn();
  } catch (err) {
    state.failed++;
    state.failures.push({ name, error: err.stack });
    process.stdout.write('E');
  }
}


/**
 * Record an arbitrary failure without doing an assertion. Used by
 * test-specific helpers that detect a "test environment is wrong"
 * condition (e.g. findViolation in test_check_layout.js: an expected
 * invariant violation wasn't even produced). Equivalent to assertEq
 * (true, false, name) but lets the helper attach a more useful `info`
 * payload (typically { error: '...' }).
 */
function fail(name, info = {}) {
  state.failed++;
  state.failures.push({ name, ...info });
  process.stdout.write('F');
}


/**
 * Print the summary and set process.exitCode appropriately.
 * Matches the format that build/run_tests.js's captured-output parser
 * keys off:  "N passed, M failed"  on its own line.
 *
 * On failure, follows with an indented per-failure block (name + any
 * of { error, actual, expected } that the failure carries).
 *
 * Sets process.exitCode rather than calling process.exit() — Node
 * drains any pending I/O (including the failure block's writes) before
 * exiting, so the user never sees a truncated final line. Tests that
 * need post-report async cleanup should still do it before calling
 * report(); this module doesn't await anything.
 */
function report() {
  console.log('');
  console.log(`${state.passed} passed, ${state.failed} failed`);
  if (state.failed > 0) {
    console.log('\nFailures:');
    for (const f of state.failures) {
      console.log(`  ${f.name}`);
      if (f.error) console.log(`    error: ${f.error}`);
      if (f.actual) console.log(`    actual:   ${f.actual}`);
      if (f.expected) console.log(`    expected: ${f.expected}`);
    }
    process.exitCode = 1;
  } else {
    process.exitCode = 0;
  }
}


module.exports = {
  assertEq,
  assertTrue,
  assertThrows,
  test,
  fail,
  report,
};
