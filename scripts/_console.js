/*
 * _console.js — Shared console-output helper.
 *
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║  KEEP IN SYNC WITH _console.py                                   ║
 * ║                                                                  ║
 * ║  This file mirrors scripts/_console.py — same API, same          ║
 * ║  behaviour, same constants. Banner width, label pad width,       ║
 * ║  indent widths, status symbols, and ANSI codes must match        ║
 * ║  exactly between the two files. If you change one, change the    ║
 * ║  other in the same commit.                                       ║
 * ╚══════════════════════════════════════════════════════════════════╝
 *
 * Mirror of scripts/_console.py for Node.js scripts. Same API,
 * same symbols, same banner format — so the pipeline's output
 * looks identical regardless of which language produced a given
 * line.
 *
 * Symbols
 * ───────
 *   ok(msg)    → ✅  success
 *   err(msg)   → ❌  hard failure
 *   warn(msg)  → ⚠️   warning; build continues
 *   info(msg)  → ℹ️   advisory
 *
 *   The status symbols are emoji with intrinsic colour. ANSI codes
 *   are NOT applied to them. ⚠ and ℹ get U+FE0F appended to force
 *   emoji presentation on terminals that otherwise default these
 *   to text/monochrome glyphs.
 *
 * Phase banner
 * ────────────
 *   banner(label) prints a 60-char-wide rule preceded by a blank
 *   line. Banner dashes ARE dimmed via ANSI when the stream is a
 *   TTY (the dashes are text, not emoji).
 *
 * Colour suppression
 * ──────────────────
 *   Banner ANSI emitted iff the target stream is a TTY. Honours
 *   NO_COLOR and FORCE_COLOR env vars (https://no-color.org).
 *   Emoji are emitted unconditionally — they're not ANSI.
 *
 * Streams
 * ───────
 *   ok / banner / detail / info → stdout
 *   err / warn                  → stderr
 */

const _RESET = '\x1b[0m';
const _DIM   = '\x1b[2m';

const _SYM_OK   = '✅';
const _SYM_ERR  = '❌';
const _SYM_WARN = '⚠️';
const _SYM_INFO = 'ℹ️';

const _BANNER_WIDTH = 70;
const _BANNER_LEAD  = '─── ';

const _BULLET_INDENT = '  ';     // 2 spaces before the emoji
const _DETAIL_INDENT = '     ';  // 5 spaces — aligns under message text

// Label-value pairing — for ok_pair / err_pair / etc. The value
// column starts after a colon + padding so a sequence of pair
// lines aligns vertically. 18 covers every label currently used.
const _LABEL_PAD_WIDTH = 23;


function colourEnabled(stream) {
  if (process.env.NO_COLOR) return false;
  if (process.env.FORCE_COLOR) return true;
  return Boolean(stream && stream.isTTY);
}


function banner(label) {
  const used = _BANNER_LEAD.length + label.length + 1;
  const tailLen = Math.max(0, _BANNER_WIDTH - used);
  const tail = '─'.repeat(tailLen);
  let line;
  if (colourEnabled(process.stdout)) {
    line = `${_DIM}${_BANNER_LEAD}${_RESET}${label} ${_DIM}${tail}${_RESET}`;
  } else {
    line = `${_BANNER_LEAD}${label} ${tail}`;
  }
  process.stdout.write(`\n${line}\n`);
}


function ok(msg) {
  process.stdout.write(`${_BULLET_INDENT}${_SYM_OK} ${msg}\n`);
}


function err(msg) {
  process.stderr.write(`${_BULLET_INDENT}${_SYM_ERR} ${msg}\n`);
}


function warn(msg) {
  process.stderr.write(`${_BULLET_INDENT}${_SYM_WARN} ${msg}\n`);
}


function info(msg) {
  process.stdout.write(`${_BULLET_INDENT}${_SYM_INFO} ${msg}\n`);
}


function _formatPair(label, value) {
  const labelWithColon = `${label}:`;
  const padCount = Math.max(1, _LABEL_PAD_WIDTH - labelWithColon.length);
  return `${labelWithColon}${' '.repeat(padCount)}${value}`;
}


function okPair(label, value) {
  process.stdout.write(`${_BULLET_INDENT}${_SYM_OK} ${_formatPair(label, value)}\n`);
}


function errPair(label, value) {
  process.stderr.write(`${_BULLET_INDENT}${_SYM_ERR} ${_formatPair(label, value)}\n`);
}


function warnPair(label, value) {
  process.stderr.write(`${_BULLET_INDENT}${_SYM_WARN} ${_formatPair(label, value)}\n`);
}


function infoPair(label, value) {
  process.stdout.write(`${_BULLET_INDENT}${_SYM_INFO} ${_formatPair(label, value)}\n`);
}


function detail(msg, { stream = process.stderr } = {}) {
  stream.write(`${_DETAIL_INDENT}${msg}\n`);
}


module.exports = {
  banner, ok, err, warn, info, detail,
  ok_pair: okPair, err_pair: errPair, warn_pair: warnPair, info_pair: infoPair,
  colourEnabled,
};
