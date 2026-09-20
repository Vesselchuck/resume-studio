/**
 * measure_dom.js — Extracts solveLayout input from the measurement HTML.
 *
 * Opens the measurement HTML in Playwright (caller does that), then runs
 * `extractMeasurements(page, maxPages)` to produce the data structure
 * that solve_layout.js consumes.
 *
 * IMPORTANT: This module measures TOTAL rendered heights by reading
 * `getBoundingClientRect()` on the actual container elements (each
 * block, each job, the sidebar, the main column). This correctly
 * captures CSS flex `gap` between children, which is invisible when
 * summing children's individual heights.
 *
 * Per-item heights are also measured (used by the solver to decide
 * split points). The solver re-derives the gap from the parent
 * container so it can split consistently:
 *   total_block = heading + sum(items) + (items.length - 1) * itemGap
 * And similarly for jobs (header + bullets + bullet-gap).
 *
 * Inter-block gaps and separators between blocks/sections are
 * exposed as separate fields so the solver can charge them
 * appropriately when placing multiple blocks on one page.
 */


/**
 * Page geometry — measured from the actual DOM, not assumed from
 * stylesheet values.
 *
 *   page1Capacity: vertical pixels available for body-grid content
 *                  on page 1 (i.e. below the resume header).
 *   pageNCapacity: same for pages 2+ (no resume header).
 *   sidebarBlockGap: flex gap between sidebar blocks (between
 *                    consecutive <section> children of <aside>).
 *   mainColumnSectionGap: flex gap between main-col sections.
 *   mainColSeparatorHeight: rendered height of <hr class="section-sep">
 *                           inside .main-col, INCLUDING its own margins
 *                           (which compensate to --section-rhythm).
 *   sidebarSeparatorHeight: same, for the sidebar's <hr>. Differs from
 *                           the main-col value because the rhythm calc
 *                           subtracts a different parent gap per context
 *                           (see styles/_layout.scss .section-sep rules).
 *
 * Inter-block spacing on a page = separatorHeight + 2 * blockGap
 * (the gap appears once on each side of the <hr>; use the column-
 * appropriate separator height).
 */
async function measurePageGeometry(page) {
  return await page.evaluate(() => {
    const pageEl = document.querySelector('.page');
    if (!pageEl) throw new Error('measurement HTML has no .page element');

    const cs = window.getComputedStyle(pageEl);
    const parsePx = (s) => {
      const n = parseFloat(s);
      return Number.isFinite(n) ? n : 0;
    };
    const padTop = parsePx(cs.paddingTop);
    const padBottom = parsePx(cs.paddingBottom);

    // Total pixel height of one printed page. Read from the CSS
    // variable --page-h (e.g. "11in") rather than hardcoding so any
    // stylesheet change auto-propagates.
    const rootStyle = window.getComputedStyle(document.documentElement);
    const pageHeightStr = rootStyle.getPropertyValue('--page-h').trim();
    const probe = document.createElement('div');
    probe.style.height = pageHeightStr;
    probe.style.position = 'absolute';
    probe.style.visibility = 'hidden';
    document.body.appendChild(probe);
    const pageHeightPx = probe.getBoundingClientRect().height;
    probe.remove();

    // Page-1 header offset: pixels between page content-area top
    // (= padding top) and body-grid top. Pages 2+ have no header.
    const bodyGridEl = pageEl.querySelector('.body-grid');
    const pageRect = pageEl.getBoundingClientRect();
    const gridRect = bodyGridEl.getBoundingClientRect();
    const headerOffset = gridRect.top - (pageRect.top + padTop);

    const page1Capacity = pageHeightPx - padTop - padBottom - headerOffset;
    const pageNCapacity = pageHeightPx - padTop - padBottom;

    // Per-column separator height. Today both columns produce the
    // same value because .section-sep uses one unified formula and
    // both .sidebar and .main-col have gap: 0 (see styles/_layout.scss
    // — the gap: 0 is a load-bearing invariant the solver relies on).
    // We still measure each column independently so a future per-
    // column rhythm override would propagate to the solver without
    // any code change here.
    const measureHr = (el) => {
      if (!el) return 0;
      const sepCS = window.getComputedStyle(el);
      return el.getBoundingClientRect().height
        + parsePx(sepCS.marginTop)
        + parsePx(sepCS.marginBottom);
    };
    const mainColSepEl = document.querySelector('.main-col hr.section-sep');
    const sidebarSepEl = document.querySelector('.sidebar hr.section-sep');
    const mainColSeparatorHeight = measureHr(mainColSepEl);
    const sidebarSeparatorHeight = measureHr(sidebarSepEl);

    // Flex gap on the sidebar/main containers. Computed style returns
    // the resolved pixel value (e.g. "16px") or "normal" (= 0).
    const sidebarEl = pageEl.querySelector('.sidebar');
    const mainColEl = pageEl.querySelector('.main-col');
    const sidebarBlockGap = sidebarEl ? parsePx(window.getComputedStyle(sidebarEl).rowGap) : 0;
    const mainColumnSectionGap = mainColEl ? parsePx(window.getComputedStyle(mainColEl).rowGap) : 0;

    return {
      page1Capacity,
      pageNCapacity,
      mainColSeparatorHeight,
      sidebarSeparatorHeight,
      sidebarBlockGap,
      mainColumnSectionGap,
    };
  });
}


/**
 * Sidebar measurements — one entry per [data-measure="block"].
 *
 * For each block:
 *   • totalHeight: getBoundingClientRect height (includes inner gaps)
 *   • headingHeight: heading element's box height + its margins
 *   • itemGap: parent's flex row-gap (gap between siblings inside
 *              the items container)
 *   • items: per-item heights (without inter-item gap)
 *
 * Invariant the solver relies on:
 *   totalHeight ≈ headingHeight + sum(items.height) +
 *                 (items.length - 1) * itemGap +
 *                 (heading-to-items gap if heading present)
 *
 * The "heading-to-items gap" is the .block flex-container's row-gap
 * between the heading child and the items-container child. We capture
 * it as `headingToItemsGap` so the solver can correctly compute heights
 * for both whole-block and continuation (no-heading) renderings.
 */
async function measureSidebar(page) {
  return await page.evaluate(() => {
    const parsePx = (s) => {
      const n = parseFloat(s);
      return Number.isFinite(n) ? n : 0;
    };
    const blocks = [];
    const blockEls = document.querySelectorAll('.sidebar [data-measure="block"]');
    for (const blockEl of blockEls) {
      const id = blockEl.dataset.id;
      const type = blockEl.dataset.type;

      // .block is itself a flex column with row-gap between heading
      // and items-container. Capture that gap.
      const blockCS = window.getComputedStyle(blockEl);
      const headingToItemsGap = parsePx(blockCS.rowGap);

      // Total rendered height of the entire block.
      const blockRect = blockEl.getBoundingClientRect();
      const totalHeight = blockRect.height
        + parsePx(blockCS.marginTop)
        + parsePx(blockCS.marginBottom);

      // Heading box.
      const headingEl = blockEl.querySelector('[data-measure="block-heading"]');
      let headingHeight = 0;
      if (headingEl) {
        const hRect = headingEl.getBoundingClientRect();
        const hcs = window.getComputedStyle(headingEl);
        headingHeight = hRect.height
          + parsePx(hcs.marginTop)
          + parsePx(hcs.marginBottom);
      }

      // Items container: its row-gap is the inter-item gap.
      let itemGap = 0;
      const itemsContainer = blockEl.querySelector('.plain-list, .details-list');
      if (itemsContainer) {
        const containerCS = window.getComputedStyle(itemsContainer);
        itemGap = parsePx(containerCS.rowGap);
      }

      const itemEls = blockEl.querySelectorAll('[data-measure="item"]');
      const items = [];
      for (const itemEl of itemEls) {
        const rect = itemEl.getBoundingClientRect();
        const ics = window.getComputedStyle(itemEl);
        items.push({
          height: rect.height
            + parsePx(ics.marginTop)
            + parsePx(ics.marginBottom),
        });
      }

      blocks.push({
        id, type, totalHeight,
        headingHeight, headingToItemsGap, itemGap, items,
      });
    }
    return blocks;
  });
}


/**
 * Main column measurements.
 *
 * Atomic sections (summary, education) report only `totalHeight` —
 * they don't split.
 *
 * Experience reports per-job heights with bullet-level granularity:
 *   • totalHeight: full experience section rendered height
 *   • headingHeight: "Work Experience" heading box (with margins)
 *   • headingToJobsGap: flex gap between heading and first job
 *   • jobGap: flex gap between consecutive jobs
 *   • jobs[]: array of { id, isGap, totalHeight, headerHeight,
 *                        headerToBulletsGap, bulletGap, bullets }
 *
 * Each job:
 *   totalHeight ≈ headerHeight + sum(bullets.height) +
 *                 (bullets.length - 1) * bulletGap +
 *                 (headerToBulletsGap if bullets present)
 */
async function measureMainColumn(page) {
  return await page.evaluate(() => {
    const parsePx = (s) => {
      const n = parseFloat(s);
      return Number.isFinite(n) ? n : 0;
    };
    function elBlockHeight(el) {
      const rect = el.getBoundingClientRect();
      const cs = window.getComputedStyle(el);
      return rect.height + parsePx(cs.marginTop) + parsePx(cs.marginBottom);
    }

    const sections = [];
    const sectionEls = document.querySelectorAll('.main-col [data-measure="section"]');

    for (const sEl of sectionEls) {
      const sectionType = sEl.dataset.sectionType;
      const sCS = window.getComputedStyle(sEl);

      const headingEl = sEl.querySelector('[data-measure="section-heading"]');
      const headingHeight = headingEl ? elBlockHeight(headingEl) : 0;
      const totalHeight = elBlockHeight(sEl);

      if (sectionType === 'summary' || sectionType === 'education') {
        // Atomic — solver doesn't split these.
        sections.push({ kind: sectionType, headingHeight, totalHeight });
      } else if (sectionType === 'experience') {
        // .block (.experience) is a flex column with row-gap between
        // heading and consecutive jobs. CSS ALSO defines an
        // adjacent-sibling rule `.experience .job + .job { margin-block-start }`
        // to add EXTRA spacing between consecutive jobs only (so the
        // heading-to-first-job distance can match other sections while
        // inter-job distance is larger). We capture that extra margin
        // by reading the second job's marginTop and adding it to the
        // flex gap.
        const sectionRowGap = parsePx(sCS.rowGap);
        const headingToJobsGap = sectionRowGap;

        const jobEls = sEl.querySelectorAll('[data-measure="job"]');
        // Inter-job gap = parent flex gap + adjacent-sibling margin
        // applied to .job + .job. We read marginTop on the second job
        // if there is one; first job has no preceding sibling-margin.
        let extraJobMarginTop = 0;
        if (jobEls.length >= 2) {
          const secondJobCS = window.getComputedStyle(jobEls[1]);
          extraJobMarginTop = parsePx(secondJobCS.marginTop);
        }
        const jobGap = sectionRowGap + extraJobMarginTop;

        const jobs = [];
        for (const jobEl of jobEls) {
          const id = jobEl.dataset.id;
          const isGap = jobEl.classList.contains('job--gap');
          const jobCS = window.getComputedStyle(jobEl);
          const jobTotalHeight = elBlockHeight(jobEl);

          const headerEl = jobEl.querySelector('[data-measure="job-header"]');
          const headerHeight = headerEl ? elBlockHeight(headerEl) : 0;

          // Header-to-bullets gap = .job flex gap PLUS .bullets'
          // margin-block-start (the CSS layers both for visual tuning).
          // We capture the actual visible spacing by summing them.
          const bulletsList = jobEl.querySelector('.bullets');
          const jobFlexGap = parsePx(jobCS.rowGap);
          const bulletsMarginTop = bulletsList
            ? parsePx(window.getComputedStyle(bulletsList).marginTop)
            : 0;
          const headerToBulletsGap = jobFlexGap + bulletsMarginTop;
          const bulletGap = bulletsList
            ? parsePx(window.getComputedStyle(bulletsList).rowGap)
            : 0;

          const bulletEls = jobEl.querySelectorAll('[data-measure="bullet"]');
          const bullets = [];
          for (const bEl of bulletEls) {
            bullets.push({ height: elBlockHeight(bEl) });
          }
          jobs.push({
            id, isGap, totalHeight: jobTotalHeight,
            headerHeight, headerToBulletsGap, bulletGap, bullets,
          });
        }

        sections.push({
          kind: 'experience', totalHeight,
          headingHeight, headingToJobsGap, jobGap, jobs,
        });
      } else {
        throw new Error(`unknown data-section-type: ${sectionType}`);
      }
    }

    return sections;
  });
}


/**
 * Top-level: extract everything solveLayout needs.
 *
 * @param {playwright.Page} page  Already navigated to dist/index.html
 *                                in measurement mode.
 * @param {number} maxPages       From meta.maxPages in resume.yml.
 */
async function extractMeasurements(page, maxPages) {
  const pageGeometry = await measurePageGeometry(page);
  const sidebar = await measureSidebar(page);
  const mainColumn = await measureMainColumn(page);
  return { pageGeometry, maxPages, sidebar, mainColumn };
}


module.exports = {
  extractMeasurements,
};
