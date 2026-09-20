/*
 * _output_name.js — where this build's PDFs live, on the Node side.
 *
 * The mirror of build/_output_name.py, and deliberately a much smaller
 * file, because it does not mirror the interesting half. The filename
 * stem is derived from `name.first` / `name.last` in the YAML, and
 * Node in this project cannot read YAML — that is the same constraint
 * that makes studio_server's document detection a column-anchored
 * regex instead of a parse.
 *
 * So the derivation happens once, in Python, and travels in the build
 * metadata:
 *
 *   build.py  ──  output_stem  ──▶  dist/pdf_meta.json
 *                                          │
 *                                          ▼
 *                                   _output_name.js
 *                                          │
 *                        ┌─────────────────┼──────────────────┐
 *                   pipeline.js         resume.js       studio_server.js
 *                  (writes them)     (prunes them)        (opens them)
 *
 * Nothing here guesses at a name. When the metadata is absent the stem
 * falls back to the bare document suffix ('Resume'), which names a
 * file that does not exist — the honest answer, since no build has run.
 *
 * Because the stem follows your name, changing your name changes the
 * filenames, and yesterday's PDFs would otherwise sit in dist/ next to
 * today's under a different name. pruneStale() clears them: see its
 * docstring for exactly how narrowly it is scoped, since it deletes.
 */

const path = require('path');
const fs = require('fs');

const {
  DOC_SUFFIX,
  SEPARATOR,
  GRAYSCALE_SUFFIX,
  RETIRED_GRAYSCALE_SUFFIXES,
  LEGACY,
} = JSON.parse(
  fs.readFileSync(path.join(__dirname, '_constants.json'), 'utf-8')
).output_name;


/** Escape a constant for literal use inside a RegExp. */
function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}


function assertVariant(variant) {
  if (!Object.prototype.hasOwnProperty.call(DOC_SUFFIX, variant)) {
    throw new Error(
      `unknown variant ${variant}; expected one of ${Object.keys(DOC_SUFFIX).join(', ')}`);
  }
}


/**
 * Read `output_stem` out of a build's metadata JSON.
 *
 * Every failure mode — no file, half-written file, a field of the
 * wrong type — lands on the bare document suffix rather than throwing,
 * because this is called from property getters that the Studio server
 * hits on every status poll, including before anything has been built.
 *
 * @param {string} metaFile — dist/pdf_meta.json or dist/letter_meta.json
 * @param {string} variant  — 'resume' or 'letter'
 */
function readStem(metaFile, variant) {
  assertVariant(variant);
  try {
    const meta = JSON.parse(fs.readFileSync(metaFile, 'utf-8'));
    if (typeof meta.output_stem === 'string' && meta.output_stem) {
      return meta.output_stem;
    }
  } catch {
    // Fall through to the suffix-only stem.
  }
  return DOC_SUFFIX[variant];
}


/**
 * The two PDF paths for one document.
 *
 * The color variant takes the bare stem — it is the file that gets
 * attached to an application, and it deserves the clean name. The
 * grayscale variant is suffixed.
 *
 * @param {string} dist     — the project's dist/ directory
 * @param {string} metaFile — that document's metadata JSON
 * @param {string} variant  — 'resume' or 'letter'
 * @returns {{stem: string, colorPdf: string, grayscalePdf: string}}
 */
function outputPaths(dist, metaFile, variant) {
  const stem = readStem(metaFile, variant);
  return {
    stem,
    colorPdf: path.join(dist, `${stem}.pdf`),
    grayscalePdf: path.join(dist, `${stem}${GRAYSCALE_SUFFIX}.pdf`),
  };
}


/**
 * Every filename this document's outputs could plausibly occupy.
 *
 * Anchored on both ends and built from the same constants the writer
 * uses, so it matches the project's own output namespace and nothing
 * else: an optional name part, the document suffix, an optional
 * grayscale suffix, `.pdf`. `Gaius_Iulius_Resume_Grayscale.pdf` and
 * `Resume.pdf` match; `letter_meta.json`, `styles.css` and a PDF
 * you dropped in dist/ yourself do not.
 *
 * RETIRED_GRAYSCALE_SUFFIXES widens it, and only it — never the paths
 * a build writes. The grayscale marker has been respelled once
 * (`-grayscale` → `_Grayscale`), and a file written under the old
 * spelling would otherwise fall outside the pattern the moment the
 * constant changed: not deleted, not overwritten, just left in dist/
 * looking like a current deliverable. Matching the retired spellings
 * here is what makes a respelling self-cleaning.
 */
function outputPattern(variant) {
  assertVariant(variant);
  const grayscale = [GRAYSCALE_SUFFIX, ...RETIRED_GRAYSCALE_SUFFIXES]
    .map(escapeRe).join('|');
  return new RegExp(
    `^(?:.+${escapeRe(SEPARATOR)})?${escapeRe(DOC_SUFFIX[variant])}`
    + `(?:${grayscale})?\\.pdf$`);
}


/**
 * Delete this document's outputs from earlier builds that today's
 * build will not overwrite.
 *
 * Two things put them there. The first is history: outputs used to be
 * called resume-color.pdf and letter-grayscale.pdf, and those names
 * are listed in _constants.json under LEGACY; the grayscale marker has
 * since been respelled again, which RETIRED_GRAYSCALE_SUFFIXES covers.
 * The second is ongoing — the stem follows `name.first` /
 * `name.last`, so correcting a typo in your surname renames all four
 * files and strands the old ones.
 *
 * Either way a stale PDF in dist/ is worse than clutter: it is a
 * complete, plausible-looking resume that is not the one you just
 * built, sitting in the directory you go to when you need to attach
 * one.
 *
 * THIS FUNCTION DELETES FILES, so its reach is drawn tightly:
 *
 *   • only inside dist/, which is build output and gitignored;
 *   • only names matching outputPattern(variant) or LEGACY[variant],
 *     both generated from the constants this project writes with;
 *   • never a path in `keep`, which is what the current build produced.
 *
 * A file it cannot remove is reported and skipped, not thrown on: the
 * PDFs are already written by the time this runs, and a PDF viewer
 * holding an old file open is not a reason to fail a build that
 * succeeded.
 *
 * @param {string}   dist    — the project's dist/ directory
 * @param {string}   variant — 'resume' or 'letter'
 * @param {string[]} keep    — absolute paths this build wrote
 * @param {?function} onRemove — called with (relativeName) per deletion
 * @param {?function} onFailure — called with (relativeName, message)
 * @returns {string[]} the names removed
 */
function pruneStale(dist, variant, keep, onRemove = null, onFailure = null) {
  const pattern = outputPattern(variant);
  const kept = new Set(keep.map(p => path.resolve(p)));
  const removed = [];

  let entries;
  try {
    entries = fs.readdirSync(dist);
  } catch {
    return removed;                      // No dist/ yet: nothing to prune.
  }

  const candidates = new Set(
    entries.filter(name => pattern.test(name) || LEGACY[variant].includes(name)));

  for (const name of candidates) {
    const full = path.join(dist, name);
    if (kept.has(path.resolve(full))) continue;
    try {
      if (!fs.statSync(full).isFile()) continue;
      fs.rmSync(full, { force: true });
      removed.push(name);
      if (onRemove) onRemove(name);
    } catch (err) {
      if (onFailure) onFailure(name, err.message);
    }
  }
  return removed;
}


module.exports = {
  DOC_SUFFIX,
  SEPARATOR,
  GRAYSCALE_SUFFIX,
  RETIRED_GRAYSCALE_SUFFIXES,
  LEGACY,
  readStem,
  outputPaths,
  outputPattern,
  pruneStale,
};
