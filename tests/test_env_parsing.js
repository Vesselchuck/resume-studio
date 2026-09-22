/**
 * test_env_parsing.js — how the Node side reads the env vars a user
 * types by hand.
 *
 * Both of these were bugs that read a variable as "set" rather than
 * reading its value:
 *
 *   • FORCE_COLOR=0 forced color ON (any non-empty string was truthy).
 *   • cmd.exe's `set PYTHON=py && node resume.js` stores "py " with the
 *     trailing space, and spawning "py " fails with ENOENT.
 */

const path = require('path');
const { assertEq, test, report } = require('./_framework');

const ROOT = path.resolve(__dirname, '..');
const c = require(path.join(ROOT, 'build', '_console'));
const { detectPython, pythonOverride } = require(path.join(ROOT, 'build', 'detect_python'));

const TTY = { isTTY: true };
const PIPE = { isTTY: false };


/* ─── _console.colorEnabled ───────────────────────────────────── */

test('no env: color follows the stream', () => {
  assertEq(c.colorEnabled(TTY, {}), true, 'TTY → color');
  assertEq(c.colorEnabled(PIPE, {}), false, 'pipe → none');
});

test('FORCE_COLOR=0 / false disables color, even on a TTY', () => {
  for (const v of ['0', 'false', 'FALSE', ' 0 ']) {
    assertEq(c.colorEnabled(TTY, { FORCE_COLOR: v }), false, `FORCE_COLOR=${JSON.stringify(v)}`);
  }
});

test('FORCE_COLOR=1/2/3/true (or empty) enables color, even when piped', () => {
  for (const v of ['1', '2', '3', 'true', 'True', '']) {
    assertEq(c.colorEnabled(PIPE, { FORCE_COLOR: v }), true, `FORCE_COLOR=${JSON.stringify(v)}`);
  }
});

test('an unrecognized FORCE_COLOR falls back to the stream', () => {
  assertEq(c.colorEnabled(PIPE, { FORCE_COLOR: 'banana' }), false, 'pipe');
  assertEq(c.colorEnabled(TTY, { FORCE_COLOR: 'banana' }), true, 'TTY');
});

test('NO_COLOR, when non-empty, wins over everything', () => {
  assertEq(c.colorEnabled(TTY, { NO_COLOR: '1' }), false, 'TTY');
  assertEq(c.colorEnabled(PIPE, { NO_COLOR: '1', FORCE_COLOR: '1' }), false, 'over FORCE_COLOR');
});

test('an empty NO_COLOR is not set (no-color.org)', () => {
  assertEq(c.colorEnabled(TTY, { NO_COLOR: '' }), true, 'TTY keeps color');
});


/* ─── detect_python: the PYTHON override ──────────────────────── */

function withPython(value, fn) {
  const had = Object.prototype.hasOwnProperty.call(process.env, 'PYTHON');
  const old = process.env.PYTHON;
  if (value === undefined) delete process.env.PYTHON;
  else process.env.PYTHON = value;
  try {
    return fn();
  } finally {
    if (had) process.env.PYTHON = old;
    else delete process.env.PYTHON;
  }
}

test("cmd.exe's trailing space is trimmed", () => {
  withPython('py ', () => {
    assertEq(pythonOverride(), 'py', 'override');
    assertEq(detectPython(), 'py', 'detectPython returns it trimmed');
  });
  withPython('\tpython3.12 \r\n', () => assertEq(pythonOverride(), 'python3.12', 'all whitespace'));
});

test('a blank PYTHON counts as unset', () => {
  withPython('   ', () => assertEq(pythonOverride(), null, 'whitespace only'));
  withPython('', () => assertEq(pythonOverride(), null, 'empty'));
  withPython(undefined, () => assertEq(pythonOverride(), null, 'absent'));
});

test('a path with inner spaces is kept intact', () => {
  withPython(' C:\\Program Files\\Python312\\python.exe ', () =>
    assertEq(pythonOverride(), 'C:\\Program Files\\Python312\\python.exe', 'inner spaces'));
});


report();
