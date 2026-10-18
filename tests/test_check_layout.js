/**
 * test_check_layout.js — Tests for build/check_layout.js.
 *
 * checkLayoutInvariants() runs assertions inside a real browser via
 * Playwright (page.evaluate), so the only meaningful test launches
 * Chromium and feeds it small synthetic HTML fixtures designed to
 * exercise each invariant. A pure-Node mock of the page object would
 * lock in the API shape but couldn't verify the actual logic — which
 * is the part most likely to break.
 *
 * If Chromium isn't available (e.g. fresh checkout without
 * `npx playwright install`), the suite skips with a clear message
 * rather than failing. Solver tests still run independently.
 */

const path = require('path');
const { checkLayoutInvariants } = require(
  path.resolve(__dirname, '..', 'build', 'check_layout'),
);
const { assertEq, assertTrue, fail, report } = require('./_framework');


// ─── Fixture helpers ─────────────────────────────────────────────

/**
 * Build a synthetic resume HTML page that mimics the production
 * structure closely enough for invariant testing.
 *
 * Production rules we have to honor:
 *   • .page is 8.5×11in with a configurable padding.
 *   • .body-grid is a flex child that fills the remaining vertical
 *     space below the page header (so its bottom == page bottom −
 *     padding-bottom).
 *   • Section separators are <hr class="section-sep"> with margins
 *     compensated to produce a uniform visible gap. We use a single
 *     CSS variable here to make rhythm-violation tests easy.
 *
 * Options:
 *   pageCount: how many .page elements (default 2).
 *   sidebarSepGap: visible inter-section gap inside sidebar.
 *   mainColSepGap: visible inter-section gap inside main col.
 *   omitGrid: array of page indices (1-based) where .body-grid is
 *     stripped — for testing 'grid-presence'.
 *   shrinkGridBy: pixels to subtract from .body-grid height on every
 *     page — for testing 'grid-bottom'.
 *   overflowMainColPx: pixels to extend a main-col descendant past
 *     the page-content bottom — for testing 'content-overflow'.
 *   sidebarHrCount, mainColHrCount: number of separators per column.
 */
function buildFixture(opts = {}) {
  const {
    pageCount = 2,
    sidebarSepGap = 30,
    mainColSepGap = 30,
    omitGrid = [],
    shrinkGridBy = 0,
    overflowMainColPx = 0,
    sidebarHrCount = 2,
    mainColHrCount = 1,
  } = opts;

  // Tiny CSS that mimics the production layout shape. Padding is
  // chosen so the math is round; specific numbers don't matter.
  const css = `
    :root {
      --page-w: 8.5in;
      --page-h: 11in;
      --page-pad: 36px;
      --sidebar-sep-margin: ${(sidebarSepGap - 1) / 2}px;
      --maincol-sep-margin: ${(mainColSepGap - 1) / 2}px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #ccc; }
    .page {
      width: var(--page-w);
      height: var(--page-h);
      padding: var(--page-pad);
      background: white;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      position: relative;
    }
    .body-grid {
      flex: 1 1 auto;
      display: grid;
      grid-template-columns: 1fr 2fr;
      gap: 16px;
    }
    .sidebar, .main-col { display: flex; flex-direction: column; gap: 0; }
    .block { padding: 8px 0; background: #eee; }
    hr.section-sep { border: 0; height: 1px; background: #888; }
    .sidebar > hr.section-sep { margin: var(--sidebar-sep-margin) 0; }
    .main-col > hr.section-sep { margin: var(--maincol-sep-margin) 0; }
    .shrink-grid { height: calc(100% - ${shrinkGridBy}px); flex: 0 0 auto; }
    .overflow-spacer { height: ${overflowMainColPx}px; }
  `;

  function buildColumn(cls, hrCount) {
    // Render: block, hr, block, hr, block, ... (alternating). The blocks
    // are <div>s, not <section>s: this fixture models geometry only, and
    // content conservation (invariant 5) reads sections, headings and
    // ids that it does not have. contentFixture below models those.
    const blocks = [];
    for (let i = 0; i <= hrCount; i++) {
      blocks.push(`<div class="block" data-block-${i}>block ${cls}-${i}</div>`);
    }
    const parts = [];
    for (let i = 0; i < blocks.length; i++) {
      parts.push(blocks[i]);
      if (i < hrCount) parts.push('<hr class="section-sep">');
    }
    if (cls === 'main-col' && overflowMainColPx > 0) {
      // A child that pushes the column past the page content bottom.
      parts.push('<div class="overflow-spacer">overflow</div>');
    }
    return `<div class="${cls}">${parts.join('')}</div>`;
  }

  function buildPage(idx) {
    const skipGrid = omitGrid.includes(idx);
    const gridContent = skipGrid
      ? ''
      : `<div class="body-grid${shrinkGridBy > 0 ? ' shrink-grid' : ''}">
          ${buildColumn('sidebar', sidebarHrCount)}
          ${buildColumn('main-col', mainColHrCount)}
        </div>`;
    return `<article class="page" data-page="${idx}">
      <header><h1>Page ${idx}</h1></header>
      ${gridContent}
    </article>`;
  }

  const pages = [];
  for (let i = 1; i <= pageCount; i++) pages.push(buildPage(i));

  return `<!doctype html><html><head>
    <meta charset="utf-8"><title>fixture</title><style>${css}</style>
  </head><body>${pages.join('')}</body></html>`;
}


/**
 * Alternative fixture that places .body-grid via absolute positioning.
 * Lets us put the grid's bottom edge at a *known* pixel offset from
 * the page bottom — useful for testing the grid-bottom tolerance
 * threshold deterministically without fighting flex/grid layout.
 *
 * Page is 8.5×11 in (816×1056 px) with 36 px padding all sides.
 * `gridBottomOffsetPx` is the distance from the page bottom to the
 * grid's bottom edge: 36 = grid bottom exactly at contentBottom (no
 * violation), 41 = grid 5 px short (violation), 37 = 1 px short
 * (within tolerance).
 */
function absolutePositionedFixture(opts = {}) {
  const { gridBottomOffsetPx = 36, pageCount = 2 } = opts;
  const css = `
    * { box-sizing: border-box; margin: 0; padding: 0; }
    .page {
      width: 8.5in; height: 11in; padding: 36px;
      background: white; overflow: hidden; position: relative;
    }
    .body-grid {
      position: absolute;
      left: 36px; right: 36px; top: 100px;
      bottom: ${gridBottomOffsetPx}px;
      display: grid; grid-template-columns: 1fr 2fr; gap: 16px;
    }
    .sidebar, .main-col { display: flex; flex-direction: column; }
  `;
  function buildPage(idx) {
    return `<article class="page" data-page="${idx}">
      <header><h1>Page ${idx}</h1></header>
      <div class="body-grid">
        <div class="sidebar"></div>
        <div class="main-col"></div>
      </div>
    </article>`;
  }
  const pages = [];
  for (let i = 1; i <= pageCount; i++) pages.push(buildPage(i));
  return `<!doctype html><html><head>
    <meta charset="utf-8"><title>fixture</title><style>${css}</style>
  </head><body>${pages.join('')}</body></html>`;
}


/**
 * A two-page document in the final templates' shape, for invariants 5
 * and 6: a sidebar list begun on page 1 and continued on page 2, and an
 * experience section whose one job is split the same way.
 *
 *   dropExperienceHeading: page 1's experience section has no heading —
 *     what the solver produced when it charged the heading to page 1 and
 *     sent the first job to page 2 as a continuation.
 *   longWord: text for the first sidebar item, wrapped nowhere.
 *   contactRow: text for the page-1 header's last contact row, laid out
 *     as the templates' header is (_components.scss): name left, the
 *     contact block right-aligned, a top rule below, and a page footer
 *     at the right margin. Null (the default) draws no header or footer.
 *   noEducationItems: the education section has its heading and no
 *     items, as `items: []` renders.
 *
 * SOURCE is the matching solver input (what measure_dom.js returns),
 * cut down to what the check reads.
 */
function contentFixture(opts = {}) {
  const {
    dropExperienceHeading = false, longWord = 'item one', contactRow = null,
    noEducationItems = false,
  } = opts;
  const css = `
    * { box-sizing: border-box; margin: 0; padding: 0; }
    .page {
      width: 8.5in; height: 11in; padding: 36px; position: relative;
      display: flex; flex-direction: column; overflow: hidden;
    }
    .resume-header { display: flex; justify-content: space-between; align-items: flex-end; }
    .header-contact { display: flex; flex-direction: column; align-items: flex-end; text-align: right; }
    .top-rule { border: 0; border-top: 1px solid #888; margin-bottom: 12px; }
    .page-footer { position: absolute; bottom: 12px; right: 36px; }
    .body-grid {
      flex: 1 1 auto; display: grid; gap: 32px;
      grid-template-columns: 200px minmax(0, 1fr);
    }
    .sidebar, .main-col { display: flex; flex-direction: column; min-width: 0; }
    hr.section-sep { border: 0; border-top: 1px solid #888; margin: 12px 0; }
  `;
  const heading = dropExperienceHeading ? '' : '<h2 class="section-heading"><span>Work</span></h2>';
  const header = contactRow === null ? '' : `
      <header class="resume-header">
        <div class="header-left"><h1 class="name"><span class="name-text">A Name</span></h1></div>
        <address class="header-contact">
          <span class="header-contact-item">(000) 000-0000</span>
          <span class="header-contact-item">${contactRow}</span>
        </address>
      </header>
      <hr class="top-rule">`;
  const footer = (n) => (contactRow === null ? ''
    : `<footer class="page-footer">Page ${n} of 2</footer>`);
  const educationItems = noEducationItems ? '' : `
          <article class="edu-item"><header class="entry-header">
            <h3 class="entry-title">A degree</h3></header></article>`;
  return `<!doctype html><html><head>
    <meta charset="utf-8"><title>fixture</title><style>${css}</style>
  </head><body>
    <article class="page">${header}<div class="body-grid">
      <aside class="sidebar">
        <section class="block" id="langs">
          <h2 class="section-heading"><span>Languages</span></h2>
          <ul class="plain-list"><li>${longWord}</li><li>item two</li></ul>
        </section>
      </aside>
      <div class="main-col">
        <section class="block">
          <h2 class="section-heading"><span>Summary</span></h2>
          <p class="summary-text">A summary.</p>
        </section>
        <hr class="section-sep">
        <section class="block experience">${heading}
          <article class="job" id="a">
            <header class="entry-header"><h3 class="entry-title">Job A</h3></header>
            <ul class="bullets"><li>one</li><li>two</li></ul>
          </article>
        </section>
      </div>
    </div>${footer(1)}</article>
    <article class="page"><div class="body-grid">
      <aside class="sidebar">
        <section class="block block--continued" data-continues-from="langs">
          <ul class="plain-list"><li>item three</li></ul>
        </section>
      </aside>
      <div class="main-col">
        <section class="block experience" aria-label="Work experience continued">
          <article class="job job--continued" data-continues-from="a">
            <ul class="bullets"><li>three</li></ul>
          </article>
        </section>
        <hr class="section-sep">
        <section class="block">
          <h2 class="section-heading"><span>Education</span></h2>${educationItems}
        </section>
      </div>
    </div>${footer(2)}</article>
  </body></html>`;
}

const SOURCE = {
  sidebar: [{ id: 'langs', items: [{}, {}, {}] }],
  mainColumn: [
    { kind: 'summary' },
    { kind: 'experience', jobs: [{ id: 'a', bullets: [{}, {}, {}] }] },
    { kind: 'education' },
  ],
};


// ─── Suite ───────────────────────────────────────────────────────

(async () => {
  // Try to load Playwright. Skip the whole suite if missing or if
  // Chromium can't launch — this keeps the test runner usable on
  // checkouts without browsers installed.
  let chromium;
  try {
    chromium = require('playwright').chromium;
  } catch {
    console.log('SKIP test_check_layout: playwright not installed');
    process.exit(0);
  }

  let browser;
  try {
    browser = await chromium.launch();
  } catch (err) {
    console.log(`SKIP test_check_layout: chromium launch failed (${err.message.split('\n')[0]})`);
    process.exit(0);
  }

  const ctx = await browser.newContext();

  /**
   * Run checkLayoutInvariants on a fixture and return the result.
   */
  async function check(html, options) {
    const page = await ctx.newPage();
    await page.setContent(html, { waitUntil: 'load' });
    const r = await checkLayoutInvariants(page, options);
    await page.close();
    return r;
  }

  function violationsByInvariant(violations) {
    const m = {};
    for (const v of violations) {
      m[v.invariant] = (m[v.invariant] || 0) + 1;
    }
    return m;
  }

  /**
   * Find the first violation matching the invariant name. If none,
   * record a clean failure rather than letting `undefined.field`
   * crash the suite.
   */
  function findViolation(violations, invariantName, testName) {
    const v = violations.find((x) => x.invariant === invariantName);
    if (!v) {
      fail(testName, { error: `expected ${invariantName} violation; none found` });
      return null;
    }
    return v;
  }


  // ── Happy path ─────────────────────────────────────────────────
  {
    const html = buildFixture({ pageCount: 2 });
    const r = await check(html, { expectedPageCount: 2 });
    assertEq(r.ok, true, 'happy path: ok=true');
    assertEq(r.violations, [], 'happy path: no violations');
    assertTrue(r.rhythmMeasurements.length >= 2,
      'happy path: rhythm measurements collected');
  }


  // ── Page-count mismatch ───────────────────────────────────────
  {
    const html = buildFixture({ pageCount: 1 });
    const r = await check(html, { expectedPageCount: 2 });
    assertEq(r.ok, false, 'page-count mismatch: ok=false');
    const counts = violationsByInvariant(r.violations);
    assertEq(counts['page-count'], 1, 'page-count: exactly one violation');
    const v = findViolation(r.violations, 'page-count', 'page-count: violation present');
    if (v) {
      assertEq(v.expected, 2, 'page-count: expected=2');
      assertEq(v.actual, 1, 'page-count: actual=1');
    }
  }


  // ── Page-count: caller-provided expectedPageCount honored ─────
  // Verifies CRIT-1's fix (resume.js used to silently drop the option,
  // so checkLayoutInvariants always saw the hardcoded default 2).
  {
    const html = buildFixture({ pageCount: 3 });
    const r = await check(html, { expectedPageCount: 3 });
    assertEq(r.violations.filter((v) => v.invariant === 'page-count'),
      [], 'expectedPageCount=3 honored: no page-count violation');
  }


  // ── Page-count: no expectedPageCount, no count assumed ────────
  // Caller passes no options. There is no default count any more (it
  // was 2), so a 3-page document is not a violation; presence still is.
  {
    const html = buildFixture({ pageCount: 3 });
    const r = await check(html);  // no options
    assertEq(r.violations.filter((v) => v.invariant === 'page-count'),
      [], 'no expectedPageCount: a 3-page HTML produces no page-count violation');
  }


  // ── No .page elements at all ──────────────────────────────────
  {
    const html = '<!doctype html><html><body><p>nothing</p></body></html>';
    const r = await check(html, { expectedPageCount: 2 });
    assertEq(r.ok, false, 'no pages: ok=false');
    const counts = violationsByInvariant(r.violations);
    assertEq(counts['page-presence'], 1, 'no pages: page-presence violation');
    // Function returns early — no other invariants checked.
    assertEq(r.violations.length, 1, 'no pages: only page-presence reported');
  }


  // ── Missing .body-grid on page 1 ──────────────────────────────
  {
    const html = buildFixture({ pageCount: 2, omitGrid: [1] });
    const r = await check(html, { expectedPageCount: 2 });
    assertEq(r.ok, false, 'missing body-grid: ok=false');
    const counts = violationsByInvariant(r.violations);
    assertEq(counts['grid-presence'], 1, 'missing body-grid: grid-presence violation');
    const v = findViolation(r.violations, 'grid-presence', 'grid-presence: violation present');
    if (v) assertEq(v.page, 1, 'grid-presence: page=1');
  }


  // ── Body-grid bottom doesn't reach page-content bottom ───────
  // To deterministically place the grid bottom at a known offset
  // from contentBottom (regardless of flex/grid layout), we use
  // absolute positioning. Page is 1056 px tall, padding 36/36, so
  // contentBottom is at 1020 px. Place the grid at bottom: 5 px
  // (i.e. its bottom edge is at 1056 - 5 = 1051 px) — wait, we want
  // a 5 px SHORTAGE: grid ends 5 px above contentBottom (1015), so
  // its bottom is 1015. Set bottom: 41 px (1056 - 41 = 1015).
  {
    const html = absolutePositionedFixture({ gridBottomOffsetPx: 41 });
    const r = await check(html, { expectedPageCount: 2 });
    const counts = violationsByInvariant(r.violations);
    assertTrue((counts['grid-bottom'] || 0) >= 1,
      'grid 5px short: grid-bottom violation reported');
    const v = findViolation(r.violations, 'grid-bottom', 'grid-bottom: violation present');
    if (v) {
      assertTrue(parseFloat(v.delta_px) >= 4 && parseFloat(v.delta_px) <= 6,
        'grid-bottom: delta is ~5 px');
    }
  }


  // ── Body-grid 1px short — within tolerance, no violation ─────
  // bottom: 37 px → grid bottom at 1019 → exactly 1 px above
  // contentBottom (1020). Should NOT trigger (tolerance is 1 px).
  {
    const html = absolutePositionedFixture({ gridBottomOffsetPx: 37 });
    const r = await check(html, { expectedPageCount: 2 });
    const counts = violationsByInvariant(r.violations);
    assertEq(counts['grid-bottom'], undefined,
      'grid 1px short: no grid-bottom violation (within tolerance)');
  }


  // ── Section-rhythm uniform → no violation ─────────────────────
  // (Sanity check: happy-path fixture already has uniform rhythm
  // because both columns use the same gap by default.)
  {
    const html = buildFixture({
      pageCount: 2, sidebarSepGap: 30, mainColSepGap: 30,
    });
    const r = await check(html, { expectedPageCount: 2 });
    const counts = violationsByInvariant(r.violations);
    assertEq(counts['section-rhythm'], undefined,
      'uniform rhythm: no section-rhythm violation');
  }


  // ── Section-rhythm mismatch — two columns with different gaps ─
  {
    const html = buildFixture({
      pageCount: 2, sidebarSepGap: 30, mainColSepGap: 50,
    });
    const r = await check(html, { expectedPageCount: 2 });
    const counts = violationsByInvariant(r.violations);
    assertTrue((counts['section-rhythm'] || 0) >= 1,
      'rhythm mismatch: section-rhythm violation reported');
    const v = findViolation(r.violations, 'section-rhythm', 'section-rhythm: violation present');
    if (v) {
      assertTrue(parseFloat(v.delta_px) >= 19,
        'section-rhythm: delta reflects 20px difference');
    }
  }


  // ── Section-rhythm with only one separator total — skip check ──
  // The check requires >= 2 measurements. Use 1 hr in main col, 0 in
  // sidebar.
  {
    const html = buildFixture({
      pageCount: 1, mainColHrCount: 1, sidebarHrCount: 0,
    });
    const r = await check(html, { expectedPageCount: 1 });
    const counts = violationsByInvariant(r.violations);
    assertEq(counts['section-rhythm'], undefined,
      'single rhythm measurement: rhythm check skipped');
  }


  // ── Content overflow — main-col descendant past page bottom ──
  // Use a generously large spacer so it reliably overflows the page
  // regardless of how flex/grid constraints arrange the column.
  // Production overflow is rarely this big, but the test only needs
  // to verify the *detection* works; magnitude isn't the assertion.
  {
    const html = buildFixture({ pageCount: 2, overflowMainColPx: 1500 });
    const r = await check(html, { expectedPageCount: 2 });
    assertEq(r.ok, false, 'content overflow: ok=false');
    const counts = violationsByInvariant(r.violations);
    assertTrue((counts['content-overflow'] || 0) >= 1,
      'content overflow: content-overflow violation reported');
    const v = findViolation(r.violations, 'content-overflow', 'content-overflow: violation present');
    if (v) {
      assertEq(v.column, 'main-col', 'content-overflow: identifies main-col');
      assertTrue(parseFloat(v.overflow_px) > 0,
        'content-overflow: overflow_px is positive');
    }
  }


  // ── Result shape — rhythmMeasurements has expected fields ────
  {
    const html = buildFixture({ pageCount: 2 });
    const r = await check(html, { expectedPageCount: 2 });
    assertTrue(Array.isArray(r.rhythmMeasurements),
      'rhythmMeasurements: is an array');
    if (r.rhythmMeasurements.length > 0) {
      const m = r.rhythmMeasurements[0];
      assertTrue(typeof m.column === 'string',
        'rhythmMeasurement: has column field');
      assertTrue(typeof m.gap_px === 'string',
        'rhythmMeasurement: gap_px is a string (formatted)');
    }
  }


  // ── Multiple violations reported in a single run ──────────────
  // Combine: missing grid on page 2 + main-col overflow on every
  // page. Expect at least one of each.
  {
    const html = buildFixture({
      pageCount: 2, omitGrid: [2], overflowMainColPx: 1500,
    });
    const r = await check(html, { expectedPageCount: 2 });
    assertEq(r.ok, false, 'multiple violations: ok=false');
    const counts = violationsByInvariant(r.violations);
    assertTrue((counts['grid-presence'] || 0) >= 1,
      'multiple violations: grid-presence reported');
    assertTrue((counts['content-overflow'] || 0) >= 1,
      'multiple violations: content-overflow reported');
  }


  // ── Content conservation: a split list and job, all there ────
  {
    const r = await check(contentFixture(), { expectedPageCount: 2 });
    assertEq(r.violations, [], 'conservation, HTML only: clean');
    const s = await check(contentFixture(), { expectedPageCount: 2, measurements: SOURCE });
    assertEq(s.violations, [], 'conservation, against the source: clean');
  }


  // ── Content conservation: the experience heading printed nowhere ─
  // The shape the solver produced when the heading's fit and the first
  // job's disagreed by a rounding error. Every other invariant is clean.
  {
    const r = await check(contentFixture({ dropExperienceHeading: true }),
      { expectedPageCount: 2 });
    const v = r.violations.filter((x) => x.invariant === 'content-conservation');
    assertEq(r.violations.length, v.length, 'lost heading: only conservation fires');
    assertEq(v.map((x) => [x.what, x.id]), [['main section', 'experience']],
      'lost heading: reported as the experience section');
  }


  // ── Content conservation: against the source ─────────────────
  // Missing bullets and items, and content the source does not have,
  // are invisible from the HTML alone.
  {
    const shortJob = { ...SOURCE, mainColumn: [
      SOURCE.mainColumn[0],
      { kind: 'experience', jobs: [{ id: 'a', bullets: [{}, {}, {}, {}] }, { id: 'b', bullets: [{}] }] },
      SOURCE.mainColumn[2],
    ] };
    const r = await check(contentFixture(), { expectedPageCount: 2, measurements: shortJob });
    const problems = r.violations.map((x) => `${x.id}: ${x.problem}`);
    assertEq(problems, ['a: 3 of 4 bullets rendered', 'b: missing'],
      'source: a missing bullet and a missing job');

    const extraBlock = { ...SOURCE, sidebar: [{ id: 'langs', items: [{}, {}] }, { id: 'skills', items: [{}] }] };
    const s = await check(contentFixture(), { expectedPageCount: 2, measurements: extraBlock });
    assertEq(s.violations.map((x) => `${x.id}: ${x.problem}`),
      ['langs: 3 of 2 items rendered', 'skills: missing'],
      'source: an item too many and a missing block');
  }


  // ── Horizontal overflow: a word wider than its column ────────
  {
    const r = await check(contentFixture({ longWord: 'x'.repeat(120) }), { expectedPageCount: 2 });
    const v = findViolation(r.violations, 'horizontal-overflow', 'horizontal-overflow: violation present');
    if (v) {
      assertEq([v.page, v.column], [1, 'sidebar'], 'horizontal-overflow: page 1 sidebar');
      assertTrue(parseFloat(v.overflow_px) > 100, 'horizontal-overflow: overflow_px is the overrun');
    }
    const clean = await check(contentFixture(), { expectedPageCount: 2 });
    assertEq(clean.violations.filter((x) => x.invariant === 'horizontal-overflow'), [],
      'horizontal-overflow: none on ordinary text');
  }


  // ── Horizontal overflow: a contact row too long for the header ─
  // The header and the footer are outside both columns. A long contact
  // row pushed the right-aligned contact block past the right margin
  // (and, longer, off the page, where .page clips it) while this check
  // walked only the columns and said clean.
  {
    const r = await check(contentFixture({ contactRow: 'x'.repeat(110) + '@example.com' }),
      { expectedPageCount: 2, measurements: SOURCE });
    const v = r.violations.filter((x) => x.invariant === 'horizontal-overflow');
    assertEq(v.map((x) => [x.page, x.column]), [[1, 'resume-header']],
      `horizontal-overflow: a long contact row is caught in the page-1 header (${JSON.stringify(r.violations)})`);
    assertTrue(v.length === 1 && parseFloat(v[0].overflow_px) > 100,
      'horizontal-overflow: ...by how far it runs past the right margin');
    const clean = await check(contentFixture({ contactRow: 'a@example.com' }),
      { expectedPageCount: 2, measurements: SOURCE });
    assertEq(clean.violations, [],
      'horizontal-overflow: a header and footer inside the margins are clean');
  }


  // ── Content conservation: an education section with no items ─
  // `items: []` builds (with a warning): the section is its heading
  // alone. It used to be told apart by its items, so it read as a
  // section of no known kind and failed the build.
  {
    const r = await check(contentFixture({ noEducationItems: true }), { expectedPageCount: 2 });
    assertEq(r.violations, [], 'empty education: clean from the HTML alone');
    const s = await check(contentFixture({ noEducationItems: true }),
      { expectedPageCount: 2, measurements: SOURCE });
    assertEq(s.violations, [], 'empty education: clean against the source');
  }


  // ── The viewport is put back ─────────────────────────────────
  // The engine measures the next build on the same page; it must not
  // inherit the check's 816×1056.
  {
    const page = await ctx.newPage();
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.setContent(contentFixture(), { waitUntil: 'load' });
    await checkLayoutInvariants(page, { expectedPageCount: 2 });
    assertEq(page.viewportSize(), { width: 1280, height: 720 }, 'viewport restored');
    assertEq(await page.evaluate(() => matchMedia('print').matches), false, 'media restored');
    await page.close();
  }


  // ── ...also when the check throws ────────────────────────────
  // A check that cannot run throws rather than reporting a violation.
  // It used to leave the page in print media at 816×1056 for the next
  // measurement on it.
  {
    const page = await ctx.newPage();
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.setContent(contentFixture(), { waitUntil: 'load' });
    await page.evaluate(() => {
      Document.prototype.querySelectorAll = () => { throw new Error('the check broke'); };
    });
    let err = null;
    try {
      await checkLayoutInvariants(page, { expectedPageCount: 2 });
    } catch (e) {
      err = e;
    }
    assertTrue(Boolean(err) && /the check broke/.test(err.message),
      `a check that cannot run throws its own error (${err && err.message.split('\n')[0]})`);
    assertEq(page.viewportSize(), { width: 1280, height: 720 }, 'viewport restored after a throw');
    assertEq(await page.evaluate(() => matchMedia('print').matches), false, 'media restored after a throw');
    await page.close();
  }


  // ── Cleanup ──────────────────────────────────────────────────
  await ctx.close();
  await browser.close();


  // ── Report ────────────────────────────────────────────────────
  report();
})().catch((err) => {
  console.error('test_check_layout: unexpected error');
  console.error(err.stack || err.message);
  process.exit(1);
});
