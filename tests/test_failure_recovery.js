/**
 * test_failure_recovery.js — the engine and the Studio server get back
 * on their feet after the failures they used to stay stuck on.
 *
 * WHAT THIS GUARDS
 * ----------------
 *   • Chromium killed under the engine (a crash, an OOM kill): the next
 *     render launches a new one. It used to keep the dead page cached,
 *     and every render failed until the Studio was restarted.
 *   • The window shown again while a hidden-release is already queued
 *     (behind a render, when the delay ran out): Chromium stays open. It
 *     used to be closed anyway, and not relaunched while shown.
 *   • GET /api/datafiles with a folder named x.yml and a dangling
 *     symlink (Emacs's .#x.yml lock) in data/: the other files are
 *     listed. One unreadable entry used to fail the whole request (500).
 *   • A failed render ends with a render event on /api/events, so every
 *     other window that saw it start stops showing "Rendering". Only a
 *     successful render used to send one.
 *
 * Runs in a throwaway copy of the project (tests/_project.js). Needs
 * Chromium; without it this prints the runner's SKIP marker. The
 * Chromium-kill part finds Chromium among this process's children:
 * through /proc on Linux, PowerShell's Win32_Process on Windows, and
 * ps elsewhere.
 */

const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn, execFileSync } = require('child_process');
const { assertEq, assertTrue, fail, report } = require('./_framework');
const { tempProject, realDistFingerprint } = require('./_project');

const SUITE = 'test_failure_recovery';
const READY_PREFIX = '\x1eSTUDIO_READY ';

const sleep = ms => new Promise(res => setTimeout(res, ms));

async function waitFor(pred, ms = 15000) {
  const until = Date.now() + ms;
  while (!pred() && Date.now() < until) await sleep(25);
  return pred();
}

async function rejects(promise) {
  try { await promise; return null; } catch (err) { return err; }
}

/** Direct children of this process whose command name is Chromium's. */
function chromiumChildren() {
  const isChromium = name => /chrom|headless/i.test(name);
  if (process.platform === 'win32') {
    // Playwright spawns chrome.exe (or the headless shell) directly.
    const out = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
      // No double quotes: Node escapes them on the command line, and
      // Windows PowerShell 5.1 does not always read them back the same.
      `Get-CimInstance Win32_Process -Filter 'ParentProcessId=${process.pid}' | ` +
      "ForEach-Object { [string]$_.ProcessId + ' ' + $_.Name }"], { encoding: 'utf-8' });
    return out.split(/\r?\n/).map(l => l.trim().split(/\s+/))
      .filter(([pid, name]) => /^\d+$/.test(pid) && isChromium(name || ''))
      .map(([pid]) => Number(pid));
  }
  if (!fs.existsSync('/proc/self/stat')) {
    const out = execFileSync('ps', ['-A', '-o', 'pid=,ppid=,comm='], { encoding: 'utf-8' });
    return out.split('\n').map(l => l.trim().split(/\s+/))
      .filter(([pid, ppid, ...comm]) => Number(ppid) === process.pid && isChromium(comm.join(' ')))
      .map(([pid]) => Number(pid));
  }
  const found = [];
  for (const entry of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const stat = fs.readFileSync(`/proc/${entry}/stat`, 'utf-8');
      const comm = stat.slice(stat.indexOf('(') + 1, stat.lastIndexOf(')'));
      const ppid = Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1]);
      if (ppid === process.pid && isChromium(comm)) found.push(Number(entry));
    } catch { /* gone meanwhile */ }
  }
  return found;
}

/* ─── Engine ──────────────────────────────────────────────────── */

async function chromiumKilled(createEngine, root) {
  const engine = await createEngine({ root, warm: true });
  try {
    const first = await engine.renderPreview({ doc: 'resume' });
    const pids = chromiumChildren();
    assertTrue(pids.length > 0, `N1: Chromium is found among this process's children (${pids})`);
    for (const pid of pids) process.kill(pid, 'SIGKILL');   // TerminateProcess on Windows

    assertTrue(await waitFor(() => !engine.status().browserOpen, 5000),
      'N1: a killed Chromium is noticed: the engine no longer reports it open');
    const err = await rejects(engine.renderPreview({ doc: 'resume', recompileStyles: true }));
    assertEq(err ? err.message.split('\n')[0] : null, null,
      'N1: the next render after the kill succeeds');
    const after = await engine.renderPreview({ doc: 'resume', recompileStyles: true });
    assertTrue(after.timings.browserLaunch > 0 || engine.status().browserOpen,
      'N1: ...on a newly launched Chromium');
    assertEq(after.images.map(im => im.hash), first.images.map(im => im.hash),
      'N1: ...which draws the same pages');
    assertTrue(chromiumChildren().every(pid => !pids.includes(pid)),
      'N1: the killed Chromium is not the one in use');
  } finally {
    await engine.dispose();
  }
}

async function shownWhileReleaseQueued(createEngine, root) {
  const DELAY = 50;
  const engine = await createEngine({ root, warm: true, hiddenReleaseMs: DELAY });
  try {
    await engine.renderPreview({ doc: 'resume' });
    assertTrue(engine.status().browserOpen, 'N3: a warm engine has Chromium open');

    // Hold the queue, the way a long render does; the delay runs out
    // meanwhile, so the release is queued behind it.
    let open;
    const gate = new Promise(res => { open = res; });
    const held = engine.exclusive(() => gate);
    engine.setWindowHidden(true);
    await sleep(DELAY * 4);
    engine.setWindowHidden(false);   // shown before the queue gets to the release
    open();
    await held;
    await engine.exclusive(async () => {});
    await sleep(DELAY * 4);
    assertTrue(engine.status().browserOpen,
      'N3: shown again while a release was queued: Chromium stays open');
    const r = await engine.renderPreview({ doc: 'resume', recompileStyles: true });
    assertEq(r.timings.browserLaunch, 0, 'N3: ...so the next render pays no launch');
  } finally {
    await engine.dispose();
  }
}

/* ─── Server ──────────────────────────────────────────────────── */

function startServer(root) {
  const env = { ...process.env };
  for (const k of ['RESUME_DATA_SOURCE', 'RESUME_DATA_FILE', 'LETTER_DATA_FILE', 'STUDIO_PORT']) {
    delete env[k];
  }
  const child = spawn(process.execPath,
    [path.join(root, 'build', 'studio_server.js'), '--port', '0', '--exit-with-parent'],
    { cwd: root, env, stdio: ['pipe', 'pipe', 'pipe'] });
  const log = [];
  child.stderr.setEncoding('utf-8');
  child.stderr.on('data', chunk => log.push(chunk));
  return new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('server not ready in 60 s')), 60000);
    child.stdout.setEncoding('utf-8');
    child.stdout.on('data', (chunk) => {
      buf += chunk;
      log.push(chunk);
      const line = buf.split('\n').find(l => l.startsWith(READY_PREFIX));
      if (line) {
        clearTimeout(timer);
        resolve({ child, log, port: JSON.parse(line.slice(READY_PREFIX.length)).port });
      }
    });
    child.once('exit', () => reject(new Error('server exited before it was ready')));
  });
}

function request(port, method, route, body) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : JSON.stringify(body);
    const req = http.request({
      host: '127.0.0.1', port, method, path: route,
      headers: {
        host: `127.0.0.1:${port}`,
        ...(payload !== null ? { 'content-type': 'application/json',
                                 'content-length': Buffer.byteLength(payload) } : {}),
      },
    }, (res) => {
      let text = '';
      res.setEncoding('utf-8');
      res.on('data', (c) => { text += c; });
      res.on('end', () => {
        let data = null;
        try { data = JSON.parse(text); } catch { /* not JSON */ }
        resolve({ status: res.statusCode, data });
      });
    });
    req.setTimeout(60000, () => req.destroy(new Error(`${method} ${route} timed out`)));
    req.on('error', reject);
    if (payload !== null) req.write(payload);
    req.end();
  });
}

/** Listen on /api/events; resolves once the stream is open. */
function events(port, onEvent) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/api/events',
      headers: { host: `127.0.0.1:${port}` } }, (res) => {
      let buf = '';
      res.setEncoding('utf-8');
      res.on('data', (chunk) => {
        buf += chunk;
        let idx;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const name = /^event: (.+)$/m.exec(block);
          const data = /^data: (.+)$/m.exec(block);
          if (name && data) {
            try { onEvent(name[1], JSON.parse(data[1])); } catch { /* not ours */ }
          }
        }
      });
      resolve({ close: () => req.destroy() });
    });
    req.on('error', () => resolve({ close: () => {} }));
  });
}

async function serverTests(root) {
  const dataDir = path.join(root, 'data');
  const srv = await startServer(root);
  const { port } = srv;
  try {
    // N4 — entries in data/ that are not readable files.
    fs.mkdirSync(path.join(dataDir, 'folder.yml'));
    let r = await request(port, 'GET', '/api/datafiles');
    assertEq(r.status, 200, `N4: /api/datafiles answers with a folder named folder.yml in data/ (${r.data && r.data.error})`);
    // Windows refuses symlinks without Developer Mode or admin rights
    // (EPERM). Emacs there writes its lock as a plain file, which lists
    // fine, so only the folder case applies; the link case runs elsewhere.
    let linked = true;
    try {
      fs.symlinkSync(path.join(dataDir, 'gone.yml'), path.join(dataDir, '.#resume_default.yml'));
    } catch (err) {
      if (err.code !== 'EPERM') throw err;
      linked = false;
      console.log('  (no symlink permission: the dangling-link case is not checked here)');
    }
    r = await request(port, 'GET', '/api/datafiles');
    if (linked) {
      assertEq(r.status, 200, `N4: ...and with a dangling .#x.yml link as well (${r.data && r.data.error})`);
    }
    const names = ((r.data && r.data.files) || []).map(f => f.name);
    assertTrue(names.includes('resume_default.yml') && names.includes('letter_default.yml'),
      `N4: ...and lists the readable files (${names})`);
    assertTrue(!names.includes('folder.yml') && !names.includes('.#resume_default.yml'),
      'N4: ...and not the folder or the dangling link');
    fs.rmSync(path.join(dataDir, 'folder.yml'), { recursive: true });
    if (linked) fs.rmSync(path.join(dataDir, '.#resume_default.yml'));

    // F1 — a failed render ends with an event, as a successful one does.
    const seen = [];
    const stream = await events(port, (name, data) => {
      if (name === 'render') seen.push(data);
    });
    try {
      fs.writeFileSync(path.join(dataDir, 'broken.yml'), 'letter:\n  body: [unclosed\n');
      r = await request(port, 'POST', '/api/pick', { doc: 'letter', name: 'broken.yml' });
      assertEq(r.status, 200, 'F1: a broken letter file can be picked');
      await sleep(200);   // the pick's own file events settle
      seen.length = 0;
      r = await request(port, 'POST', '/api/preview', { doc: 'letter', scale: 1 });
      assertEq(r.status, 500, 'F1: the preview of a broken file fails');
      await waitFor(() => seen.some(e => e.state !== 'start'), 2000);
      const letter = seen.filter(e => e.doc === 'letter');
      assertEq(letter.length > 0 && letter[0].state, 'start', 'F1: the failed render announced its start');
      assertTrue(letter.length > 1 && letter[letter.length - 1].state !== 'start',
        `F1: ...and its end (${JSON.stringify(letter.map(e => e.state))})`);

      // A successful render still ends with 'done'.
      await request(port, 'POST', '/api/pick', { doc: 'letter', name: null });
      await sleep(200);
      seen.length = 0;
      r = await request(port, 'POST', '/api/preview', { doc: 'letter', scale: 1 });
      assertEq(r.status, 200, `F1: a good render succeeds (${r.data && r.data.error})`);
      await waitFor(() => seen.some(e => e.state === 'done'), 2000);
      assertEq(seen.filter(e => e.doc === 'letter').map(e => e.state), ['start', 'done'],
        'F1: a successful render sends start then done, once each');
    } finally {
      stream.close();
      fs.rmSync(path.join(dataDir, 'broken.yml'), { force: true });
    }
  } catch (err) {
    err.message += `\n${srv.log.join('').split('\n').slice(-30).join('\n')}`;
    throw err;
  } finally {
    const exited = new Promise(res => srv.child.once('exit', res));
    srv.child.stdin.end();
    if (await Promise.race([exited.then(() => true), sleep(10000).then(() => false)]) === false) {
      try { srv.child.kill('SIGKILL'); } catch { /* gone */ }
    }
  }
}

(async () => {
  try {
    const { chromium } = require('playwright');
    const b = await chromium.launch();
    await b.close();
  } catch (err) {
    console.log(`SKIP ${SUITE}: chromium unavailable (${String(err.message).split('\n')[0]})`);
    process.exitCode = 0;
    return;
  }

  const before = realDistFingerprint();
  const project = tempProject('failure-recovery-test');
  const run = async (label, fn) => {
    try {
      await fn();
    } catch (err) {
      fail(label, { error: err.stack || err.message });
    }
  };
  try {
    const { createEngine } = project.require('build/engine');
    await run('N1 run', () => chromiumKilled(createEngine, project.root));
    await run('N3 run', () => shownWhileReleaseQueued(createEngine, project.root));
    await run('server run', () => serverTests(project.root));
  } finally {
    project.remove();
  }
  assertEq(realDistFingerprint(), before, "isolation: nothing was written to this checkout's dist/");
  report();
})();
