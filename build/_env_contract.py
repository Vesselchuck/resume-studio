"""
_env_contract.py — Cross-process env-var name constants.

The names live in build/_constants.json (under the `env_contract`
key). This module loads them at import time and exposes them as
module-level `ENV_*` names so existing `from _env_contract import
ENV_RESUME_DATA_SOURCE` style imports keep working. The matching
_env_contract.js does the same. There is no parallel hand-maintained
list; cross-language parity is structural.

The three constants here are env-var NAMES (the strings used as
os.environ.get() keys), not their values. Values are documented at
each consumer's read site, since they carry domain-specific meaning
(e.g. RESUME_DATA_SOURCE accepts 'default' or 'local').

Imported by:
  • build.py            — reads ENV_RESUME_DATA_SOURCE
  • snapshot_pdf.py     — sets all three when invoking render.js
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
#   'local'   → require data/resume.local.yml (error if missing)
#   unset     → use local if present, else default (developer-friendly)
ENV_RESUME_DATA_SOURCE = _env_constants["RESUME_DATA_SOURCE"]

# When '1', render.js skips the visual-regression snapshot step.
# Set by snapshot_pdf.py --update-all so the two intermediate builds
# (whose PDFs ARE the new fixtures) don't compare against the
# fixtures they're about to replace.
ENV_SKIP_SNAPSHOT = _env_constants["SKIP_SNAPSHOT"]

# Free-form label appended to render.js's first phase banner so the
# user sees "Tests (default data)" / "Tests (local data)" at the
# top of each pass during a multi-source rebuild. Set by
# snapshot_pdf.py --update-all; unset in normal builds.
ENV_RESUME_PIPELINE_SUFFIX = _env_constants["RESUME_PIPELINE_SUFFIX"]

del _env_constants, _f
