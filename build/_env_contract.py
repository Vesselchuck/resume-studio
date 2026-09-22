"""
_env_contract.py — Cross-process env-var name constants.

The names live in build/_constants.json (under the `env_contract`
key). This module loads them at import time and exposes them as
module-level `ENV_*` names so existing `from _env_contract import
ENV_RESUME_DATA_SOURCE` style imports keep working. The matching
_env_contract.js does the same. There is no parallel hand-maintained
list; cross-language parity is structural.

The constants here are env-var NAMES (the strings used as
os.environ.get() keys), not their values. Values are documented at
each consumer's read site, since they carry domain-specific meaning
(e.g. RESUME_DATA_SOURCE accepts 'default' or 'mine').

Imported by:
  • build.py            — reads ENV_RESUME_DATA_SOURCE
  • snapshot_pdf.py     — sets RESUME_DATA_SOURCE and removes the two
                          data-file variables when invoking resume.js
"""

import json
from pathlib import Path

_CONSTANTS_PATH = Path(__file__).parent / "_constants.json"
with _CONSTANTS_PATH.open(encoding="utf-8") as _f:
    _env_constants = {
        k: v for k, v in json.load(_f)["env_contract"].items()
        if not k.startswith("_comment")
    }

# Selects which YAML data file the build consumes.
#   'default' → require data/resume_default.yml (error if missing)
#   'mine'    → require data/resume.yml (error if missing)
#   unset     → use yours if present, else the shipped placeholder
ENV_RESUME_DATA_SOURCE = _env_constants["RESUME_DATA_SOURCE"]

# An explicit data file to read, overriding the source search entirely.
# Absolute, or relative to the project root.
ENV_RESUME_DATA_FILE = _env_constants["RESUME_DATA_FILE"]
ENV_LETTER_DATA_FILE = _env_constants["LETTER_DATA_FILE"]

# Which PDF variants a build produces: a comma-separated subset of
# 'color' and 'grayscale'. Unset means both. A variant left out has its
# dist/ output removed, so snapshot_pdf.py can treat "the file exists"
# as meaning "this run built it".
ENV_RESUME_VARIANTS = _env_constants["RESUME_VARIANTS"]

# Whether a build checks its PDFs against the committed fixtures:
# 'off' (default) skips, 'on' reports without failing, 'strict' fails.
ENV_RESUME_SNAPSHOT = _env_constants["RESUME_SNAPSHOT"]

# Whether the build runs the unit suites first.
#   'on' (default) → run them; a failure stops the build
#   'off'          → skip them, go straight to building
ENV_RESUME_TESTS = _env_constants["RESUME_TESTS"]

# When '1', resume.js skips the visual-regression snapshot step.
# Set by snapshot_pdf.py --update-all so the two intermediate builds
# (whose PDFs ARE the new fixtures) don't compare against the
# fixtures they're about to replace.
ENV_SKIP_SNAPSHOT = _env_constants["SKIP_SNAPSHOT"]

# Free-form label appended to resume.js's first phase banner so the
# user sees "Tests (default data)" / "Tests (my data)" at the
# top of each pass during a multi-source rebuild. Set by
# snapshot_pdf.py --update-all; unset in normal builds.
ENV_RESUME_PIPELINE_SUFFIX = _env_constants["RESUME_PIPELINE_SUFFIX"]

del _env_constants, _f
