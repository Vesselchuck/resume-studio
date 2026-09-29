/*
 * _dist_lock.js — one writer in dist/ at a time, across processes.
 *
 * A build and a live preview write the same files: dist/index.html,
 * pdf_meta.json, placement.json, styles.css (and the letter's pair).
 * Inside the Studio that is never a problem, because the engine's queue
 * runs one thing at a time and its Build is a child it waits for. But
 * `node resume.js` in a terminal while the Studio is open is two
 * processes, and they interleaved: the CLI's final pass rendered
 * against the placement the preview had just solved for other data
 * (UndefinedError on a job id, or "placement does not match"), the
 * crop stamped the preview's metadata, the PDF went out under the
 * preview's name, and the prune deleted the file the CLI had just
 * written because pdf_meta.json named another. Measured: 6 builds of 6
 * failed with previews running back to back.
 *
 * So both take this lock around their use of dist/: resume.js and
 * letter.js from the start of the build to the prune (not the tests
 * before it, which touch only temp copies, nor the snapshot after it,
 * which reads the build record); the engine around each preview. The
 * engine's own Build takes no lock of its own — the CLI child it runs
 * takes it, and the engine's queue keeps previews out meanwhile.
 *
 * A LOCK MUST NEVER OUTLIVE ITS HOLDER. The file is dist/.lock, created
 * with O_EXCL (CREATE_NEW on Windows, where that is atomic too), and
 * holding the holder's pid and host. A waiter treats it as abandoned,
 * and removes it, when either
 *
 *   • the pid is not running on this host (process.kill(pid, 0) throws
 *     ESRCH; EPERM means it runs as someone else, so it is alive), or
 *   • the file has not been touched for STALE_MS. The holder touches it
 *     every HEARTBEAT_MS while it holds it, so only a holder that is
 *     gone (or frozen) stops. This is what covers a pid that was
 *     reused — Windows hands pids out again quickly — and a lock left
 *     by another machine sharing the folder.
 *
 * And a waiter gives up after WAIT_MS with an error that names the
 * holder, rather than waiting on a live but stuck one forever.
 *
 * Breaking a stale lock is done by one waiter at a time: it first
 * takes dist/.lock.break (O_EXCL, like the lock), re-reads the lock to
 * check it is still the one judged stale, removes it, and drops
 * .lock.break. Without that, two waiters that judged the same crashed
 * lock stale both proceeded whenever one re-read the lock just before
 * the other replaced it: measured, 24 overlapping holders in 8 rounds
 * of 8 processes with 2 ms of rename latency (an antivirus scanning
 * dist/ adds that and more). A .lock.break left by a waiter that died
 * in those few milliseconds is removed once it is BREAK_STALE_MS old.
 *
 * "Not refreshed" is judged over the WAITER's own clock, too: the
 * waiter must have watched the same mtime for 2 × HEARTBEAT_MS of its
 * own polling, with no gap in its polling longer than a second. A
 * laptop that sleeps mid-build wakes both processes with the file
 * minutes old by the wall clock and the holder's next heartbeat still
 * up to HEARTBEAT_MS away (timers run on a monotonic clock); judged by
 * the file's age alone, the waiter took over a live lock on waking.
 *
 * And a holder that blocks its event loop — the CLI runs build.py and
 * crop_pdf.py with execFileSync — would stop refreshing the lock, so
 * a step longer than STALE_MS (a cold first run on a slow machine)
 * had its lock taken by the next preview. Pass { blocking: true } and
 * the heartbeat runs on a worker thread, which execFileSync does not
 * stop.
 *
 * Cost on a preview: a failed stat (the turn file, below), an exclusive
 * create, a write and an unlink: 0.03–0.04 ms per acquire and release
 * in a Linux sandbox (n=2000), and no measurable change in a full
 * preview (median 120.6 vs 121.6 ms without the lock, n=60 each).
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const LOCK_NAME = '.lock';
const HEARTBEAT_MS = 2000;
const STALE_MS = 15000;
const WAIT_MS = 60000;
const POLL_MS = 50;
const TURN_NAME = '.lock.next';
const TURN_FRESH_MS = 500;
const BREAK_NAME = '.lock.break';
const BREAK_STALE_MS = 5000;
const MAX_POLL_GAP_MS = 1000;
const { performance } = require('perf_hooks');

const sleep = ms => new Promise(res => setTimeout(res, ms));

// Locks this process holds right now. A lock file naming this pid when
// the count is 0 is one this process failed to remove (see release).
let heldHere = 0;

function readHolder(file) {
  try {
    const text = fs.readFileSync(file, 'utf-8');
    let info = null;
    try { info = JSON.parse(text); } catch { /* half-written: judged by age */ }
    return { text, info };
  } catch {
    return null;
  }
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === 'EPERM';
  }
}

/** Why the lock at `file` may be taken over, or null while it is held. */
function staleReason(file, holder, now = Date.now()) {
  let age;
  try {
    age = now - fs.statSync(file).mtimeMs;
  } catch {
    return 'gone';
  }
  const info = holder && holder.info;
  if (info && Number.isInteger(info.pid) && info.host === os.hostname()) {
    if (info.pid === process.pid) {
      if (heldHere === 0) return 'left behind by this process';
    } else if (!pidAlive(info.pid)) {
      return `its process (pid ${info.pid}) is not running`;
    }
  }
  if (age > STALE_MS) return `not refreshed for ${Math.round(age / 1000)} s`;
  return null;
}

function describe(holder) {
  const info = holder && holder.info;
  if (!info) return 'another process';
  return `${info.owner || 'another process'} (pid ${info.pid})`;
}

function tryCreate(file, owner) {
  try {
    const fd = fs.openSync(file, 'wx');
    try {
      fs.writeSync(fd, JSON.stringify({
        pid: process.pid, host: os.hostname(), owner, since: new Date().toISOString(),
      }));
    } finally {
      fs.closeSync(fd);
    }
    return true;
  } catch (err) {
    if (err.code === 'EEXIST') return false;
    // Windows: a lock file just deleted while someone had it open is
    // "delete pending" until they close it, and creating it again is
    // refused with EPERM rather than EEXIST. Busy, not an error.
    if (err.code === 'EPERM' || err.code === 'EACCES' || err.code === 'EBUSY') return false;
    throw err;
  }
}

/*
 * Taking turns. The Studio can preview back to back — an unchanged
 * render is a few milliseconds — and release-then-acquire within one
 * process is quicker than a waiter's next poll, so a CLI build could
 * wait out many previews in a row (measured: 9 s for a 2.5 s build).
 * A waiter therefore claims the next turn in dist/.lock.next, refreshing
 * it each poll, and nobody else takes a free lock while that claim is
 * fresh. A waiter that is gone stops refreshing, and its claim lapses
 * after TURN_FRESH_MS. With no waiter the file does not exist: one
 * failed stat per acquire.
 */
function othersTurn(turn) {
  try {
    if (Date.now() - fs.statSync(turn).mtimeMs > TURN_FRESH_MS) return false;
    return Number(fs.readFileSync(turn, 'utf-8')) !== process.pid;
  } catch {
    return false;
  }
}

function claimTurn(turn) {
  if (othersTurn(turn)) return;
  try { fs.writeFileSync(turn, String(process.pid)); } catch { /* next poll */ }
}

function dropTurn(turn) {
  try {
    if (Number(fs.readFileSync(turn, 'utf-8')) === process.pid) fs.unlinkSync(turn);
  } catch { /* not ours, or gone */ }
}

/** Remove a stale lock; true when there is something new to look at. */
function breakStale(file, judged) {
  // One breaker at a time; see the header.
  const breaker = path.join(path.dirname(file), BREAK_NAME);
  let fd;
  try {
    fd = fs.openSync(breaker, 'wx');
  } catch (err) {
    try {
      if (Date.now() - fs.statSync(breaker).mtimeMs > BREAK_STALE_MS) fs.unlinkSync(breaker);
    } catch { /* gone, or not ours to judge yet */ }
    return false;                                   // Another waiter is on it: wait.
  }
  try {
    fs.closeSync(fd);
    const now = readHolder(file);
    if (!now || now.text !== judged.text) return true;   // Taken over meanwhile: look again.
    const aside = `${file}.stale-${process.pid}-${Date.now()}`;
    try {
      fs.renameSync(file, aside);
    } catch {
      return false;
    }
    try { fs.unlinkSync(aside); } catch { /* removed next time round */ }
    return true;
  } finally {
    try { fs.unlinkSync(breaker); } catch { /* removed as stale later */ }
  }
}

function startBeat(file, blocking) {
  const touch = () => {
    const t = new Date();
    try { fs.utimesSync(file, t, t); } catch { /* released or broken */ }
  };
  if (!blocking) {
    const beat = setInterval(touch, HEARTBEAT_MS);
    beat.unref();
    return () => clearInterval(beat);
  }
  const { Worker } = require('worker_threads');
  const w = new Worker(`
    const fs = require('fs');
    const { file, ms } = require('worker_threads').workerData;
    setInterval(() => {
      const t = new Date();
      try { fs.utimesSync(file, t, t); } catch {}
    }, ms);`, { eval: true, workerData: { file, ms: HEARTBEAT_MS } });
  w.unref();
  w.on('error', () => { /* a lost heartbeat only makes the lock look stale */ });
  return () => { w.terminate().catch(() => {}); };
}

/**
 * Take dist/'s lock, waiting for another holder to finish.
 *
 * @param {string} dist
 * @param {object} [opts]
 * @param {string} [opts.owner]   — who we are, for the waiter's message
 * @param {function} [opts.onWait] — called once, with the holder's
 *   description, if we have to wait at all
 * @param {number} [opts.waitMs]  — give up after this long
 * @param {boolean} [opts.blocking] — this process blocks its event
 *   loop while holding the lock (execFileSync): heartbeat from a thread
 * @returns {Promise<function>} release(): idempotent, synchronous
 */
async function acquire(dist, {
  owner = 'a build', onWait = null, waitMs = WAIT_MS, blocking = false,
} = {}) {
  fs.mkdirSync(dist, { recursive: true });
  const file = path.join(dist, LOCK_NAME);
  const turn = path.join(dist, TURN_NAME);
  const started = Date.now();
  let told = false;
  // How long WE have seen the lock unchanged (see the header).
  let seen = null;
  let lastPoll = performance.now();
  for (;;) {
    const queued = othersTurn(turn);
    if (!queued && tryCreate(file, owner)) break;
    const holder = readHolder(file);
    const mono = performance.now();
    let mtime = null;
    try { mtime = fs.statSync(file).mtimeMs; } catch { /* gone */ }
    if (!seen || seen.mtime !== mtime || seen.text !== (holder && holder.text)
        || mono - lastPoll > MAX_POLL_GAP_MS) {
      seen = { mtime, text: holder && holder.text, since: mono };
    }
    lastPoll = mono;
    const reason = holder && staleReason(file, holder);
    if (reason && (!reason.startsWith('not refreshed')
                   || mono - seen.since >= 2 * HEARTBEAT_MS)) {
      if (breakStale(file, holder)) {
        seen = null;
        continue;
      }
    }
    if (!holder && !queued) {       // Released (or delete pending) meanwhile.
      await sleep(5);
      continue;
    }
    claimTurn(turn);
    if (!told && onWait) onWait(describe(holder));
    told = true;
    if (Date.now() - started > waitMs) {
      dropTurn(turn);
      const err = new Error(
        `dist/ is in use by ${describe(holder)} and was not released within `
        + `${Math.round(waitMs / 1000)} s. If nothing else is building, delete `
        + `${path.join('dist', LOCK_NAME)}.`);
      err.code = 'EDISTLOCKED';
      throw err;
    }
    await sleep(POLL_MS);
  }

  dropTurn(turn);
  heldHere += 1;
  const stopBeat = startBeat(file, blocking);

  let released = false;
  return function release() {
    if (released) return;
    released = true;
    heldHere -= 1;
    stopBeat();
    const holder = readHolder(file);
    // Only our own lock: if it was broken as stale (a frozen holder)
    // and taken by someone else, it is theirs now.
    if (!holder || !holder.info || holder.info.pid !== process.pid) return;
    try { fs.unlinkSync(file); } catch { /* left to the staleness check */ }
  };
}

module.exports = { acquire, LOCK_NAME, TURN_NAME, BREAK_NAME, HEARTBEAT_MS, STALE_MS, WAIT_MS, staleReason };
