/**
 * run_tests.js — Cross-platform unit test runner.
 *
 * Runs both Python unittest tests under tests/test_*.py and Node
 * test files under tests/test_*.js. Picks the right Python via the
 * shared detect_python module so the python3-vs-py difference on
 * Windows is invisible.
 *
 * Run with:  node scripts/run_tests.js
 *
 * Optional argument: a specific test path. For Python this is a
 * dotted path passed to unittest. For JS, pass the bare file stem;
 * the runner matches it against tests/test_*.js.
 *
 *   node scripts/run_tests.js
 *   node scripts/run_tests.js test_validate_data
 *   node scripts/run_tests.js test_solve_layout
 *
 * Behavior:
 *   • No arg → runs ALL Python tests, then ALL JS tests. Exits with
 *     code 1 if any test failed, 0 if all passed.
 *   • Arg matching a Python module → runs only that Python test.
 *   • Arg matching a JS file (tests/test_<arg>.js exists) → runs
 *     only that JS test.
 *   • Arg matching neither → exits 2 with an error.
 *
 * Exits 0 on success, 1 on any test failure, 2 on runner/usage error.
 */

const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');
const { detectPython } = require('./detect_python');

const ROOT = path.resolve(__dirname, '..');
const TESTS_DIR = path.join(ROOT, 'tests');
const PYTHON = detectPython();

const userArg = process.argv[2];

function runPython(args) {
  try {
    execFileSync(PYTHON, args, { cwd: ROOT, stdio: 'inherit' });
    return 0;
  } catch (err) {
    return typeof err.status === 'number' ? err.status : 2;
  }
}

function runNode(jsFile) {
  try {
    execFileSync(process.execPath, [jsFile], { cwd: ROOT, stdio: 'inherit' });
    return 0;
  } catch (err) {
    return typeof err.status === 'number' ? err.status : 2;
  }
}

function listJsTests() {
  if (!fs.existsSync(TESTS_DIR)) return [];
  return fs.readdirSync(TESTS_DIR)
    .filter((f) => f.startsWith('test_') && f.endsWith('.js'))
    .map((f) => path.join(TESTS_DIR, f));
}

let exitCode = 0;

if (userArg) {
  // Single-target mode. Try JS first (it's specific by filename),
  // then fall back to Python (unittest dotted path).
  const jsCandidate = path.join(TESTS_DIR,
    userArg.endsWith('.js') ? userArg : `${userArg}.js`);
  if (fs.existsSync(jsCandidate)) {
    exitCode = runNode(jsCandidate);
  } else {
    const dotted = userArg.startsWith('tests.') ? userArg : `tests.${userArg}`;
    exitCode = runPython(['-B', '-m', 'unittest', dotted]);
  }
} else {
  // Discover-all mode: Python unittest discovery + every test_*.js
  // in tests/. Run Python first since it's faster.
  exitCode = runPython(['-B', '-m', 'unittest', 'discover', 'tests/']);
  for (const jsFile of listJsTests()) {
    const code = runNode(jsFile);
    // Keep going through all tests so the user sees every failure;
    // but record the worst exit code.
    if (code !== 0 && exitCode === 0) exitCode = code;
    else if (code !== 0) exitCode = Math.max(exitCode, code);
  }
}

process.exit(exitCode);
