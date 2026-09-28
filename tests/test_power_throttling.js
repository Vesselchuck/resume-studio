/**
 * test_power_throttling.js — the engine asks for the Windows power-
 * throttling opt-out, and asking cannot break its start.
 *
 * WHAT THIS GUARDS
 * ----------------
 *   • After the first preview the engine has asked the worker to opt its
 *     process tree out (build/_power.py), and status().powerThrottling
 *     says what came of it. On Windows: this process and the worker are
 *     among the processes opted out. Elsewhere the engine is told it is
 *     on Windows (the `platform` option), so the whole JavaScript path
 *     runs and the worker answers that it is not.
 *   • Not on Windows, unasked: nothing is sent.
 *   • The launch race. Chromium's launch runs on its own during start-up
 *     and asks for the opt-out when it finishes. If it finished before
 *     Python had booted, the state it touched was not declared yet: the
 *     launch threw after Chromium was up, the first render launched a
 *     second Chromium, and the first was never closed. Here Python is
 *     made to start 3 s late; the engine must start, render, and let
 *     its process exit on its own after dispose(). Needs a POSIX shell
 *     to delay Python, so this part does not run on Windows — the bug is
 *     in JavaScript ordering, which is the same everywhere.
 *
 * Runs in a throwaway copy of the project (tests/_project.js). Needs
 * Chromium; without it this prints the runner's SKIP marker.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawn } = require('child_process');
const { assertEq, assertTrue, fail, report } = require('./_framework');
const { tempProject, realDistFingerprint } = require('./_project');
const { detectPython } = require('../build/detect_python');

const SUITE = 'test_power_throttling';
const WINDOWS = process.platform === 'win32';

function skip(reason) {
  console.log(`SKIP ${SUITE}: ${reason}`);
  process.exitCode = 0;
}

async function optOutTests(createEngine, root) {
  const engine = await createEngine({ root, warm: true, platform: 'win32' });
  try {
    assertEq(engine.status().powerThrottling === null
      || typeof engine.status().powerThrottling === 'object', true,
    'status carries powerThrottling');
    await engine.renderPreview({ doc: 'letter' });
    await engine.exclusive(async () => {});
    const r = engine.status().powerThrottling;
    assertTrue(r && Array.isArray(r.applied) && Array.isArray(r.failed),
      'after the first preview the opt-out has run and reported');
    if (WINDOWS) {
      assertTrue(r.supported, `on Windows it is supported (${JSON.stringify(r)})`);
      assertTrue(r.applied.includes(process.pid), 'the Node process is opted out');
      const workerPid = engine.status().worker && engine.status().worker.pid;
      if (workerPid) assertTrue(r.applied.includes(workerPid), 'the Python worker is opted out');
    } else {
      assertEq(r.supported, false, 'off Windows the worker says it is not supported');
      assertTrue(/not Windows/.test(r.reason || ''), 'and why');
    }
  } finally {
    await engine.dispose();
  }

  if (!WINDOWS) {
    const plain = await createEngine({ root, warm: true });
    try {
      await plain.renderPreview({ doc: 'letter' });
      await plain.exclusive(async () => {});
      assertEq(plain.status().powerThrottling, null, 'off Windows, nothing is sent unasked');
    } finally {
      await plain.dispose();
    }
  }
}

/** The launch race, in a child process that must exit on its own. */
async function launchRaceTest(root) {
  const python = detectPython();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'slowpy-'));
  const slowPython = path.join(tmp, 'python');
  fs.writeFileSync(slowPython, `#!/bin/sh\nsleep 3\nexec "${python}" "$@"\n`, { mode: 0o755 });
  const script = `
    (async () => {
      const { createEngine } = require(${JSON.stringify(path.join(root, 'build', 'engine'))});
      const e = await createEngine({ root: ${JSON.stringify(root)}, warm: true,
        platform: 'win32', python: ${JSON.stringify(slowPython)} });
      const r = await e.renderPreview({ doc: 'letter' });
      await e.exclusive(async () => {});
      console.log('RESULT ' + JSON.stringify({ pages: r.images.length,
        launch: r.timings.browserLaunch, opted: e.status().powerThrottling !== null }));
      await e.dispose();
    })().catch((err) => { console.log('ERROR ' + err.message); process.exitCode = 1; });
  `;
  try {
    // Its own process group, so a Chromium it leaves behind can be killed
    // with it: otherwise that orphan holds these pipes open and this
    // suite, having reported the failure, would never exit either.
    const child = spawn(process.execPath, ['-e', script],
      { cwd: root, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
    let out = '';
    child.stdout.on('data', (c) => { out += c; });
    child.stderr.on('data', (c) => { out += c; });
    const code = await new Promise((resolve) => {
      const timer = setTimeout(() => {
        try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already gone */ }
        child.stdout.destroy();
        child.stderr.destroy();
        resolve('timeout');
      }, 60000);
      child.once('exit', (c) => { clearTimeout(timer); resolve(c); });
    });
    // A Chromium left behind by a clean exit is still in the group.
    try { process.kill(-child.pid, 'SIGKILL'); } catch { /* nothing left */ }
    const line = (out.match(/^RESULT (.*)$/m) || [])[1];
    const result = line ? JSON.parse(line) : null;
    assertTrue(result && result.pages > 0, `launch race: the engine renders (${(out.match(/^ERROR.*$/m) || [''])[0]})`);
    assertEq(result && result.launch, 0,
      'launch race: the first render uses the Chromium launched at start-up, not a second one');
    assertTrue(result && result.opted, 'launch race: the opt-out ran');
    assertEq(code, 0, 'launch race: after dispose() the process exits on its own (no Chromium left behind)');
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

(async () => {
  try {
    const { chromium } = require('playwright');
    const b = await chromium.launch();
    await b.close();
  } catch (err) {
    return skip(`chromium unavailable (${String(err.message).split('\n')[0]})`);
  }

  const before = realDistFingerprint();
  const project = tempProject('power-throttling-test');
  try {
    const { createEngine } = project.require('build/engine');
    await optOutTests(createEngine, project.root);
    if (!WINDOWS) await launchRaceTest(project.root);
  } catch (err) {
    fail('power throttling run', { error: err.stack || err.message });
  } finally {
    project.remove();
  }
  assertEq(realDistFingerprint(), before, "isolation: nothing was written to this checkout's dist/");

  // Every engine here was disposed: nothing it started may still be a
  // child of this process. A Chromium left behind is exactly the launch
  // race's symptom, and would also keep this suite from ever exiting —
  // so it is reported, then killed.
  await new Promise(res => setTimeout(res, 500));
  const left = process._getActiveHandles()
    .filter(h => h && h.constructor && h.constructor.name === 'ChildProcess' && h.exitCode === null);
  assertEq(left.map(h => h.spawnfile), [], 'no child process of this suite outlives its engine');
  for (const h of left) {
    try { h.kill('SIGKILL'); } catch { /* already gone */ }
    for (const s of [h.stdin, h.stdout, h.stderr]) if (s) s.destroy();
  }
  report();
})();
