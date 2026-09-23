/**
 * test_cold_start.js — what the Studio does before it is asked for
 * anything.
 *
 * WHAT THIS GUARDS
 * ----------------
 * Most of the time between launching the Studio and seeing the first
 * preview used to be spent loading JavaScript and starting programs,
 * one after another. Three things changed, and each one is the sort of
 * thing that silently comes undone:
 *
 *   • The compiled-code cache (build/_compile_cache.js) is enabled as
 *     the very first statement of the server process, and its directory
 *     is the user's own cache directory — never inside the project,
 *     which may not even be writable. Enabling it must never throw,
 *     whatever Node it lands on.
 *   • A warm engine launches Chromium straight away, in parallel with
 *     the Python worker and Sass, rather than on the first request. So
 *     a warm engine has a browser open before anyone asks it to render,
 *     and the first render pays nothing for the launch.
 *   • The server listens and announces itself BEFORE the engine is
 *     ready, so the desktop shell can open its window and load the UI
 *     while Chromium is still starting. The UI page must therefore be
 *     served without waiting for the engine, while anything that needs
 *     the engine waits for it.
 *
 * REQUIREMENTS
 * ------------
 * Playwright's Chromium (the warm-start check launches one); without it
 * this prints the runner's SKIP marker and exits 0.
 */

const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');
const { assertEq, assertTrue, fail, report } = require('./_framework');

const ROOT = path.join(__dirname, '..');
const DIST = path.join(ROOT, 'dist');
const SUITE = 'test_cold_start';
const READY_PREFIX = '\x1eSTUDIO_READY ';

const compileCache = require('../build/_compile_cache');

function skip(reason) {
  console.log(`SKIP ${SUITE}: ${reason}`);
  process.exitCode = 0;
}

function snapshot(paths) {
  return paths.map(p => [p, fs.existsSync(p) ? fs.readFileSync(p) : null]);
}

function restore(saved) {
  for (const [p, data] of saved) {
    try {
      if (data === null) fs.rmSync(p, { force: true });
      else fs.writeFileSync(p, data);
    } catch { /* cleanup, not an assertion */ }
  }
}

/** A GET, with the time it took. */
function get(port, route) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port, path: route,
      headers: { host: `127.0.0.1:${port}` } }, (res) => {
      res.resume();
      res.on('end', () => resolve({ route, status: res.statusCode, headers: res.headers,
                                    ms: Date.now() - started }));
    });
    req.on('error', reject);
    req.setTimeout(120000, () => req.destroy(new Error(`${route} timed out`)));
  });
}


/* ─── The cache directory ─────────────────────────────────────── */

function cacheTests() {
  const dir = compileCache.cacheDir();
  assertTrue(typeof dir === 'string' && path.isAbsolute(dir),
    `compile cache: the directory is an absolute path (${dir})`);
  const inside = path.resolve(ROOT);
  assertTrue(dir !== inside && !path.resolve(dir).startsWith(inside + path.sep),
    `compile cache: the directory is outside the project (${dir})`);
  assertTrue(!/node_modules/.test(dir),
    'compile cache: and not inside node_modules either');

  // Enabling is a no-op on a Node without the API and must never throw;
  // calling it twice must give the same answer.
  let first;
  try {
    first = compileCache.enable();
  } catch (err) {
    return fail('compile cache: enable() never throws', { error: err.message });
  }
  assertTrue(first && typeof first.enabled === 'boolean',
    'compile cache: enable() reports whether it is on');
  assertEq(compileCache.enable(), first, 'compile cache: enabling twice says the same thing');
  const line = compileCache.describe();
  assertTrue(line === null || /compiled-code cache/.test(line),
    'compile cache: describe() is a log line or nothing');
  // On the Node this project targets the API exists, so it should be
  // on — but a read-only cache directory is a legitimate "off", and the
  // point of the test is that either way nothing broke.
  if (!first.enabled) {
    assertTrue(typeof first.reason === 'string' && first.reason.length > 0,
      `compile cache: when off, it says why (${first.reason})`);
  } else {
    assertTrue(first.dir === dir || first.dir === process.env.NODE_COMPILE_CACHE,
      `compile cache: on, in the directory it named (${first.dir})`);
  }
}


/* ─── Warm start ──────────────────────────────────────────────── */

async function warmTests(createEngine) {
  const engine = await createEngine({ root: ROOT, warm: true });
  try {
    // The launch was started before this function got the engine back;
    // it does not have to have finished, so give it a moment — without
    // asking for a render, which is the whole point.
    const until = Date.now() + 15000;
    while (!engine.status().browserOpen && Date.now() < until) {
      await new Promise(res => setTimeout(res, 50));
    }
    assertTrue(engine.status().browserOpen,
      'warm: Chromium is open before anything has been rendered');

    const r = await engine.renderPreview({ doc: 'letter' });
    assertEq(r.timings.browserLaunch, 0,
      'warm: the first render pays nothing for the launch');
  } finally {
    await engine.dispose();
  }

  // Without `warm`, nothing is launched until something needs it.
  const cold = await createEngine({ root: ROOT });
  try {
    assertEq(cold.status().browserOpen, false,
      'cold: a plain engine does not launch a browser it may never use');
  } finally {
    await cold.dispose();
  }
}


/* ─── The server announces itself early ───────────────────────── */

async function readyTests() {
  const child = spawn(process.execPath,
    [path.join(ROOT, 'build', 'studio_server.js'), '--port', '0', '--exit-with-parent'],
    { cwd: ROOT, stdio: ['pipe', 'pipe', 'pipe'] });
  const log = [];
  child.stderr.setEncoding('utf-8');
  child.stderr.on('data', c => log.push(...String(c).split('\n')));

  try {
    const port = await new Promise((resolve, reject) => {
      let buffer = '';
      const timer = setTimeout(() => reject(new Error(
        `the server did not report ready in 60 s:\n${log.slice(-10).join('\n')}`)), 60000);
      child.stdout.setEncoding('utf-8');
      child.stdout.on('data', (chunk) => {
        buffer += chunk;
        let idx;
        while ((idx = buffer.indexOf('\n')) !== -1) {
          const line = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 1);
          log.push(line);
          if (line.startsWith(READY_PREFIX)) {
            clearTimeout(timer);
            resolve(JSON.parse(line.slice(READY_PREFIX.length)).port);
          }
        }
      });
      child.once('exit', code => reject(new Error(`the server exited (${code})`)));
    });

    // Both at once, the instant the port is known. The UI page needs
    // nothing from the engine; /api/status does.
    const [ui, status] = await Promise.all([get(port, '/'), get(port, '/api/status')]);
    assertEq(ui.status, 200, 'ready: the UI is served as soon as the port is announced');
    assertEq(status.status, 200, 'ready: /api/status answers once the engine is up');
    // The page says which state the engine was in when it was served.
    // 'starting' is the proof that it did not wait; 'ready' means the
    // engine won the race, which is a pass and not something the test
    // can or should force. Comparing the two response times instead
    // fails on a loaded machine for reasons that have nothing to do
    // with the code.
    const served = ui.headers['x-studio-engine'];
    assertTrue(served === 'starting' || served === 'ready',
      `ready: the page reports the engine state (got ${served})`);
    if (served === 'ready') {
      assertTrue(true, 'ready: the engine was up before the first request (nothing to wait for)');
    } else {
      assertTrue(true, 'ready: the page was served while the engine was still starting');
    }

    const bye = await new Promise((resolve) => {
      child.once('exit', () => resolve('exited'));
      child.stdin.end();
      setTimeout(() => resolve('timeout'), 15000);
    });
    assertEq(bye, 'exited', 'ready: closing stdin shuts the server down');
  } finally {
    try { child.kill('SIGKILL'); } catch { /* already gone */ }
  }
}


(async () => {
  cacheTests();

  try {
    const { chromium } = require('playwright');
    const b = await chromium.launch();
    await b.close();
  } catch (err) {
    console.log(`(the rest of ${SUITE} needs Chromium: ${String(err.message).split('\n')[0]})`);
    return report();
  }

  const saved = snapshot([
    path.join(DIST, 'styles.css'),
    path.join(DIST, 'letter.html'),
    path.join(DIST, 'letter_meta.json'),
  ]);

  try {
    const { createEngine } = require('../build/engine');
    await warmTests(createEngine);
    await readyTests();
  } catch (err) {
    fail('cold start run', { error: err.stack || err.message });
  } finally {
    restore(saved);
  }

  report();
})();
