/**
 * test_pipeline_errors.js — how the pipeline's phases fail.
 *
 * WHAT THIS GUARDS
 * ----------------
 *   • printPdfs removes its temp file when the print itself fails part-
 *     way. The print used to run outside the try whose finally removes
 *     it, so a failed print left a partial PDF in the temp directory.
 *   • A letter that does not fit is a layout verdict (`kind: 'layout'`),
 *     which is what lets the engine's preview show it as "Layout check
 *     failed" while failing the render on anything else.
 *
 * Runs against stub pages and a stub Python adapter — no browser.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { assertEq, assertTrue, fail, report } = require('./_framework');

const ROOT = path.resolve(__dirname, '..');
const { createPipeline } = require(path.join(ROOT, 'build', 'pipeline'));

/** Run `fn` with stdout and stderr silenced; resolves to the error it threw, or null. */
async function quietly(fn) {
  const out = process.stdout.write;
  const err = process.stderr.write;
  process.stdout.write = () => true;
  process.stderr.write = () => true;
  try {
    await fn();
    return null;
  } catch (e) {
    return e;
  } finally {
    process.stdout.write = out;
    process.stderr.write = err;
  }
}

const leftovers = () => fs.readdirSync(os.tmpdir())
  .filter(name => name.startsWith(`resume-print-${process.pid}-`));

(async () => {
  try {
    // A print that writes some of its file and then fails.
    let cropped = false;
    const pipeline = createPipeline({
      root: ROOT, python: { cropPdf: async () => { cropped = true; } },
    });
    const page = {
      pdf: async ({ path: file }) => {
        fs.writeFileSync(file, '%PDF-1.7\n(partial)');
        throw new Error('the print failed part-way');
      },
    };
    const before = leftovers();
    const err = await quietly(() => pipeline.printPdfs(page,
      { output: path.join(os.tmpdir(), `never-written-${process.pid}.pdf`), quiet: true }));
    assertTrue(Boolean(err) && /part-way/.test(err.message), 'P1: a failed print fails printPdfs');
    assertEq(cropped, false, 'P1: ...without cropping anything');
    assertEq(leftovers().filter(f => !before.includes(f)), [],
      'P1: ...and its partial temp file is removed');

    // A letter that runs 40px past its sheet.
    const letter = createPipeline({ root: ROOT, python: {}, variant: 'letter' });
    const long = { evaluate: async () => ({ overflowPx: 40, lineHeight: 16 }) };
    const over = await quietly(() => letter.verifyLetterFits(long));
    assertTrue(Boolean(over), 'L2: a letter that does not fit fails the check');
    assertEq(over && over.kind, 'layout', 'L2: ...as a layout verdict');
    assertEq(over && over.alreadyReported, true, 'L2: ...already reported');
    const short = { evaluate: async () => ({ overflowPx: -120, lineHeight: 16 }) };
    assertEq(await quietly(() => letter.verifyLetterFits(short)), null, 'L2: a letter that fits passes');

    // Nothing to measure is not a verdict on the letter: the check could not run.
    const empty = { evaluate: async () => null };
    const none = await quietly(() => letter.verifyLetterFits(empty));
    assertTrue(Boolean(none) && none.kind !== 'layout',
      'L2: a letter with nothing to measure fails, but not as a layout verdict');
  } catch (err) {
    fail('pipeline errors run', { error: err.stack || err.message });
  }
  report();
})();
