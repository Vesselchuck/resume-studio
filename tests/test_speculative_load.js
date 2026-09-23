/**
 * test_speculative_load.js — guessing the final HTML must not change
 * what gets printed.
 *
 * WHAT THIS GUARDS
 * ----------------
 * While the measurement pass is being measured, the engine builds the
 * final HTML against the placement already on disk and loads it into a
 * second page (see "Speculative final load" in build/engine.js). When
 * the solver's answer turns out to be the same placement, that page is
 * printed from instead of loading the final HTML again.
 *
 * The hazard is obvious and total: print a page whose HTML is not the
 * final HTML and the preview is a lie. So the guess is checked against
 * the solved placement and against the final HTML's bytes before it is
 * used, and this suite drives the engine through a sequence of real
 * edits — ones that leave the placement alone, one that changes it, one
 * that invalidates it outright — and asserts:
 *
 *   • the guess is used when the placement is unchanged;
 *   • it is not used when the placement changes (adding a job) or when
 *     the old placement cannot build at all (removing one), and the
 *     render succeeds anyway;
 *   • every page of every render is pixel-identical to the same
 *     sequence run with speculation off;
 *   • dist/index.html after each render is byte-identical to what the
 *     non-speculative run wrote, because that file is the deliverable
 *     the CLI and tests/test_engine_equivalence.js compare;
 *   • the layout invariants still ran, on the page that was printed.
 *
 * Two engines are used, one with speculation on and one with it off,
 * because the comparison must be between the two paths and nothing
 * else. The data is a temporary copy of data/resume_default.yml named
 * through RESUME_DATA_FILE, so nothing in data/ is touched.
 *
 * REQUIREMENTS
 * ------------
 * Playwright's Chromium; without it this prints the runner's SKIP
 * marker and exits 0, as test_engine_equivalence.js does.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const { assertEq, assertTrue, fail, report } = require('./_framework');

const ROOT = path.join(__dirname, '..');
const DIST = path.join(ROOT, 'dist');
const SUITE = 'test_speculative_load';

const SCRATCH = [
  path.join(DIST, 'styles.css'),
  path.join(DIST, 'index.html'),
  path.join(DIST, 'placement.json'),
  path.join(DIST, 'pdf_meta.json'),
];

function skip(reason) {
  console.log(`SKIP ${SUITE}: ${reason}`);
  process.exitCode = 0;
}

function snapshot(paths) {
  return paths.map(p => [p, fs.existsSync(p) ? fs.readFileSync(p) : null]);
}

function restore(saved) {
  for (const [p, data] of saved) {
    try {
      if (data === null) fs.rmSync(p, { force: true });
      else fs.writeFileSync(p, data);
    } catch { /* cleanup, not an assertion */ }
  }
}

function sha(file) {
  try {
    return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex').slice(0, 16);
  } catch {
    return 'absent';
  }
}

/** Replace `from` with `to` in a file, insisting that it was there. */
function edit(file, from, to) {
  const text = fs.readFileSync(file, 'utf-8');
  if (!text.includes(from)) throw new Error(`fixture: "${from}" not in ${file}`);
  fs.writeFileSync(file, text.replace(from, to));
}

// A whole extra job. Enough content to move what sits on which page, so
// the solver's answer really does change and the guess really is wrong.
const NEW_JOB = `
      - id: speculative-test-job
        title: Munus Novum, Res Additae
        date: MMM YYYY – MMM YYYY
        datetime: "YYYY-MM"
        location: Roma, Imperium Romanum
        bullets:
          - "Integer posuere erat a ante venenatis dapibus posuere velit aliquet. Cras mattis consectetur purus sit amet fermentum nulla vitae."
          - "Maecenas faucibus mollis interdum. Morbi leo risus, porta ac consectetur ac, vestibulum at eros. Donec ullamcorper nulla non metus."
          - "Aenean lacinia bibendum nulla sed consectetur. Nullam quis risus eget urna mollis ornare vel eu leo. Cum sociis natoque."

      - id: nulla`;

/**
 * The edits, in order. Each one is applied and then rendered.
 *
 * `expect` is what the speculative engine should report for that
 * render: 'used' when the guess held, 'missed' when the placement came
 * out different, 'failed' when the old placement could not even build
 * against the new data, and null when speculation does not apply —
 * either because the measurement was answered from the memo, so there
 * was nothing to hide a guess behind, or because there is no placement
 * on disk to guess from at all.
 *
 * The first render is the second of those: runSequence clears
 * dist/placement.json before it, so there is nothing to guess with.
 * That is deliberate and load-bearing. This step used to expect 'used',
 * which made the suite's result depend on whatever happened to be in
 * dist/ when it started — 'used' after a build of this same data,
 * 'failed' after a build of yours, null on a clean checkout. The guess
 * itself is measured by the nine edits below, each of which starts from
 * a placement this sequence wrote.
 */
const STEPS = [
  ['the first render', null, null],
  ['a bullet on page 1', f => edit(f, 'Phasellus scelerisque magna', 'Phasellus scelerisque magnum'), 'used'],
  ['a bullet on page 2', f => edit(f, 'Sed finibus accumsan', 'Sed finibus accumsam'), 'used'],
  ['a new glyph', f => edit(f, 'Aliquam mattis', 'Aliquam Žmattis'), 'used'],
  ['a comment-only save', f => edit(f, '    jobs:\n', '    jobs:\n      # a comment only\n'), null],
  ['an identical save', f => fs.writeFileSync(f, fs.readFileSync(f)), null],
  ['a whole new job', f => edit(f, '\n      - id: nulla', NEW_JOB), 'missed'],
  ['an edit after it', f => edit(f, 'Integer posuere erat', 'Integer posuere ERAT'), 'used'],
  ['the job removed again',
    f => edit(f, NEW_JOB.replace('Integer posuere erat', 'Integer posuere ERAT'),
      '\n      - id: nulla'), 'failed'],
  ['back to where it started',
    f => edit(f, 'Phasellus scelerisque magnum', 'Phasellus scelerisque magna'), 'used'],
];

/** Run the whole sequence on one engine, collecting what it produced. */
async function runSequence(engine, dataFile, htmlPath) {
  const env = { RESUME_DATA_FILE: dataFile, RESUME_DATA_SOURCE: null };
  const out = [];
  fs.copyFileSync(path.join(ROOT, 'data', 'resume_default.yml'), dataFile);
  // Start from no placement, so the first render has nothing to guess
  // from no matter what ran before this suite — see STEPS. The file is
  // restored with the rest of SCRATCH when the suite finishes.
  fs.rmSync(path.join(DIST, 'placement.json'), { force: true });
  for (const [label, apply] of STEPS) {
    if (apply) apply(dataFile);
    const r = await engine.renderPreview({ doc: 'resume', env });
    out.push({
      label,
      speculative: r.timings.speculative || null,
      hashes: r.images.map(im => im.hash),
      pages: r.pages,
      invariants: r.invariants,
      html: sha(htmlPath),
      placement: sha(path.join(DIST, 'placement.json')),
      cached: r.timings.cached || null,
    });
  }
  return out;
}


(async () => {
  try {
    require('playwright');
  } catch (err) {
    return skip(`playwright not installed (${err.message.split('\n')[0]})`);
  }
  try {
    const { chromium } = require('playwright');
    const b = await chromium.launch();
    await b.close();
  } catch (err) {
    return skip(`chromium launch failed (${err.message.split('\n')[0]})`);
  }

  let createEngine;
  try {
    ({ createEngine } = require('../build/engine'));
  } catch (err) {
    fail('the engine module loads', { error: err.stack || err.message });
    return report();
  }

  const saved = snapshot(SCRATCH);
  const dataFile = path.join(os.tmpdir(), `speculative-${process.pid}.yml`);
  const htmlPath = path.join(DIST, 'index.html');
  let engine = null;

  try {
    // With speculation, then without: the same engine code, the same
    // data, the same order, one switch different.
    engine = await createEngine({ root: ROOT, speculative: true });
    const fast = await runSequence(engine, dataFile, htmlPath);
    await engine.dispose();

    engine = await createEngine({ root: ROOT, speculative: false });
    const plain = await runSequence(engine, dataFile, htmlPath);
    await engine.dispose();
    engine = null;

    assertEq(fast.length, STEPS.length, 'every edit was rendered');

    for (let i = 0; i < STEPS.length; i++) {
      const [label, , expect] = STEPS[i];
      const a = fast[i];
      const b = plain[i];

      assertEq(a.speculative, expect, `${label}: the guess was ${expect || 'not attempted'}`);
      assertEq(b.speculative, null, `${label}: speculation off means no guess`);
      assertEq(a.hashes, b.hashes, `${label}: the same pages, pixel for pixel`);
      assertEq(a.html, b.html, `${label}: dist/index.html is the same file`);
      assertEq(a.placement, b.placement, `${label}: dist/placement.json is the same file`);
      assertEq(a.pages, b.pages, `${label}: the same page count`);
      assertEq(a.invariants, { ok: true }, `${label}: the layout invariants passed`);
      assertEq(b.invariants, { ok: true }, `${label}: ...with speculation off too`);
      assertEq(a.cached, b.cached, `${label}: the early cutoff behaved the same way`);
    }

    // The sequence has to have exercised all three outcomes, or the
    // assertions above are only about the easy case.
    const seen = new Set(fast.map(r => r.speculative));
    assertTrue(seen.has('used'), 'the guess held for at least one edit');
    assertTrue(seen.has('missed'), 'a changed placement was caught and fell back');
    assertTrue(seen.has('failed'), 'a placement that could not build fell back');
    assertTrue(fast.filter(r => r.speculative === 'used').length >= 4,
      'the guess held for most edits (that is the whole point of it)');

    // A render that fell back is still a complete render: same pages as
    // the run that never guessed, which the per-step checks above have
    // already asserted — and the returned page count is honest.
    assertTrue(fast.every(r => r.hashes.length === r.pages),
      'every render returned one image per page');
  } catch (err) {
    fail('speculative load run', { error: err.stack || err.message });
  } finally {
    if (engine) { try { await engine.dispose(); } catch { /* best effort */ } }
    try { fs.rmSync(dataFile, { force: true }); } catch { /* best effort */ }
    restore(saved);
  }

  report();
})();
