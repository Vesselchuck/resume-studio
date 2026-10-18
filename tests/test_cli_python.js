/**
 * test_cli_python.js — what the CLI drivers print when a Python step
 * fails (build/_cli.js, pythonRunner).
 *
 * The step's stderr is inherited, so its own error reaches the terminal
 * directly and err.stderr is always null. The fallback line was gated
 * on that null, so "… failed: Command failed: python -B …" printed
 * under the script's own message whenever its stdout was empty. The
 * fallback is for a step that never ran to an exit, which has said
 * nothing about why, and for one that crashed: the scripts exit only 1
 * or 2, and a native crash (0xC0000005 on Windows) prints nothing.
 *
 * And a dist/ lock the CLIs cannot take is reported as an error, not
 * as "Unexpected error" with a stack trace.
 *
 * Needs the project's Python, as every build does.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { assertEq, assertTrue, fail, test, report } = require('./_framework');

const ROOT = path.resolve(__dirname, '..');
const { pythonRunner, describeStatus, acquireDist } = require(path.join(ROOT, 'build', '_cli'));
const { detectPython } = require(path.join(ROOT, 'build', 'detect_python'));

const PYTHON = detectPython();

/** Run `fn` with this process's stdout and stderr captured. */
function captured(fn) {
  const out = process.stdout.write;
  const err = process.stderr.write;
  let stdout = '';
  let stderr = '';
  process.stdout.write = (chunk) => { stdout += String(chunk); return true; };
  process.stderr.write = (chunk) => { stderr += String(chunk); return true; };
  let thrown = null;
  try {
    fn();
  } catch (e) {
    thrown = e;
  } finally {
    process.stdout.write = out;
    process.stderr.write = err;
  }
  return { stdout, stderr, thrown };
}

test('a step that exited is not given a second, fallback error', () => {
  const runPython = pythonRunner(ROOT, PYTHON);
  const r = captured(() => runPython(['-c', 'import sys; sys.exit(2)'], 'Step failed'));
  assertTrue(r.thrown !== null, 'the failure is thrown');
  assertEq(r.thrown && r.thrown.status, 2, 'with its exit status');
  assertEq(r.thrown && r.thrown.alreadyReported, true, 'marked reported');
  assertEq(r.stderr, '', 'no "Command failed" line under the step\'s own message');
});

test('what a failed step printed to stdout is still replayed', () => {
  const runPython = pythonRunner(ROOT, PYTHON);
  const r = captured(() => runPython(
    ['-c', 'import sys; print("partial progress"); sys.exit(1)'], 'Step failed'));
  assertTrue(r.stdout.includes('partial progress'), `replayed: ${JSON.stringify(r.stdout)}`);
  assertEq(r.stderr, '', 'and no fallback line');
});

test('a step that never ran says why', () => {
  const runPython = pythonRunner(ROOT, path.join(ROOT, 'no-such-python'));
  const r = captured(() => runPython(['-c', 'pass'], 'Step failed'));
  assertEq(r.thrown && r.thrown.alreadyReported, true, 'marked reported');
  assertTrue(r.stderr.includes('Step failed:'), `the fallback names the step: ${JSON.stringify(r.stderr)}`);
});

test('a step that exited with a status the scripts never use says so', () => {
  // The scripts exit 1 or 2, having said why. Anything else is the
  // interpreter dying under them, which prints nothing: the build
  // failed without a word.
  const runPython = pythonRunner(ROOT, PYTHON);
  const r = captured(() => runPython(['-c', 'import os; os._exit(3)'], 'Step failed'));
  assertEq(r.thrown && r.thrown.alreadyReported, true, 'marked reported');
  assertTrue(r.stderr.includes('Step failed: exited 3'), `names the status: ${JSON.stringify(r.stderr)}`);
});

test('a native crash says so, in the hex Windows documents it in', () => {
  // A read of address 0 in C (faulthandler's own test hook; ctypes would
  // turn it into an OSError on Windows): 0xC0000005 there, silently;
  // SIGSEGV elsewhere, a signal, which the never-ran fallback covers.
  const runPython = pythonRunner(ROOT, PYTHON);
  const r = captured(() => runPython(['-c', 'import faulthandler; faulthandler._read_null()'], 'Step failed'));
  assertEq(r.thrown && r.thrown.alreadyReported, true, 'marked reported');
  assertTrue(r.stderr.includes('Step failed:'), `says the step failed: ${JSON.stringify(r.stderr)}`);
  if (process.platform === 'win32') {
    assertTrue(r.stderr.includes('3221225477 (0xC0000005)'), `with the status in hex: ${JSON.stringify(r.stderr)}`);
  }
  assertEq(describeStatus(3221225477), '3221225477 (0xC0000005)', 'describeStatus, large');
  assertEq(describeStatus(3), '3', 'and small');
});

/** _framework's test() is synchronous; this one awaits the lock. */
async function lockTest() {
  // A dist/ lock that cannot be taken is an error, not an unexpected
  // one. dist/.lock as a directory: acquire gives up, and the drivers
  // printed that as "Unexpected error" and a stack trace.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cli-lock-'));
  try {
    fs.mkdirSync(path.join(root, 'dist', '.lock'), { recursive: true });
    const err0 = process.stderr.write;
    let stderr = '';
    process.stderr.write = (chunk) => { stderr += String(chunk); return true; };
    let thrown = null;
    try {
      await acquireDist(root, 'the test');
    } catch (e) {
      thrown = e;
    } finally {
      process.stderr.write = err0;
    }
    assertEq(thrown && thrown.alreadyReported, true, 'marked reported');
    assertTrue(stderr.includes('Could not take dist/') && stderr.includes('can be neither created nor read'),
      `says what and why: ${JSON.stringify(stderr)}`);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

lockTest()
  .catch(err => fail('a dist/ lock that cannot be taken is an error, not an unexpected one',
    { error: err.stack || err.message }))
  .finally(report);
