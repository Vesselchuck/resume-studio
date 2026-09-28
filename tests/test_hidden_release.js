/**
 * test_hidden_release.js — Chromium is closed while the Studio window is
 * hidden, and comes back when it is shown.
 *
 * WHAT THIS GUARDS
 * ----------------
 * Chromium is most of what the warm engine holds in memory. After the
 * window has been hidden for a while (five minutes in the app), the
 * engine closes it; see "Releasing Chromium while hidden" in
 * build/engine.js. This suite checks, with the delay cut to 200 ms:
 *
 *   • hidden long enough: the browser is closed;
 *   • shown again before the delay: nothing is closed;
 *   • shown again after a close: Chromium is relaunched without waiting
 *     for a render;
 *   • a render while it is closed still works, and relaunches it — and
 *     while the window stays hidden, Chromium is closed again after the
 *     delay (the Studio renders on every save, hidden or not);
 *   • a close never lands in the middle of a render;
 *   • renders before and after a close give identical pixels;
 *   • dispose() with a close pending leaves nothing running;
 *   • the server's POST /api/visibility reaches the engine.
 *
 * Runs in a throwaway copy of the project (tests/_project.js). Needs
 * Chromium; without it this prints the runner's SKIP marker.
 */

const path = require('path');
const http = require('http');
const { spawn } = require('child_process');
const { assertEq, assertTrue, fail, report } = require('./_framework');
const { tempProject, realDistFingerprint } = require('./_project');

const SUITE = 'test_hidden_release';
const READY_PREFIX = '\x1eSTUDIO_READY ';
const DELAY = 200;

const sleep = ms => new Promise(res => setTimeout(res, ms));

function skip(reason) {
  console.log(`SKIP ${SUITE}: ${reason}`);
  process.exitCode = 0;
}

async function waitFor(pred, ms = 15000) {
  const until = Date.now() + ms;
  while (!pred() && Date.now() < until) await sleep(25);
  return pred();
}

const hashes = r => r.images.map(im => im.hash);

async function engineTests(createEngine, root) {
  const engine = await createEngine({ root, warm: true, hiddenReleaseMs: DELAY });
  try {
    const first = await engine.renderPreview({ doc: 'resume' });
    assertTrue(engine.status().browserOpen, 'a warm engine has Chromium open');

    // Hidden, then shown before the delay: nothing happens.
    engine.setWindowHidden(true);
    await sleep(DELAY / 4);
    engine.setWindowHidden(false);
    await sleep(DELAY * 2);
    await engine.exclusive(async () => {});
    assertTrue(engine.status().browserOpen, 'shown again in time: Chromium stays open');

    // Hidden long enough: closed.
    engine.setWindowHidden(true);
    assertTrue(await waitFor(() => !engine.status().browserOpen),
      'hidden past the delay: Chromium is closed');

    // A render while closed works, relaunches, and draws the same pixels.
    const whileClosed = await engine.renderPreview({ doc: 'resume', recompileStyles: true });
    assertTrue(whileClosed.timings.browserLaunch > 0, 'a render while closed launches Chromium');
    assertEq(hashes(whileClosed), hashes(first), 'the relaunched Chromium draws the same pages');
    assertTrue(engine.status().browserOpen, 'and it stays open after that render');

    // Still hidden, and nothing else reported: the render restarted the
    // delay, so Chromium closes again once the saves stop.
    assertTrue(await waitFor(() => !engine.status().browserOpen),
      'a render while hidden restarts the delay: closed again with no new report');
    // Then shown: relaunched without a render.
    engine.setWindowHidden(false);
    assertTrue(await waitFor(() => engine.status().browserOpen),
      'shown after a close: Chromium is relaunched without waiting for a render');
    const afterShow = await engine.renderPreview({ doc: 'resume', recompileStyles: true });
    assertEq(afterShow.timings.browserLaunch, 0, 'so the next render pays nothing for the launch');
    assertEq(hashes(afterShow), hashes(first), 'and draws the same pages');

    // A close due while a render runs waits for it: the render completes.
    engine.setWindowHidden(true);
    const inFlight = engine.renderPreview({ doc: 'letter' });
    await sleep(DELAY + 50);
    const letter = await inFlight;
    assertTrue(letter.images.length > 0, 'a render in progress when the delay ends completes');
    assertTrue(await waitFor(() => !engine.status().browserOpen), 'and the close follows it');

    // Dispose with a close pending.
    engine.setWindowHidden(false);
    await waitFor(() => engine.status().browserOpen);
    engine.setWindowHidden(true);
  } finally {
    await engine.dispose();
  }
  await sleep(DELAY * 2);
  assertEq(engine.status().browserOpen, false, 'dispose with a close pending leaves no browser');
}

function post(port, route, body) {
  const data = JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: '127.0.0.1', port, path: route, method: 'POST',
      headers: {
        'content-type': 'application/json', 'content-length': Buffer.byteLength(data),
        host: `127.0.0.1:${port}`, origin: `http://127.0.0.1:${port}`,
      },
    }, (res) => {
      let buf = '';
      res.on('data', (c) => { buf += c; });
      res.on('end', () => resolve({ status: res.statusCode, data: JSON.parse(buf || '{}') }));
    });
    req.on('error', reject);
    req.end(data);
  });
}

/**
 * The server, as its own process (its shutdown ends the process): the
 * endpoint answers and the engine behind it takes the report.
 */
async function serverTest(project) {
  const child = spawn(process.execPath,
    [path.join(project.root, 'build', 'studio_server.js'), '--exit-with-parent'],
    { cwd: project.root, stdio: ['pipe', 'pipe', 'inherit'] });
  try {
    const port = await new Promise((resolve, reject) => {
      let buf = '';
      const timer = setTimeout(() => reject(new Error('server not ready in 30 s')), 30000);
      child.stdout.setEncoding('utf-8');
      child.stdout.on('data', (chunk) => {
        buf += chunk;
        const line = buf.split('\n').find(l => l.startsWith(READY_PREFIX));
        if (line) {
          clearTimeout(timer);
          resolve(JSON.parse(line.slice(READY_PREFIX.length)).port);
        }
      });
      child.once('exit', () => reject(new Error('server exited before it was ready')));
    });
    const r = await post(port, '/api/visibility', { hidden: true });
    assertEq(r.status, 200, 'POST /api/visibility answers 200');
    assertEq(r.data, { hidden: true }, 'and echoes what it was told');
    const back = await post(port, '/api/visibility', { hidden: false });
    assertEq(back.data, { hidden: false }, 'shown again');
  } finally {
    const exited = new Promise(res => child.once('exit', res));
    child.stdin.end();
    await Promise.race([exited, sleep(10000)]);
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
  const project = tempProject('hidden-release-test');
  try {
    const { createEngine } = project.require('build/engine');
    await engineTests(createEngine, project.root);
    await serverTest(project);
  } catch (err) {
    fail('hidden release run', { error: err.stack || err.message });
  } finally {
    project.remove();
  }
  assertEq(realDistFingerprint(), before, "isolation: nothing was written to this checkout's dist/");
  report();
})();
