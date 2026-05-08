/**
 * test_solve_layout.js — Unit tests for scripts/solve_layout.js.
 *
 * The solver is a pure function. Tests feed synthetic measurements
 * (matching the shape produced by measure_dom.js) and assert the
 * placement output structure.
 */

const path = require('path');
const {
  solveLayout,
  solveSidebar,
  solveMainColumn,
  combinePages,
  sidebarBlockHeight,
  jobHeight,
  SolverError,
} = require(path.resolve(__dirname, '..', 'scripts', 'solve_layout'));


// ─── Tiny test framework ────────────────────────────────────────
let passed = 0;
let failed = 0;
const failures = [];

function assertEq(actual, expected, name) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    passed++;
    process.stdout.write('.');
  } else {
    failed++;
    failures.push({ name, actual: a, expected: e });
    process.stdout.write('F');
  }
}

function assertThrows(fn, predicate, name) {
  try {
    fn();
  } catch (err) {
    if (predicate(err)) {
      passed++;
      process.stdout.write('.');
      return;
    }
    failed++;
    failures.push({ name, error: 'predicate failed', actual: err.message });
    process.stdout.write('F');
    return;
  }
  failed++;
  failures.push({ name, error: 'expected throw, got success' });
  process.stdout.write('F');
}

function test(name, fn) {
  try {
    fn();
  } catch (err) {
    failed++;
    failures.push({ name, error: err.stack });
    process.stdout.write('E');
  }
}


// ─── Helpers — synthesize measurement-shaped data ───────────────

/**
 * Geometry helper.
 *   sidebar inter-block-cost = 2 * sidebarBlockGap + sidebarSeparatorHeight
 *   main col inter-section-cost = 2 * mainColumnSectionGap + mainColSeparatorHeight
 * For default values: 2*0 + 0 = 0 (no overhead between blocks unless
 * specified) — keeps simple tests readable. Pass `sep` to set both
 * separator heights at once, or `sbSep`/`mcSep` to differentiate.
 */
function geometry(opts = {}) {
  const sep = opts.sep ?? 0;
  return {
    page1Capacity: opts.page1 ?? 1000,
    pageNCapacity: opts.pageN ?? 1100,
    sidebarSeparatorHeight: opts.sbSep ?? sep,
    mainColSeparatorHeight: opts.mcSep ?? sep,
    sidebarBlockGap: opts.sbGap ?? 0,
    mainColumnSectionGap: opts.mcGap ?? 0,
  };
}

function listBlock(id, headingHeight, itemHeights, opts = {}) {
  const items = itemHeights.map((h) => ({ height: h }));
  const itemGap = opts.itemGap ?? 0;
  const headingToItemsGap = opts.headingToItemsGap ?? 0;
  const totalHeight = headingHeight
    + (items.length > 0 ? headingToItemsGap : 0)
    + items.reduce((s, it) => s + it.height, 0)
    + Math.max(0, items.length - 1) * itemGap;
  return {
    id, type: 'list',
    totalHeight, headingHeight, headingToItemsGap, itemGap, items,
  };
}

function detailsBlock(id, headingHeight, rowHeights, opts = {}) {
  const items = rowHeights.map((h) => ({ height: h }));
  const itemGap = opts.itemGap ?? 0;
  const headingToItemsGap = opts.headingToItemsGap ?? 0;
  const totalHeight = headingHeight
    + (items.length > 0 ? headingToItemsGap : 0)
    + items.reduce((s, it) => s + it.height, 0)
    + Math.max(0, items.length - 1) * itemGap;
  return {
    id, type: 'details',
    totalHeight, headingHeight, headingToItemsGap, itemGap, items,
  };
}

function job(id, headerHeight, bulletHeights, isGap = false, opts = {}) {
  const bullets = bulletHeights.map((h) => ({ height: h }));
  const bulletGap = opts.bulletGap ?? 0;
  const headerToBulletsGap = opts.headerToBulletsGap ?? 0;
  const totalHeight = isGap
    ? headerHeight
    : headerHeight
      + (bullets.length > 0 ? headerToBulletsGap : 0)
      + bullets.reduce((s, b) => s + b.height, 0)
      + Math.max(0, bullets.length - 1) * bulletGap;
  return {
    id, isGap, totalHeight,
    headerHeight, headerToBulletsGap, bulletGap, bullets,
  };
}

function experience(headingHeight, jobs, opts = {}) {
  const jobGap = opts.jobGap ?? 0;
  const headingToJobsGap = opts.headingToJobsGap ?? 0;
  const totalHeight = headingHeight
    + (jobs.length > 0 ? headingToJobsGap : 0)
    + jobs.reduce((s, j) => s + j.totalHeight, 0)
    + Math.max(0, jobs.length - 1) * jobGap;
  return {
    kind: 'experience',
    totalHeight, headingHeight, headingToJobsGap, jobGap, jobs,
  };
}

function summary(headingHeight, bodyHeight) {
  return {
    kind: 'summary', headingHeight,
    totalHeight: headingHeight + bodyHeight,
  };
}

function education(headingHeight, bodyHeight) {
  return {
    kind: 'education', headingHeight,
    totalHeight: headingHeight + bodyHeight,
  };
}


// ─── Height helper tests ────────────────────────────────────────
test('sidebarBlockHeight: matches synthesized totalHeight for whole block', () => {
  const b = listBlock('a', 30, [20, 20, 20, 20], { itemGap: 5, headingToItemsGap: 10 });
  // Expected: 30 + 10 + 80 + 3*5 = 135
  assertEq(b.totalHeight, 135, 'totalHeight derivation');
  assertEq(sidebarBlockHeight(b, 0, 4, false), 135, 'sidebarBlockHeight whole');
});

test('sidebarBlockHeight: continuation drops heading and headingToItemsGap', () => {
  const b = listBlock('a', 30, [20, 20, 20, 20], { itemGap: 5, headingToItemsGap: 10 });
  // Continuation, all 4 items: 80 + 3*5 = 95
  assertEq(sidebarBlockHeight(b, 0, 4, true), 95, 'continuation height');
});

test('sidebarBlockHeight: partial slice with fewer gaps', () => {
  const b = listBlock('a', 30, [20, 20, 20, 20], { itemGap: 5, headingToItemsGap: 10 });
  // First 2 items: 30 + 10 + 40 + 1*5 = 85
  assertEq(sidebarBlockHeight(b, 0, 2, false), 85, '2-item slice');
  // Last 2 items as continuation: 40 + 1*5 = 45
  assertEq(sidebarBlockHeight(b, 2, 2, true), 45, '2-item continuation');
});

test('jobHeight: regular job whole', () => {
  const j = job('j', 30, [20, 20, 20], false, { bulletGap: 5, headerToBulletsGap: 8 });
  // Expected: 30 + 8 + 60 + 2*5 = 108
  assertEq(j.totalHeight, 108, 'totalHeight derivation');
  assertEq(jobHeight(j, 0, 3, false), 108, 'jobHeight whole');
});

test('jobHeight: gap job is just header', () => {
  const j = job('relocation', 25, [], true);
  assertEq(j.totalHeight, 25, 'gap totalHeight');
  assertEq(jobHeight(j, 0, 0, false), 25, 'gap jobHeight');
});


// ─── Sidebar tests ──────────────────────────────────────────────
test('sidebar: single block fits on one page', () => {
  const b = listBlock('skills', 30, [20, 20, 20]);
  const result = solveSidebar([b], geometry({ page1: 500 }), 10);
  assertEq(result.length, 1, 'one page');
  assertEq(result[0].entries.length, 1, 'one entry');
  assertEq(result[0].entries[0],
    { block_id: 'skills', continuation: false, items_offset: 0, items_limit: null },
    'block placed whole');
});

test('sidebar: two blocks on one page with gap+separator overhead', () => {
  const a = listBlock('a', 30, [20, 20, 20]);  // height 90
  const b = listBlock('b', 30, [20, 20]);      // height 70
  // interBlockCost = 2*8 + 5 = 21
  // Total: 90 + 21 + 70 = 181
  const result = solveSidebar([a, b], geometry({ page1: 200, sbGap: 8, sep: 5 }), 10);
  assertEq(result.length, 1, 'one page');
  assertEq(result[0].entries.length, 2, 'two entries');
});

test('sidebar: overhead pushes second block to next page', () => {
  const a = listBlock('a', 30, [20, 20, 20]);
  const b = listBlock('b', 30, [20, 20]);
  // interBlockCost = 2*8 + 5 = 21. With cap 180, total 90+21+70=181 fails
  const result = solveSidebar([a, b], geometry({ page1: 180, pageN: 200, sbGap: 8, sep: 5 }), 10);
  assertEq(result.length, 2, 'two pages');
  assertEq(result[0].entries[0].block_id, 'a', 'page 1: a');
  assertEq(result[1].entries[0].block_id, 'b', 'page 2: b');
});

test('sidebar: 6-item list splits as 3+3', () => {
  // heading 30, 6 items × 20 = 120, gaps 5 → each item adds 25 effectively after first
  // Whole: 30 + 0 + 120 + 5*5 = 175
  // Capacity 110: 30 + 60 + 2*5 = 100 (3 items), 30 + 80 + 3*5 = 125 (4 items: too big)
  //   → bestK = 3
  // Tail: 60 + 2*5 = 70 (3 items continuation) ≤ pageN cap
  const b = listBlock('a', 30, [20, 20, 20, 20, 20, 20], { itemGap: 5 });
  const result = solveSidebar([b], geometry({ page1: 110, pageN: 200 }), 10);
  assertEq(result.length, 2, 'two pages');
  assertEq(result[0].entries[0].items_limit, 3, 'page 1: 3 items');
  assertEq(result[1].entries[0],
    { block_id: 'a', continuation: true, items_offset: 3, items_limit: null },
    'page 2: continuation');
});

test('sidebar: 4-item list splits as 3+1', () => {
  const b = listBlock('a', 30, [20, 20, 20, 20], { itemGap: 5 });
  // Capacity: 30 + 0 + 60 + 2*5 = 100 (3 items)
  const result = solveSidebar([b], geometry({ page1: 105, pageN: 200 }), 10);
  assertEq(result.length, 2, 'two pages');
  assertEq(result[0].entries[0].items_limit, 3, 'page 1: 3 items');
  assertEq(result[1].entries[0].items_offset, 3, 'page 2: from item 3');
});

test('sidebar: 3-item list cannot split (need 3 origin + 1 receiver = 4 items min)', () => {
  const first = listBlock('first', 30, [20]);
  const a = listBlock('a', 30, [50, 50, 50]);  // 30 + 150 = 180
  // Page 1 cap 100: first fits (50). a doesn't fit (180 > 50 remaining).
  // Receiver minimum is 1 → checking bridge: 3-item split as 1+2, but origin
  // requires ≥3 → no bridge → push whole to page 2.
  const result = solveSidebar([first, a], geometry({ page1: 100, pageN: 200 }), 10);
  assertEq(result.length, 2, 'two pages');
  assertEq(result[1].entries[0].block_id, 'a', 'a whole on page 2');
  assertEq(result[1].entries[0].items_limit, null, 'a not split');
});

test('sidebar: details block is atomic (never bridges)', () => {
  const first = listBlock('first', 30, [20]);
  const contact = detailsBlock('contact', 30, [20, 20, 20, 20, 20, 20, 20, 20]);
  const result = solveSidebar([first, contact], geometry({ page1: 100, pageN: 250 }), 10);
  assertEq(result.length, 2, 'two pages');
  assertEq(result[1].entries[0].block_id, 'contact', 'details on page 2');
  assertEq(result[1].entries[0].items_limit, null, 'details not split');
});

test('sidebar: throws SolverError for impossible block', () => {
  assertThrows(
    () => solveSidebar(
      [listBlock('huge', 30, [200, 200, 200])],
      geometry({ page1: 100, pageN: 100 }),
      10,
    ),
    (err) => err instanceof SolverError && /taller than a page/.test(err.message),
    'oversized block throws',
  );
});

test('sidebar: respects maxPages cap', () => {
  assertThrows(
    () => solveSidebar(
      [
        listBlock('a', 30, [50]),
        listBlock('b', 30, [50]),
        listBlock('c', 30, [50]),
      ],
      geometry({ page1: 100, pageN: 100 }),
      2,
    ),
    (err) => err instanceof SolverError && /exceeds maxPages/.test(err.message),
    'maxPages exceeded',
  );
});


// ─── Main column tests ──────────────────────────────────────────
test('main: summary + experience(1 job) + education on one page', () => {
  const result = solveMainColumn(
    [
      summary(30, 60),
      experience(30, [job('j1', 30, [20, 20, 20])]),
      education(30, 30),
    ],
    geometry({ page1: 500 }),
    10,
  );
  assertEq(result.length, 1, 'one page');
  assertEq(result[0].entries.length, 3, 'three entries');
  assertEq(result[0].entries[0].type, 'summary', 'summary first');
  assertEq(result[0].entries[1].type, 'experience', 'experience second');
  assertEq(result[0].entries[1].jobs.length, 1, 'experience: 1 job');
  assertEq(result[0].entries[2].type, 'education', 'education third');
});

test('main: job bridges across pages (2 bullets on origin, 1 on receiver)', () => {
  // Capacity 250: summary uses 80, heading uses 30 → 110 used.
  // Available 140. Job whole = 30 + 120 = 150 (no fit).
  // Bridge: 30 + 40 = 70 ≤ 140 (1 bullet), 30 + 80 = 110 ≤ 140 (2 bullets ✓),
  //         30 + 120 = 150 > 140 (3 bullets fail). bestK = 2.
  // Tail: 1 bullet on receiver (≥ MIN_JOB_BULLETS_ON_PAGE = 1).
  const s = summary(30, 50);
  const j = job('jbridge', 30, [40, 40, 40]);
  const result = solveMainColumn(
    [s, experience(30, [j])],
    geometry({ page1: 250, pageN: 500 }),
    10,
  );
  assertEq(result.length, 2, 'two pages');
  assertEq(result[0].entries.length, 2, 'page 1: summary + experience');
  assertEq(result[0].entries[1].jobs[0],
    { job_id: 'jbridge', continuation: false, bullets_offset: 0, bullets_limit: 2 },
    'page 1: head with 2 bullets');
  assertEq(result[1].entries[0].type, 'experience', 'page 2: experience');
  assertEq(result[1].entries[0].continuation_of, 'experience', 'is continuation');
  assertEq(result[1].entries[0].jobs[0],
    { job_id: 'jbridge', continuation: true, bullets_offset: 2, bullets_limit: null },
    'page 2: tail');
});

test('main: gap job placed atomically', () => {
  const result = solveMainColumn(
    [experience(30, [
      job('j1', 30, [20]),
      job('relocation', 25, [], true),
      job('j2', 30, [20]),
    ])],
    geometry({ page1: 500 }),
    10,
  );
  assertEq(result.length, 1, 'one page');
  assertEq(result[0].entries[0].jobs.length, 3, 'three jobs');
  assertEq(result[0].entries[0].jobs[1].job_id, 'relocation', 'gap placed');
});

test('main: experience heading not orphaned — moves with first job', () => {
  // Capacity 175: summary (130) fits with no overhead (130). Heading + min job
  // would need: heading(30) + headingToJobsGap(0) + header(30) + headerToBulletsGap(0)
  //   + bullets[0](40) = 100. Plus inter-section cost (0 here). Plus existing 130.
  //   Total 230 > 175 → push to page 2.
  const result = solveMainColumn(
    [
      summary(30, 100),
      experience(30, [job('j1', 30, [40, 40])]),
    ],
    geometry({ page1: 175, pageN: 500 }),
    10,
  );
  assertEq(result.length, 2, 'two pages');
  assertEq(result[0].entries.length, 1, 'page 1: summary only');
  assertEq(result[1].entries[0].type, 'experience', 'page 2: experience');
  assertEq(result[1].entries[0].continuation_of, null, 'not continuation');
  assertEq(result[1].entries[0].jobs[0].job_id, 'j1', 'j1 on page 2');
});

test('main: throws when exceeding maxPages', () => {
  assertThrows(
    () => solveMainColumn(
      [
        summary(30, 50),
        experience(30, [
          job('j1', 30, [40]),
          job('j2', 30, [40]),
          job('j3', 30, [40]),
        ]),
      ],
      geometry({ page1: 120, pageN: 80 }),
      2,
    ),
    (err) => err instanceof SolverError,
    'maxPages exceeded',
  );
});

test('main: oversized single bullet throws', () => {
  assertThrows(
    () => solveMainColumn(
      [experience(30, [job('j', 30, [500])])],
      geometry({ page1: 100, pageN: 100 }),
      10,
    ),
    (err) => err instanceof SolverError,
    'oversized bullet throws',
  );
});


// ─── combinePages ───────────────────────────────────────────────
test('combinePages: zips equal-length columns', () => {
  const result = combinePages(
    [{ entries: ['s1'] }, { entries: ['s2'] }],
    [{ entries: ['m1'] }, { entries: ['m2'] }],
  );
  assertEq(result.pages.length, 2, 'two pages');
  assertEq(result.pages[0].sidebar_blocks, ['s1'], 'p1 sidebar');
  assertEq(result.pages[0].main_sections, ['m1'], 'p1 main');
});

test('combinePages: pads shorter column', () => {
  const result = combinePages(
    [{ entries: ['s1'] }],
    [{ entries: ['m1'] }, { entries: ['m2'] }],
  );
  assertEq(result.pages.length, 2, 'two pages');
  assertEq(result.pages[1].sidebar_blocks, [], 'p2 sidebar empty');
  assertEq(result.pages[1].main_sections, ['m2'], 'p2 main');
});


// ─── End-to-end via solveLayout ─────────────────────────────────
test('solveLayout: full input produces full placement', () => {
  const input = {
    pageGeometry: geometry({ page1: 500, pageN: 500 }),
    maxPages: 10,
    sidebar: [
      detailsBlock('details', 30, [20, 20, 20]),
      listBlock('skills', 30, [15, 15, 15, 15, 15]),
    ],
    mainColumn: [
      summary(30, 60),
      experience(30, [job('j1', 30, [20, 20, 20])]),
      education(30, 30),
    ],
  };
  const result = solveLayout(input);
  assertEq(result.pages.length, 1, 'one page');
  assertEq(result.pages[0].sidebar_blocks.length, 2, 'two sidebar entries');
  assertEq(result.pages[0].main_sections.length, 3, 'three main entries');
});

test('solveLayout: rejects invalid maxPages', () => {
  assertThrows(
    () => solveLayout({
      pageGeometry: geometry(), maxPages: 0, sidebar: [], mainColumn: [],
    }),
    (err) => err instanceof SolverError && /maxPages/.test(err.message),
    'maxPages=0',
  );
});


// ─── maxPages cap edge cases ────────────────────────────────────
test('maxPages: sidebar alone exceeds cap throws SolverError', () => {
  // 5 blocks, each one fills a page on its own.
  const blocks = ['a', 'b', 'c', 'd', 'e'].map((id) =>
    listBlock(id, 30, [50])
  );
  let caught = null;
  try {
    solveSidebar(blocks, geometry({ page1: 100, pageN: 100 }), 3);
  } catch (e) { caught = e; }
  assertEq(caught instanceof SolverError, true, 'throws SolverError');
  assertEq(/maxPages.*3/.test(caught.message), true, 'mentions cap value');
  assertEq(caught.column, 'sidebar', 'identifies column');
});

test('maxPages: main column alone exceeds cap throws SolverError', () => {
  // 5 atomic sections, each fills a page.
  let caught = null;
  try {
    solveMainColumn(
      [
        summary(30, 60),
        education(30, 60),
        // Re-use education kind as a stand-in for "atomic that fills a page".
        // (The solver doesn't enforce uniqueness here — that's validate_data's job.)
        education(30, 60),
        education(30, 60),
        education(30, 60),
      ],
      geometry({ page1: 100, pageN: 100 }),
      2,
    );
  } catch (e) { caught = e; }
  assertEq(caught instanceof SolverError, true, 'throws SolverError');
  assertEq(/maxPages.*2/.test(caught.message), true, 'mentions cap value');
});

test('maxPages: combined columns exceed cap (sidebar fits, main does not)', () => {
  // Sidebar fits in 1 page; main column needs 3. Cap is 2 → throws.
  assertThrows(
    () => solveLayout({
      pageGeometry: geometry({ page1: 80, pageN: 80 }),
      maxPages: 2,
      sidebar: [listBlock('a', 20, [20])],
      mainColumn: [
        experience(20, [job('j1', 20, [20])]),
        education(20, 30),
        summary(20, 30),
      ],
    }),
    (err) => err instanceof SolverError,
    'combined columns exceed cap',
  );
});

test('maxPages: cap of 10 (project default) accommodates real-resume sized content', () => {
  // Sanity check: typical content (5 sidebar blocks, 7 jobs with 2-3
  // bullets each, summary + education) should comfortably solve in
  // 2 pages with realistic geometry — well under the 10-page cap.
  const result = solveLayout({
    pageGeometry: geometry({ page1: 900, pageN: 1000, sep: 30, sbGap: 16, mcGap: 11 }),
    maxPages: 10,
    sidebar: [
      detailsBlock('details', 25, [20, 20, 20]),
      listBlock('skills', 25, Array(20).fill(18), { itemGap: 6 }),
      listBlock('langs', 25, [18, 18, 18], { itemGap: 6 }),
    ],
    mainColumn: [
      summary(28, 120),
      experience(28, [
        job('j1', 50, [40, 40, 40], false, { bulletGap: 6, headerToBulletsGap: 8 }),
        job('j2', 50, [40, 40], false, { bulletGap: 6, headerToBulletsGap: 8 }),
        job('j3', 50, [40, 40, 40], false, { bulletGap: 6, headerToBulletsGap: 8 }),
      ]),
      education(28, 60),
    ],
  });
  // Don't assert exact page count — different geometries pack
  // differently. Just verify it solved within cap.
  assertEq(result.pages.length <= 10, true, `<=10 pages (got ${result.pages.length})`);
  assertEq(result.pages.length >= 1, true, 'at least 1 page');
});

test('maxPages: cap of 1 forces all-on-one-page or throws', () => {
  // Tiny content: should fit on 1 page.
  const ok = solveLayout({
    pageGeometry: geometry({ page1: 500, pageN: 500 }),
    maxPages: 1,
    sidebar: [listBlock('a', 20, [20])],
    mainColumn: [summary(20, 50)],
  });
  assertEq(ok.pages.length, 1, 'tiny content fits in 1 page');

  // Content that doesn't fit on 1 page → throw.
  assertThrows(
    () => solveLayout({
      pageGeometry: geometry({ page1: 100, pageN: 100 }),
      maxPages: 1,
      sidebar: [listBlock('a', 20, [50])],   // 70 fits page 1
      mainColumn: [
        summary(20, 50),                      // 70 — fits when alone
        education(20, 60),                    // 80 → would need page 2
      ],
    }),
    (err) => err instanceof SolverError,
    'oversized content + maxPages=1 throws',
  );
});


// ─── Non-progressing solver detection ───────────────────────────
//
// If a single item is structurally too tall to fit on any page (even
// an empty one), the solver could loop forever pushing it to the next
// page. The "non-progressing" detector catches this in two ways:
//   (1) An entry pushed to a fresh empty page that still doesn't fit
//       throws immediately ("taller than a page").
//   (2) The same entry pushed forward MAX_CONSECUTIVE_PUSHES times
//       in a row throws with a "cannot fit on any page" message.

test('non-progressing: single bullet taller than pageNCapacity throws', () => {
  // pageN = 200, but bullet is 500 → no possible page can hold it.
  // The solver should bail with a clear message instead of looping.
  let caught = null;
  try {
    solveMainColumn(
      [experience(30, [job('huge', 30, [500])])],
      geometry({ page1: 200, pageN: 200 }),
      10,
    );
  } catch (e) { caught = e; }
  assertEq(caught instanceof SolverError, true, 'throws SolverError');
  assertEq(caught.column, 'main', 'identifies main column');
  // Error should include either job_id or specific height info so the
  // user can identify the culprit.
  const hasContext = caught.job_id === 'huge' ||
    /huge/.test(caught.message) ||
    /500/.test(caught.message) ||
    /taller than a page/.test(caught.message) ||
    /cannot fit/.test(caught.message);
  assertEq(hasContext, true, `error has actionable context: ${caught.message}`);
});

test('non-progressing: oversized sidebar item throws with actionable error', () => {
  let caught = null;
  try {
    solveSidebar(
      [listBlock('huge', 30, [500, 500, 500, 500])],
      geometry({ page1: 200, pageN: 200 }),
      10,
    );
  } catch (e) { caught = e; }
  assertEq(caught instanceof SolverError, true, 'throws SolverError');
  assertEq(caught.column, 'sidebar', 'identifies sidebar');
  assertEq(caught.block_id, 'huge', 'identifies block by id');
});

test('non-progressing: oversized FIRST item never causes infinite loop', () => {
  // Edge case: the very first thing we try to place is too big.
  // The "current page is empty AND entry doesn't fit" branch detects
  // this and throws immediately rather than spinning. We verify by
  // confirming the throw happens AND the error has actionable context;
  // a non-progressing solver would either loop forever or hit the
  // MAX_CONSECUTIVE_PUSHES guard with a different (also-acceptable)
  // error.
  let caught = null;
  try {
    solveSidebar(
      [listBlock('first', 30, [500])],
      geometry({ page1: 100, pageN: 100 }),
      10,
    );
  } catch (e) { caught = e; }
  assertEq(caught instanceof SolverError, true, 'throws SolverError');
  assertEq(caught.block_id, 'first', 'identifies the block');
});

test('non-progressing: oversized LATER item also detected (not just first)', () => {
  // First block fits fine; second block is too big.
  let caught = null;
  try {
    solveSidebar(
      [
        listBlock('a', 30, [20]),
        listBlock('huge', 30, [500, 500]),
      ],
      geometry({ page1: 100, pageN: 100 }),
      10,
    );
  } catch (e) { caught = e; }
  assertEq(caught instanceof SolverError, true, 'throws SolverError');
  assertEq(caught.block_id, 'huge', 'identifies the right block');
});


// ─── Report ─────────────────────────────────────────────────────
console.log('');
console.log(`${passed} passed, ${failed} failed`);
if (failed > 0) {
  console.log('\nFailures:');
  for (const f of failures) {
    console.log(`  ${f.name}`);
    if (f.error) console.log(`    error: ${f.error}`);
    if (f.actual) console.log(`    actual:   ${f.actual}`);
    if (f.expected) console.log(`    expected: ${f.expected}`);
  }
  process.exit(1);
}
process.exit(0);
