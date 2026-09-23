/**
 * test_preview_stream.js — pages delivered one at a time must be the
 * pages delivered all at once.
 *
 * WHAT THIS GUARDS
 * ----------------
 * A render used to reach its caller in one reply, so page 1 — the page
 * being looked at — waited for every page behind it to be rendered and
 * encoded. A caller can now pass `onPage` and an `order`, and each page
 * arrives as build/worker.py finishes it, most wanted first.
 *
 * Two things must stay true, and both are easy to break:
 *
 *   • The pixels. Rendering page by page uses the same rasterizer with
 *     the same crop at the same scale, and the images must hash exactly
 *     as the one-reply path's do — including the page-key fast path,
 *     which must still skip a page whose PDF-level key did not change.
 *   • The old shape. A caller that does not ask for streaming must get
 *     exactly what it got before: one reply, every page, every PNG.
 *     tests/test_engine_equivalence.js and the Build path depend on it.
 *
 * The streamed reply itself leaves out the PNGs it has already
 * delivered (marked `sent`), so this also checks that a caller which
 * puts the two halves back together ends up with the whole render.
 *
 * The rule for dropping pages from a superseded render is the UI's
 * (applyStreamedPage in ui/index.html). What can be checked here is the
 * property it rests on — that a page carries the render it belongs to —
 * and that the rule, applied to an interleaving where a late page of an
 * abandoned render arrives after a newer one has started, keeps the
 * newer render's pages and drops the older ones.
 *
 * REQUIREMENTS
 * ------------
 * Playwright's Chromium; without it this prints the runner's SKIP
 * marker and exits 0, as test_engine_equivalence.js does.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { assertEq, assertTrue, fail, report } = require('./_framework');

const ROOT = path.join(__dirname, '..');
const DIST = path.join(ROOT, 'dist');
const SUITE = 'test_preview_stream';

const SCRATCH = [
  path.join(DIST, 'styles.css'),
  path.join(DIST, 'index.html'),
  path.join(DIST, 'placement.json'),
  path.join(DIST, 'pdf_meta.json'),
  path.join(DIST, 'letter.html'),
  path.join(DIST, 'letter_meta.json'),
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

function edit(file, from, to) {
  const text = fs.readFileSync(file, 'utf-8');
  if (!text.includes(from)) throw new Error(`fixture: "${from}" not in ${file}`);
  fs.writeFileSync(file, text.replace(from, to));
}

/**
 * The UI's rule for a streamed page, as ui/index.html applies it: a
 * page whose render id is not the one being waited for is dropped.
 * Mirrored here so an interleaving can be replayed against it.
 */
function applyIfCurrent(held, current, event) {
  if (event.renderId !== current) return held;
  held[event.image.page] = event.image.hash;
  return held;
}


(async () => {
  try {
    const { chromium } = require('playwright');
    const b = await chromium.launch();
    await b.close();
  } catch (err) {
    return skip(`chromium unavailable (${String(err.message).split('\n')[0]})`);
  }

  let createEngine;
  try {
    ({ createEngine } = require('../build/engine'));
  } catch (err) {
    fail('the engine module loads', { error: err.stack || err.message });
    return report();
  }

  let engine;
  try {
    engine = await createEngine({ root: ROOT });
  } catch (err) {
    fail('the engine starts (Python worker boots)', { error: err.stack || err.message });
    return report();
  }

  const saved = snapshot(SCRATCH);
  const dataFile = path.join(os.tmpdir(), `stream-${process.pid}.yml`);
  fs.copyFileSync(path.join(ROOT, 'data', 'resume_default.yml'), dataFile);
  const env = { RESUME_DATA_FILE: dataFile, RESUME_DATA_SOURCE: null };

  /** One render, optionally streamed; returns what arrived and how. */
  const render = async ({ order = null, known = null, stream = true, doc = 'resume' } = {}) => {
    const arrived = [];
    const r = await engine.renderPreview({
      doc, env, known: known || undefined,
      order: stream ? order : undefined,
      onPage: stream ? (im) => arrived.push({ page: im.page, hash: im.hash, png: Boolean(im.png) }) : undefined,
    });
    return { r, arrived };
  };

  try {
    // A first render of this data, so the later ones have a previous
    // render to skip pages against.
    const first = await render({ order: null });
    assertTrue(first.r.images.length >= 2, 'the fixture resume has at least two pages');
    const pages = first.r.images.map(im => im.page);
    assertEq(first.arrived.map(a => a.page), pages,
      'stream: with no order given, the pages arrive in page order');
    assertEq(first.arrived.map(a => a.hash), first.r.images.map(im => im.hash),
      'stream: the pages that arrived are the pages in the result');
    assertTrue(first.arrived.every(a => a.png), 'stream: each arrived with its PNG');
    assertEq(first.r.timings.streamedPages, pages.length,
      'stream: every page of the render was streamed');

    // The result is still whole: every page, every PNG, in page order.
    assertTrue(first.r.images.every(im => im.png && !im.unchanged && !im.sent),
      'stream: the result still carries every page image');

    // Order: the pages on screen first. Asked for back to front, the
    // pages must arrive back to front.
    edit(dataFile, 'Phasellus scelerisque magna', 'Phasellus scelerisque magnum');
    const reversed = await render({ order: [...pages].reverse() });
    assertEq(reversed.arrived.map(a => a.page), [...pages].reverse(),
      'stream: the pages arrive in the order the caller asked for');
    assertEq(reversed.r.images.map(im => im.page), pages,
      'stream: the result is still in page order');

    // Naming only the page on screen puts it first and the rest after.
    edit(dataFile, 'Phasellus scelerisque magnum', 'Phasellus scelerisque magnificum');
    const lastFirst = await render({ order: [pages[pages.length - 1]] });
    assertEq(lastFirst.arrived.map(a => a.page),
      [pages[pages.length - 1], ...pages.slice(0, -1)],
      'stream: a partial order is honored, the rest follow in page order');

    // An order naming pages that do not exist is not a way to lose one.
    edit(dataFile, 'Phasellus scelerisque magnificum', 'Phasellus scelerisque magna');
    const nonsense = await render({ order: [99, 0, -1, pages[0], pages[0]] });
    assertEq([...nonsense.arrived.map(a => a.page)].sort(), [...pages].sort(),
      'stream: an order full of nonsense still delivers every page once');

    // The same pixels as the one-reply path, on the same data.
    const streamed = await render({ order: [...pages].reverse() });
    const plain = await render({ stream: false });
    assertEq(plain.arrived.length, 0, 'no onPage: nothing is streamed');
    assertEq(plain.r.timings.streamedPages, undefined,
      'no onPage: the worker is not asked to stream');
    assertTrue(plain.r.images.every(im => im.png && !im.sent),
      'no onPage: one reply, with every PNG in it');
    assertEq(streamed.r.images.map(im => im.hash), plain.r.images.map(im => im.hash),
      'stream: the pages are pixel for pixel the one-reply path\'s');

    // The unchanged-page fast path still works while streaming: an edit
    // that only touches page 1 must not re-render page 2.
    edit(dataFile, 'Phasellus scelerisque magna', 'Phasellus scelerisque magnissima');
    const onePage = await render({ order: null });
    assertTrue(onePage.r.timings.renderedPages < pages.length,
      `stream: an edit on one page leaves the others unrendered `
      + `(${onePage.r.timings.renderedPages} of ${pages.length})`);
    assertEq(onePage.arrived.map(a => a.page), pages,
      'stream: the unrendered pages are still delivered');
    assertTrue(onePage.r.images.every(im => im.png),
      'stream: ...each with its image, reused or fresh');

    // A caller that holds the pages: the unchanged ones come back as
    // {hash, unchanged} with no PNG, streamed or not.
    const known = Object.fromEntries(onePage.r.images.map(im => [im.page, im.hash]));
    edit(dataFile, 'Sed finibus accumsan', 'Sed finibus accumsam');
    const withKnown = await render({ order: null, known });
    const unchanged = withKnown.r.images.filter(im => im.unchanged);
    assertTrue(unchanged.length > 0 && unchanged.every(im => !im.png),
      'stream: a page the caller already holds comes back without its PNG');
    assertEq(withKnown.arrived.filter(a => !a.png).length, unchanged.length,
      'stream: ...and is streamed the same way');

    // The letter is one page, and streaming must not have invented a
    // second one or lost the only one.
    const letter = await render({ doc: 'letter', order: [1] });
    assertEq(letter.arrived.map(a => a.page), [1], 'stream: the letter streams its one page');
    assertTrue(letter.r.images.length === 1 && letter.r.images[0].png,
      'stream: the letter result carries that page');

    // A render answered from the early cutoff streams nothing and
    // returns everything — the caller must handle both.
    const cached = await render({ doc: 'letter', order: [1] });
    assertEq(cached.r.timings.cached, 'render', 'stream: an unchanged render is still not redone');
    assertEq(cached.arrived.length, 0, 'stream: ...and streams no pages');
    assertTrue(cached.r.images.every(im => im.png),
      'stream: ...but the reply carries every page');

    // Superseded renders: each page names the render it belongs to, and
    // the UI's rule keeps the current one's pages and drops the rest.
    const older = 'render-1';
    const newer = 'render-2';
    const events = [
      { renderId: older, image: { page: 1, hash: 'old-1' } },
      { renderId: newer, image: { page: 1, hash: 'new-1' } },
      { renderId: older, image: { page: 2, hash: 'old-2' } },   // late, from the abandoned render
      { renderId: newer, image: { page: 2, hash: 'new-2' } },
    ];
    let held = {};
    for (const ev of events) held = applyIfCurrent(held, newer, ev);
    assertEq(held, { 1: 'new-1', 2: 'new-2' },
      'stream: a late page from a superseded render is ignored');
  } catch (err) {
    fail('preview streaming run', { error: err.stack || err.message });
  } finally {
    try { fs.rmSync(dataFile, { force: true }); } catch { /* best effort */ }
    restore(saved);
    await engine.dispose();
  }

  report();
})();
