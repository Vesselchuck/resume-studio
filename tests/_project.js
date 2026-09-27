/**
 * _project.js — a throwaway copy of the project, for the suites that
 * run the engine or the Studio server.
 *
 * WHY
 * ───
 * Those suites build. A build writes dist/ — styles.css, index.html,
 * placement.json, pdf_meta.json, letter.html, letter_meta.json,
 * favicon.svg — and reads it back: the engine guesses from the
 * placement already there, and the stylesheet is recompiled only when
 * it is stale. Run against this checkout, a suite shares all of that
 * with your last build and with every suite that ran before it.
 *
 * The suites used to cope by saving a list of dist/ files and writing
 * them back afterwards, each with its own list. That kept failing:
 *   • test_cold_start.js and then test_speculative_load.js were each
 *     caught passing or failing according to what an earlier build had
 *     left (the second one: its first render guessed from whatever
 *     placement.json was there);
 *   • favicon.svg was on no list, so after a test run it carried the
 *     initials of whichever data the last suite happened to build;
 *   • test_cold_start.js named no data file, so its letter preview
 *     built your own data/letter.yml when you had one.
 * A suite killed mid-run also never wrote anything back.
 *
 * A copy removes the shared state instead of managing it. Its data/
 * holds only the shipped *_default.yml templates, its dist/ starts
 * empty, and it is deleted afterwards.
 *
 * LOAD THE ENGINE FROM THE COPY
 * ─────────────────────────────
 * `project.require('build/engine')`, not `require('../build/engine')`
 * with `root` pointed at the copy. The Python worker finds the project
 * from its own file's location (build/worker.py's ROOT), not from the
 * `root` option, so an engine loaded from this checkout would still
 * have Python writing this checkout's dist/. `realDistFingerprint()`
 * is how each suite proves it did not.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');

const ROOT = path.join(__dirname, '..');

/** Copy what a build needs into `dest`. node_modules is linked, not copied. */
function copyProject(dest) {
  const copyDir = (from, to) => {
    fs.mkdirSync(to, { recursive: true });
    for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
      if (entry.name === '__pycache__') continue;
      const src = path.join(from, entry.name);
      const dst = path.join(to, entry.name);
      if (entry.isDirectory()) copyDir(src, dst);
      else if (entry.isFile()) fs.copyFileSync(src, dst);
    }
  };
  for (const dir of ['build', 'styles', 'templates', 'fonts', 'ui', 'schemas']) {
    if (fs.existsSync(path.join(ROOT, dir))) copyDir(path.join(ROOT, dir), path.join(dest, dir));
  }
  for (const file of ['resume.js', 'letter.js', 'package.json']) {
    fs.copyFileSync(path.join(ROOT, file), path.join(dest, file));
  }
  fs.mkdirSync(path.join(dest, 'data'), { recursive: true });
  for (const file of fs.readdirSync(path.join(ROOT, 'data'))) {
    if (/_default\.ya?ml$/.test(file)) {
      fs.copyFileSync(path.join(ROOT, 'data', file), path.join(dest, 'data', file));
    }
  }
  fs.symlinkSync(path.join(ROOT, 'node_modules'), path.join(dest, 'node_modules'),
    process.platform === 'win32' ? 'junction' : 'dir');
}

/**
 * A fresh copy in the system temp directory.
 *
 *   root          the copy; pass it as the engine's or server's root
 *   dist          root/dist
 *   require(rel)  a module loaded from the copy, e.g. 'build/engine'
 *   remove()      delete the copy (the node_modules link, not its target)
 */
function tempProject(prefix) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), `${prefix}-`));
  const root = path.join(tmp, 'project');
  copyProject(root);
  return {
    root,
    dist: path.join(root, 'dist'),
    require: rel => require(path.join(root, rel)),
    remove() {
      try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* best effort */ }
    },
  };
}

/**
 * Every file in this checkout's dist/, with its size and mtime. Taken
 * before and after a suite: if anything wrote there, even the same
 * bytes back, the two differ.
 */
function realDistFingerprint() {
  const dist = path.join(ROOT, 'dist');
  const out = [];
  const walk = (dir) => {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.isFile()) {
        const st = fs.statSync(p);
        out.push(`${path.relative(dist, p)} ${st.size} ${st.mtimeMs}`);
      }
    }
  };
  walk(dist);
  return out.sort();
}

module.exports = { ROOT, copyProject, tempProject, realDistFingerprint };
