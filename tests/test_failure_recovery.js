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
 *   • A page that never settles (document.fonts.ready that never
 *     resolves) or a renderer spinning in a loop: the render fails at
 *     its deadline, Chromium is closed, and the next render works. It
 *     used to block the engine's queue, and every render behind it,
 *     forever. A build that never ends likewise, and a Chromium that
 *     never opens a page for the render.
 *   • A renderer that crashes while Chromium lives on: the next render
 *     opens a new page in the same Chromium. The crashed page used to
 *     stay cached, and every render failed with "Page crashed".
 *   • A failed render leaves no data selection behind on the worker.
 *   • GET /api/datafiles with a folder named x.yml and a dangling
 *     symlink (Emacs's .#x.yml lock) in data/: the other files are
 *     listed. One unreadable entry used to fail the whole request (500).
 *   • dispose() with a render in flight or queued: no Chromium is left
 *     running. A render that asked for the page after dispose() had
 *     closed the browser used to launch a new one.
 *   • Chromium killed while it is being launched: the message does not
 *     tell the user to reinstall it, and the next render works.
 *   • A failed render ends with a render event on /api/events, so every
 *     other window that saw it start stops showing "Rendering". Only a
 *     successful render used to send one.
 *   • A warm start whose Python fails: createEngine rejects and no
 *     Chromium is left running. The launch it had started used to
 *     finish with nothing left to close it. With no Python to detect at
 *     all, createEngine rejects too: detection used to exit the process.
 *   • A Build or a preview still queued when dispose() runs: it fails
 *     saying the engine is shut down. The Build used to run `node
 *     resume.js` anyway, and a restyling preview to start the Sass
 *     compiler dispose() had just stopped.
 *   • A cover letter that runs off its page: the preview's layout check
 *     fails, as `node letter.js` does. It used to pass.
 *   • The layout check itself throwing (anything but a violation): the
 *     render fails, and nothing is reused from it. It used to be shown
 *     as a layout violation, and that verdict was cached.
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

/** rejects(), but a promise still pending after `ms` is a failure too. */
function rejectsWithin(promise, ms = 20000) {
  return Promise.race([rejects(promise), new Promise((resolve) => {
    // Unref'd: a settled race must not keep the suite running.
    setTimeout(() => resolve(new Error(`still pending after ${ms} ms`)), ms).unref();
  })]);
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

/**
 * Chromium renderer processes below the given browser pids: every
 * descendant whose command line says --type=renderer. On Linux the
 * renderers hang off a zygote, not the browser itself, hence the walk.
 */
function rendererPids(browserPids) {
  let rows;
  if (process.platform === 'win32') {
    const out = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
      "Get-CimInstance Win32_Process | ForEach-Object { [string]$_.ProcessId + ' ' + " +
      "[string]$_.ParentProcessId + ' ' + $_.CommandLine }"], { encoding: 'utf-8', maxBuffer: 64 << 20 });
    rows = out.split(/\r?\n/).map((l) => {
      const m = /^\s*(\d+)\s+(\d+)\s?(.*)$/.exec(l);
      return m && { pid: Number(m[1]), ppid: Number(m[2]), cmd: m[3] };
    }).filter(Boolean);
  } else if (fs.existsSync('/proc/self/stat')) {
    rows = [];
    for (const entry of fs.readdirSync('/proc')) {
      if (!/^\d+$/.test(entry)) continue;
      try {
        const stat = fs.readFileSync(`/proc/${entry}/stat`, 'utf-8');
        const ppid = Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1]);
        const cmd = fs.readFileSync(`/proc/${entry}/cmdline`, 'utf-8').split('\0').join(' ');
        rows.push({ pid: Number(entry), ppid, cmd });
      } catch { /* gone meanwhile */ }
    }
  } else {
    const out = execFileSync('ps', ['-A', '-o', 'pid=,ppid=,command='], { encoding: 'utf-8' });
    rows = out.split('\n').map((l) => {
      const m = /^\s*(\d+)\s+(\d+)\s+(.*)$/.exec(l);
      return m && { pid: Number(m[1]), ppid: Number(m[2]), cmd: m[3] };
    }).filter(Boolean);
  }
  const below = new Set(browserPids);
  for (let grew = true; grew;) {
    grew = false;
    for (const r of rows) {
      if (!below.has(r.pid) && below.has(r.ppid)) { below.add(r.pid); grew = true; }
    }
  }
  return rows.filter(r => below.has(r.pid) && /--type=renderer/.test(r.cmd)).map(r => r.pid);
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

async function disposeWithRenderInFlight(createEngine, root) {
  const engine = await createEngine({ root, warm: true });
  await engine.renderPreview({ doc: 'resume' });
  // A render in flight and one queued behind it when dispose() runs.
  // Both end in errors; neither may leave a Chromium behind.
  const settle = p => p.then(() => null, e => e);
  const a = settle(engine.renderPreview({ doc: 'resume', recompileStyles: true }));
  const b = settle(engine.renderPreview({ doc: 'letter', recompileStyles: true }));
  await sleep(50);
  await engine.dispose();
  await Promise.all([a, b]);
  await sleep(1000);
  const left = chromiumChildren();
  for (const pid of left) { try { process.kill(pid, 'SIGKILL'); } catch { /* gone */ } }
  assertEq(left, [], 'N4: dispose() with renders in flight and queued leaves no Chromium running');
}

async function failedStart(createEngine, root) {
  // A warm start launches Chromium first and then waits for Python. Here
  // Python exits at once (Node standing in for it: `node -B worker.py`
  // is a bad option), so createEngine fails while Chromium is launching.
  const err = await rejectsWithin(createEngine({ root, warm: true, python: process.execPath }));
  assertTrue(Boolean(err) && !/still pending/.test(err.message),
    `S1: an engine whose Python exits at once fails to start (${err && err.message})`);
  await sleep(1000);
  const left = chromiumChildren();
  for (const pid of left) { try { process.kill(pid, 'SIGKILL'); } catch { /* gone */ } }
  assertEq(left, [], 'S1: ...and leaves no Chromium running');
}

async function noPython(createEngine, root) {
  // No interpreter to detect: PYTHON names a program that does not
  // exist. Detection used to exit the process, so the Studio's server
  // ended without the failure frame the desktop window waits for. Now
  // createEngine rejects, and the server reports it like any failed start.
  const before = process.env.PYTHON;
  process.env.PYTHON = path.join(root, 'no-such-python');
  let err;
  try {
    err = await rejectsWithin(createEngine({ root, warm: false }));
  } finally {
    if (before === undefined) delete process.env.PYTHON;
    else process.env.PYTHON = before;
  }
  assertTrue(Boolean(err) && /No Python interpreter/.test(err.message),
    `S2: with no usable Python, createEngine rejects instead of exiting (${err && err.message})`);
}

async function queuedAfterDispose(createEngine, root) {
  const marker = path.join(root, 'build-ran.txt');
  fs.writeFileSync(path.join(root, 'mark.js'),
    `require('fs').writeFileSync(${JSON.stringify(marker)}, 'ran');\n`);
  const engine = await createEngine({ root, warm: true });
  try {
    await engine.renderPreview({ doc: 'letter', scale: 1 });
    await engine.exclusive(async () => {});   // the Sass warm-up queued after the first preview
    // A Build and a restyling preview queued behind a job still running
    // when dispose() is called.
    let open;
    let running;
    const gate = new Promise(res => { open = res; });
    const started = new Promise(res => { running = res; });
    const held = engine.exclusive(() => { running(); return gate; });
    await started;
    const settle = p => p.then(() => null, e => e);
    const built = settle(engine.build({ script: 'mark.js' }));
    const restyled = settle(engine.renderPreview({ doc: 'letter', scale: 1, recompileStyles: true }));
    const disposing = engine.dispose();
    open();
    await held;
    await disposing;
    const [b, p] = await Promise.all([built, restyled]);
    assertTrue(Boolean(b) && /shut down/.test(b.message),
      `D1: a Build queued when dispose() ran fails saying the engine is shut down (${b && b.message})`);
    assertTrue(!fs.existsSync(marker), 'D1: ...and runs nothing');
    assertTrue(Boolean(p) && /shut down/.test(p.message),
      `D1: a preview queued when dispose() ran fails the same way (${p && p.message})`);
    assertEq(engine.status().sassWarm, false, 'D1: ...and does not start the Sass compiler again');
  } finally {
    await engine.dispose();
    fs.rmSync(path.join(root, 'mark.js'), { force: true });
    fs.rmSync(marker, { force: true });
  }
}

async function letterOverflow(createEngine, root) {
  const base = fs.readFileSync(path.join(root, 'data', 'letter_default.yml'), 'utf-8');
  if (!base.includes('    Vale,')) throw new Error('fixture: no "Vale," in letter_default.yml');
  const para = `    ${'Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod. '.repeat(5).trim()}\n\n`;
  const file = path.join(root, 'data', 'long_letter.yml');
  fs.writeFileSync(file, base.replace('    Vale,', para.repeat(12) + '    Vale,'));
  const engine = await createEngine({ root, warm: true });
  try {
    const env = { LETTER_DATA_FILE: file };
    const long = await engine.renderPreview({ doc: 'letter', scale: 1, env });
    assertEq(long.invariants && long.invariants.ok, false,
      'L1: the preview of a letter that runs off its page fails the layout check');
    assertTrue(/does not fit|overflows/.test((long.invariants && long.invariants.message) || ''),
      `L1: ...and says so (${long.invariants && long.invariants.message})`);
    const again = await engine.renderPreview({ doc: 'letter', scale: 1, env });
    assertEq(again.invariants && again.invariants.ok, false, 'L1: ...also when the render is reused');
    const fits = await engine.renderPreview({ doc: 'letter', scale: 1 });
    assertEq(fits.invariants && fits.invariants.ok, true, 'L1: the shipped letter passes it');
  } finally {
    await engine.dispose();
    fs.rmSync(file, { force: true });
  }
}

async function checkThatErrs(createEngine, root) {
  // No speculation: the check then always loads the final HTML itself.
  const engine = await createEngine({ root, warm: true, speculative: false });
  try {
    await engine.renderPreview({ doc: 'resume' });
    const pl = engine.pipelines.resume;
    const verify = pl.verifyInvariants;

    // A real violation, from the real check: one page more than the
    // placement has.
    pl.verifyInvariants = (page, pages, ...rest) => verify(page, pages + 1, ...rest);
    let r = null;
    let violated = null;
    try {
      r = await engine.renderPreview({ doc: 'resume', recompileStyles: true });
    } catch (e) {
      violated = e;
    } finally {
      pl.verifyInvariants = verify;
    }
    assertEq(r && r.invariants && r.invariants.ok, false,
      `V1: a layout violation is reported with the render, not as a failure (${violated && violated.message})`);

    // The check itself failing is not a layout verdict.
    pl.verifyInvariants = async () => { throw new Error('the check itself broke'); };
    const err = await rejectsWithin(engine.renderPreview({ doc: 'resume', recompileStyles: true }));
    pl.verifyInvariants = verify;
    assertTrue(Boolean(err) && /check itself broke/.test(err.message),
      `V1: an error inside the layout check fails the render (${err && err.message})`);
    const next = await engine.renderPreview({ doc: 'resume' });
    assertTrue(next.timings.cached !== 'render',
      'V1: ...and nothing from that render is reused by the next one');
    assertEq(next.invariants && next.invariants.ok, true, 'V1: ...which checks the layout again and passes');
  } finally {
    await engine.dispose();
  }
}

async function chromiumKilledWhileLaunching(createEngine, root) {
  const engine = await createEngine({ root, warm: false });
  const said = [];
  const write = process.stderr.write;
  process.stderr.write = function (chunk, ...rest) {
    said.push(String(chunk));
    return write.call(this, chunk, ...rest);
  };
  // Kill the Chromium the first render launches, as soon as it appears.
  let killed = false;
  const watch = setInterval(() => {
    for (const pid of chromiumChildren()) {
      killed = true;
      try { process.kill(pid, 'SIGKILL'); } catch { /* gone */ }
    }
  }, 1);
  try {
    const err = await rejects(engine.renderPreview({ doc: 'letter', scale: 1 }));
    clearInterval(watch);
    assertTrue(killed && err, `N5: the render whose Chromium was killed while launching fails (${killed})`);
    const text = said.join('');
    assertTrue(!/npx playwright install/.test(text),
      'N5: a Chromium killed while launching is not reported as a missing installation');
    assertTrue(/closed before it was ready/.test(text), 'N5: ...it is reported as having closed');
    assertEq(await rejects(engine.renderPreview({ doc: 'letter', scale: 1 })), null,
      'N5: the next render launches a new one');
  } finally {
    clearInterval(watch);
    process.stderr.write = write;
    await engine.dispose();
  }
}

async function stuckPage(createEngine, root) {
  const engine = await createEngine({ root, warm: true, timeouts: { pageMs: 1500 } });
  try {
    await engine.renderPreview({ doc: 'letter' });
    const pl = engine.pipelines.letter;
    const load = pl.openDocument;

    // A failed render's env is not left on the worker for the next caller.
    const worker = pl.python;
    const envErr = await rejectsWithin(engine.renderPreview({ doc: 'letter',
      env: { LETTER_DATA_FILE: path.join(root, 'data', 'missing.yml') } }));
    assertTrue(Boolean(envErr), 'E1: a render of a missing file fails');
    assertEq(worker.buildEnv, null, 'E1: ...and leaves no env behind on the worker');

    // The shape of a font load that never finishes.
    pl.openDocument = page => page.evaluate(() => new Promise(() => {}));
    let t0 = Date.now();
    let err = await rejectsWithin(engine.renderPreview({ doc: 'letter', recompileStyles: true }));
    assertEq(err && err.kind, 'timeout', `T1: a page that never settles fails the render (${err && err.message})`);
    assertTrue(Date.now() - t0 < 10000, `T1: ...at its deadline (${Date.now() - t0} ms)`);
    pl.openDocument = load;
    let next = await rejectsWithin(engine.renderPreview({ doc: 'letter', recompileStyles: true }));
    assertEq(next ? next.message.split('\n')[0] : null, null, 'T1: the next render works');

    // A renderer busy forever: the print never answers.
    pl.openDocument = async (page, opts) => {
      await load(page, opts);
      page.evaluate(() => { for (;;) { /* spin */ } }).catch(() => {});
    };
    t0 = Date.now();
    err = await rejectsWithin(engine.renderPreview({ doc: 'letter', recompileStyles: true }));
    assertEq(err && err.kind, 'timeout', `T1: a spinning renderer fails the render (${err && err.message})`);
    assertTrue(Date.now() - t0 < 10000, `T1: ...at its deadline (${Date.now() - t0} ms)`);
    pl.openDocument = load;
    next = await rejectsWithin(engine.renderPreview({ doc: 'letter', recompileStyles: true }));
    assertEq(next ? next.message.split('\n')[0] : null, null, 'T1: the next render works again');

  } finally {
    await engine.dispose();
  }
}

async function stuckOpen(createEngine, root) {
  const engine = await createEngine({ root, warm: true, hiddenReleaseMs: 50, timeouts: { pageMs: 1500 } });
  let proto = null;
  let newContext = null;
  try {
    // The engine's Browser class, from the page it hands the pipeline.
    const pl = engine.pipelines.letter;
    const load = pl.openDocument;
    pl.openDocument = (page, opts) => {
      proto = Object.getPrototypeOf(page.context().browser());
      return load(page, opts);
    };
    try {
      await engine.renderPreview({ doc: 'letter', scale: 1 });
    } finally {
      pl.openDocument = load;
    }
    // Hidden, Chromium is closed; the next render launches a new one,
    // whose newContext() here never answers.
    engine.setWindowHidden(true);
    assertTrue(await waitFor(() => !engine.status().browserOpen, 5000),
      'T2: hidden, Chromium is closed');
    newContext = proto.newContext;
    let asked = 0;
    proto.newContext = function () { asked += 1; return new Promise(() => {}); };
    const t0 = Date.now();
    const err = await rejectsWithin(engine.renderPreview({ doc: 'letter', scale: 1, recompileStyles: true }));
    proto.newContext = newContext;
    newContext = null;
    assertEq(asked, 1, 'T2: the render asked the new Chromium for a page');
    assertEq(err && err.kind, 'timeout',
      `T2: a Chromium that never opens a page fails the render (${err && err.message})`);
    assertTrue(Date.now() - t0 < 10000, `T2: ...at its deadline (${Date.now() - t0} ms)`);
    engine.setWindowHidden(false);
    const next = await rejectsWithin(engine.renderPreview({ doc: 'letter', scale: 1, recompileStyles: true }));
    assertEq(next ? next.message.split('\n')[0] : null, null, 'T2: the next render works');
  } finally {
    if (newContext) proto.newContext = newContext;
    await engine.dispose();
  }
}

async function crashedPage(createEngine, root) {
  const engine = await createEngine({ root, warm: true });
  try {
    await engine.renderPreview({ doc: 'resume' });
    const browsers = chromiumChildren();
    const pl = engine.pipelines.letter;
    const load = pl.openDocument;
    // The renderer dies, Chromium itself does not: its renderer
    // processes are killed, as an out-of-memory kill or a crash would.
    // (CDP's Page.crash was used first. On Playwright's headless shell
    // build of Chromium 148 it left the page hung instead of crashed, so
    // the render only ended at its 30 s deadline, with a new Chromium:
    // not the case this checks.)
    let killed = [];
    pl.openDocument = async (page, opts) => {
      pl.openDocument = load;
      killed = rendererPids(browsers);
      for (const pid of killed) {
        try { process.kill(pid, 'SIGKILL'); } catch { /* gone already */ }
      }
      await sleep(500);
      return load(page, opts);
    };
    const t0 = Date.now();
    await rejectsWithin(engine.renderPreview({ doc: 'letter', recompileStyles: true }));
    assertTrue(killed.length > 0, `C1: the page's renderer is found and killed (${killed})`);
    assertTrue(Date.now() - t0 < 15000, `C1: the render on a crashed page fails at once, not at a deadline (${Date.now() - t0} ms)`);
    assertTrue(await waitFor(() => !engine.status().browserOpen, 5000), 'C1: a crashed page is dropped');
    const err = await rejectsWithin(engine.renderPreview({ doc: 'letter', recompileStyles: true }));
    assertEq(err ? err.message.split('\n')[0] : null, null, 'C1: the next render works');
    const again = await rejectsWithin(engine.renderPreview({ doc: 'resume', recompileStyles: true }));
    assertEq(again ? again.message.split('\n')[0] : null, null, 'C1: ...and the one after');
    assertEq(chromiumChildren(), browsers, 'C1: ...in the same Chromium, not a new one');
  } finally {
    await engine.dispose();
  }
}

async function stuckBuild(createEngine, root) {
  fs.writeFileSync(path.join(root, 'hang.js'), 'setInterval(() => {}, 1000);\n');
  const engine = await createEngine({ root, timeouts: { buildMs: 1000 } });
  try {
    const t0 = Date.now();
    const err = await rejectsWithin(engine.build({ script: 'hang.js' }));
    assertEq(err && err.kind, 'timeout', `B1: a build that never ends fails at its deadline (${err && err.message})`);
    assertTrue(Date.now() - t0 < 10000, `B1: ...and not later (${Date.now() - t0} ms)`);
    const next = await rejectsWithin(engine.renderPreview({ doc: 'letter' }));
    assertEq(next ? next.message.split('\n')[0] : null, null, 'B1: the queue behind it moves on');
  } finally {
    await engine.dispose();
    fs.rmSync(path.join(root, 'hang.js'), { force: true });
  }
}

/* ─── Server ──────────────────────────────────────────────────── */

// Each server's launch token, by port, from its ready frame; every
// request here sends it, as the desktop shell and the page do.
const tokens = new Map();

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
        const { port, token } = JSON.parse(line.slice(READY_PREFIX.length));
        tokens.set(port, token);
        resolve({ child, log, port });
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
        'x-studio-token': tokens.get(port),
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
      headers: { host: `127.0.0.1:${port}`, 'x-studio-token': tokens.get(port) } }, (res) => {
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
    await run('N4 run', () => disposeWithRenderInFlight(createEngine, project.root));
    await run('N5 run', () => chromiumKilledWhileLaunching(createEngine, project.root));
    await run('S1 run', () => failedStart(createEngine, project.root));
    await run('S2 run', () => noPython(createEngine, project.root));
    await run('D1 run', () => queuedAfterDispose(createEngine, project.root));
    await run('L1 run', () => letterOverflow(createEngine, project.root));
    await run('V1 run', () => checkThatErrs(createEngine, project.root));
    await run('T1 run', () => stuckPage(createEngine, project.root));
    await run('T2 run', () => stuckOpen(createEngine, project.root));
    await run('C1 run', () => crashedPage(createEngine, project.root));
    await run('B1 run', () => stuckBuild(createEngine, project.root));
    await run('server run', () => serverTests(project.root));
  } finally {
    project.remove();
  }
  assertEq(realDistFingerprint(), before, "isolation: nothing was written to this checkout's dist/");
  report();
})();
