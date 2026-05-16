/**
 * solve_layout.js — Pure layout solver.
 *
 * Takes measurements (with explicit gap accounting) plus page
 * geometry and produces a `placement` object specifying which
 * content lives on which page (with split points for bridging).
 *
 * No I/O, no Playwright, no fs. Easy to unit test.
 *
 * INPUT shape:
 *   {
 *     pageGeometry: {
 *       page1Capacity: number,
 *       pageNCapacity: number,
 *       mainColSeparatorHeight: number,  // <hr> inside .main-col
 *       sidebarSeparatorHeight: number,  // <hr> inside .sidebar
 *       sidebarBlockGap: number,         // flex gap inside <aside>
 *       mainColumnSectionGap: number,    // flex gap inside <main-col>
 *     },
 *     maxPages: number,
 *     sidebar: [
 *       { id, type, totalHeight,
 *         headingHeight, headingToItemsGap, itemGap,
 *         items: [{ height }] },
 *       ...
 *     ],
 *     mainColumn: [
 *       { kind: 'summary'|'education', headingHeight, totalHeight },
 *       { kind: 'experience', totalHeight,
 *         headingHeight, headingToJobsGap, jobGap,
 *         jobs: [
 *           { id, isGap, totalHeight,
 *             headerHeight, headerToBulletsGap, bulletGap,
 *             bullets: [{ height }] },
 *           ...
 *         ]
 *       },
 *     ],
 *   }
 *
 * Inter-block cost (between consecutive entries on the same page):
 *   sidebar:    2 * sidebarBlockGap + sidebarSeparatorHeight
 *   main col:   2 * mainColumnSectionGap + mainColSeparatorHeight
 * The flex gap appears once on each side of the <hr>.
 *
 * Note: today both `*Gap` measurements are 0 because styles/_layout.scss
 * sets gap: 0 on .sidebar and .main-col as a load-bearing invariant
 * (see the comment there). The `2 * gap` term is NOT dead code — it
 * is graceful coverage for a future where someone introduces a
 * nonzero column gap. Removing the term would silently underestimate
 * inter-block cost in that scenario.
 *
 * OUTPUT shape (`solveLayout(...)` / `combinePages(...)` return value):
 *   {
 *     pages: [
 *       {
 *         page_number: number,            // 1-indexed
 *         sidebar_blocks: [
 *           {
 *             block_id: string,           // references data['sidebar']['blocks'][].id
 *             continuation: boolean,      // true → render without a heading (block began earlier)
 *             items_offset: number,       // start index into the source items[]
 *             items_limit: number | null, // null = render to end; otherwise this many items
 *           },
 *           ...
 *         ],
 *         main_sections: [
 *           // 'summary' / 'education' sections are atomic — one page, no bridging:
 *           { type: 'summary' | 'education', continuation_of: null },
 *
 *           // 'experience' section — may bridge across pages, may host job continuations:
 *           {
 *             type: 'experience',
 *             continuation_of: 'experience' | null, // non-null = section's heading was on an earlier page
 *             jobs: [
 *               {
 *                 job_id: string,             // references experience.jobs[].id
 *                 continuation: boolean,      // true → job's bullets continue from previous page
 *                 bullets_offset: number,     // start index into the source bullets[]
 *                 bullets_limit: number | null, // null = render to end; otherwise this many bullets
 *               },
 *               ...
 *             ],
 *           },
 *         ],
 *       },
 *       ...
 *     ],
 *   }
 *
 * `build.py` reads this from `dist/placement.json` and adds a derived
 * `sidebar_aria` string field to each page (joined headings, for the
 * page's <aside aria-label>) before passing it to the template. The
 * solver itself never emits `sidebar_aria`.
 */

class SolverError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'SolverError';
    Object.assign(this, details);
  }
}

// Bridging policy — the widow/orphan rules the solver enforces when
// content doesn't fit on a single page. Module-scoped (not parameters)
// because they encode the design's policy, not per-build config.
// Tests reference these by name; see test_solve_layout.js.

// A bridged job must keep at least this many bullets on each page it
// spans. With the value at 1: a job whose bullets don't all fit can
// leave any non-empty suffix for the next page, but never a header
// alone with zero bullets, and never a single trailing bullet stranded
// from its header. Raising to 2 would refuse single-bullet tails.
const MIN_JOB_BULLETS_ON_PAGE = 1;

// A bridged sidebar 'list' block must keep at least this many items on
// the page where its heading lives (the "origin"). Prevents a lonely
// heading + 1-2 items from appearing on one page with the bulk of the
// list on the next — visually that reads as a labelling mistake rather
// than a deliberate split. 3 is the minimum that looks intentional.
const MIN_SIDEBAR_ITEMS_ON_ORIGIN = 3;

// The receiving page of a bridged sidebar block must hold at least
// this many items. With the value at 1: a single trailing item is
// allowed on the next page (it inherits no heading — it just continues
// the previous block silently). 'details' blocks never bridge, so this
// only governs 'list' blocks.
const MIN_SIDEBAR_ITEMS_ON_RECEIVER = 1;

// Cascade-prevention guard: if the same content unit is pushed to the
// next page this many consecutive times without making progress, the
// solver gives up with a clear error instead of looping. 2 is enough
// to absorb legitimate "didn't fit, retry on fresh page" cases while
// catching pathological inputs (e.g. a single bullet taller than a
// whole page).
const MAX_CONSECUTIVE_PUSHES = 2;


// ─── Sidebar block height helpers ────────────────────────────────

/**
 * Height of the rendered sidebar-block portion that contains items
 * [offset .. offset+limit), accounting for heading (only if not
 * continuation) and inter-item gap.
 *
 * Equals what the rendered `.block` element will measure to in the
 * final HTML, modulo sub-pixel rounding.
 */
function sidebarBlockHeight(block, offset, count, isContinuation) {
  if (count === 0) return 0;
  const heading = isContinuation ? 0 : block.headingHeight;
  const headingGap = (!isContinuation && count > 0) ? block.headingToItemsGap : 0;
  const itemSlice = block.items.slice(offset, offset + count);
  const itemTotal = itemSlice.reduce((s, it) => s + it.height, 0);
  const itemGapTotal = (count - 1) * block.itemGap;
  return heading + headingGap + itemTotal + itemGapTotal;
}


// ─── Job height helpers ──────────────────────────────────────────

/**
 * Height of a job rendered with a slice of bullets. Mirrors what the
 * rendered `.job` element will measure to in the final HTML.
 */
function jobHeight(job, offset, count, isContinuation) {
  if (job.isGap) return job.headerHeight;
  const header = isContinuation ? 0 : job.headerHeight;
  const headerGap = (!isContinuation && count > 0) ? job.headerToBulletsGap : 0;
  const slice = job.bullets.slice(offset, offset + count);
  const bulletTotal = slice.reduce((s, b) => s + b.height, 0);
  const bulletGapTotal = count > 1 ? (count - 1) * job.bulletGap : 0;
  return header + headerGap + bulletTotal + bulletGapTotal;
}


// ─── Shared "max items that fit" helper ──────────────────────────

/**
 * Find the largest k (number of items from `offset`) such that
 * heightFn(container, offset, k, isContinuation) fits in `available`.
 *
 * Returns 0 if no items fit. Generic over the container shape:
 * pass `sidebarBlockHeight` for sidebar list blocks, `jobHeight` for
 * job bullets. The height function must be monotonic in `count` for
 * the early-break to be correct (both shipped height fns are — adding
 * an item only increases the height by that item's slice + gap).
 */
function maxFitting(heightFn, container, offset, remaining, isContinuation, available) {
  if (remaining === 0) return 0;
  let k = 0;
  for (let i = 1; i <= remaining; i++) {
    if (heightFn(container, offset, i, isContinuation) > available) break;
    k = i;
  }
  return k;
}


// ─── Shared page-management helpers ──────────────────────────────
//
// Both solvers walk a stream of entries left-to-right, placing each
// entry on the current page or pushing it to a new one. Two patterns
// repeat verbatim between solveSidebar and solveMainColumn and are
// extracted here:
//
//   • Push-tracker — detects non-progressing placement (same entry
//     pushed forward MAX_CONSECUTIVE_PUSHES times in a row without
//     fitting) so the solver can fail with a clear diagnostic instead
//     of looping forever. Each solver uses entry-specific signatures
//     (block_id@offset, job_id@bullets_offset, etc.), so the tracker
//     is parameterized by signature string, not by entry shape.
//
//   • Max-pages guard — both solvers throw a structurally-identical
//     SolverError when they'd otherwise create more pages than the
//     user's maxPages cap allows. The only thing that varies is the
//     column tag in the error message and details.
//
// Page-state itself (currentPage shape, capacity transition, the
// activeJobs/activeSection book-keeping for main column) is NOT
// extracted: those genuinely differ between columns, and a wrapping
// abstraction would just push the divergence through callbacks
// without removing it.

/**
 * Create a fresh push-tracker.
 *
 * `push(signature)` records a "push to next page" attempt. Returns
 * true iff this is now beyond MAX_CONSECUTIVE_PUSHES (caller should
 * throw a non-progress error). `reset()` is called by the caller
 * after a successful placement.
 */
function makePushTracker() {
  let consecutive = 0;
  let lastSignature = null;
  return {
    push(signature) {
      if (signature === lastSignature) consecutive++;
      else { consecutive = 1; lastSignature = signature; }
      return consecutive > MAX_CONSECUTIVE_PUSHES;
    },
    reset() {
      consecutive = 0;
    },
  };
}

/**
 * Throw a SolverError if adding another page would exceed `maxPages`.
 * Called from each solver's newPage() helper, BEFORE creating the
 * new page. `column` is 'sidebar' or 'main' — appears in the error
 * message and details so callers can distinguish which column ran
 * out of space.
 */
function enforceMaxPages(pages, maxPages, column) {
  if (pages.length >= maxPages) {
    throw new SolverError(
      `${column === 'main' ? 'main column' : column} content exceeds maxPages (${maxPages})`,
      { column, pages_filled: pages.length },
    );
  }
}


// ─── Sidebar solver ──────────────────────────────────────────────

function solveSidebar(blocks, geometry, maxPages) {
  const interBlockCost = 2 * geometry.sidebarBlockGap + geometry.sidebarSeparatorHeight;

  const pages = [];
  // Each slot: { block, items_offset, items_remaining }
  const queue = blocks.map((b) => ({
    block: b,
    items_offset: 0,
    items_remaining: b.items.length,
  }));

  let currentPage = { entries: [] };
  let currentCapacity = geometry.page1Capacity;
  let currentUsed = 0;
  pages.push(currentPage);

  const pushTracker = makePushTracker();

  const newPage = () => {
    enforceMaxPages(pages, maxPages, 'sidebar');
    currentPage = { entries: [] };
    currentCapacity = geometry.pageNCapacity;
    currentUsed = 0;
    pages.push(currentPage);
  };

  while (queue.length > 0) {
    const slot = queue[0];
    const { block, items_offset, items_remaining } = slot;
    const isContinuation = items_offset > 0;

    // Cost of putting this entry on the page: block height + inter-block
    // overhead if there's already content on the page.
    const wholeBlockHeight = sidebarBlockHeight(
      block, items_offset, items_remaining, isContinuation,
    );
    const overhead = currentPage.entries.length > 0 ? interBlockCost : 0;
    const wholeWithOverhead = wholeBlockHeight + overhead;
    const available = currentCapacity - currentUsed;

    // Case 1: whole entry fits.
    if (wholeWithOverhead <= available) {
      currentPage.entries.push({
        block_id: block.id,
        continuation: isContinuation,
        items_offset,
        items_limit: null,
      });
      currentUsed += wholeWithOverhead;
      queue.shift();
      pushTracker.reset();
      continue;
    }

    // Case 2: try to bridge.
    const minOnOrigin = isContinuation
      ? MIN_SIDEBAR_ITEMS_ON_RECEIVER
      : MIN_SIDEBAR_ITEMS_ON_ORIGIN;
    const isSplittable = block.type === 'list';

    if (isSplittable && items_remaining > 0) {
      const availForBlock = available - overhead;
      const bestK = maxFitting(
        sidebarBlockHeight,
        block, items_offset, items_remaining, isContinuation, availForBlock,
      );
      const tailRemaining = items_remaining - bestK;
      if (bestK >= minOnOrigin && tailRemaining >= MIN_SIDEBAR_ITEMS_ON_RECEIVER) {
        currentPage.entries.push({
          block_id: block.id,
          continuation: isContinuation,
          items_offset,
          items_limit: bestK,
        });
        currentUsed += sidebarBlockHeight(block, items_offset, bestK, isContinuation)
          + overhead;
        slot.items_offset = items_offset + bestK;
        slot.items_remaining = tailRemaining;
        newPage();
        pushTracker.reset();
        continue;
      }
    }

    // Case 3: doesn't fit and can't bridge — push to next page.
    if (pushTracker.push(`${block.id}@${items_offset}`)) {
      throw new SolverError(
        `sidebar block '${block.id}' (offset ${items_offset}) cannot fit on any page`,
        { column: 'sidebar', block_id: block.id, items_offset,
          remaining_height: wholeBlockHeight },
      );
    }
    if (currentPage.entries.length === 0) {
      throw new SolverError(
        `sidebar block '${block.id}' (offset ${items_offset}) is taller than a page`,
        { column: 'sidebar', block_id: block.id, items_offset,
          height: wholeBlockHeight, capacity: currentCapacity },
      );
    }
    newPage();
  }

  return pages;
}


// ─── Main column solver ──────────────────────────────────────────
//
// Operates on a stream of "units" derived from the input sections.
// Atomic sections (summary, education) become a single unit. The
// experience section becomes one heading-unit followed by one job-unit
// per job. The solver walks units left-to-right, placing them and
// charging an inter-section cost when a NEW section starts on a page
// that already has prior section content.

function solveMainColumn(sections, geometry, maxPages) {
  const interSectionCost = 2 * geometry.mainColumnSectionGap + geometry.mainColSeparatorHeight;

  // Build units.
  const units = [];
  for (const s of sections) {
    if (s.kind === 'summary' || s.kind === 'education') {
      units.push({
        kind: 'atomic-section',
        section_type: s.kind,
        height: s.totalHeight,
      });
    } else if (s.kind === 'experience') {
      units.push({
        kind: 'experience-heading',
        height: s.headingHeight,
        headingToJobsGap: s.headingToJobsGap,
        jobGap: s.jobGap,
      });
      for (const j of s.jobs) {
        units.push({
          kind: 'job',
          job: j,
          jobGap: s.jobGap,                   // inter-job gap within a section
          headingToJobsGap: s.headingToJobsGap, // for first-job-on-section
          bullets_offset: 0,
          bullets_remaining: j.bullets.length,
        });
      }
    } else {
      throw new SolverError(`unknown section kind: ${s.kind}`);
    }
  }

  const pages = [];
  let currentPage = {
    entries: [],
    activeSection: null,                       // 'experience' | null
    activeSectionContinuationOf: null,
    activeJobs: [],
    // True iff at least one job has been placed under the active section
    // on this page; controls whether the next job is preceded by jobGap
    // (vs headingToJobsGap if it's the first job, or 0 if continuation).
    hasFirstJobInActiveSection: false,
  };
  let currentCapacity = geometry.page1Capacity;
  let currentUsed = 0;
  pages.push(currentPage);

  const pushTracker = makePushTracker();

  const finalizeActiveSection = () => {
    if (currentPage.activeSection === 'experience' && currentPage.activeJobs.length > 0) {
      currentPage.entries.push({
        type: 'experience',
        continuation_of: currentPage.activeSectionContinuationOf,
        jobs: currentPage.activeJobs,
      });
    }
    currentPage.activeSection = null;
    currentPage.activeSectionContinuationOf = null;
    currentPage.activeJobs = [];
    currentPage.hasFirstJobInActiveSection = false;
  };

  const newPage = (continuationOfSection = null) => {
    finalizeActiveSection();
    enforceMaxPages(pages, maxPages, 'main');
    currentPage = {
      entries: [],
      activeSection: continuationOfSection,
      activeSectionContinuationOf: continuationOfSection,
      activeJobs: [],
      hasFirstJobInActiveSection: false,
    };
    currentCapacity = geometry.pageNCapacity;
    currentUsed = 0;
    pages.push(currentPage);
  };

  let unitIndex = 0;
  while (unitIndex < units.length) {
    const unit = units[unitIndex];
    const available = currentCapacity - currentUsed;

    if (unit.kind === 'atomic-section') {
      // Inter-section cost iff there's already prior section content
      // on this page (entries OR active jobs).
      const sepCost = (currentPage.entries.length > 0 ||
                       currentPage.activeJobs.length > 0)
        ? interSectionCost : 0;
      const total = sepCost + unit.height;
      if (total <= available) {
        finalizeActiveSection();
        currentPage.entries.push({
          type: unit.section_type,
          continuation_of: null,
        });
        currentUsed += total;
        unitIndex++;
        pushTracker.reset();
        continue;
      }
      if (pushTracker.push(`atomic:${unit.section_type}`) ||
          (currentPage.entries.length === 0 && currentPage.activeJobs.length === 0)) {
        throw new SolverError(
          `${unit.section_type} section is too tall to fit on any page`,
          { column: 'main', section_type: unit.section_type, height: unit.height },
        );
      }
      newPage();
      continue;
    }

    if (unit.kind === 'experience-heading') {
      // Heading must be placed with at least its first job's minimum.
      const nextJobUnit = units[unitIndex + 1];
      if (!nextJobUnit || nextJobUnit.kind !== 'job') {
        throw new SolverError('experience heading not followed by any job');
      }
      const j = nextJobUnit.job;
      const minJobHeight = j.isGap
        ? j.headerHeight
        : j.headerHeight + j.headerToBulletsGap + j.bullets[0].height;
      const sepCost = (currentPage.entries.length > 0 ||
                       currentPage.activeJobs.length > 0)
        ? interSectionCost : 0;
      const minTotal = sepCost + unit.height + unit.headingToJobsGap + minJobHeight;

      if (minTotal <= available) {
        finalizeActiveSection();
        currentPage.activeSection = 'experience';
        currentPage.activeSectionContinuationOf = null;
        // Charge: separator + heading. The first job will charge
        // headingToJobsGap when it processes.
        currentUsed += sepCost + unit.height;
        unitIndex++;
        pushTracker.reset();
        continue;
      }
      if (pushTracker.push('experience-heading') ||
          (currentPage.entries.length === 0 && currentPage.activeJobs.length === 0)) {
        throw new SolverError(
          `experience heading + first job '${j.id}' (min height ${minJobHeight}px + heading ${unit.height}px) cannot fit on any page (capacity ${currentCapacity}px)`,
          {
            column: 'main',
            job_id: j.id,
            min_job_height: minJobHeight,
            heading_height: unit.height,
            capacity: currentCapacity,
          },
        );
      }
      newPage();
      continue;
    }

    if (unit.kind === 'job') {
      const j = unit.job;
      const isContinuation = unit.bullets_offset > 0;
      const inActiveExperience = currentPage.activeSection === 'experience'
        || currentPage.activeSectionContinuationOf === 'experience';

      // Starting an experience-continuation section on this page (no
      // heading)? That's a section start, charge inter-section cost.
      if (!inActiveExperience) {
        const sepCost = (currentPage.entries.length > 0)
          ? interSectionCost : 0;
        // Ensure space for at least the minimum (gap-job header, or
        // header + first bullet for regular jobs). If insufficient,
        // push to next page.
        const minHere = j.isGap
          ? j.headerHeight
          : (isContinuation
              ? j.bullets[unit.bullets_offset].height
              : j.headerHeight + j.headerToBulletsGap + j.bullets[0].height);
        if (sepCost + minHere > available) {
          if (pushTracker.push(`start-exp-cont:${j.id}@${unit.bullets_offset}`) ||
              currentPage.entries.length === 0) {
            throw new SolverError(
              `cannot start experience continuation for '${j.id}' on a page`,
              { column: 'main', job_id: j.id, bullets_offset: unit.bullets_offset },
            );
          }
          newPage('experience');
          continue;
        }
        currentPage.activeSection = 'experience';
        currentPage.activeSectionContinuationOf = 'experience';
        currentUsed += sepCost;
      }

      // Inter-job gap: 0 if first job in active section, else jobGap.
      // For a section that JUST got its heading, hasFirstJobInActiveSection
      // is false → charge headingToJobsGap.
      // For a continuation section (no heading), the first job has no
      // preceding gap.
      let gapBefore;
      if (!currentPage.hasFirstJobInActiveSection) {
        gapBefore = (currentPage.activeSectionContinuationOf === 'experience')
          ? 0  // continuation: first job has no leading gap
          : unit.headingToJobsGap;
      } else {
        gapBefore = unit.jobGap;
      }

      const available2 = currentCapacity - currentUsed - gapBefore;

      if (j.isGap) {
        if (j.headerHeight <= available2) {
          currentPage.activeJobs.push({
            job_id: j.id, continuation: false,
            bullets_offset: 0, bullets_limit: null,
          });
          currentUsed += gapBefore + j.headerHeight;
          currentPage.hasFirstJobInActiveSection = true;
          unitIndex++;
          pushTracker.reset();
          continue;
        }
        // Gap job doesn't fit → push to next page (continuation context).
        if (pushTracker.push(`gap:${j.id}`) ||
            (currentPage.entries.length === 0 && currentPage.activeJobs.length === 0)) {
          throw new SolverError(
            `gap job '${j.id}' cannot fit on any page`,
            { column: 'main', job_id: j.id },
          );
        }
        newPage('experience');
        continue;
      }

      // Regular job: try whole, then bridge.
      const wholeHeight = jobHeight(j, unit.bullets_offset, unit.bullets_remaining, isContinuation);
      if (wholeHeight <= available2) {
        currentPage.activeJobs.push({
          job_id: j.id, continuation: isContinuation,
          bullets_offset: unit.bullets_offset, bullets_limit: null,
        });
        currentUsed += gapBefore + wholeHeight;
        currentPage.hasFirstJobInActiveSection = true;
        unitIndex++;
        pushTracker.reset();
        continue;
      }

      // Try to bridge.
      const bestK = maxFitting(
        jobHeight,
        j, unit.bullets_offset, unit.bullets_remaining, isContinuation, available2,
      );
      const tailRemaining = unit.bullets_remaining - bestK;
      const bridgeOK = bestK >= MIN_JOB_BULLETS_ON_PAGE
        && tailRemaining >= MIN_JOB_BULLETS_ON_PAGE;
      if (bridgeOK) {
        const placedHeight = jobHeight(j, unit.bullets_offset, bestK, isContinuation);
        currentPage.activeJobs.push({
          job_id: j.id, continuation: isContinuation,
          bullets_offset: unit.bullets_offset, bullets_limit: bestK,
        });
        currentUsed += gapBefore + placedHeight;
        currentPage.hasFirstJobInActiveSection = true;
        unit.bullets_offset += bestK;
        unit.bullets_remaining = tailRemaining;
        newPage('experience');
        pushTracker.reset();
        continue;
      }

      // Can't bridge — push whole to next page.
      if (pushTracker.push(`job:${j.id}@${unit.bullets_offset}`) ||
          (currentPage.entries.length === 0 && currentPage.activeJobs.length === 0)) {
        throw new SolverError(
          `job '${j.id}' is taller than a page`,
          { column: 'main', job_id: j.id, height: wholeHeight,
            capacity: currentCapacity },
        );
      }
      newPage('experience');
      continue;
    }

    throw new SolverError(`unknown unit kind: ${unit.kind}`);
  }

  finalizeActiveSection();
  return pages;
}


function combinePages(sidebarPages, mainColumnPages) {
  const total = Math.max(sidebarPages.length, mainColumnPages.length);
  const pages = [];
  for (let i = 0; i < total; i++) {
    const sidebar = sidebarPages[i] || { entries: [] };
    const main = mainColumnPages[i] || { entries: [] };
    pages.push({
      page_number: i + 1,
      sidebar_blocks: sidebar.entries,
      main_sections: main.entries,
    });
  }
  return { pages };
}


function solveLayout(input) {
  const { pageGeometry, maxPages, sidebar, mainColumn } = input;
  if (!pageGeometry || typeof pageGeometry.page1Capacity !== 'number') {
    throw new SolverError('input.pageGeometry.page1Capacity is required (number)');
  }
  if (!Number.isInteger(maxPages) || maxPages < 1) {
    throw new SolverError(`input.maxPages must be a positive integer; got ${maxPages}`);
  }

  const sidebarPages = solveSidebar(sidebar, pageGeometry, maxPages);
  const mainPages = solveMainColumn(mainColumn, pageGeometry, maxPages);

  const total = Math.max(sidebarPages.length, mainPages.length);
  if (total > maxPages) {
    throw new SolverError(
      `total page count ${total} exceeds maxPages ${maxPages}`,
      { sidebar_pages: sidebarPages.length, main_pages: mainPages.length },
    );
  }

  return combinePages(sidebarPages, mainPages);
}


module.exports = {
  solveLayout,
  solveSidebar,
  solveMainColumn,
  combinePages,
  SolverError,
  // Helpers exported for tests.
  sidebarBlockHeight,
  jobHeight,
  // Bridging-policy tunables exported so test assertions can reference
  // the names instead of magic numbers. Rationale at the definition site.
  MIN_JOB_BULLETS_ON_PAGE,
  MIN_SIDEBAR_ITEMS_ON_ORIGIN,
  MIN_SIDEBAR_ITEMS_ON_RECEIVER,
  MAX_CONSECUTIVE_PUSHES,
};
