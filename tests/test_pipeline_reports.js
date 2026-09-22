/**
 * test_pipeline_reports.js — an error marked reported() was printed.
 *
 * reported() tells the drivers (resume.js, letter.js, engine.js) "this
 * phase already said what went wrong", so they skip their own
 * "Unexpected error" block. A phase that marks an error reported
 * without printing anything makes the build exit 1 in silence.
 * verifyLetterFits did exactly that when the letter HTML had no
 * .page / .letter to measure.
 *
 * Runs against a stub page — no browser needed.
 */

const path = require('path');
const { assertTrue, fail, report } = require('./_framework');

const ROOT = path.resolve(__dirname, '..');
const { createPipeline } = require(path.join(ROOT, 'build', 'pipeline'));

/** Run `fn` with stderr captured; resolves to { err, stderr }. */
async function captureStderr(fn) {
  const original = process.stderr.write.bind(process.stderr);
  let stderr = '';
  process.stderr.write = (chunk, ...rest) => {
    stderr += String(chunk);
    return true;
  };
  let err = null;
  try {
    await fn();
  } catch (e) {
    err = e;
  } finally {
    process.stderr.write = original;
  }
  return { err, stderr };
}

(async () => {
  const pipeline = createPipeline({ root: ROOT, python: {}, variant: 'letter' });

  // page.evaluate returning null is what the in-page probe returns when
  // .page, .letter or the letter's last child is missing.
  const page = { evaluate: async () => null };
  const { err, stderr } = await captureStderr(() => pipeline.verifyLetterFits(page));

  if (!err) {
    fail('verifyLetterFits throws when there is nothing to measure',
      { error: 'resolved instead of throwing' });
  } else {
    assertTrue(err.alreadyReported === true, 'the error is marked reported');
    assertTrue(stderr.includes('Could not measure the cover letter'),
      'and something was actually printed before it was thrown');
    assertTrue(stderr.includes('letter.html'), 'naming the file it tried to measure');
  }

  report();
})();
