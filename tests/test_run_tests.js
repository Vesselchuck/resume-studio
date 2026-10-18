/**
 * test_run_tests.js — what build/run_tests.js makes of a suite's output.
 *
 * The runner is the gate inside `node resume.js` (architecture.md,
 * Load-bearing invariants), so a suite it reports wrongly is a build it
 * lets through wrongly. Each case here runs the real runner in a copy
 * of the few files it needs, against one fake suite:
 *
 *   • "3 passed, 2 failed" from a suite whose exit code was reset to 0
 *     used to show ✅ "3 passed" and exit 0.
 *   • A suite printing more than 1 MB was killed by spawnSync's default
 *     maxBuffer and reported as "could not run", its output lost.
 *   • A suite that skips a part and passes the rest keeps its count.
 *   • "OK (skipped=1, expected failures=1)" showed as a plain pass.
 *   • STRICT_TESTS fails a JS skip and an unexpected Python skip, and
 *     lets the expected ones (EXPECTED_SKIPS in the runner) through,
 *     a skip raised in setUpModule included. Each expected skip only
 *     where it is right: the other platform's test, and no private
 *     data files only on GitHub; a missing Tesseract never.
 *   • A single target that names no test exits 2, not unittest's 1.
 *   • A run that writes this checkout's dist/ says which files and who
 *     held dist/.lock, and fails under STRICT_TESTS.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawnSync } = require('child_process');

const { assertEq, assertTrue, test, report } =
  require(path.join(__dirname, '_framework'));

const ROOT = path.join(__dirname, '..');
const RUNNER_FILES = ['run_tests.js', 'detect_python.js', '_console.js', '_constants.json'];


/**
 * Run the real runner over a project whose only JS suite is `suite`
 * (the source of tests/test_fake.js; none when null) and whose Python
 * tests are `py` (file name → source; none by default).
 *
 *   strict  run with STRICT_TESTS=1 (it is always cleared otherwise,
 *           so a CI run's own setting does not reach the fake one)
 *   github  run with GITHUB_ACTIONS=true (cleared otherwise, likewise)
 *   args    arguments for the runner (a single target)
 *   label   which summary line to return: the fake JS suite's by
 *           default, 'Python' for the unittest one
 *   prepare called with the fake project's directory before the run
 */
function runWith(suite, { py = {}, strict = false, github = false, args = [], label = 'fake',
  prepare = null } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'run-tests-'));
  try {
    fs.mkdirSync(path.join(dir, 'build'));
    fs.mkdirSync(path.join(dir, 'tests'));
    for (const f of RUNNER_FILES) {
      fs.copyFileSync(path.join(ROOT, 'build', f), path.join(dir, 'build', f));
    }
    if (suite !== null) {
      fs.writeFileSync(path.join(dir, 'tests', 'test_fake.js'), suite, 'utf-8');
    }
    for (const [name, source] of Object.entries(py)) {
      fs.writeFileSync(path.join(dir, 'tests', name), source, 'utf-8');
    }
    if (prepare) prepare(dir);
    const env = { ...process.env, NO_COLOR: '1' };
    delete env.STRICT_TESTS;
    delete env.GITHUB_ACTIONS;
    if (strict) env.STRICT_TESTS = '1';
    if (github) env.GITHUB_ACTIONS = 'true';
    const r = spawnSync(process.execPath, [path.join(dir, 'build', 'run_tests.js'), ...args], {
      cwd: dir, encoding: 'utf-8', env, maxBuffer: 64 * 1024 * 1024,
    });
    const at = new RegExp(`\\b${label}\\b`);
    const line = (r.stdout + r.stderr).split('\n').find(l => at.test(l)) || '';
    return { status: r.status, line: line.replace(/\s+/g, ' ').trim(), out: r.stdout + r.stderr };
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

/** A Python test module with one passing test and one that skips for `reason`. */
function pySkipping(reason) {
  return [
    'import unittest',
    'class T(unittest.TestCase):',
    '    def test_passes(self):',
    '        pass',
    '    def test_skips(self):',
    `        self.skipTest(${JSON.stringify(reason)})`,
    '',
  ].join('\n');
}

const PASSING_JS = `console.log('.'); console.log('1 passed, 0 failed');`;

// The skip test_power.py makes on this platform (its test for the
// other one), and the one it makes on the other.
const OWN_PLATFORM_SKIP = process.platform === 'win32'
  ? 'the no-op path is for other platforms' : 'Windows only';
const OTHER_PLATFORM_SKIP = process.platform === 'win32'
  ? 'Windows only' : 'the no-op path is for other platforms';
const NO_REAL_DATA = 'no real data files in data/ — nothing of yours to leak';
const PASSING_PY = 'import unittest\nclass P(unittest.TestCase):\n    def test_ok(self):\n        pass\n';


test('a suite that reports failures fails, even if it exits 0', () => {
  const r = runWith(`
    console.log('..FF.');
    console.log('3 passed, 2 failed');
    process.exitCode = 0;
  `);
  assertEq(r.status, 1, 'runner exit code');
  assertTrue(/2 failed/.test(r.line), `named as failed: ${r.line}`);
});

test('a suite may print more than 1 MB', () => {
  const r = runWith(`
    process.stdout.write('x'.repeat(3 * 1024 * 1024) + '\\n');
    console.log('1 passed, 0 failed');
  `);
  assertEq(r.status, 0, 'runner exit code');
  assertTrue(/1 passed/.test(r.line), `counted: ${r.line}`);
});

test('a partial skip keeps its pass count and still counts as a skip', () => {
  const r = runWith(`
    console.log('....');
    console.log('SKIP test_fake: chromium unavailable for the end-to-end part');
    console.log('4 passed, 0 failed');
  `);
  assertEq(r.status, 0, 'not a failure without STRICT_TESTS');
  assertTrue(/4 passed, part skipped/.test(r.line), `line: ${r.line}`);
});

test('a whole-suite skip is still a skip', () => {
  const r = runWith(`console.log('SKIP test_fake: playwright not installed');`);
  assertEq(r.status, 0, 'runner exit code');
  assertTrue(/fake: skipped/.test(r.line), `line: ${r.line}`);
});

test('a passing suite is a pass', () => {
  const r = runWith(`console.log('..'); console.log('2 passed, 0 failed');`);
  assertEq(r.status, 0, 'runner exit code');
  assertTrue(/2 passed in/.test(r.line), `line: ${r.line}`);
});

test('a Python skip beside an expected failure is still a skip', () => {
  const r = runWith(null, {
    label: 'Python',
    py: { 'test_fakepy.py': pySkipping('no reason at all') + [
      'class U(unittest.TestCase):',
      '    @unittest.expectedFailure',
      '    def test_known_bad(self):',
      '        self.assertTrue(False)',
      '',
    ].join('\n') },
  });
  assertEq(r.status, 0, 'not a failure without STRICT_TESTS');
  assertTrue(/Python: 1 skipped/.test(r.line), `line: ${r.line}`);
});

test('without STRICT_TESTS any skip is a warning only, unmarked', () => {
  const r = runWith(PASSING_JS, { py: { 'test_fakepy.py': pySkipping('no reason at all') } });
  assertEq(r.status, 0, 'runner exit code');
  assertTrue(/test_skips .* — no reason at all/.test(r.out), `reason shown: ${r.out}`);
  assertTrue(!/not an expected skip/.test(r.out), 'no strict-mode mark');
});

test('STRICT_TESTS lets an expected Python skip through', () => {
  const r = runWith(PASSING_JS, { strict: true, github: true,
    py: { 'test_fakepy.py': pySkipping(NO_REAL_DATA) } });
  assertEq(r.status, 0, `runner exit code: ${r.out}`);
});

test('STRICT_TESTS fails "no real data files" off GitHub', () => {
  // On his machine that skip means _real_files() found nothing, and
  // every anonymity check behind it went with it.
  const r = runWith(PASSING_JS, { strict: true,
    py: { 'test_fakepy.py': pySkipping(NO_REAL_DATA) } });
  assertEq(r.status, 1, `runner exit code: ${r.out}`);
  assertTrue(/nothing of yours to leak \(not an expected skip\)/.test(r.out), `marked: ${r.out}`);
});

test('STRICT_TESTS fails a missing Tesseract, on GitHub too', () => {
  const r = runWith(PASSING_JS, { strict: true, github: true,
    py: { 'test_fakepy.py': pySkipping('Tesseract OCR not found on PATH or in its default '
      + 'Windows folder — screenshots are not read') } });
  assertEq(r.status, 1, `runner exit code: ${r.out}`);
});

test("STRICT_TESTS lets only the other platform's test skip", () => {
  const own = runWith(PASSING_JS, { strict: true,
    py: { 'test_fakepy.py': pySkipping(OWN_PLATFORM_SKIP) } });
  assertEq(own.status, 0, `the other platform's test skipped here: ${own.out}`);
  const other = runWith(PASSING_JS, { strict: true,
    py: { 'test_fakepy.py': pySkipping(OTHER_PLATFORM_SKIP) } });
  assertEq(other.status, 1, `this platform's own test skipped: ${other.out}`);
});

test('STRICT_TESTS fails an unexpected Python skip and names it', () => {
  const r = runWith(PASSING_JS, { strict: true,
    py: { 'test_fakepy.py': pySkipping('could not build a copy of the project') } });
  assertEq(r.status, 1, 'runner exit code');
  assertTrue(/could not build a copy of the project \(not an expected skip\)/.test(r.out),
    `marked: ${r.out}`);
});

test('STRICT_TESTS reads a skip raised in setUpModule', () => {
  const module = (reason) => [
    'import unittest',
    'def setUpModule():',
    `    raise unittest.SkipTest(${JSON.stringify(reason)})`,
    'class T(unittest.TestCase):',
    '    def test_never_runs(self):',
    '        pass',
    '',
  ].join('\n');
  const passing = 'import unittest\nclass P(unittest.TestCase):\n    def test_ok(self):\n        pass\n';
  const ok = runWith(PASSING_JS, { strict: true,
    py: { 'test_fakepy.py': module(OWN_PLATFORM_SKIP), 'test_pass.py': passing } });
  assertEq(ok.status, 0, `expected reason: ${ok.out}`);
  const bad = runWith(PASSING_JS, { strict: true,
    py: { 'test_fakepy.py': module('chromium is missing'), 'test_pass.py': passing } });
  assertEq(bad.status, 1, 'unexpected reason');
});

test('STRICT_TESTS fails a JS skip', () => {
  const r = runWith(`console.log('SKIP test_fake: chromium unavailable');`, { strict: true,
    py: { 'test_fakepy.py': 'import unittest\nclass P(unittest.TestCase):\n    def test_ok(self):\n        pass\n' } });
  assertEq(r.status, 1, 'runner exit code');
});

test('a single target that names no test is a usage error', () => {
  const r = runWith(PASSING_JS, { args: ['test_nothing_here'] });
  assertEq(r.status, 2, `runner exit code: ${r.out}`);
  assertTrue(/no test named test_nothing_here/.test(r.out), `says why: ${r.out}`);
});

test('a single target runs the JS suite first and the Python twin as tests.<name>', () => {
  const py = { 'test_fake.py': 'import unittest\nclass P(unittest.TestCase):\n    def test_ok(self):\n        pass\n' };
  const js = runWith(PASSING_JS, { py, args: ['test_fake'] });
  assertEq(js.status, 0, 'JS target exit code');
  assertTrue(/1 passed, 0 failed/.test(js.out) && !/Ran 1 test/.test(js.out), `ran the JS suite: ${js.out}`);
  const twin = runWith(PASSING_JS, { py, args: ['tests.test_fake'] });
  assertEq(twin.status, 0, 'Python target exit code');
  assertTrue(/Ran 1 test/.test(twin.out), `ran the Python suite: ${twin.out}`);
  const dotted = runWith(null, { py, args: ['test_fake.P.test_ok'] });
  assertEq(dotted.status, 0, `a dotted test in the Python module: ${dotted.out}`);
});

/* ─── this checkout's dist/ ──────────────────────────────────── */

// A suite that writes into the fake project's dist/, as a Studio or a
// build running beside the tests would.
const WRITES_DIST = `
  const fs = require('fs'); const path = require('path');
  const dist = path.join(__dirname, '..', 'dist');
  fs.mkdirSync(dist, { recursive: true });
  fs.writeFileSync(path.join(dist, 'Gaius_Caesar_Resume.pdf'), 'new');
  fs.writeFileSync(path.join(dist, 'same.txt'), 'same');
  fs.utimesSync(path.join(dist, 'same.txt'), new Date(), new Date(Date.now() + 5000));
  fs.rmSync(path.join(dist, 'gone.json'));
  console.log('.'); console.log('1 passed, 0 failed');
`;

function seedDist(dir) {
  const dist = path.join(dir, 'dist');
  fs.mkdirSync(dist);
  fs.writeFileSync(path.join(dist, 'same.txt'), 'same');
  fs.writeFileSync(path.join(dist, 'gone.json'), '{}');
  fs.writeFileSync(path.join(dist, 'untouched.css'), 'x');
  fs.writeFileSync(path.join(dist, '.lock'), '{"pid":4242,"owner":"node resume.js"}');
}

test('a run that writes dist/ names each file and the lock holder', () => {
  const r = runWith(WRITES_DIST, { prepare: seedDist });
  assertEq(r.status, 0, `a warning only without STRICT_TESTS: ${r.out}`);
  assertTrue(/dist\/ changed during the test run \(3 files\)/.test(r.out), `reported: ${r.out}`);
  assertTrue(/Gaius_Caesar_Resume\.pdf: added/.test(r.out), `added: ${r.out}`);
  assertTrue(/same\.txt: rewritten with the same bytes/.test(r.out), `rewritten: ${r.out}`);
  assertTrue(/gone\.json: removed/.test(r.out), `removed: ${r.out}`);
  assertTrue(!/untouched\.css/.test(r.out), `an untouched file is not named: ${r.out}`);
  assertTrue(/dist\/\.lock before the run: held: \{"pid":4242,"owner":"node resume\.js"\}/.test(r.out),
    `the holder: ${r.out}`);
  assertTrue(/Studio/.test(r.out), `the hint: ${r.out}`);
});

test('STRICT_TESTS fails a run that writes dist/', () => {
  const r = runWith(WRITES_DIST, { strict: true, prepare: seedDist, py: { 'test_pass.py': PASSING_PY } });
  assertTrue(/dist\/ changed during the test run/.test(r.out), `the reason: ${r.out}`);
  assertEq(r.status, 1, `runner exit code: ${r.out}`);
});

test('a run that leaves dist/ alone says nothing about it', () => {
  const r = runWith(PASSING_JS, { strict: true, prepare: seedDist, py: { 'test_pass.py': PASSING_PY } });
  assertEq(r.status, 0, `runner exit code: ${r.out}`);
  assertTrue(!/dist\//.test(r.out), `nothing reported: ${r.out}`);
});


report();
