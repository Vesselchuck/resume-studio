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
 *       separatorHeight: number,        // <hr class="section-sep">
 *       sidebarBlockGap: number,        // flex gap inside <aside>
 *       mainColumnSectionGap: number,   // flex gap inside <main-col>
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
 *   sidebar:    2 * sidebarBlockGap + separatorHeight
 *   main col:   2 * mainColumnSectionGap + separatorHeight
 * The flex gap appears once on each side of the <hr>.
 *
 * OUTPUT shape: see combinePages() — same as before.
 */

class SolverError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'SolverError';
    Object.assign(this, details);
  }
}

const MIN_JOB_BULLETS_ON_PAGE = 1;
const MIN_SIDEBAR_ITEMS_ON_ORIGIN = 3;
const MIN_SIDEBAR_ITEMS_ON_RECEIVER = 1;
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


/**
 * Find the largest k (number of items from `offset`) such that the
 * resulting block portion fits in `available` pixels.
 *
 * Returns 0 if no items fit.
 */
function maxFittingSidebarItems(block, offset, remaining, isContinuation, available) {
  if (remaining === 0) return 0;
  let k = 0;
  for (let i = 1; i <= remaining; i++) {
    const h = sidebarBlockHeight(block, offset, i, isContinuation);
    if (h > available) break;
    k = i;
  }
  return k;
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


function maxFittingBullets(job, offset, remaining, isContinuation, available) {
  if (remaining === 0) return 0;
  let k = 0;
  for (let i = 1; i <= remaining; i++) {
    const h = jobHeight(job, offset, i, isContinuation);
    if (h > available) break;
    k = i;
  }
  return k;
}


// ─── Sidebar solver ──────────────────────────────────────────────

function solveSidebar(blocks, geometry, maxPages) {
  const interBlockCost = 2 * geometry.sidebarBlockGap + geometry.separatorHeight;

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

  let consecutivePushes = 0;
  let lastPushedSignature = null;

  const newPage = () => {
    if (pages.length >= maxPages) {
      throw new SolverError(
        `sidebar content exceeds maxPages (${maxPages})`,
        { column: 'sidebar', pages_filled: pages.length },
      );
    }
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
      consecutivePushes = 0;
      continue;
    }

    // Case 2: try to bridge.
    const minOnOrigin = isContinuation
      ? MIN_SIDEBAR_ITEMS_ON_RECEIVER
      : MIN_SIDEBAR_ITEMS_ON_ORIGIN;
    const isSplittable = block.type === 'list';

    if (isSplittable && items_remaining > 0) {
      const availForBlock = available - overhead;
      const bestK = maxFittingSidebarItems(
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
        consecutivePushes = 0;
        continue;
      }
    }

    // Case 3: doesn't fit and can't bridge — push to next page.
    const signature = `${block.id}@${items_offset}`;
    if (signature === lastPushedSignature) consecutivePushes++;
    else { consecutivePushes = 1; lastPushedSignature = signature; }
    if (consecutivePushes > MAX_CONSECUTIVE_PUSHES) {
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
  const interSectionCost = 2 * geometry.mainColumnSectionGap + geometry.separatorHeight;

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

  let consecutivePushes = 0;
  let lastPushedSignature = null;

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
    if (pages.length >= maxPages) {
      throw new SolverError(
        `main column content exceeds maxPages (${maxPages})`,
        { column: 'main', pages_filled: pages.length },
      );
    }
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
        consecutivePushes = 0;
        continue;
      }
      const sig = `atomic:${unit.section_type}`;
      if (sig === lastPushedSignature) consecutivePushes++;
      else { consecutivePushes = 1; lastPushedSignature = sig; }
      if (consecutivePushes > MAX_CONSECUTIVE_PUSHES ||
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
        consecutivePushes = 0;
        continue;
      }
      const sig = 'experience-heading';
      if (sig === lastPushedSignature) consecutivePushes++;
      else { consecutivePushes = 1; lastPushedSignature = sig; }
      if (consecutivePushes > MAX_CONSECUTIVE_PUSHES ||
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
          const sig = `start-exp-cont:${j.id}@${unit.bullets_offset}`;
          if (sig === lastPushedSignature) consecutivePushes++;
          else { consecutivePushes = 1; lastPushedSignature = sig; }
          if (consecutivePushes > MAX_CONSECUTIVE_PUSHES ||
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
          consecutivePushes = 0;
          continue;
        }
        // Gap job doesn't fit → push to next page (continuation context).
        const sig = `gap:${j.id}`;
        if (sig === lastPushedSignature) consecutivePushes++;
        else { consecutivePushes = 1; lastPushedSignature = sig; }
        if (consecutivePushes > MAX_CONSECUTIVE_PUSHES ||
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
        consecutivePushes = 0;
        continue;
      }

      // Try to bridge.
      const bestK = maxFittingBullets(
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
        consecutivePushes = 0;
        continue;
      }

      // Can't bridge — push whole to next page.
      const sig = `job:${j.id}@${unit.bullets_offset}`;
      if (sig === lastPushedSignature) consecutivePushes++;
      else { consecutivePushes = 1; lastPushedSignature = sig; }
      if (consecutivePushes > MAX_CONSECUTIVE_PUSHES ||
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
};
