/*
 * _console.js — Shared console-output helper.
 *
 * The shared constants (status symbols, banner geometry, ANSI codes,
 * indent widths) live in build/_constants.json. This module reads
 * them at require-time. The matching _console.py does the same. There
 * is no longer a parallel hand-maintained constants block; cross-
 * language parity is structural, not enforced by a sync test.
 *
 * Mirror of build/_console.py for Node.js scripts. Same API,
 * same symbols, same banner format — so the pipeline's output
 * looks identical regardless of which language produced a given
 * line.
 *
 * Symbols
 * ───────
 *   ok(msg)    → ✅  success
 *   err(msg)   → ❌  hard failure
 *   warn(msg)  → ⚠️   warning; build continues
 *   ok_pair / err_pair / warn_pair / info_pair  → aligned label:value
 *
 *   The status symbols are emoji with intrinsic color. ANSI codes
 *   are NOT applied to them. ⚠ and ℹ get U+FE0F appended to force
 *   emoji presentation on terminals that otherwise default these
 *   to text/monochrome glyphs.
 *
 * Phase banner
 * ────────────
 *   banner(label) prints a 70-char-wide rule preceded by a blank
 *   line. Banner dashes ARE dimmed via ANSI when the stream is a
 *   TTY (the dashes are text, not emoji).
 *
 * Color suppression
 * ──────────────────
 *   Banner ANSI emitted iff the target stream is a TTY. Honors
 *   NO_COLOR and FORCE_COLOR env vars (https://no-color.org).
 *   Emoji are emitted unconditionally — they're not ANSI.
 *
 * Streams
 * ───────
 *   ok / banner / ok_pair / info_pair → stdout
 *   err / warn / err_pair / warn_pair → stderr
 *   detail                            → stderr by default; pass { stream }
 *                                       in opts to override (e.g. stdout when
 *                                       continuing an ok/info bullet).
 */

const path = require('path');
const fs = require('fs');

// Load shared constants from _constants.json at require-time. The
// `_comment_*` keys are documentation only and not consumed here.
const _consoleConstants = JSON.parse(
  fs.readFileSync(path.join(__dirname, '_constants.json'), 'utf-8')
).console;

const _RESET           = _consoleConstants.RESET;
const _DIM             = _consoleConstants.DIM;
const _SYM_OK          = _consoleConstants.SYM_OK;
const _SYM_ERR         = _consoleConstants.SYM_ERR;
const _SYM_WARN        = _consoleConstants.SYM_WARN;
const _SYM_INFO        = _consoleConstants.SYM_INFO;
const _BANNER_WIDTH    = _consoleConstants.BANNER_WIDTH;
const _BANNER_LEAD     = _consoleConstants.BANNER_LEAD;
const _BULLET_INDENT   = _consoleConstants.BULLET_INDENT;
const _DETAIL_INDENT   = _consoleConstants.DETAIL_INDENT;
const _LABEL_PAD_WIDTH = _consoleConstants.LABEL_PAD_WIDTH;


function colorEnabled(stream) {
  if (process.env.NO_COLOR) return false;
  if (process.env.FORCE_COLOR) return true;
  return Boolean(stream && stream.isTTY);
}


// Module-level state for banner(): the first banner emitted does not
// prepend a blank line (otherwise the very first line of output is a
// stray blank). All subsequent banners get the blank-line lead so
// phases stay visually separated.
let _firstBanner = true;


function banner(label) {
  const used = _BANNER_LEAD.length + label.length + 1;
  const tailLen = Math.max(0, _BANNER_WIDTH - used);
  const tail = '─'.repeat(tailLen);
  let line;
  if (colorEnabled(process.stdout)) {
    line = `${_DIM}${_BANNER_LEAD}${_RESET}${label} ${_DIM}${tail}${_RESET}`;
  } else {
    line = `${_BANNER_LEAD}${label} ${tail}`;
  }
  if (_firstBanner) {
    process.stdout.write(`${line}\n`);
    _firstBanner = false;
  } else {
    process.stdout.write(`\n${line}\n`);
  }
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


function _formatPair(label, value) {
  const labelWithColon = `${label}:`;
  const padCount = Math.max(1, _LABEL_PAD_WIDTH - labelWithColon.length);
  return `${labelWithColon}${' '.repeat(padCount)}${value}`;
}


// Public-facing functions use snake_case to match _console.py's API
// exactly (build.py, snapshot_pdf.py, etc. call `c.ok_pair(...)` —
// JS callers do too, via the require()d module). Previously this file
// defined `okPair` etc. internally and re-exported them under snake_case
// names, which added an indirection layer for no JS-idiomatic gain
// since no consumer ever imported the camelCase forms. The leading-
// underscore `_formatPair` stays camelCase because it's internal to
// this file.

function ok_pair(label, value) {
  process.stdout.write(`${_BULLET_INDENT}${_SYM_OK} ${_formatPair(label, value)}\n`);
}


function err_pair(label, value) {
  process.stderr.write(`${_BULLET_INDENT}${_SYM_ERR} ${_formatPair(label, value)}\n`);
}


function warn_pair(label, value) {
  process.stderr.write(`${_BULLET_INDENT}${_SYM_WARN} ${_formatPair(label, value)}\n`);
}


function info_pair(label, value) {
  process.stdout.write(`${_BULLET_INDENT}${_SYM_INFO} ${_formatPair(label, value)}\n`);
}


function detail(msg, { stream = process.stderr } = {}) {
  stream.write(`${_DETAIL_INDENT}${msg}\n`);
}


module.exports = {
  banner, ok, err, warn, detail,
  ok_pair, err_pair, warn_pair, info_pair,
};
