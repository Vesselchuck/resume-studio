/**
 * _cli.js — what the two cold CLI drivers (resume.js, letter.js) share.
 *
 * Both drivers run Python the same way, prune stale outputs the same
 * way and read the same `data_source` field back from their metadata.
 * Each used to carry its own copy of these; a fix to one had to be
 * remembered in the other. build/engine.js, the warm driver, has its
 * own adapter backed by build/worker.py and does not use this module.
 */

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { reported } = require('./pipeline');
const { pruneStale } = require('./_output_name');
const distLock = require('./_dist_lock');
const c = require('./_console');


/**
 * The env passed to a CLI build's subprocesses (build.py,
 * build_letter.py, crop_pdf.py, snapshot_pdf.py, the test runner).
 * When the driver would itself print color (its stdout is a TTY, or
 * FORCE_COLOR asks for it), forward that via FORCE_COLOR=1 so
 * subprocesses keep their ANSI codes — otherwise execFileSync's
 * pipe-captured stdout would look like non-TTY to them and they'd
 * suppress color. Asking colorEnabled() rather than isTTY is what keeps
 * a user's FORCE_COLOR=0 from being overwritten with 1. NO_COLOR
 * passthrough is automatic since process.env is inherited.
 *
 * PYTHONUTF8 / PYTHONIOENCODING: the Python children print emoji (the
 * _console symbols). With stdout captured into a pipe, Windows Python
 * encodes to the ANSI code page (cp1252), where ✅ raises
 * UnicodeEncodeError and the build dies on its first status line.
 * build/engine.js sets the same two for its worker.
 */
function subprocessEnv() {
  const env = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
  if (c.colorEnabled(process.stdout)) env.FORCE_COLOR = '1';
  return env;
}


/**
 * An exit status as people look it up: Windows' NTSTATUS crash codes
 * are documented in hex, so 3221225477 is also given as 0xC0000005.
 */
function describeStatus(status) {
  if (status <= 255) return String(status);
  return `${status} (0x${(status >>> 0).toString(16).toUpperCase().padStart(8, '0')})`;
}


/**
 * runPython for one project and interpreter.
 *
 * Runs a Python script as a subprocess, handling stdio and errors the
 * same way every time. `-B` is added unconditionally to suppress
 * __pycache__ creation.
 *
 * On success, the captured stdout is written through to the parent's
 * stdout so the subprocess's _console output appears in order.
 *
 * On failure:
 *   • The subprocess's stderr was already streamed to the parent's
 *     stderr (stdio 'inherit'), so its failure markers from
 *     _console.err() etc. have already reached the user.
 *   • Captured stdout (which is NOT streamed) is flushed, so any
 *     in-progress success markers are not lost.
 *   • If the subprocess never ran to an exit (it could not be spawned,
 *     or a signal killed it), or exited with a status the scripts never
 *     use (a native crash), a fallback _console error says why it
 *     stopped. One that exited 1 or 2 has already said why, on stderr.
 *   • The error is re-thrown, marked reported(), so the orchestrator
 *     decides the exit policy without printing it again.
 *
 * Returned function: runPython(scriptArgs, fallbackLabel)
 *   scriptArgs    — args after `-B`, typically [script_path, ...]
 *   fallbackLabel — error label used if the subprocess never ran to
 *                   an exit, or crashed
 */
function pythonRunner(root, python) {
  return function runPython(scriptArgs, fallbackLabel) {
    try {
      const out = execFileSync(python, ['-B', ...scriptArgs], {
        cwd: root,
        encoding: 'utf-8',
        env: subprocessEnv(),
        // Explicit: stdin ignored, stdout captured into `out` for
        // ordered replay below, stderr inherited so subprocess
        // diagnostics stream live. Without this, the implicit Node
        // default (which inherits stderr) happens to do the right
        // thing, but a future stdio override would silently break
        // the streaming-diagnostics contract.
        stdio: ['ignore', 'pipe', 'inherit'],
      });
      process.stdout.write(out);
    } catch (err) {
      if (err.stdout) process.stdout.write(err.stdout);
      // A null status means the script never ran to an exit — spawn
      // failed (a bad PYTHON) or a signal killed it — so whatever it
      // printed, it did not print why it stopped. A script that exited
      // with a status did: these scripts say why before every non-zero
      // exit, and Python prints a traceback for anything they do not
      // catch, all on the inherited stderr, which err.stderr never holds.
      // Testing that (always null) printed this fallback, "Command
      // failed: python -B …", under the script's own message whenever
      // its stdout happened to be empty.
      //
      // But only 1 and 2 are statuses these scripts exit with (2: a
      // missing dependency or input; 1: everything else, uncaught
      // exceptions included). Anything else is the interpreter dying
      // under them — a native crash is 3221225477 (0xC0000005) on
      // Windows — which prints nothing, and the build failed without a
      // word.
      if (typeof err.status !== 'number') {
        c.err(`${fallbackLabel}: ${err.message}`);
      } else if (err.status !== 1 && err.status !== 2) {
        c.err(`${fallbackLabel}: exited ${describeStatus(err.status)} without saying why`);
      }
      throw reported(err);
    }
  };
}


/**
 * Clear this document's outputs from earlier builds that this one did
 * not overwrite.
 *
 * The PDF is named after you, so the set of filenames a build occupies
 * moves when `name.first` / `name.last` does — and it moved for
 * everyone twice already: once when outputs stopped being called
 * {DOC}-color.pdf, and again when the grayscale variant stopped being
 * built at all. Left alone, dist/ accumulates complete,
 * plausible-looking documents under names that are no longer current,
 * in the exact directory you open when you need to attach one.
 *
 * Runs after printPdfs and recordBuilt, so `keep` is what actually
 * landed on disk and the build record lists it. Only names that record
 * says a build wrote are removed (and the retired ones no build writes
 * now, beside a PDF a build did write), never a PDF of yours that
 * merely looks like one.
 * _output_name.pruneStale does the deleting and is scoped there; this
 * only supplies the reporting.
 */
function pruneStaleOutputs(root, variant, keep) {
  pruneStale(
    path.join(root, 'dist'),
    variant,
    keep,
    name => c.info_pair('Removed stale PDF', `${path.join('dist', name)} (not this build's name)`),
    (name, why) => c.warn_pair('Could not remove', `${path.join('dist', name)} — ${why}`),
  );
}


/**
 * Take dist/'s lock for a CLI build (build/_dist_lock.js), waiting for
 * another holder and saying so. Giving up — a holder that did not let
 * go, a dist/.lock that can be neither created nor read, a dist/ that
 * cannot be made — is about this computer, not a bug in the build, so
 * it is reported as an error and its message, marked reported(). It
 * used to reach the drivers' catch-all as "Unexpected error" and a
 * stack trace. An error with no code is a bug, and is left to that.
 */
async function acquireDist(root, owner) {
  try {
    return await distLock.acquire(path.join(root, 'dist'), {
      owner,
      blocking: true,     // execFileSync steps: heartbeat from a thread
      onWait: who => c.info_pair('Waiting for dist/', `in use by ${who}`),
    });
  } catch (err) {
    if (typeof err.code !== 'string') throw err;
    c.err('Could not take dist/ for this build');
    c.detail(err.message);
    throw reported(err);
  }
}


/** data_source from a build's metadata JSON, or null if it can't be read. */
function readDataSource(metaFile) {
  try {
    const meta = JSON.parse(fs.readFileSync(metaFile, 'utf-8'));
    return typeof meta.data_source === 'string' ? meta.data_source : null;
  } catch {
    return null;
  }
}


module.exports = {
  subprocessEnv, pythonRunner, pruneStaleOutputs, readDataSource, acquireDist, describeStatus,
};
