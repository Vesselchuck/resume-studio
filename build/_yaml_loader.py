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
    42, -7, 0x1f, 0o17                           → int   (unchanged)
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

import yaml

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
#: forms YAML 1.1 allowed. `0x`/`0o` are kept because they are
#: unambiguous and cost nothing.
CORE_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
CORE_INT = re.compile(r"^[-+]?(?:[0-9]+|0x[0-9a-fA-F]+|0o[0-7]+)$")


class ResumeLoader(_BaseLoader):
    """SafeLoader with YAML 1.2 core typing. See the module docstring."""


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


_rebuild_resolvers()


def load(stream):
    """
    Parse one YAML document. The project's only entry point for that.

    Accepts a file object or a string, mirroring yaml.safe_load, and
    returns whatever the document contains — callers check that they
    got a mapping, because "the file is empty" and "the file is a list"
    deserve their own error messages rather than an AttributeError.
    """
    return yaml.load(stream, Loader=ResumeLoader)
