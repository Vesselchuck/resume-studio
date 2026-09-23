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
 *                   (writes it)      (prunes stale)       (opens it)
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
 * The PDF path for one document.
 *
 * One document, one PDF, under the bare stem — it is the file that
 * gets attached to an application, and it deserves the clean name.
 * There used to be a second, grayscale PDF under a suffixed name;
 * see outputPattern for what remains of it.
 *
 * @param {string} dist     — the project's dist/ directory
 * @param {string} metaFile — that document's metadata JSON
 * @param {string} variant  — 'resume' or 'letter'
 * @returns {{stem: string, pdf: string}}
 */
function outputPaths(dist, metaFile, variant) {
  const stem = readStem(metaFile, variant);
  return {
    stem,
    pdf: path.join(dist, `${stem}.pdf`),
  };
}


/**
 * Every filename this document's outputs could plausibly occupy.
 *
 * Anchored on both ends and built from the same constants the writer
 * uses, so it matches the project's own output namespace and nothing
 * else: an optional name part, the document suffix, an optional
 * retired grayscale suffix, `.pdf`. `Resume.pdf` and
 * `Gaius_Caesar_Resume_Grayscale.pdf` match; `letter_meta.json`,
 * `styles.css` and a PDF you dropped in dist/ yourself do not.
 *
 * This pattern is WIDER than what a build writes, deliberately, and
 * RETIRED_GRAYSCALE_SUFFIXES is the whole of the difference. This
 * project used to write a second, black-and-white PDF beside each
 * document (under `_Grayscale`, and before that `-grayscale`). It does
 * not any more — the one PDF prints correctly either way — but a copy
 * from an older build, or from an older spelling, would otherwise fall
 * outside the pattern the moment the constant changed: not deleted,
 * not overwritten, just left in dist/ looking like a current
 * deliverable, next to the file it is not. Matching the retired
 * spellings here is what makes both the removal and the earlier
 * respelling self-cleaning.
 */
function outputPattern(variant) {
  assertVariant(variant);
  const retired = RETIRED_GRAYSCALE_SUFFIXES.map(escapeRe).join('|');
  return new RegExp(
    `^(?:.+${escapeRe(SEPARATOR)})?${escapeRe(DOC_SUFFIX[variant])}`
    + `(?:${retired})?\\.pdf$`);
}


/**
 * Delete this document's outputs from earlier builds that today's
 * build will not overwrite.
 *
 * Two things put them there. The first is history: outputs used to be
 * called resume-color.pdf and letter-grayscale.pdf, and those names
 * are listed in _constants.json under LEGACY; there also used to be a
 * second, black-and-white PDF per document, which
 * RETIRED_GRAYSCALE_SUFFIXES covers under both spellings it had. The
 * second is ongoing — the stem follows `name.first` / `name.last`, so
 * correcting a typo in your surname renames the files and strands the
 * old ones.
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
 * @param {object}   [deps]  — test seam: { fs, platform }
 * @returns {string[]} the names removed
 *
 * "Never a path in `keep`" is decided by FILE IDENTITY, not by the
 * path string. On a case-insensitive filesystem (NTFS, APFS by
 * default) writing Gaius_CAESAR_Resume.pdf over an existing
 * Gaius_Caesar_Resume.pdf keeps the old spelling on disk, so readdir
 * returns a name that string-compares unequal to the path the build
 * just wrote — and the file this build produced would be deleted as
 * "stale". Comparing dev+ino (as BigInt, so a 64-bit NTFS file index
 * does not lose precision) sees they are the same file. For a
 * filesystem that reports no inode (ino 0), win32/darwin also compare
 * paths case-insensitively; elsewhere the exact resolved path, which
 * was the whole comparison before. Both checks only ever KEEP a file:
 * on a case-sensitive APFS volume the case-folded check can spare a
 * genuinely stale differently-cased PDF, which errs the safe way.
 */
function pruneStale(dist, variant, keep, onRemove = null, onFailure = null,
                    { fs: fsImpl = fs, platform = process.platform } = {}) {
  const pattern = outputPattern(variant);
  const caseInsensitive = platform === 'win32' || platform === 'darwin';
  const fold = p => (caseInsensitive ? p.toLowerCase() : p);
  const identity = (p) => {
    try {
      const st = fsImpl.statSync(p, { bigint: true });
      // A zero or missing inode is "unknown", not an identity: two
      // different files would otherwise collide on `dev:0`.
      if (st.ino === undefined || st.ino === null || BigInt(st.ino) === 0n) return null;
      return `${st.dev}:${st.ino}`;
    } catch {
      return null;                       // Missing: nothing to match by.
    }
  };

  const keptPaths = new Set(keep.map(p => fold(path.resolve(p))));
  const keptIds = new Set(keep.map(identity).filter(Boolean));
  const removed = [];

  let entries;
  try {
    entries = fsImpl.readdirSync(dist);
  } catch {
    return removed;                      // No dist/ yet: nothing to prune.
  }

  const candidates = new Set(
    entries.filter(name => pattern.test(name) || LEGACY[variant].includes(name)));

  for (const name of candidates) {
    const full = path.join(dist, name);
    if (keptPaths.has(fold(path.resolve(full)))) continue;
    const id = identity(full);
    if (id && keptIds.has(id)) continue;
    try {
      if (!fsImpl.statSync(full).isFile()) continue;
      fsImpl.rmSync(full, { force: true });
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
  RETIRED_GRAYSCALE_SUFFIXES,
  LEGACY,
  readStem,
  outputPaths,
  outputPattern,
  pruneStale,
};
