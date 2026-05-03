/**
 * detect_python.js — Shared Python interpreter detection.
 *
 * Honors the PYTHON env var when set. Otherwise tries platform-
 * appropriate candidates in order and returns the first that
 * responds to `--version` with exit code 0 AND doesn't appear to be
 * the Microsoft Store alias stub on Windows.
 *
 * Used by render.js (build pipeline) and run_tests.js (test runner)
 * so both pick the same interpreter without duplicating logic.
 */

const { execFileSync } = require('child_process');

function detectPython() {
  if (process.env.PYTHON) return process.env.PYTHON;
  const candidates = process.platform === 'win32'
    ? ['python', 'py', 'python3']
    : ['python3', 'python'];
  for (const cmd of candidates) {
    try {
      const out = execFileSync(cmd, ['--version'], {
        stdio: 'pipe',
        encoding: 'utf-8',
      });
      // Microsoft Store alias stub sometimes exits 0 while printing
      // an install-prompt to stderr/stdout. Reject that case.
      if (/was not found/i.test(out)) continue;
      return cmd;
    } catch {
      // Non-zero exit (typical for missing command or Store stub) —
      // try the next candidate.
    }
  }
  console.error('\n❌ NO PYTHON INTERPRETER FOUND');
  console.error(`Tried: ${candidates.join(', ')}`);
  console.error('\nInstall Python 3 or set the PYTHON env var explicitly:');
  console.error('  bash/zsh:    PYTHON=python3.12 node render.js');
  console.error('  cmd.exe:     set PYTHON=py && node render.js');
  console.error('  PowerShell:  $env:PYTHON="py"; node render.js');
  process.exit(1);
}

module.exports = { detectPython };
