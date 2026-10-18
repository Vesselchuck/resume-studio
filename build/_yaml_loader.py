"""
_yaml_loader.py — one YAML loader for the whole project: fast, and
without YAML 1.1's habit of guessing what you meant.

WHY THIS EXISTS
───────────────
Two unrelated problems, one place to fix both.

1. SPEED. `yaml.safe_load` is PyYAML's pure-Python parser. On this
   project's data file (~17 KB) it costs ~12.6 ms; the same parse
   through libyaml costs ~0.8 ms. A resume preview parses the file
   twice — once for the measurement build, once for the final build —
   so the warm render loop was spending ~25 ms per cycle re-deriving
   the same dict the slow way. The C parser ships inside the PyYAML
   wheel on every platform this runs on, but not on every platform
   that exists, hence the fallback below.

2. TYPING. PyYAML implements YAML 1.1, which resolves untagged
   scalars by pattern. That is convenient for configuration files and
   actively wrong for a résumé, where almost every value is a piece of
   text that happens to look like something else:

       langs:    [no, yes, on, off]   →  [False, True, True, False]
       datetime: 2024-01-05           →  datetime.date(2024, 1, 5)
       hours:    22:30                →  1350          (sexagesimal!)
       gpa:      3.90                 →  3.9           (trailing zero lost)

   The first line is the famous one — "no" is the ISO code for
   Norwegian, so listing it as a language silently deletes it. The
   last is the one most likely to bite here: a GPA of 3.90 renders as
   "3.9", and nobody rounds their own GPA down on purpose.

WHAT THIS CHANGES
─────────────────
The loader below resolves scalars the way YAML 1.2's core schema does,
with two deliberate deviations. Concretely:

    true / True / TRUE / false / False / FALSE   → bool
    yes / no / on / off / y / n                  → str   (was bool)
    2024-01-05, 2024-01-05T09:00:00Z             → str   (was date)
    22:30                                        → str   (was int)
    42, -7, 0, 0x1f, 0o17                        → int   (unchanged)
    0451, 09, 010, 02139                         → str   (was octal / a crash)
    3.90, 1e5, .inf, .nan                        → str   (was float)
    null / Null / NULL / ~ / (empty)             → None  (unchanged)

The deviations from 1.2 are dropping floats and timestamps entirely
rather than re-resolving them in their stricter 1.2 forms. Both are
judgment calls about this project rather than about YAML:

  • Floats, because every number in a résumé is a number you are
    showing someone — a GPA, a version, a score — and the one thing
    you never want is for its printed form to change. Nothing in this
    project does arithmetic on data from the YAML; `meta.maxPages` is
    an integer and integers still resolve.

  • Timestamps, because every date here is a display string
    ("MMM YYYY – MMM YYYY", "September 16, 2026") and the schema
    validators require `str`. Before this, writing `date: 2026-09-16`
    — the most natural thing in the world — produced "'letter.date'
    must be a string if provided", which is a true statement and a
    baffling one, since what you wrote *was* a date.

Explicit tags still work and still mean what they say: `!!float 3.90`
is a float, `!!timestamp 2024-01-05` is a date. Nothing is taken away
from anyone who asks for it by name; only the guessing is gone.

Leading zeros deserve their own line. YAML 1.1 reads `010` as octal 8
and `0451` as 297, and a leading-zero number with an 8 or 9 in it
(`09`, `02139`) as a ValueError traceback, because PyYAML's int
constructor hands anything starting with `0` to int(value, 8). In a
résumé a number with a leading zero is an identifier — a ZIP code, a
room number, a course code — never a quantity, so it stays text, the
same way `3.90` does. YAML 1.2 core would read `0451` as decimal 451;
that loses the zero just as surely, so this project does not.

DUPLICATE KEYS
──────────────
PyYAML silently keeps the LAST of two identical keys in one mapping.
In a résumé that means a job with two `bullets:` blocks prints only the
second, and nothing says the first ever existed. ResumeLoader refuses
the file instead and names the key and both line numbers. Keys brought
in through a `<<:` merge are not duplicates — overriding a merged key
is what merging is for.

CONSEQUENCES ELSEWHERE
──────────────────────
`gap: true` in a job entry is still a real boolean, which is why the
1.2 core booleans were kept rather than dropping bools altogether.
build.validate_data() additionally rejects a quoted `gap: "true"`,
because a non-empty string is truthy and `gap: "false"` would
otherwise mark a gap entry while appearing to say the opposite.
"""

from __future__ import annotations

import re
import types

import yaml

#: What load() raises for a file it cannot read as YAML: a syntax error,
#: a duplicate key, a bad explicit tag. Re-exported so callers can catch
#: it and print a clean message without importing yaml themselves.
YAMLError = yaml.YAMLError

# libyaml if it is there, pure Python if it is not. The only difference
# is speed: both are the same SafeLoader semantics, and the resolver
# table below is applied in Python either way, because CSafeLoader uses
# the C scanner/parser with PyYAML's own Resolver mixin on top.
try:
    from yaml import CSafeLoader as _BaseLoader
    USING_LIBYAML = True
except ImportError:                      # pragma: no cover — platform dependent
    from yaml import SafeLoader as _BaseLoader
    USING_LIBYAML = False


#: Tags whose YAML 1.1 implicit resolvers are removed outright. See the
#: module docstring for why each one goes.
DROPPED_TAGS = (
    "tag:yaml.org,2002:bool",        # replaced with the 1.2 core form below
    "tag:yaml.org,2002:int",         # replaced with the 1.2 core form below
    "tag:yaml.org,2002:float",       # dropped: trailing zeros are meaning
    "tag:yaml.org,2002:timestamp",   # dropped: dates here are display text
)

#: YAML 1.2 core schema, minus the sexagesimal and underscore-separated
#: forms YAML 1.1 allowed, and minus decimal numbers with a leading zero
#: (see the module docstring: those are identifiers, so they stay text).
#: `0x`/`0o` are kept because they are unambiguous and cost nothing;
#: as in YAML 1.2 core they take no sign.
CORE_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
CORE_INT = re.compile(
    r"^(?:[-+]?(?:0|[1-9][0-9]*)|0x[0-9a-fA-F]+|0o[0-7]+)$")


class ResumeLoader(_BaseLoader):
    """SafeLoader with YAML 1.2 core typing. See the module docstring."""

    def construct_mapping(self, node, deep=False):
        """Refuse a mapping that states the same key twice.

        Checked before SafeConstructor flattens `<<:` merges, and only
        over the keys written in this mapping, so a key that overrides
        a merged-in one is not mistaken for a duplicate.
        """
        if isinstance(node, yaml.MappingNode):
            seen = {}
            for key_node, _value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                key = self.construct_object(key_node, deep=True)
                try:
                    first = seen.get(key)
                except TypeError:           # unhashable; let PyYAML report it
                    continue
                if first is not None:
                    # `1:` and `true:` are two spellings but one key once
                    # read (True == 1 in Python), and the mapping can hold
                    # only one of them, so they are refused like any other
                    # pair — with both spellings named, since the message
                    # would otherwise call `1:` a duplicate of itself.
                    first_mark, first_key = first
                    what = (f"duplicate key {key!r}" if type(first_key) is type(key)
                            else f"key {key!r}, which is read as the same key "
                                 f"as {first_key!r}")
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping", node.start_mark,
                        f"found {what} (first set on line "
                        f"{self._editor_line(first_mark)}). YAML would keep "
                        f"only the second one and silently drop the first — "
                        f"merge the two into one {key!r}, or delete one",
                        key_node.start_mark,
                    )
                seen[key] = (key_node.start_mark, key)
        return super().construct_mapping(node, deep=deep)

    #: The text being parsed, when load() was given a string; see load().
    source_text = None

    def _editor_line(self, mark):
        """mark's 1-based line as an editor counts it (see editor_position)."""
        if self.source_text is None:
            return mark.line + 1
        return editor_position(self.source_text, mark)[0]


def _construct_core_int(loader, node):
    """Build an int the YAML 1.2 way: decimal is base 10, always.

    PyYAML's own constructor is YAML 1.1's: it reads a leading `0` as
    octal and splits on `:` as base 60. The implicit resolver above no
    longer lets those shapes reach it, but an explicit `!!int 010` still
    would, so the conversion is replaced too rather than trusted.
    """
    raw = loader.construct_scalar(node)
    text = raw.strip().replace("_", "")
    sign = 1
    if text[:1] in ("-", "+"):
        sign = -1 if text[0] == "-" else 1
        text = text[1:]
    try:
        if text[:2] in ("0x", "0X"):
            return sign * int(text[2:], 16)
        if text[:2] in ("0o", "0O"):
            return sign * int(text[2:], 8)
        if text[:2] in ("0b", "0B"):
            return sign * int(text[2:], 2)
        return sign * int(text, 10)
    except ValueError:
        raise yaml.constructor.ConstructorError(
            None, None,
            f"{raw!r} is tagged !!int but is not an integer", node.start_mark,
        ) from None


def _rebuild_resolvers():
    """
    Replace ResumeLoader's implicit resolver table with a filtered copy.

    A copy, emphatically: `yaml_implicit_resolvers` is a plain class
    attribute, so mutating the inherited dict in place would silently
    re-type every other yaml.safe_load() in the process — including
    ones in libraries that never asked for any of this.
    """
    table = {
        first_char: [(tag, regexp) for tag, regexp in resolvers
                     if tag not in DROPPED_TAGS]
        for first_char, resolvers in _BaseLoader.yaml_implicit_resolvers.items()
    }
    ResumeLoader.yaml_implicit_resolvers = table

    # Re-add bool and int in their 1.2 core forms. The `first` argument
    # is the set of first characters that make the resolver worth
    # testing at all; getting it wrong means the resolver is simply
    # never consulted, which is a silent failure, so it is spelled out
    # rather than derived.
    ResumeLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool", CORE_BOOL, list("tTfF"))
    ResumeLoader.add_implicit_resolver(
        "tag:yaml.org,2002:int", CORE_INT, list("-+0123456789"))

    # add_constructor copies the inherited table before writing to it,
    # so this, like the resolvers, stays private to ResumeLoader.
    ResumeLoader.add_constructor("tag:yaml.org,2002:int", _construct_core_int)


_rebuild_resolvers()


def editor_position(text, mark):
    """
    (line, column), 1-based, as an editor counts them, for a YAML mark.

    YAML counts U+0085, U+2028 and U+2029 as line breaks, and editors do
    not (VS Code, Notepad: CR, LF, CRLF only), so mark.line was one too
    far down for every one of them above the error — and text pasted
    from a word processor carries U+2028. Columns are UTF-16 units, as
    in an editor's goto-line, so an emoji earlier on the line does not
    shift them. mark.index counts code points after any BOM (libyaml)
    or including it (the pure-Python reader); stripping the BOM from
    `text` before composing makes both agree.
    """
    head = text[:mark.index]
    breaks = list(re.finditer(r"\r\n|\r|\n", head))
    start = breaks[-1].end() if breaks else 0
    return len(breaks) + 1, len(head[start:].encode("utf-16-le")) // 2 + 1


def load(stream, name=None):
    """
    Parse one YAML document. The project's only entry point for that.

    Accepts a file object or a string, mirroring yaml.safe_load, and
    returns whatever the document contains — callers check that they
    got a mapping, because "the file is empty" and "the file is a list"
    deserve their own error messages rather than an AttributeError.

    `name` is what an error says the text came from, normally the file's
    path; without it PyYAML says `in "<unicode string>"`. Given a string,
    an error's line and column are also the ones an editor shows (see
    editor_position), not YAML's own count; for a character YAML cannot
    hold, they replace the offset PyYAML gives (see ReaderError).
    """
    text = None
    if isinstance(stream, str):
        # A leading BOM is not content (YAML skips it), and dropping it
        # here makes libyaml's marks and the pure-Python reader's count
        # the same text. See editor_position.
        stream = text = stream[1:] if stream.startswith("\ufeff") else stream
    loader = ResumeLoader(stream)
    loader.source_text = text
    try:
        return loader.get_single_data()
    except yaml.MarkedYAMLError as e:
        # Replaced rather than edited: libyaml's marks are read-only.
        for attr in ("context_mark", "problem_mark"):
            mark = getattr(e, attr)
            if mark is None:
                continue
            line, column = mark.line, mark.column
            if text is not None and mark.index is not None:
                line, column = (n - 1 for n in editor_position(text, mark))
            setattr(e, attr, yaml.error.Mark(
                mark.name if name is None else name, mark.index, line, column,
                getattr(mark, "buffer", None), getattr(mark, "pointer", None)))
        raise
    except yaml.reader.ReaderError as e:
        if name is not None:
            e.name = name
        if text is None or e.position is None or isinstance(e.character, bytes):
            raise
        # A control character. PyYAML gives only an offset into the text,
        # and libyaml counts it in UTF-8 bytes, so each accented letter
        # or emoji before it moved it further from anything an editor
        # shows. Bytes → characters → the editor's line and column.
        index = e.position
        if USING_LIBYAML:
            index = len(text.encode("utf-8", "surrogatepass")[:index]
                        .decode("utf-8", "ignore"))
        line, column = editor_position(text, types.SimpleNamespace(index=index))
        raise ReaderError(e, line, column) from None
    finally:
        loader.dispose()


class ReaderError(yaml.reader.ReaderError):
    """A character YAML cannot hold, placed at the line and column an
    editor shows (see load) rather than at PyYAML's offset."""

    def __init__(self, error, line, column):
        super().__init__(error.name, error.position, error.character,
                         error.encoding, error.reason)
        self.line, self.column = line, column

    def __str__(self):
        return (f"unacceptable character #x{self.character:04x}: "
                f"{self.reason}\n  in \"{self.name}\", line {self.line}, "
                f"column {self.column}")


def compose(text):
    """
    The node tree of one YAML document, with a start mark on every node.

    Only for LOCATING a value after validation has already refused it
    (build.locate_schema_error). load() keeps no positions: recording a
    line for every mapping, list and scalar on every render would tax
    the warm loop for the benefit of the rare file that fails, so the
    failing file's text is parsed a second time instead, here, on the
    error path only. Same loader, so tags resolve exactly as load() saw
    them (a `null` here is a null there).
    """
    return yaml.compose(text, Loader=ResumeLoader)
