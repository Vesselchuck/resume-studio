"""
console.py — Shared console-output helper.

╔══════════════════════════════════════════════════════════════════╗
║  KEEP IN SYNC WITH _console.js                                   ║
║                                                                  ║
║  This file mirrors scripts/_console.js — same API, same          ║
║  behaviour, same constants. Banner width, label pad width,       ║
║  indent widths, status symbols, and ANSI codes must match        ║
║  exactly between the two files. If you change one, change the    ║
║  other in the same commit.                                       ║
╚══════════════════════════════════════════════════════════════════╝

Single source of truth for status symbols, ANSI colours, phase
banners, and TTY-aware colour suppression. Imported by every script
that prints to stdout/stderr so the pipeline's output is uniform.

Symbols
───────
  ok(msg)    → ✅  success — past-tense action completed
  err(msg)   → ❌  hard failure — exits non-zero somewhere
  warn(msg)  → ⚠️   something noteworthy; build continues
  info(msg)  → ℹ️   advisory; no impact on success/failure

The status symbols are emoji with intrinsic colour. ANSI codes are
NOT applied to them — the emoji glyphs already carry their own
colour, and double-colouring would either fight (ANSI + emoji on
some terminals) or be redundant. ⚠ and ℹ get the U+FE0F variation
selector appended ("⚠️", "ℹ️") to force emoji presentation on
terminals that otherwise default these to text/monochrome glyphs
(notably Windows cmd before VS-16, and many Linux text terminals).

Phase banner
────────────
  banner(label) prints a 60-char-wide rule:
      ─── Tests ─────────────────────────────────────────────

  Banner dashes are dimmed via ANSI when the stream is a TTY;
  ANSI dim still applies here because the dashes themselves are
  text, not emoji.

Indented detail
───────────────
  detail(msg) prints 5-space-indented text under a phase or
  under a multi-line error. No symbol prefix. The 5-space indent
  aligns the detail text with the start of the message text on
  bullet lines, which is `(2 spaces lead) + (2-col emoji) +
  (1 space)` = column 5.

Colour suppression
──────────────────
  ANSI codes (used only for banner dimming) are emitted only when
  stdout is a TTY (or when NO_COLOR is unset and FORCE_COLOR is
  set). Emoji are emitted unconditionally — they're not ANSI, so
  there's nothing to "suppress." If a terminal can't render emoji,
  it shows a fallback glyph (typically a question mark in a box),
  which still readably distinguishes success/failure.

Two-stream design
─────────────────
  ok / banner / detail / info → stdout
  err / warn                  → stderr
  This matches Unix conventions (errors and warnings to stderr) so
  shell pipelines can split them. Callers don't need to specify a
  stream — the helper picks the right one.
"""

import os
import sys


# ANSI dim/reset for the banner dashes only. The four status emoji
# carry their own colour and don't use ANSI.
_RESET = '\x1b[0m'
_DIM   = '\x1b[2m'

# Status symbols — emoji with intrinsic colour. ⚠ and ℹ have the
# U+FE0F variation selector appended to force emoji presentation
# on terminals that default them to text/monochrome.
_SYM_OK   = '✅'
_SYM_ERR  = '❌'
_SYM_WARN = '⚠️'
_SYM_INFO = 'ℹ️'

# Banner geometry: total width 60, including spaces around the label.
# `─── ` (4) + label + ` ` (1) + tail = 60.
_BANNER_WIDTH = 70
_BANNER_LEAD  = '─── '

# Indent geometry. Bullet lines render as `(BULLET_INDENT) +
# (emoji, 2 columns) + ' ' + message`, so detail text aligned with
# the start of the message lives at column BULLET_INDENT + 3.
_BULLET_INDENT = '  '       # 2 spaces before the symbol on bullet lines
_DETAIL_INDENT = '     '    # 5 spaces — aligns under the bullet's message text


def _colour_enabled(stream) -> bool:
    """
    True if we should emit ANSI codes on `stream`.

    Honours NO_COLOR (https://no-color.org) — if NO_COLOR is set to
    any value, all output is monochrome regardless of TTY status.
    Honours FORCE_COLOR for the inverse case (e.g. CI tools that
    capture output but want colour preserved). Default is "colour
    iff TTY".
    """
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    return hasattr(stream, 'isatty') and stream.isatty()


# Label-value pairing — for `ok_pair`/`err_pair`/etc. The value
# column starts after a colon + padding so a sequence of pair lines
# aligns vertically. 18 covers every label currently used in the
# pipeline with a bit of headroom for future additions.
_LABEL_PAD_WIDTH = 23


def banner(label: str) -> None:
    """
    Print a phase banner to stdout.

      ─── Tests ─────────────────────────────────────────────

    The label is left-padded by `─── ` (3 dashes + space) and the
    line is right-padded with `─` to exactly _BANNER_WIDTH columns.
    Always preceded by a blank line for visual separation.
    """
    used = len(_BANNER_LEAD) + len(label) + 1  # leading dashes + label + trailing space
    tail = '─' * max(0, _BANNER_WIDTH - used)
    if _colour_enabled(sys.stdout):
        # Dim the dashes so the label stands out without being shouty.
        head_dashes = _DIM + _BANNER_LEAD + _RESET
        tail_dashes = _DIM + tail + _RESET
        line = f'{head_dashes}{label} {tail_dashes}'
    else:
        line = f'{_BANNER_LEAD}{label} {tail}'
    print(f'\n{line}', flush=True)


def ok(msg: str) -> None:
    """Print a success line: ✅ <msg>"""
    print(f'{_BULLET_INDENT}{_SYM_OK} {msg}', flush=True)


def err(msg: str) -> None:
    """Print an error headline: ❌ <msg>. Goes to stderr."""
    print(f'{_BULLET_INDENT}{_SYM_ERR} {msg}', file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    """Print a warning: ⚠️ <msg>. Goes to stderr."""
    print(f'{_BULLET_INDENT}{_SYM_WARN} {msg}', file=sys.stderr, flush=True)


def info(msg: str) -> None:
    """Print an info line: ℹ️ <msg>"""
    print(f'{_BULLET_INDENT}{_SYM_INFO} {msg}', flush=True)


def _format_pair(label: str, value: str) -> str:
    """
    Format a "label: value" pair with the value column padded so a
    series of pair lines aligns vertically. Label is followed by a
    colon and enough spaces to bring its total width (label + ":" +
    padding) to _LABEL_PAD_WIDTH characters before the value starts.

    If the label is already wider than the pad width (uncommon),
    the alignment falls through gracefully — the value just starts
    at "label: " with one trailing space, no negative padding.
    """
    label_with_colon = f'{label}:'
    pad_count = max(1, _LABEL_PAD_WIDTH - len(label_with_colon))
    return f'{label_with_colon}{" " * pad_count}{value}'


def ok_pair(label: str, value: str) -> None:
    """Print an aligned label:value success line: ✅ <label>: <value>"""
    print(f'{_BULLET_INDENT}{_SYM_OK} {_format_pair(label, value)}', flush=True)


def err_pair(label: str, value: str) -> None:
    """Print an aligned label:value error line. Goes to stderr."""
    print(f'{_BULLET_INDENT}{_SYM_ERR} {_format_pair(label, value)}',
          file=sys.stderr, flush=True)


def warn_pair(label: str, value: str) -> None:
    """Print an aligned label:value warning. Goes to stderr."""
    print(f'{_BULLET_INDENT}{_SYM_WARN} {_format_pair(label, value)}',
          file=sys.stderr, flush=True)


def info_pair(label: str, value: str) -> None:
    """Print an aligned label:value info line."""
    print(f'{_BULLET_INDENT}{_SYM_INFO} {_format_pair(label, value)}', flush=True)


def detail(msg: str, *, stream=None) -> None:
    """
    Print indented detail text — no symbol prefix.

    Used for sub-lines under an err/warn/info headline (extra
    context, hints, multi-line error explanation). Default stream
    is stderr to match the typical "more about an error" use case;
    pass stream=sys.stdout if it's continuing an info/ok line.

    Indented to column 5 so detail text aligns visually under the
    start of the message text on bullet lines (which have a 2-space
    lead, then a 2-column emoji, then a space — total 5 columns
    before the message starts).
    """
    if stream is None:
        stream = sys.stderr
    print(f'{_DETAIL_INDENT}{msg}', file=stream, flush=True)
