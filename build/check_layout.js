/**
 * check_layout.js — Layout invariant assertions for the rendered HTML.
 *
 * Exports:
 *   • checkLayoutInvariants(page, options) — async function that takes
 *     a Playwright Page (already navigated to the resume HTML) plus
 *     optional { expectedPageCount, measurements }. Returns { ok,
 *     violations, rhythmMeasurements, fill } — fill[i] is how full page
 *     i+1 is, 0–1 (see invariant 4). A layout problem is a violation,
 *     never an exception: it throws only when the check cannot run (the
 *     page closed or crashed, say). Either way the page's viewport and
 *     media are put back as they were, so the next measurement on the
 *     same page (the engine's) sees what a fresh page would.
 *
 * The invariants checked:
 *   1. .page elements exist; with `expectedPageCount` (the placement's
 *      page count, which the pipeline always passes), that many.
 *   2. Each .body-grid's bottom edge equals (page bottom − padding-bottom),
 *      so the column divider terminates at the print margin.
 *   3. Section rhythm — visible spacing between sibling .block elements
 *      separated by .section-sep — is identical across all instances
 *      (sidebar and main column).
 *   4. No content overflow — each page's content fits within its
 *      padding-bottom. The .page elements use overflow:hidden, so a
 *      placement bug would otherwise silently clip content (invisible
 *      until someone inspects the PDF).
 *   5. Content conservation — everything renders exactly once. From the
 *      HTML alone: each sidebar block and each job is begun once, every
 *      continued piece follows its beginning, and each main section's
 *      heading appears once (an experience section seen only as a
 *      continuation means its heading was lost). With `measurements`
 *      (the solver's input, as measure_dom.js returns it) also against
 *      the source: every block, job and main section is there, with
 *      all its items and bullets, none twice.
 *   6. No horizontal overflow — nothing in a column, element or text,
 *      reaches past the column's left or right edge, and nothing else
 *      on the page (the page-1 header, the page footer) past the page's
 *      side margins. overflow-wrap breaks a long word (_base.scss); this
 *      catches what still gets out, which would run over the divider,
 *      into the margin or off the page.
 */

const LAYOUT_CONSTANTS = Object.freeze({
  // US Letter at 96 DPI: 8.5in = 816 px, 11in = 1056 px (exact since
  // 1in = 96 px in CSS).
  LETTER_VIEWPORT_W: 816,
  LETTER_VIEWPORT_H: 1056,
  // Tolerance (px) for layout-invariant geometry checks. 1 px allows for
  // browser sub-pixel rounding without false positives.
  INVARIANT_TOLERANCE_PX: 1,
});

/**
 * What the source holds, from the solver's input: each sidebar block's
 * item count, each job's bullet count, the main sections in order.
 * Null without measurements; invariant 5 then checks the HTML alone.
 */
function expectedContent(measurements) {
  if (!measurements) return null;
  const experience = measurements.mainColumn.find((s) => s.kind === 'experience');
  return {
    blocks: measurements.sidebar.map((b) => ({ id: b.id, items: b.items.length })),
    jobs: experience
      ? experience.jobs.map((j) => ({ id: j.id, bullets: j.bullets.length }))
      : [],
    sections: measurements.mainColumn.map((s) => s.kind),
  };
}

async function checkLayoutInvariants(page, options = {}) {
  // expectedPageCount: how many .page elements the caller expects, the
  // placement's page count. Without it only their presence is checked:
  // there is no right number to assume (it used to default to 2).
  const expectedPageCount = options.expectedPageCount ?? null;
  // Before the page is touched: malformed measurements throw here, with
  // nothing yet to put back.
  const expected = expectedContent(options.measurements);

  // Put back what this changes, so a later measurement on this page
  // (the engine reuses it) does not inherit print media and a
  // Letter-sized viewport that a freshly opened page would not have.
  // In a finally: a check that threw (a page closing under it) used to
  // leave both behind for the engine's next render.
  const viewportBefore = page.viewportSize();

  let result;
  let threw = true;
  try {
    await page.emulateMedia({ media: 'print' });
    await page.setViewportSize({
      width: LAYOUT_CONSTANTS.LETTER_VIEWPORT_W,
      height: LAYOUT_CONSTANTS.LETTER_VIEWPORT_H,
    });
    result = await evaluateInvariants(page, expectedPageCount, expected);
    threw = false;
  } finally {
    try {
      await page.emulateMedia({ media: null });
      if (viewportBefore) await page.setViewportSize(viewportBefore);
    } catch (err) {
      // A page that failed the check usually fails this too; the error
      // worth reporting is the first one.
      if (!threw) throw err;
    }
  }

  return {
    ok: result.violations.length === 0,
    violations: result.violations,
    rhythmMeasurements: result.rhythmMeasurements,
    fill: result.fill || [],
  };
}

/** The checks themselves, run inside the page. See checkLayoutInvariants. */
function evaluateInvariants(page, expectedPageCount, expected) {
  return page.evaluate((cfg) => {
    const violations = [];
    const TOL = cfg.tolerance;

    // Invariant 1: page elements exist and count is correct.
    const pages = document.querySelectorAll('.page');
    if (pages.length === 0) {
      violations.push({ invariant: 'page-presence', error: 'no .page elements found' });
      return { violations, rhythmMeasurements: [] };
    }
    if (cfg.expectedPageCount !== null && pages.length !== cfg.expectedPageCount) {
      violations.push({
        invariant: 'page-count',
        expected: cfg.expectedPageCount,
        actual: pages.length,
      });
    }

    // Invariant 2: .body-grid bottom = page bottom − padding-bottom.
    pages.forEach((p, i) => {
      const grid = p.querySelector('.body-grid');
      if (!grid) {
        violations.push({ invariant: 'grid-presence', page: i + 1, error: 'no .body-grid' });
        return;
      }
      const pageRect = p.getBoundingClientRect();
      const gridRect = grid.getBoundingClientRect();
      const cs = window.getComputedStyle(p);
      const padBottom = parseFloat(cs.paddingBottom);
      const expected = pageRect.bottom - padBottom;
      const actual = gridRect.bottom;
      const delta = Math.abs(expected - actual);
      if (delta > TOL) {
        violations.push({
          invariant: 'grid-bottom',
          page: i + 1,
          expected_bottom: expected.toFixed(2),
          actual_bottom: actual.toFixed(2),
          delta_px: delta.toFixed(2),
        });
      }
    });

    // Invariant 3: section-rhythm uniform across all .section-sep
    // instances. Measure the distance from prev-sibling.bottom to
    // next-sibling.top — this is the visible "gap between sections."
    const rhythmMeasurements = [];
    document.querySelectorAll('hr.section-sep').forEach((hr) => {
      const prev = hr.previousElementSibling;
      const next = hr.nextElementSibling;
      if (!prev || !next) return;
      const prevBottom = prev.getBoundingClientRect().bottom;
      const nextTop = next.getBoundingClientRect().top;
      const column = hr.parentElement?.classList.contains('sidebar')
        ? 'sidebar'
        : hr.parentElement?.classList.contains('main-col')
        ? 'main-col'
        : 'unknown';
      rhythmMeasurements.push({ column, gap_px: nextTop - prevBottom });
    });
    if (rhythmMeasurements.length >= 2) {
      const reference = rhythmMeasurements[0].gap_px;
      rhythmMeasurements.forEach((m, i) => {
        const delta = Math.abs(m.gap_px - reference);
        if (delta > TOL) {
          violations.push({
            invariant: 'section-rhythm',
            measurement_index: i,
            column: m.column,
            gap_px: m.gap_px.toFixed(2),
            reference_px: reference.toFixed(2),
            delta_px: delta.toFixed(2),
          });
        }
      });
    }

    // Invariant 4: no content overflow. Each .page has overflow: hidden,
    // so a placement bug would silently clip content (invisible until
    // someone looks at the PDF). Walk each column and verify the
    // lowest-positioned descendant doesn't extend past the page's
    // padding-bottom. If something overflows, the layout solver
    // produced a placement that doesn't actually fit — fail loudly
    // here rather than ship a clipped PDF.
    //
    // The same walk yields how full each page is (`fill`): the lowest
    // content in either column, as a fraction of the body grid's height
    // (its top to the bottom margin). Not an invariant, a measurement the
    // Studio shows for the last page — "how much room is left".
    const fill = [];
    pages.forEach((p, i) => {
      const pageRect = p.getBoundingClientRect();
      const cs = window.getComputedStyle(p);
      const padBottom = parseFloat(cs.paddingBottom);
      const contentBottom = pageRect.bottom - padBottom;
      const grid = p.querySelector('.body-grid');
      const gridTop = grid ? grid.getBoundingClientRect().top : pageRect.top;
      let lowest = gridTop;
      // Check sidebar and main-col separately so error messages
      // identify which column is overflowing.
      ['sidebar', 'main-col'].forEach((colClass) => {
        const col = p.querySelector('.' + colClass);
        if (!col) return;
        let maxBottom = -Infinity;
        let culprit = null;
        col.querySelectorAll('*').forEach((el) => {
          const r = el.getBoundingClientRect();
          if (r.bottom > maxBottom) {
            maxBottom = r.bottom;
            culprit = el;
          }
        });
        if (maxBottom > lowest) lowest = maxBottom;
        if (maxBottom > contentBottom + TOL) {
          violations.push({
            invariant: 'content-overflow',
            page: i + 1,
            column: colClass,
            overflow_px: (maxBottom - contentBottom).toFixed(2),
            culprit: culprit ? (culprit.id || culprit.dataset?.id
              || culprit.tagName + '.' + String(culprit.className || '').split(' ')[0]) : null,
          });
        }
      });
      const room = contentBottom - gridTop;
      fill.push(room > 0 ? Math.max(0, Math.min(1, (lowest - gridTop) / room)) : null);
    });

    // Invariant 5: content conservation. Overflow and rhythm cannot see
    // content that is missing: a solver that drops a section's heading,
    // or a whole job, produces a placement that fits perfectly. That
    // happened — the experience heading, charged to a page whose first
    // job then went to the next one, printed nowhere — and every other
    // check stayed clean. So count what is there. A block or job is
    // begun by the element carrying its id and continued by elements
    // carrying data-continues-from (templates/_macros.j2).
    const conservation = (detail) =>
      violations.push({ invariant: 'content-conservation', ...detail });
    const collect = (selector, countSelector, what) => {
      const byId = new Map();
      document.querySelectorAll(selector).forEach((el) => {
        const continued = el.hasAttribute('data-continues-from');
        const id = continued ? el.getAttribute('data-continues-from') : el.id;
        if (!byId.has(id)) byId.set(id, { begun: 0, firstBegins: !continued, count: 0 });
        const entry = byId.get(id);
        if (!continued) entry.begun++;
        entry.count += el.querySelectorAll(countSelector).length;
        const headed = el.querySelector(':scope > .section-heading, :scope > .entry-header') !== null;
        if (headed === continued) {
          conservation({ what, id, problem: continued
            ? 'a continued piece repeats the heading' : 'begun without its heading' });
        }
      });
      byId.forEach((entry, id) => {
        if (entry.begun !== 1) conservation({ what, id, problem: `begun ${entry.begun} times` });
        else if (!entry.firstBegins) conservation({ what, id, problem: 'continued before it begins' });
      });
      return byId;
    };
    const blocks = collect('.sidebar > section.block',
      ':scope > .plain-list > li, :scope > .details-list > .detail-row', 'sidebar block');
    const jobs = collect('.main-col article.job', ':scope > .bullets > li', 'job');

    // Main sections: each heading once, and an experience section split
    // across pages begins (with its heading) before it continues.
    //
    // The kind is told by exclusion. The final templates mark only the
    // experience section (render_main_section, templates/_macros.j2);
    // the summary always draws its .summary-text; what is left is
    // education. Told apart by its .edu-item instead, an education
    // section with `items: []`, which builds with a warning, read as a
    // section of no known kind, and its own heading failed the build.
    const sections = new Map();
    document.querySelectorAll('.main-col > section.block').forEach((el) => {
      const kind = el.classList.contains('experience') ? 'experience'
        : el.querySelector(':scope > .summary-text') ? 'summary' : 'education';
      if (!sections.has(kind)) sections.set(kind, { headings: 0, pieces: 0, firstHeaded: null });
      const entry = sections.get(kind);
      const headed = el.querySelector(':scope > .section-heading') !== null;
      if (entry.firstHeaded === null) entry.firstHeaded = headed;
      entry.pieces++;
      if (headed) entry.headings++;
    });
    sections.forEach((entry, kind) => {
      if (entry.headings !== 1) {
        conservation({ what: 'main section', id: kind, problem: `heading printed ${entry.headings} times` });
      } else if (!entry.firstHeaded) {
        conservation({ what: 'main section', id: kind, problem: 'continued before its heading' });
      } else if (kind !== 'experience' && entry.pieces !== 1) {
        conservation({ what: 'main section', id: kind, problem: `printed ${entry.pieces} times` });
      }
    });

    // Against the source, when the caller passes it.
    if (cfg.expected) {
      const compare = (byId, expected, what, unit) => {
        const known = new Set(expected.map((e) => e.id));
        for (const e of expected) {
          const got = byId.get(e.id);
          if (!got) conservation({ what, id: e.id, problem: 'missing' });
          else if (got.count !== e[unit]) {
            conservation({ what, id: e.id, problem: `${got.count} of ${e[unit]} ${unit} rendered` });
          }
        }
        byId.forEach((_, id) => {
          if (!known.has(id)) conservation({ what, id, problem: 'not in the source' });
        });
      };
      compare(blocks, cfg.expected.blocks, 'sidebar block', 'items');
      compare(jobs, cfg.expected.jobs, 'job', 'bullets');
      for (const kind of cfg.expected.sections) {
        if (!sections.has(kind)) conservation({ what: 'main section', id: kind, problem: 'missing' });
      }
      sections.forEach((_, kind) => {
        if (!cfg.expected.sections.includes(kind)) {
          conservation({ what: 'main section', id: kind, problem: 'not in the source' });
        }
      });
    }

    // Invariant 6: no horizontal overflow. Invariant 4 looks down, not
    // sideways. A word too long for its line used to run out of its
    // column (a URL in a sidebar list ran over the divider into the main
    // column, see _base.scss) while every check stayed clean. Text is
    // measured by its own boxes: an element keeps its column's width
    // while the text inside it runs past.
    //
    // Each column is held to its own edges. Everything else on the page
    // (the page-1 header and its rule, the "Page N of M" footer) is held
    // to the page's side margins, the edges of its content box, where
    // the columns end too: a long contact row pushed the right-aligned
    // contact block into the right margin, or past it to where .page
    // clips, and no column saw it. Such a region is named by its class.
    pages.forEach((p, i) => {
      const pageRect = p.getBoundingClientRect();
      const pcs = window.getComputedStyle(p);
      const margins = {
        left: pageRect.left + parseFloat(pcs.paddingLeft),
        right: pageRect.right - parseFloat(pcs.paddingRight),
      };
      const regions = [];
      ['sidebar', 'main-col'].forEach((colClass) => {
        const col = p.querySelector('.' + colClass);
        if (col) regions.push({ name: colClass, root: col, edge: col.getBoundingClientRect() });
      });
      p.querySelectorAll(':scope > :not(.body-grid)').forEach((el) => {
        const name = String(el.className || '').split(' ')[0] || el.tagName.toLowerCase();
        regions.push({ name, root: el, edge: margins, whole: true });
      });
      regions.forEach(({ name: colClass, root: col, edge, whole }) => {
        let worst = 0;
        let culprit = null;
        const consider = (r, el) => {
          if (r.width === 0 && r.height === 0) return;
          const over = Math.max(r.right - edge.right, edge.left - r.left);
          if (over > worst) { worst = over; culprit = el; }
        };
        // A column is its own edge; any other region is measured too.
        if (whole) consider(col.getBoundingClientRect(), col);
        col.querySelectorAll('*').forEach((el) => consider(el.getBoundingClientRect(), el));
        const range = document.createRange();
        const walker = document.createTreeWalker(col, NodeFilter.SHOW_TEXT);
        for (let n = walker.nextNode(); n; n = walker.nextNode()) {
          if (!n.textContent.trim()) continue;
          range.selectNodeContents(n);
          for (const r of range.getClientRects()) consider(r, n.parentElement);
        }
        if (worst > TOL) {
          violations.push({
            invariant: 'horizontal-overflow',
            page: i + 1,
            column: colClass,
            overflow_px: worst.toFixed(2),
            culprit: culprit ? (culprit.id || culprit.dataset?.id
              || culprit.tagName + '.' + String(culprit.className || '').split(' ')[0]) : null,
          });
        }
      });
    });

    return {
      violations,
      fill,
      rhythmMeasurements: rhythmMeasurements.map((m) => ({
        column: m.column,
        gap_px: m.gap_px.toFixed(2),
      })),
    };
  }, {
    tolerance: LAYOUT_CONSTANTS.INVARIANT_TOLERANCE_PX,
    expectedPageCount: expectedPageCount,
    expected,
  });
}

module.exports = { checkLayoutInvariants };
