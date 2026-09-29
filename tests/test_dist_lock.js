/**
 * test_dist_lock.js — one writer in dist/ at a time (build/_dist_lock.js).
 *
 * `node resume.js` in a terminal while the Studio previews writes the
 * same dist/ files from two processes. Before the lock, 6 builds of 6
 * failed or went wrong with previews running back to back: the CLI's
 * final pass read the preview's placement, the crop stamped the
 * preview's metadata. The end-to-end part below is that sequence.
 *
 * The rest is what makes a lock safe to have at all: it never outlives
 * a holder that is gone (a dead pid, a holder that stopped refreshing
 * it, a file this process left behind), and a waiter never waits on a
 * live holder forever.
 *
 * The end-to-end part needs Playwright's Chromium; without it this
 * prints the runner's SKIP marker for that part and runs the rest.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawn, spawnSync } = require('child_process');

const { assertEq, assertTrue, fail, report } = require('./_framework');
const { tempProject, realDistFingerprint } = require('./_project');

const SUITE = 'test_dist_lock';
const ROOT = path.join(__dirname, '..');
const lock = require(path.join(ROOT, 'build', '_dist_lock'));
const { detectPython } = require(path.join(ROOT, 'build', 'detect_python'));

/** _framework's test() for an async body: a rejection is recorded, not lost. */
async function test(name, fn) {
  try {
    await fn();
  } catch (err) {
    fail(name, { error: err.stack || err.message });
  }
}

function withTempDir(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'distlock-'));
  return Promise.resolve(fn(dir)).finally(() => fs.rmSync(dir, { recursive: true, force: true }));
}

const lockFile = dir => path.join(dir, lock.LOCK_NAME);

function writeLock(dir, info, ageMs = 0) {
  fs.writeFileSync(lockFile(dir), JSON.stringify(info));
  if (ageMs) {
    const t = new Date(Date.now() - ageMs);
    fs.utimesSync(lockFile(dir), t, t);
  }
}

/** A pid that is certainly not running: a child that has exited. */
function deadPid() {
  const r = spawnSync(process.execPath, ['-e', 'process.stdout.write(String(process.pid))'],
    { encoding: 'utf-8' });
  return Number(r.stdout);
}

/** A child process that holds the lock on `dir` for `ms`, then releases it. */
function holdInChild(dir, ms) {
  const code = `
    const l = require(${JSON.stringify(path.join(ROOT, 'build', '_dist_lock'))});
    l.acquire(${JSON.stringify(dir)}, { owner: 'the test holder' }).then((release) => {
      console.log('held');
      setTimeout(() => { release(); console.log('released'); }, ${ms});
    });`;
  const child = spawn(process.execPath, ['-e', code], { stdio: ['ignore', 'pipe', 'inherit'] });
  const held = new Promise((res) => {
    child.stdout.on('data', (d) => { if (String(d).includes('held')) res(); });
  });
  const exited = new Promise(res => child.once('exit', res));
  return { child, held, exited };
}


async function unitTests() {
  await test('acquire creates the lock and release removes it', () => withTempDir(async (dir) => {
    const release = await lock.acquire(dir, { owner: 'me' });
    const info = JSON.parse(fs.readFileSync(lockFile(dir), 'utf-8'));
    assertEq([info.pid, info.owner, info.host], [process.pid, 'me', os.hostname()], 'names its holder');
    release();
    assertTrue(!fs.existsSync(lockFile(dir)), 'removed');
    release();   // idempotent
  }));

  await test('a second process waits for the first to release', () => withTempDir(async (dir) => {
    const h = holdInChild(dir, 400);
    await h.held;
    let told = null;
    const t0 = Date.now();
    const release = await lock.acquire(dir, { owner: 'waiter', onWait: (who) => { told = who; } });
    const waited = Date.now() - t0;
    release();
    await h.exited;
    assertTrue(waited >= 250, `waited for the holder (${waited} ms)`);
    assertTrue(/the test holder \(pid \d+\)/.test(told), `named who it waited for: ${told}`);
  }));

  await test('a lock whose process is gone is taken at once', () => withTempDir(async (dir) => {
    writeLock(dir, { pid: deadPid(), host: os.hostname(), owner: 'a crashed build' });
    const t0 = Date.now();
    const release = await lock.acquire(dir, { waitMs: 5000 });
    assertTrue(Date.now() - t0 < 1000, 'no waiting on a dead pid');
    release();
  }));

  await test('a lock nobody has refreshed is taken, even if its pid is alive', () => withTempDir(async (dir) => {
    // The pid-reuse case: a live pid (the test's parent) that is not the
    // holder any more, and a file older than STALE_MS.
    writeLock(dir, { pid: process.ppid, host: os.hostname(), owner: 'long gone' },
      lock.STALE_MS + 5000);
    // After watching it unchanged for 2 heartbeats itself (a laptop
    // waking from sleep sees every lock "old").
    const t0 = Date.now();
    const release = await lock.acquire(dir, { waitMs: 8000 });
    assertTrue(Date.now() - t0 >= 2 * lock.HEARTBEAT_MS - 100, 'watched it first');
    release();
  }));

  await test('a lock this process left behind is taken', () => withTempDir(async (dir) => {
    writeLock(dir, { pid: process.pid, host: os.hostname(), owner: 'me, earlier' });
    const release = await lock.acquire(dir, { waitMs: 2000 });
    release();
  }));

  await test('a half-written lock file is judged by its age alone', () => withTempDir(async (dir) => {
    fs.writeFileSync(lockFile(dir), '{"pid":');
    let threw = null;
    try { await lock.acquire(dir, { waitMs: 200 }); } catch (err) { threw = err; }
    assertEq(threw && threw.code, 'EDISTLOCKED', 'fresh: still respected');
    const t = new Date(Date.now() - lock.STALE_MS - 5000);
    fs.utimesSync(lockFile(dir), t, t);
    (await lock.acquire(dir, { waitMs: 8000 }))();
  }));

  await test('a live holder is waited for only so long, and named', () => withTempDir(async (dir) => {
    const h = holdInChild(dir, 3000);
    await h.held;
    let threw = null;
    try { await lock.acquire(dir, { waitMs: 300 }); } catch (err) { threw = err; }
    assertEq(threw && threw.code, 'EDISTLOCKED', 'gives up');
    assertTrue(threw && /the test holder \(pid \d+\)/.test(threw.message),
      `says who holds it: ${threw && threw.message}`);
    h.child.kill();
    await h.exited;
    (await lock.acquire(dir, { waitMs: 5000 }))();
  }));

  await test('only one waiter breaks a stale lock at a time', () => withTempDir(async (dir) => {
    writeLock(dir, { pid: deadPid(), host: os.hostname(), owner: 'a crashed build' });
    const breaker = path.join(dir, lock.BREAK_NAME);
    fs.writeFileSync(breaker, '');          // another waiter is breaking it
    let threw = null;
    try { await lock.acquire(dir, { waitMs: 300 }); } catch (err) { threw = err; }
    assertEq(threw && threw.code, 'EDISTLOCKED', 'left to the breaker');
    const t = new Date(Date.now() - 10000);
    fs.utimesSync(breaker, t, t);           // ...who died doing it
    (await lock.acquire(dir, { waitMs: 3000 }))();
    assertTrue(!fs.existsSync(breaker), 'its leftover removed');
  }));

  await test('a blocking holder keeps its lock fresh from a thread', () => withTempDir(async (dir) => {
    const code = `
      const l = require(${JSON.stringify(path.join(ROOT, 'build', '_dist_lock'))});
      l.acquire(${JSON.stringify(dir)}, { blocking: true }).then((release) => {
        const t = new Date(Date.now() - 60000);
        require('fs').utimesSync(${JSON.stringify(lockFile(dir))}, t, t);
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 3 * l.HEARTBEAT_MS);
        release();
      });`;
    spawnSync(process.execPath, ['-e', code]);
    // Checked from inside instead: the mtime moved while the loop was blocked.
    const probe = spawnSync(process.execPath, ['-e', `
      const fs = require('fs');
      const l = require(${JSON.stringify(path.join(ROOT, 'build', '_dist_lock'))});
      l.acquire(${JSON.stringify(dir)}, { blocking: true }).then((release) => {
        const f = ${JSON.stringify(lockFile(dir))};
        const t = new Date(Date.now() - 60000);
        fs.utimesSync(f, t, t);
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1.5 * l.HEARTBEAT_MS);
        process.stdout.write(String(Date.now() - fs.statSync(f).mtimeMs < l.HEARTBEAT_MS * 1.5));
        release();
      });`], { encoding: 'utf-8' });
    assertEq(probe.stdout, 'true', 'refreshed during a blocked event loop');
  }));

  await test('release leaves a lock that is no longer ours alone', () => withTempDir(async (dir) => {
    const release = await lock.acquire(dir);
    writeLock(dir, { pid: deadPid(), host: os.hostname(), owner: 'someone who broke it' });
    release();
    assertTrue(fs.existsSync(lockFile(dir)), 'not removed');
  }));
}


/**
 * The reported sequence: a Studio engine previewing the template back
 * to back while `node resume.js` builds another file with different
 * job ids. Before the lock the build failed on the preview's placement
 * or stamped the preview's metadata.
 */
async function endToEnd() {
  try {
    const { chromium } = require('playwright');
    const b = await chromium.launch();
    await b.close();
  } catch (err) {
    console.log(`SKIP ${SUITE}: chromium unavailable for the end-to-end part `
      + `(${String(err.message).split('\n')[0]})`);
    return;
  }

  const before = realDistFingerprint();
  const project = tempProject('dist-lock-test');
  let engine;
  try {
    // A template-world copy (it merges _profile_default.yml) whose job
    // ids and summary differ from the template's.
    const src = fs.readFileSync(path.join(project.root, 'data', 'resume_default.yml'), 'utf-8');
    const alt = src.replace(/- id: nulla$/m, '- id: nulla-alt')
      .replace('Sed gravida elit velit', 'LOCKMARKER elit velit');
    assertTrue(alt !== src && alt.includes('nulla-alt') && alt.includes('LOCKMARKER'),
      'the alternative data file differs from the template');
    fs.writeFileSync(path.join(project.root, 'data', 'alt_default.yml'), alt);

    const { createEngine } = project.require('build/engine');
    engine = await createEngine({ root: project.root });
    await engine.renderPreview({ env: { RESUME_DATA_SOURCE: 'default' } });

    for (let i = 0; i < 2; i++) {
      let stop = false;
      let previews = 0;
      const loop = (async () => {
        while (!stop) {
          await engine.renderPreview({ env: { RESUME_DATA_SOURCE: 'default' }, known: {} });
          previews += 1;
        }
      })();
      const child = spawn(process.execPath, ['resume.js'], {
        cwd: project.root,
        env: { ...process.env, RESUME_TESTS: 'off', RESUME_SNAPSHOT: 'off',
               RESUME_DATA_FILE: 'data/alt_default.yml', NO_COLOR: '1' },
      });
      let out = '';
      child.stdout.on('data', (d) => { out += d; });
      child.stderr.on('data', (d) => { out += d; });
      const code = await new Promise(res => child.once('close', res));
      stop = true;
      await loop;

      assertEq(code, 0, `build ${i} succeeded alongside ${previews} previews`
        + (code ? `: ${out.split('\n').filter(l => /❌|Error/.test(l)).slice(0, 3).join(' | ')}` : ''));
      assertTrue(previews > 0, `build ${i}: previews really ran meanwhile`);
      const record = JSON.parse(fs.readFileSync(path.join(project.dist, 'outputs.json'), 'utf-8'));
      assertEq(record.resume.data_source, 'explicit', `build ${i}: recorded as the file it built`);
      const pdf = path.join(project.dist, record.resume.pdf);
      assertTrue(fs.existsSync(pdf), `build ${i}: its PDF is in dist/`);
      const text = spawnSync(detectPython(), ['-c',
        'import sys, pypdf; print("".join(p.extract_text() for p in pypdf.PdfReader(sys.argv[1]).pages))',
        pdf], { encoding: 'utf-8' }).stdout;
      assertTrue(text.includes('LOCKMARKER'), `build ${i}: the PDF shows the file it built`);
      assertTrue(!fs.existsSync(path.join(project.dist, lock.LOCK_NAME)), `build ${i}: lock released`);
    }
  } catch (err) {
    fail('dist lock end to end', { error: err.stack || err.message });
  } finally {
    if (engine) await engine.dispose();
    project.remove();
  }
  assertEq(realDistFingerprint(), before, "isolation: nothing was written to this checkout's dist/");
}


(async () => {
  await unitTests();
  await endToEnd();
  report();
})();
