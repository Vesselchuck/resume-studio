/**
 * test_cli_navigation.js — how the CLI and the engine wait for a page.
 *
 * WHAT THIS GUARDS
 * ----------------
 * resume.js and letter.js used to wait for 'networkidle' — 500 ms of
 * network silence — after each of their navigations, on a document
 * whose every asset is a local file. They now use the engine's wait:
 * `load`, then `document.fonts.ready`. tests/test_engine_equivalence.js
 * proves the two waits give the same pixels; this suite pins that the
 * CLI really uses the fast one, and that openDocument does what each
 * option says, without launching a browser.
 */

const fs = require('fs');
const path = require('path');
const { assertEq, assertTrue, fail, report } = require('./_framework');

const ROOT = path.join(__dirname, '..');
const { createPipeline } = require(path.join(ROOT, 'build', 'pipeline'));


/** Records what openDocument asks of a page, and does nothing. */
function fakePage() {
  const calls = [];
  let url = 'about:blank';
  return {
    calls,
    url: () => url,
    goto: async (target, options) => { calls.push(['goto', target, options]); url = target; },
    evaluate: async (fn, arg) => {
      calls.push(['evaluate', String(fn).includes('document.fonts.ready') ? 'fonts.ready' : 'other', arg]);
      return 'ok';
    },
  };
}


(async () => {
  try {
    // The default is the fast wait.
    for (const variant of ['resume', 'letter']) {
      const pipeline = createPipeline({ root: ROOT, python: {}, variant });
      const page = fakePage();
      assertEq(await pipeline.openDocument(page), 'goto', `${variant}: the first load navigates`);
      assertEq(page.calls[0][2], { waitUntil: 'load' }, `${variant}: navigation waits for load, not networkidle`);
      assertEq(page.calls[1] && page.calls[1][1], 'fonts.ready', `${variant}: then for document.fonts.ready`);
      assertEq(page.calls.length, 2, `${variant}: and nothing else`);
    }

    // 'networkidle' is still there to compare against.
    const slow = createPipeline({ root: ROOT, python: {}, navWait: 'networkidle' });
    const page = fakePage();
    await slow.openDocument(page);
    assertEq(page.calls[0][2], { waitUntil: 'networkidle' }, "networkidle: waits for network silence");

    // Only the two waits exist.
    let threw = false;
    try { createPipeline({ root: ROOT, python: {}, navWait: 'nope' }); } catch { threw = true; }
    assertTrue(threw, 'an unknown navWait is refused');

    // In-place loads are the engine's, and only for the page's own file.
    const pipeline = createPipeline({ root: ROOT, python: {} });
    const stranger = fakePage();
    assertEq(await pipeline.openDocument(stranger, { inPlace: true }), 'goto',
      'inPlace: a page this pipeline never navigated is navigated');
    const loads = [];
    assertEq(await pipeline.openDocument(stranger, { inPlace: false, onLoad: how => loads.push(how) }),
      'goto', 'inPlace: false always navigates');
    assertEq(loads, ['goto'], 'onLoad hears how the page was loaded');

    // The CLIs ask for it by name, and never for networkidle.
    for (const script of ['resume.js', 'letter.js']) {
      const src = fs.readFileSync(path.join(ROOT, script), 'utf-8');
      assertTrue(!/(navWait|waitUntil)\s*:\s*['"]networkidle/.test(src),
        `${script}: does not wait for networkidle`);
      assertTrue(/createPipeline\(\{[^}]*navWait: 'fonts'/.test(src),
        `${script}: creates its pipeline with navWait 'fonts'`);
      assertTrue(!/inPlace/.test(src), `${script}: never loads a document in place`);
    }
  } catch (err) {
    fail('cli navigation run', { error: err.stack || err.message });
  }
  report();
})();
