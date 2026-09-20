/**
 * check_layout.js — Layout invariant assertions for the rendered HTML.
 *
 * Exports:
 *   • checkLayoutInvariants(page, options) — async function that takes
 *     a Playwright Page (already navigated to the resume HTML) plus an
 *     optional { expectedPageCount } override (defaults to
 *     LAYOUT_CONSTANTS.EXPECTED_PAGE_COUNT). Returns { ok, violations,
 *     rhythmMeasurements }. Does not throw.
 *
 * The four invariants checked:
 *   1. EXPECTED_PAGE_COUNT .page elements exist.
 *   2. Each .body-grid's bottom edge equals (page bottom − padding-bottom),
 *      so the column divider terminates at the print margin.
 *   3. Section rhythm — visible spacing between sibling .block elements
 *      separated by .section-sep — is identical across all instances
 *      (sidebar and main column).
 *   4. No content overflow — each page's content fits within its
 *      padding-bottom. The .page elements use overflow:hidden, so a
 *      placement bug would otherwise silently clip content (invisible
 *      until someone inspects the PDF).
 */

const LAYOUT_CONSTANTS = Object.freeze({
  // US Letter at 96 DPI: 8.5in = 816 px, 11in = 1056 px (exact since
  // 1in = 96 px in CSS).
  LETTER_VIEWPORT_W: 816,
  LETTER_VIEWPORT_H: 1056,
  // Tolerance (px) for layout-invariant geometry checks. 1 px allows for
  // browser sub-pixel rounding without false positives.
  INVARIANT_TOLERANCE_PX: 1,
  // Number of .page elements expected. Catches an accidentally-deleted
  // page that would otherwise produce a 1-page PDF without complaint.
  EXPECTED_PAGE_COUNT: 2,
});

async function checkLayoutInvariants(page, options = {}) {
  // expectedPageCount: how many .page elements the caller expects.
  // Defaults to LAYOUT_CONSTANTS.EXPECTED_PAGE_COUNT for backward
  // compatibility; resume.js passes the value from the placement.
  const expectedPageCount = options.expectedPageCount
    ?? LAYOUT_CONSTANTS.EXPECTED_PAGE_COUNT;

  await page.emulateMedia({ media: 'print' });
  await page.setViewportSize({
    width: LAYOUT_CONSTANTS.LETTER_VIEWPORT_W,
    height: LAYOUT_CONSTANTS.LETTER_VIEWPORT_H,
  });

  const result = await page.evaluate((cfg) => {
    const violations = [];
    const TOL = cfg.tolerance;

    // Invariant 1: page elements exist and count is correct.
    const pages = document.querySelectorAll('.page');
    if (pages.length === 0) {
      violations.push({ invariant: 'page-presence', error: 'no .page elements found' });
      return { violations, rhythmMeasurements: [] };
    }
    if (pages.length !== cfg.expectedPageCount) {
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
    pages.forEach((p, i) => {
      const pageRect = p.getBoundingClientRect();
      const cs = window.getComputedStyle(p);
      const padBottom = parseFloat(cs.paddingBottom);
      const contentBottom = pageRect.bottom - padBottom;
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
    });

    return {
      violations,
      rhythmMeasurements: rhythmMeasurements.map((m) => ({
        column: m.column,
        gap_px: m.gap_px.toFixed(2),
      })),
    };
  }, {
    tolerance: LAYOUT_CONSTANTS.INVARIANT_TOLERANCE_PX,
    expectedPageCount: expectedPageCount,
  });

  await page.emulateMedia({ media: null });

  return {
    ok: result.violations.length === 0,
    violations: result.violations,
    rhythmMeasurements: result.rhythmMeasurements,
  };
}

module.exports = { checkLayoutInvariants };
