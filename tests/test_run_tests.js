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
 * (the source of tests/test_fake.js) and which has no Python tests.
 */
function runWith(suite) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'run-tests-'));
  try {
    fs.mkdirSync(path.join(dir, 'build'));
    fs.mkdirSync(path.join(dir, 'tests'));
    for (const f of RUNNER_FILES) {
      fs.copyFileSync(path.join(ROOT, 'build', f), path.join(dir, 'build', f));
    }
    fs.writeFileSync(path.join(dir, 'tests', 'test_fake.js'), suite, 'utf-8');
    const env = { ...process.env, NO_COLOR: '1' };
    delete env.STRICT_TESTS;
    const r = spawnSync(process.execPath, [path.join(dir, 'build', 'run_tests.js')], {
      cwd: dir, encoding: 'utf-8', env, maxBuffer: 64 * 1024 * 1024,
    });
    const line = (r.stdout + r.stderr).split('\n').find(l => /\bfake\b/.test(l)) || '';
    return { status: r.status, line: line.replace(/\s+/g, ' ').trim(), out: r.stdout + r.stderr };
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}


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


report();
