/**
 * test_worker_log.js — what Python warns about reaches the Studio's log.
 *
 * WHAT THIS GUARDS
 * ----------------
 * The worker returns what a request printed in two lists: `log` (its
 * stdout) and `diagnostics` (its stderr, where _console sends warnings
 * and errors). The engine printed `log` and dropped `diagnostics`, so a
 * warning the CLI shows — a misspelled letter key "is ignored" — never
 * reached the app's log. This suite checks that:
 *
 *   • a build that succeeds with a warning prints the warning;
 *   • a build that fails after a warning prints the warning, and does
 *     not print the failure's own lines a second time (they are the
 *     error's message and detail, which run() reports itself);
 *   • a silent worker (the speculative build) prints neither.
 *
 * They are printed to stderr, as the CLI prints them; the Studio's log
 * is a tee of stdout and stderr both (build/studio_server.js).
 *
 * Runs in a throwaway copy of the project (tests/_project.js): the
 * worker builds the letter into the copy's dist/. Needs no Chromium.
 */

const path = require('path');
const fs = require('fs');
const { assertEq, assertTrue, fail, report } = require('./_framework');
const { tempProject, realDistFingerprint } = require('./_project');

/** Run `fn` with stdout and stderr captured; resolves to { err, out, said }. */
async function capture(fn) {
  const originals = [process.stdout.write, process.stderr.write];
  let out = '';
  let said = '';
  process.stdout.write = function (chunk) {
    out += String(chunk);
    return true;
  };
  process.stderr.write = function (chunk) {
    said += String(chunk);
    return true;
  };
  let err = null;
  try {
    await fn();
  } catch (e) {
    err = e;
  } finally {
    [process.stdout.write, process.stderr.write] = originals;
  }
  return { err, out, said };
}

/** How many of `text`'s lines read `line`, its leading mark and indent aside. */
function linesReading(text, line) {
  return text.split('\n')
    .filter(l => l.trim().replace(/^(?:❌|⚠️|✅)\s*/u, '') === line.trim()).length;
}

(async () => {
  const before = realDistFingerprint();
  const project = tempProject('worker-log-test');
  let worker = null;
  try {
    const { PythonWorker } = project.require('build/engine');
    const { detectPython } = project.require('build/detect_python');
    worker = new PythonWorker({ root: project.root, python: detectPython() });
    await worker.start();
    // build.py refuses to run without a stylesheet; compiling one is
    // the pipeline's job (phase 1), done once here with the CLI's one-shot
    // compiler, so no Sass process outlives the suite.
    const { createPipeline } = project.require('build/pipeline');
    await capture(() => createPipeline({ root: project.root, python: worker }).compileSass());

    const base = fs.readFileSync(path.join(project.root, 'data', 'letter_default.yml'), 'utf-8');
    if (!base.includes('  recipient: |')) throw new Error('fixture: no recipient block in letter_default.yml');
    // A misspelled key the letter does not read: a warning, not an error.
    const warned = base.replace('  recipient: |', '  recipent: Senatus\n  recipient: |');
    const file = path.join(project.root, 'data', 'warned_letter.yml');
    fs.writeFileSync(file, warned);

    let { err, out, said } = await capture(() =>
      worker.withEnv({ LETTER_DATA_FILE: file }, () => worker.buildLetter()));
    assertEq(err && err.message, null, 'W1: a letter with an unknown key still builds');
    assertTrue(/recipent/.test(said), `W1: ...and the warning about it is printed to stderr (${JSON.stringify(said.slice(-300))})`);
    assertTrue(!/recipent/.test(out), 'W1: ...and not to stdout, with the build log');

    // Silently: nothing at all.
    ({ err, out, said } = await capture(() => worker.silently(() =>
      worker.withEnv({ LETTER_DATA_FILE: file }, () => worker.buildLetter()))));
    assertEq(out + said, '', 'W1: a silent worker prints neither its log nor its warnings');

    // A warning ahead of an error that stops the build: a final résumé
    // build with no placement to build against (nothing has solved one
    // in this copy), from data with an unknown top-level key. The letter
    // cannot show this: its validation errors come before its warnings.
    const resume = path.join(project.root, 'data', 'warned_resume.yml');
    fs.writeFileSync(resume, 'colour: red\n'
      + fs.readFileSync(path.join(project.root, 'data', 'resume_default.yml'), 'utf-8'));
    ({ err, out, said } = await capture(() =>
      worker.withEnv({ RESUME_DATA_FILE: resume }, () => worker.buildHtml('final'))));
    assertTrue(Boolean(err), `W2: a final build with no placement fails (${err && err.message})`);
    const headline = err ? err.message.split('\n')[0] : '';
    assertTrue(/placement\.json/.test(headline), `W2: ...saying why (${headline})`);
    assertTrue(/colour/.test(said), `W2: the warning before the failure is printed (${JSON.stringify(said.slice(-300))})`);
    // run() reports the failure itself, on stderr; the copy of it in the
    // worker's diagnostics must not be printed there as well.
    const detail = err ? err.message.split('\n').slice(1).filter(l => l.trim()) : [];
    const times = line => detail.filter(d => d.trim() === line.trim()).length;
    assertTrue(Boolean(headline) && linesReading(said, headline) === 1
      && detail.every(line => linesReading(said, line) === times(line)),
      `W2: ...and the failure itself (headline and detail) is printed once (${JSON.stringify(said.slice(-600))})`);
    assertTrue(!out.includes(headline), 'W2: ...and not to stdout');
  } catch (err) {
    fail('worker log run', { error: err.stack || err.message });
  } finally {
    if (worker) { try { await worker.stop(); } catch { /* gone */ } }
    project.remove();
  }
  assertEq(realDistFingerprint(), before, "isolation: nothing was written to this checkout's dist/");
  report();
})();
