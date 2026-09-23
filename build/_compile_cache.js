/**
 * _compile_cache.js — let V8 reuse the compiled form of this app's
 * JavaScript between runs.
 *
 * WHY
 * ---
 * Most of the Studio server's cold start is not work, it is reading and
 * compiling JavaScript. `require('playwright')` alone was measured at
 * ~290 ms of the ~450 ms before the first preview: thousands of
 * functions parsed and compiled from scratch, identically, on every
 * launch.
 *
 * Node 22.8 added a compile cache for exactly this: enable it and V8's
 * code cache for every module compiled afterwards is written to a
 * directory and reused on the next run. Nothing about what the modules
 * do changes — the cache holds compiled bytecode keyed by the file's
 * contents, the Node version and the V8 flags, so a stale or mismatched
 * entry is ignored rather than trusted.
 *
 * WHERE THE CACHE LIVES
 * ---------------------
 * Not in the project. Writing build artifacts into a checkout that the
 * user also edits and commits is how a .gitignore grows entries nobody
 * asked for, and the desktop app may well be installed somewhere the
 * user cannot write at all (Program Files). So it goes in the OS's own
 * per-user cache location:
 *
 *   Windows  %LOCALAPPDATA%\resume-studio\node-compile-cache
 *   macOS    ~/Library/Caches/resume-studio/node-compile-cache
 *   Linux    $XDG_CACHE_HOME/resume-studio/node-compile-cache
 *            (or ~/.cache/resume-studio/node-compile-cache)
 *   fallback <os.tmpdir()>/resume-studio-node-compile-cache
 *
 * NODE_COMPILE_CACHE in the environment wins: someone who has already
 * pointed Node at a cache directory gets that one, and this does not
 * second-guess them.
 *
 * DEGRADING QUIETLY
 * -----------------
 * On Node < 22.8 there is no such API, and on a read-only or full disk
 * enabling it fails. Both are non-events: the process runs exactly as
 * it did before, a few hundred milliseconds slower to start. Nothing
 * here throws, and nothing here prints unless asked (`describe()`),
 * because a line about a cache directory is not what a user starting
 * their resume editor wants to read.
 *
 * Call enable() as the very first thing a process does — the cache only
 * covers modules compiled after it is on.
 */

const os = require('os');
const path = require('path');

const APP = 'resume-studio';
const LEAF = 'node-compile-cache';

/** The per-user cache directory for this platform. Never inside the project. */
function cacheDir() {
  if (process.platform === 'win32') {
    const local = process.env.LOCALAPPDATA || process.env.APPDATA;
    if (local) return path.join(local, APP, LEAF);
  } else if (process.platform === 'darwin') {
    const home = os.homedir();
    if (home) return path.join(home, 'Library', 'Caches', APP, LEAF);
  } else {
    const xdg = process.env.XDG_CACHE_HOME;
    if (xdg && path.isAbsolute(xdg)) return path.join(xdg, APP, LEAF);
    const home = os.homedir();
    if (home) return path.join(home, '.cache', APP, LEAF);
  }
  return path.join(os.tmpdir(), `${APP}-${LEAF}`);
}

let result = null;

/**
 * Turn the compile cache on, once per process.
 *
 * Returns { enabled, dir, reason } and never throws.
 */
function enable() {
  if (result) return result;
  if (process.env.NODE_COMPILE_CACHE) {
    result = { enabled: true, dir: process.env.NODE_COMPILE_CACHE,
               reason: 'NODE_COMPILE_CACHE was already set' };
    return result;
  }
  let api;
  try {
    api = require('node:module').enableCompileCache;
  } catch {
    api = undefined;
  }
  if (typeof api !== 'function') {
    result = { enabled: false, dir: null,
               reason: `no compile cache on Node ${process.versions.node}` };
    return result;
  }
  const dir = cacheDir();
  try {
    const r = api(dir) || {};
    // ENABLED and ALREADY_ENABLED mean it is on. FAILED means it could
    // not be (a directory that cannot be written, most likely), and
    // DISABLED means the user turned it off with
    // NODE_DISABLE_COMPILE_CACHE — which is their business, and is not
    // something to report as on.
    const status = require('node:module').constants
      && require('node:module').constants.compileCacheStatus;
    const on = status
      ? [status.ENABLED, status.ALREADY_ENABLED].includes(r.status)
      : r.status !== 0;
    result = {
      enabled: on,
      dir: r.directory || dir,
      reason: r.message || (on ? null : `the compile cache is off (status ${r.status})`),
    };
  } catch (err) {
    result = { enabled: false, dir, reason: err.message };
  }
  return result;
}

/** One line for a log, or null when there is nothing worth saying. */
function describe() {
  const r = result || enable();
  return r.enabled ? `(compiled-code cache: ${r.dir})` : null;
}

module.exports = { enable, describe, cacheDir };
