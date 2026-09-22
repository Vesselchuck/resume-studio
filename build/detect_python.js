/**
 * detect_python.js — Shared Python interpreter detection.
 *
 * Honors the PYTHON env var when set. Otherwise tries platform-
 * appropriate candidates in order and returns the first that
 * responds to `--version` with exit code 0 AND doesn't appear to be
 * the Microsoft Store alias stub on Windows.
 *
 * Used by resume.js (build pipeline) and run_tests.js (test runner)
 * so both pick the same interpreter without duplicating logic.
 */

const { spawnSync } = require('child_process');
const c = require('./_console');

/**
 * The PYTHON override, trimmed; null when unset or blank.
 *
 * Trimmed because cmd.exe keeps everything up to the `&&`:
 * `set PYTHON=py && node resume.js` sets PYTHON to "py " (trailing
 * space), and spawning "py " fails with ENOENT — an error that names a
 * program which looks exactly like the one that exists. A value that is
 * blank after trimming is treated as unset, so auto-detection runs
 * instead of spawning "".
 */
function pythonOverride() {
  const raw = process.env.PYTHON;
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim();
  return trimmed || null;
}

function detectPython() {
  const override = pythonOverride();
  if (override) return override;
  const candidates = process.platform === 'win32'
    ? ['python', 'py', 'python3']
    : ['python3', 'python'];
  for (const cmd of candidates) {
    const r = spawnSync(cmd, ['--version'], {
      stdio: 'pipe',
      encoding: 'utf-8',
    });
    // r.error is set when spawn itself failed (ENOENT etc.) — try next.
    // Non-zero status also disqualifies the candidate.
    if (r.error || r.status !== 0) continue;
    // Microsoft Store alias stub on Windows exits 0 while printing an
    // install prompt. It lands on stdout in some shells and stderr in
    // others; inspect both. Real Python 3 prints "Python 3.x.y" to
    // stdout — no version line contains "was not found", so this is
    // a safe negative match.
    const combined = (r.stdout || '') + (r.stderr || '');
    if (/was not found/i.test(combined)) continue;
    return cmd;
  }
  c.err('No Python interpreter found');
  c.detail(`Tried: ${candidates.join(', ')}`);
  c.detail('');
  c.detail('Install Python 3 or set the PYTHON env var explicitly:');
  c.detail('  bash/zsh:    PYTHON=python3.12 node resume.js');
  c.detail('  cmd.exe:     set "PYTHON=py" && node resume.js');
  c.detail('  PowerShell:  $env:PYTHON="py"; node resume.js');
  process.exit(1);
}

module.exports = { detectPython, pythonOverride };
