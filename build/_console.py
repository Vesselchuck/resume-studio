"""
console.py — Shared console-output helper.

The shared constants (status symbols, banner geometry, ANSI codes,
indent widths) live in build/_constants.json. This module loads them
at import time and exposes them as `_NAME` module-level names so
internal references like `_RESET`, `_SYM_OK` etc. work unchanged.
The matching _console.js does the same. There is no longer a parallel
hand-maintained constants block; cross-language parity is structural,
not enforced by a sync test.

Single source of truth for status symbols, ANSI colors, phase
banners, and TTY-aware color suppression. Imported by every script
that prints to stdout/stderr so the pipeline's output is uniform.

Symbols
───────
  ok(msg)    → ✅  success — past-tense action completed
  err(msg)   → ❌  hard failure — exits non-zero somewhere
  warn(msg)  → ⚠️   something noteworthy; build continues
  ok_pair / err_pair / warn_pair / info_pair  → aligned label:value

The status symbols are emoji with intrinsic color. ANSI codes are
NOT applied to them — the emoji glyphs already carry their own
color, and double-coloring would either fight (ANSI + emoji on
some terminals) or be redundant. ⚠ and ℹ get the U+FE0F variation
selector appended ("⚠️", "ℹ️") to force emoji presentation on
terminals that otherwise default these to text/monochrome glyphs
(notably Windows cmd before VS-16, and many Linux text terminals).

Phase banner
────────────
  banner(label) prints a 70-char-wide rule:
      ─── Tests ────────────────────────────────────────────────────────────

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

Color suppression
──────────────────
  ANSI codes (used only for banner dimming) are emitted only when
  stdout is a TTY (or when NO_COLOR is unset and FORCE_COLOR is
  set). Emoji are emitted unconditionally — they're not ANSI, so
  there's nothing to "suppress." If a terminal can't render emoji,
  it shows a fallback glyph (typically a question mark in a box),
  which still readably distinguishes success/failure.

Two-stream design
─────────────────
  ok / banner / ok_pair / info_pair          → stdout
  err / warn / err_pair / warn_pair / detail → stderr (detail by default;
                                                pass stream=sys.stdout to
                                                continue an ok/info line)
  This matches Unix conventions (errors and warnings to stderr) so
  shell pipelines can split them. Callers don't need to specify a
  stream — the helper picks the right one.
"""

import io
import json
import os
import sys
from pathlib import Path


# Load shared constants from _constants.json at module-init time.
# Exposed as module-level `_NAME` names so internal references and
# external callers that imported them by name keep working.
# `_comment_*` keys are documentation only and ignored here.
_CONSTANTS_PATH = Path(__file__).parent / "_constants.json"
with _CONSTANTS_PATH.open(encoding="utf-8") as _f:
    _console_constants = {
        k: v for k, v in json.load(_f)["console"].items()
        if not k.startswith("_comment")
    }

_RESET           = _console_constants["RESET"]
_DIM             = _console_constants["DIM"]
_SYM_OK          = _console_constants["SYM_OK"]
_SYM_ERR         = _console_constants["SYM_ERR"]
_SYM_WARN        = _console_constants["SYM_WARN"]
_SYM_INFO        = _console_constants["SYM_INFO"]
_BANNER_WIDTH    = _console_constants["BANNER_WIDTH"]
_BANNER_LEAD     = _console_constants["BANNER_LEAD"]
_BULLET_INDENT   = _console_constants["BULLET_INDENT"]
_DETAIL_INDENT   = _console_constants["DETAIL_INDENT"]
_LABEL_PAD_WIDTH = _console_constants["LABEL_PAD_WIDTH"]

# Free the temp binding so it doesn't show up in dir(module).
del _console_constants, _f


def _force_utf8(stream) -> None:
    """
    Make `stream` write UTF-8, replacing what it still cannot encode.

    The status symbols are emoji. On Windows a PIPED stdout (a build run
    from resume.js, a CI log, `python build/build.py | more`) is opened
    in the ANSI code page — cp1252 on most Western machines — which has
    no ✅, so the very first ok() raised UnicodeEncodeError and turned a
    working build into a traceback. A console window is UTF-16 and was
    never affected, which is why this only ever showed up piped.

    `errors='replace'` is the backstop: if reconfiguring is refused, a
    symbol degrades to '?' rather than killing the build.

    Guarded, because the stream is not always a real text file: the warm
    worker and the unit tests swap in io.StringIO, which has no
    reconfigure(), and a detached or closed stream refuses it.
    """
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding='utf-8', errors='replace')
    except (ValueError, OSError, io.UnsupportedOperation):
        pass


_force_utf8(sys.stdout)
_force_utf8(sys.stderr)


def _color_enabled(stream) -> bool:
    """
    True if we should emit ANSI codes on `stream`.

    Honors NO_COLOR (https://no-color.org) — if NO_COLOR is set to
    a non-empty value, all output is monochrome regardless of TTY
    status. FORCE_COLOR set to 0 or false turns color off; empty, 1,
    2, 3 or true turns it on (e.g. CI tools that capture output but
    want color preserved). Any other value, or none, means "color
    iff TTY". Same rules as colorEnabled() in _console.js.
    """
    if os.environ.get('NO_COLOR'):
        return False
    force = os.environ.get('FORCE_COLOR')
    if force is not None:
        v = force.strip().lower()
        if v in ('0', 'false'):
            return False
        if v in ('', '1', '2', '3', 'true'):
            return True
    return hasattr(stream, 'isatty') and stream.isatty()


# Module-level state for banner(): the first banner emitted does not
# prepend a blank line (otherwise the very first line of output is a
# stray blank). All subsequent banners get the blank-line lead so
# phases stay visually separated.
_first_banner = True


def banner(label: str) -> None:
    """
    Print a phase banner to stdout.

      ─── Tests ────────────────────────────────────────────────────────────

    The label is left-padded by `─── ` (3 dashes + space) and the
    line is right-padded with `─` to exactly _BANNER_WIDTH columns.
    Preceded by a blank line for visual separation — except on the
    very first banner of the process, where the leading blank would
    create a stray empty line at the top of the output.
    """
    global _first_banner
    used = len(_BANNER_LEAD) + len(label) + 1  # leading dashes + label + trailing space
    tail = '─' * max(0, _BANNER_WIDTH - used)
    if _color_enabled(sys.stdout):
        # Dim the dashes so the label stands out without being shouty.
        head_dashes = _DIM + _BANNER_LEAD + _RESET
        tail_dashes = _DIM + tail + _RESET
        line = f'{head_dashes}{label} {tail_dashes}'
    else:
        line = f'{_BANNER_LEAD}{label} {tail}'
    if _first_banner:
        print(line, flush=True)
        _first_banner = False
    else:
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
