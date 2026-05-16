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
 * The three constants here are env-var NAMES (the strings used as
 * process.env[...] keys), not their values. Values are documented
 * at each consumer's read site since they carry domain-specific
 * meaning (e.g. RESUME_DATA_SOURCE accepts 'default' or 'local').
 *
 * Imported by:
 *   • render.js         — reads ENV_SKIP_SNAPSHOT, ENV_RESUME_PIPELINE_SUFFIX
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
  //   'local'   → require data/resume.local.yml (error if missing)
  //   unset     → use local if present, else default
  ENV_RESUME_DATA_SOURCE: _envConstants.RESUME_DATA_SOURCE,

  // When '1', render.js skips the visual-regression snapshot step.
  // Set by snapshot_pdf.py --update-all during multi-source rebuilds
  // so intermediate builds don't compare against soon-to-be-replaced
  // fixtures.
  ENV_SKIP_SNAPSHOT: _envConstants.SKIP_SNAPSHOT,

  // Label appended to render.js's first phase banner so the user
  // sees "Tests (default data)" / "Tests (local data)" at the top
  // of each pass during a multi-source rebuild. Unset in normal
  // builds.
  ENV_RESUME_PIPELINE_SUFFIX: _envConstants.RESUME_PIPELINE_SUFFIX,
});
