/**
 * test_worker_restarts.js — a Python worker that keeps crashing stops
 * being restarted.
 *
 * WHAT THIS GUARDS
 * ----------------
 * The engine restarts build/worker.py on the next request after it
 * dies. A worker that crashes on the same input every time used to be
 * restarted on every save, forever, each restart ~130 ms of imports.
 * PythonWorker.ensure now stops after five crashes within three minutes
 * (four restarts), the way VS Code's language client does, and fails
 * each request at once with the reason. This suite checks that:
 *
 *   • the first four crashes are restarted, the fifth is not;
 *   • once stopped, a request spawns nothing and names the problem;
 *   • a crash older than the window stops counting;
 *   • allowRestart() (the Re-render button) starts it again at once;
 *   • a worker stopped on purpose is not counted as a crash;
 *   • a worker that stops answering (stopped, deadlocked) fails the
 *     request after its deadline, is killed, and the next request
 *     starts a fresh one — it used to block the queue forever;
 *   • end to end, renderPreview fails fast once the worker is down and
 *     `retry: true` brings it back.
 *
 * And, because a request that never gets its reply hangs the queue
 * just as a dead worker did: output written to the worker's stdout
 * without a newline, glued to the front of a frame, is logged and the
 * frame after it still delivered.
 *
 * The crashing worker is Node itself standing in for Python: `node -B
 * build/worker.py` exits at once ("bad option"), on every platform.
 *
 * The end-to-end part runs in a throwaway copy of the project
 * (tests/_project.js) and needs Chromium; without it that part prints
 * the runner's SKIP marker.
 */

const path = require('path');
const { assertEq, assertTrue, fail, report } = require('./_framework');
const { tempProject, realDistFingerprint } = require('./_project');

const ROOT = path.join(__dirname, '..');
const SUITE = 'test_worker_restarts';

const { PythonWorker, FRAME_PREFIX } = require('../build/engine');
const { detectPython } = require('../build/detect_python');

/** ensure() on a worker whose interpreter dies at once: the error it gives. */
async function ensureError(worker) {
  try {
    await worker.ensure();
    return null;
  } catch (err) {
    return err;
  }
}

async function unitTests() {
  const worker = new PythonWorker({ root: ROOT, python: process.execPath });
  let clock = 1_000_000;
  worker.now = () => clock;

  for (let i = 1; i <= 5; i++) {
    const err = await ensureError(worker);
    assertTrue(err && err.kind !== 'worker_crashing' && /exited/.test(err.message),
      `crash ${i}: the worker was started and died (not refused)`);
    clock += 1000;
  }
  assertEq(worker.crashes.length, 5, 'five crashes are counted');

  const procBefore = worker.proc;
  const stopped = await ensureError(worker);
  assertEq(stopped && stopped.kind, 'worker_crashing',
    'after five crashes in three minutes, the next request is refused');
  assertTrue(stopped && /Re-render/.test(stopped.message),
    'the refusal says how to start it again');
  assertTrue(worker.proc === procBefore, 'a refused request spawns nothing');

  // The oldest crash leaves the window: four remain, so one more start.
  clock = 1_000_000 + 3 * 60 * 1000 + 1;
  const afterWindow = await ensureError(worker);
  assertTrue(afterWindow && afterWindow.kind !== 'worker_crashing',
    'a crash older than three minutes stops counting');
  const again = await ensureError(worker);
  assertEq(again && again.kind, 'worker_crashing', 'and five in the window stops it again');

  worker.allowRestart();
  const retried = await ensureError(worker);
  assertTrue(retried && retried.kind !== 'worker_crashing',
    'allowRestart() lets the next request start a worker at once');

  // A worker stopped on purpose is not a crash.
  let python;
  try {
    python = detectPython();
  } catch {
    python = null;
  }
  if (python) {
    const real = new PythonWorker({ root: ROOT, python });
    await real.start();
    await real.stop();
    await new Promise(res => setTimeout(res, 300));
    assertEq(real.crashes.length, 0, 'stop() is not counted as a crash');

    // A worker that never answers. Its requests are swallowed before
    // they reach it: alive, and silent, on every platform.
    const hung = new PythonWorker({ root: ROOT, python, timeoutMs: 500 });
    await hung.start();
    const stuck = hung.proc;
    stuck.stdin.write = () => true;
    const t0 = Date.now();
    const err = await Promise.race([hung.run({ op: 'ping' }).then(() => null, e => e),
      new Promise(res => setTimeout(() => res(new Error('still pending after 10 s')), 10000).unref())]);
    assertEq(err && err.kind, 'timeout', 'a request with no answer fails when its deadline passes');
    assertTrue(Date.now() - t0 < 5000, `...after the deadline, not never (${Date.now() - t0} ms)`);
    assertTrue(err && /stopped/.test(err.message), '...saying the worker was stopped');
    assertTrue(!hung.alive(), '...and the stuck worker is no longer used');
    assertEq(hung.crashes.length, 1, '...and counts as a crash');
    const frame = await Promise.race([hung.run({ op: 'ping' }).then(f => f, e => e),
      new Promise(res => setTimeout(() => res(new Error('still pending after 10 s')), 10000).unref())]);
    assertTrue(frame && frame.ok && hung.proc !== stuck, 'the next request starts a fresh worker and is answered');
    await hung.stop();
  }
}

function framing() {
  const worker = new PythonWorker({ root: ROOT, python: process.execPath });
  const got = [];
  const wait = id => worker.pending.set(id, {
    resolve: frame => got.push(frame.id), reject() {}, proc: null, onPartial: null,
  });
  const frame = id => FRAME_PREFIX + JSON.stringify({ id, ok: true, result: {} }) + '\n';

  wait(1);
  worker._consume(`written to fd 1 by a C library${frame(1)}`);
  assertEq(got, [1], 'a frame with output glued to its front is still delivered');

  // The same, arriving in pieces, the junk in one chunk and the frame
  // split across the next two.
  wait(2);
  const glued = `more junk${FRAME_PREFIX}junk that looked like a frame${frame(2)}`;
  worker._consume(glued.slice(0, 12));
  worker._consume(glued.slice(12, 40));
  worker._consume(glued.slice(40));
  assertEq(got, [1, 2], 'and when it arrives in pieces, after a stray RS');
  assertEq(worker.pending.size, 0, 'nothing is left waiting');
}

async function endToEnd() {
  let chromium;
  try {
    ({ chromium } = require('playwright'));
    const b = await chromium.launch();
    await b.close();
  } catch (err) {
    console.log(`SKIP ${SUITE}: chromium unavailable for the end-to-end part `
      + `(${String(err.message).split('\n')[0]})`);
    return;
  }

  const before = realDistFingerprint();
  const project = tempProject('worker-restarts-test');
  let engine;
  try {
    const { createEngine } = project.require('build/engine');
    // The Windows path too (the power-throttling housekeeping), wherever this runs.
    engine = await createEngine({ root: project.root, platform: 'win32' });
    const ok = await engine.renderPreview({ doc: 'letter' });
    assertTrue(ok.images.length > 0, 'e2e: a healthy worker renders');

    const w = engine.pipelines.letter.python;
    const realPython = w.python;
    w.python = process.execPath;   // every restart now dies at once
    w.proc.kill();
    await new Promise(res => setTimeout(res, 300));

    // The kill is crash 1; restarts 1–4 are crashes 2–5.
    const messages = [];
    for (let i = 0; i < 5; i++) {
      try {
        await engine.renderPreview({ doc: 'letter' });
        messages.push('rendered');
      } catch (err) {
        messages.push(err.kind === 'worker_crashing' ? 'refused' : 'crashed');
      }
    }
    assertEq(messages, ['crashed', 'crashed', 'crashed', 'crashed', 'refused'],
      'e2e: four restarts are tried, then requests are refused');

    const t0 = Date.now();
    let refused = null;
    try {
      await engine.renderPreview({ doc: 'letter' });
    } catch (err) {
      refused = err;
    }
    assertEq(refused && refused.kind, 'worker_crashing', 'e2e: it stays down');
    assertTrue(Date.now() - t0 < 1000, 'e2e: a refused render fails at once');

    w.python = realPython;         // "fix what it tripped on"
    const back = await engine.renderPreview({ doc: 'letter', retry: true });
    assertTrue(back.images.length > 0, 'e2e: Re-render (retry) brings the worker back');

    // A worker stopped by a signal (SIGSTOP; POSIX only) mid-render: the
    // render fails at the deadline and the next one renders.
    if (process.platform !== 'win32') {
      w.timeoutMs = 1000;
      const pid = w.proc.pid;
      process.kill(pid, 'SIGSTOP');
      const t1 = Date.now();
      const stuck = await Promise.race([
        engine.renderPreview({ doc: 'letter', recompileStyles: true }).then(() => null, e => e),
        new Promise(res => setTimeout(() => res(new Error('still pending after 15 s')), 15000).unref())]);
      try { process.kill(pid, 'SIGCONT'); } catch { /* killed, as it should be */ }
      assertEq(stuck && stuck.kind, 'timeout', 'e2e: a render on a stopped worker fails at its deadline');
      assertTrue(Date.now() - t1 < 10000, `e2e: ...instead of waiting forever (${Date.now() - t1} ms)`);
      const next = await engine.renderPreview({ doc: 'letter', recompileStyles: true });
      assertTrue(next.images.length > 0 && w.proc.pid !== pid, 'e2e: the next render gets a new worker');
      try { process.kill(pid, 'SIGKILL'); } catch { /* already gone */ }
    }
  } catch (err) {
    fail('worker restart end-to-end run', { error: err.stack || err.message });
  } finally {
    if (engine) await engine.dispose();
    project.remove();
  }
  assertEq(realDistFingerprint(), before, "isolation: nothing was written to this checkout's dist/");
}

(async () => {
  try {
    await unitTests();
    framing();
    await endToEnd();
  } catch (err) {
    fail('worker restart run', { error: err.stack || err.message });
  }
  report();
})();
