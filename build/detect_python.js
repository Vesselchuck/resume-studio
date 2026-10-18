/**
 * detect_python.js — Shared Python interpreter detection.
 *
 * Honors the PYTHON env var when set. Otherwise tries platform-
 * appropriate candidates in order and takes the first that
 * responds to `--version` with exit code 0 and a Python version,
 * doesn't appear to be the Microsoft Store alias stub on Windows,
 * and is at least MIN_PYTHON — returned as the absolute path it
 * reports for itself (see locate). PYTHON gets the same check, and
 * fails loudly on it, but is returned as written.
 *
 * detectPythonInfo() also says how to show it: the build log prints the
 * candidate's name and version (`python 3.12.1`), not the absolute
 * path, which for a per-user install is C:\Users\<account>\… — and the
 * Studio's log, where a Build's output lands, ends up in screenshots
 * and pasted reports.
 *
 * Used by resume.js (build pipeline) and run_tests.js (test runner)
 * so both pick the same interpreter without duplicating logic.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
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

/** The whole version `python --version` printed ("3.12.1", "3.13.0rc1"), or null. */
function versionText(text) {
  const m = /Python\s+(\d+\.\d+(?:\.\d+)?(?:[a-z]+\d*)?)/.exec(text || '');
  return m ? m[1] : null;
}

function tooOld(version) {
  return Boolean(version)
    && (version[0] < MIN_PYTHON[0]
        || (version[0] === MIN_PYTHON[0] && version[1] < MIN_PYTHON[1]));
}

/**
 * Ask `cmd --version`. Returns {ok, version, text, why}: `ok` when it
 * ran, exited 0 and is not the Microsoft Store alias stub; `version`
 * ([major, minor]) and `text` (the whole of it) when it printed one;
 * `why` otherwise.
 */
function probe(cmd, run, cwd) {
  const r = run(cmd, ['--version'], {
    stdio: 'pipe',
    windowsHide: true, // no console flash under the desktop app (see engine.js)
    encoding: 'utf-8',
    cwd,
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
  return { ok: true, version: parseVersion(combined), text: versionText(combined), why: null };
}

/**
 * Where on disk an auto-detected candidate is: its sys.executable, or
 * null when it cannot say or names no file (the candidate is then run
 * by its bare name, as before; see detectPythonInfo).
 *
 * The CLIs and the engine spawn the interpreter many times, and on
 * Windows a bare name is looked up in the current directory before
 * PATH — the project root, for them — so a python.exe dropped there
 * would run instead. Spawning the absolute path cannot be redirected
 * that way. The candidates themselves are probed from %SystemRoot%,
 * a directory only an administrator can write to, for the same reason.
 */
function locate(cmd, run, cwd, exists) {
  // The path goes out as UTF-8 bytes, whatever Python's stdout encoding.
  // print() wrote it in the ANSI code page (Windows Python's default for
  // a pipe), which is read here as UTF-8: C:\Users\José\… came back with
  // U+FFFD for the é, a path no spawn could find, and a Cyrillic one
  // raised UnicodeEncodeError, so that Python was skipped and another
  // picked in silence. surrogateescape gives back the bytes of a POSIX
  // path that is not UTF-8, which then fails the exists check.
  const r = run(cmd, ['-c',
    "import sys; sys.stdout.buffer.write(sys.executable.encode('utf-8', 'surrogateescape'))"], {
    stdio: 'pipe', windowsHide: true, encoding: 'utf-8', cwd,
  });
  if (r.error || r.status !== 0) return null;
  const exe = String(r.stdout || '').trim();
  return exe && path.isAbsolute(exe) && exists(exe) ? exe : null;
}

/**
 * `p` with the home directory written as `~`, for display only: a path
 * under C:\Users\<account> otherwise prints the account name into the
 * build log.
 */
function tildify(p, home = os.homedir()) {
  if (!home || !path.isAbsolute(p)) return p;
  const fold = s => (process.platform === 'win32' ? s.toLowerCase() : s);
  const h = home.replace(/[\\/]+$/, '');
  const next = p.charAt(h.length);
  return fold(p.slice(0, h.length)) === fold(h) && (next === '' || next === '/' || next === '\\')
    ? `~${p.slice(h.length)}` : p;
}

/** Where to probe bare candidate names from; see locate(). */
function probeDir(env) {
  if (process.platform !== 'win32') return undefined;
  const root = env.SystemRoot || env.SYSTEMROOT;
  return root && fs.existsSync(root) ? root : undefined;
}

const MIN_TEXT = `${MIN_PYTHON[0]}.${MIN_PYTHON[1]}`;

function howToSet() {
  c.detail('  bash/zsh:    PYTHON=python3.12 node resume.js');
  c.detail('  cmd.exe:     set "PYTHON=py" && node resume.js');
  c.detail('  PowerShell:  $env:PYTHON="py"; node resume.js');
}

/**
 * The Python interpreter to run: PYTHON when set, else the absolute
 * path of the first candidate that runs and is new enough — or its bare
 * name when it cannot say where it is, as before: skipping it picked
 * another Python, or none, without a word.
 *
 * Returns {exe, command, label}: `exe` is what to spawn; `command` is
 * what a user types to run the same interpreter (PYTHON as written, or
 * the candidate's name, which their shell finds on PATH as the probe
 * did); `label` is what the build log shows, the command with home
 * written as `~`, and its version.
 *
 * PYTHON is not resolved: it may name a wrapper (a pyenv or conda
 * shim) that picks or prepares the interpreter on every run, and
 * spawning what it ran once could change which Python runs later.
 *
 * Either way it is checked with one `--version` (a few milliseconds)
 * before anything depends on it, so a bad PYTHON or an old Python is
 * reported here, naming the problem, instead of as a spawn error or a
 * traceback halfway through a build.
 *
 * @param {object} [deps] — test seam: { run: spawnSync, env, exit, exists }
 */
function detectPythonInfo({
  run = spawnSync, env = process.env, exit = process.exit, exists = fs.existsSync,
} = {}) {
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
    return {
      exe: override,
      command: override,
      label: [tildify(override), r.text].filter(Boolean).join(' '),
    };
  }
  const candidates = process.platform === 'win32'
    ? ['python', 'py', 'python3']
    : ['python3', 'python'];
  const old = [];
  const cwd = probeDir(env);
  for (const cmd of candidates) {
    const r = probe(cmd, run, cwd);
    // Exiting 0 is not enough: a candidate must also say it is Python,
    // and which one. Anything else answering to the name is not used.
    if (!r.ok || !r.version) continue;
    if (tooOld(r.version)) {
      old.push(`${cmd} (Python ${r.version.join('.')})`);
      continue;
    }
    return {
      exe: locate(cmd, run, cwd, exists) || cmd,
      command: cmd,
      label: [cmd, r.text].filter(Boolean).join(' '),
    };
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

/** detectPythonInfo's `exe` alone: the interpreter to spawn. */
function detectPython(deps) {
  const info = detectPythonInfo(deps);
  return info && info.exe;
}

module.exports = {
  detectPython, detectPythonInfo, pythonOverride, parseVersion, tildify, MIN_PYTHON,
};
