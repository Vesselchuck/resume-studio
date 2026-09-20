/*
 * _env_contract.js — Cross-process env-var name constants.
 *
 * The names live in build/_constants.json (under the `env_contract`
 * key). This module loads them at require-time and exposes them as a
 * frozen object so destructuring imports like
 *   const { ENV_SKIP_SNAPSHOT } = require('./_env_contract');
 * keep working unchanged. The matching _env_contract.py does the
 * same. There is no parallel hand-maintained list; cross-language
 * parity is structural.
 *
 * The constants here are env-var NAMES (the strings used as
 * process.env[...] keys), not their values. Values are documented
 * at each consumer's read site since they carry domain-specific
 * meaning (e.g. RESUME_DATA_SOURCE accepts 'default' or 'mine').
 *
 * Imported by:
 *   • resume.js         — reads ENV_SKIP_SNAPSHOT, ENV_RESUME_PIPELINE_SUFFIX
 *
 * The exported object is frozen so a typo like `ENV.FOO = 'bar'` at
 * a call site throws TypeError immediately rather than silently
 * mutating the contract.
 */

const path = require('path');
const fs = require('fs');

const _envConstants = JSON.parse(
  fs.readFileSync(path.join(__dirname, '_constants.json'), 'utf-8')
).env_contract;

module.exports = Object.freeze({
  // Selects which YAML data file the build consumes.
  //   'default' → require data/resume_default.yml (error if missing)
  //   'mine'    → require data/resume.yml (error if missing)
  //   unset     → use yours if present, else the placeholder
  ENV_RESUME_DATA_SOURCE: _envConstants.RESUME_DATA_SOURCE,

  // An explicit data file to read, overriding the source search. Lets a
  // tool preview any file without copying it over the user's own.
  ENV_RESUME_DATA_FILE: _envConstants.RESUME_DATA_FILE,
  ENV_LETTER_DATA_FILE: _envConstants.LETTER_DATA_FILE,

  // When '1', resume.js skips the visual-regression snapshot step.
  // Set by snapshot_pdf.py --update-all during multi-source rebuilds
  // so intermediate builds don't compare against soon-to-be-replaced
  // fixtures.
  ENV_SKIP_SNAPSHOT: _envConstants.SKIP_SNAPSHOT,

  // Which PDF variants to produce: a comma-separated subset of
  // 'color' and 'grayscale'. Unset means both. A variant left out has
  // its dist/ output removed, so "the file exists" stays a truthful
  // signal that it was built by the run that just finished.
  ENV_RESUME_VARIANTS: _envConstants.RESUME_VARIANTS,

  // Whether a build checks its PDFs against the committed fixtures:
  // 'off' (default) skips, 'on' reports a difference without failing,
  // 'strict' fails the build the way this always used to.
  ENV_RESUME_SNAPSHOT: _envConstants.RESUME_SNAPSHOT,

  // Whether the build runs the unit suites first.
  //   'on' (default) → run them; a failure stops the build
  //   'off'          → skip them, go straight to building
  ENV_RESUME_TESTS: _envConstants.RESUME_TESTS,

  // Label appended to resume.js's first phase banner so the user
  // sees "Tests (default data)" / "Tests (my data)" at the top
  // of each pass during a multi-source rebuild. Unset in normal
  // builds.
  ENV_RESUME_PIPELINE_SUFFIX: _envConstants.RESUME_PIPELINE_SUFFIX,
});
