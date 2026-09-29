/**
 * detect_python.js — Shared Python interpreter detection.
 *
 * Honors the PYTHON env var when set. Otherwise tries platform-
 * appropriate candidates in order and returns the first that
 * responds to `--version` with exit code 0, doesn't appear to be
 * the Microsoft Store alias stub on Windows, and is at least
 * MIN_PYTHON. PYTHON gets the same check, and fails loudly on it.
 *
 * Used by resume.js (build pipeline) and run_tests.js (test runner)
 * so both pick the same interpreter without duplicating logic.
 */

const { spawnSync } = require('child_process');
const c = require('./_console');

/**
 * The oldest Python this project runs on: README.md, "Requirements",
 * says 3.10+. The code relies on it — `Path | None` in signatures that
 * are evaluated at import (build/crop_pdf.py) is a TypeError on 3.9 —
 * so an older interpreter used to fail somewhere in the middle of a
 * build with a traceback that never mentioned the version.
 */
const MIN_PYTHON = [3, 10];

/**
 * The PYTHON override, trimmed; null when unset or blank.
 *
 * Trimmed because cmd.exe keeps everything up to the `&&`:
 * `set PYTHON=py && node resume.js` sets PYTHON to "py " (trailing
 * space), and spawning "py " fails with ENOENT — an error that names a
 * program which looks exactly like the one that exists. A value that is
 * blank after trimming is treated as unset, so auto-detection runs
 * instead of spawning "".
 *
 * One pair of surrounding double or single quotes is removed too.
 * cmd.exe keeps them as part of the value — `set PYTHON="C:\Program
 * Files\Python312\python.exe"` is the natural way to write a path with
 * a space in it there — and the program is spawned without a shell,
 * so nothing else would ever take them off.
 */
function pythonOverride(env = process.env) {
  const raw = env.PYTHON;
  if (typeof raw !== 'string') return null;
  let value = raw.trim();
  const m = value.match(/^(["'])(.*)\1$/s);
  if (m) value = m[2].trim();
  return value || null;
}

/** [major, minor] out of `python --version` output, or null. */
function parseVersion(text) {
  const m = /Python\s+(\d+)\.(\d+)/.exec(text || '');
  return m ? [Number(m[1]), Number(m[2])] : null;
}

function tooOld(version) {
  return Boolean(version)
    && (version[0] < MIN_PYTHON[0]
        || (version[0] === MIN_PYTHON[0] && version[1] < MIN_PYTHON[1]));
}

/**
 * Ask `cmd --version`. Returns {ok, version, why}: `ok` when it ran,
 * exited 0 and is not the Microsoft Store alias stub; `version` when
 * it printed one; `why` otherwise.
 */
function probe(cmd, run) {
  const r = run(cmd, ['--version'], {
    stdio: 'pipe',
    windowsHide: true, // no console flash under the desktop app (see engine.js)
    encoding: 'utf-8',
  });
  // r.error is set when spawn itself failed (ENOENT etc.).
  if (r.error) return { ok: false, version: null, why: r.error.message };
  const combined = (r.stdout || '') + (r.stderr || '');
  // Microsoft Store alias stub on Windows exits 0 while printing an
  // install prompt. It lands on stdout in some shells and stderr in
  // others; inspect both. Real Python 3 prints "Python 3.x.y" to
  // stdout — no version line contains "was not found", so this is
  // a safe negative match.
  if (/was not found/i.test(combined)) {
    return { ok: false, version: null, why: 'the Microsoft Store alias, not Python' };
  }
  if (r.status !== 0) {
    return { ok: false, version: null, why: `\`${cmd} --version\` exited ${r.status}` };
  }
  // Python 2 prints its version to stderr, 3 to stdout: both are read.
  return { ok: true, version: parseVersion(combined), why: null };
}

const MIN_TEXT = `${MIN_PYTHON[0]}.${MIN_PYTHON[1]}`;

function howToSet() {
  c.detail('  bash/zsh:    PYTHON=python3.12 node resume.js');
  c.detail('  cmd.exe:     set "PYTHON=py" && node resume.js');
  c.detail('  PowerShell:  $env:PYTHON="py"; node resume.js');
}

/**
 * The Python interpreter to run: PYTHON when set, else the first
 * candidate that runs and is new enough.
 *
 * Either way it is checked with one `--version` (a few milliseconds)
 * before anything depends on it, so a bad PYTHON or an old Python is
 * reported here, naming the problem, instead of as a spawn error or a
 * traceback halfway through a build.
 *
 * @param {object} [deps] — test seam: { run: spawnSync, env, exit }
 */
function detectPython({ run = spawnSync, env = process.env, exit = process.exit } = {}) {
  const override = pythonOverride(env);
  if (override) {
    const r = probe(override, run);
    if (!r.ok) {
      c.err(`PYTHON is set to ${JSON.stringify(override)}, which could not be run`);
      c.detail(r.why);
      c.detail('');
      c.detail('PYTHON names one program: a name on PATH, or the full path to');
      c.detail('python.exe. Not a command with arguments ("py -3.12"): to choose');
      c.detail('a version, give that version\'s own path instead.');
      howToSet();
      return exit(1);
    }
    if (tooOld(r.version)) {
      c.err(`PYTHON is Python ${r.version.join('.')}; this project needs ${MIN_TEXT} or newer`);
      c.detail(`PYTHON=${JSON.stringify(override)}`);
      return exit(1);
    }
    return override;
  }
  const candidates = process.platform === 'win32'
    ? ['python', 'py', 'python3']
    : ['python3', 'python'];
  const old = [];
  for (const cmd of candidates) {
    const r = probe(cmd, run);
    if (!r.ok) continue;
    if (tooOld(r.version)) {
      old.push(`${cmd} (Python ${r.version.join('.')})`);
      continue;
    }
    return cmd;
  }
  if (old.length) {
    c.err(`No Python ${MIN_TEXT} or newer found`);
    c.detail(`Too old: ${old.join(', ')}`);
  } else {
    c.err('No Python interpreter found');
    c.detail(`Tried: ${candidates.join(', ')}`);
  }
  c.detail('');
  c.detail(`Install Python ${MIN_TEXT}+ or set the PYTHON env var explicitly:`);
  howToSet();
  return exit(1);
}

module.exports = { detectPython, pythonOverride, parseVersion, MIN_PYTHON };
