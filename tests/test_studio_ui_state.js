/**
 * test_studio_ui_state.js — the Studio page's own bookkeeping, in a real
 * browser: the build queue, a run that has ended, where focus goes, the
 * console's run headers, the drop overlay over a dialog, and another
 * window's build.
 *
 * WHAT THIS GUARDS
 * ----------------
 *   • Builds pressed during a preview all run, once each, in the order
 *     pressed, and a queued one says so on its button. One slot used to
 *     keep only the last press, and a press in the moment after the
 *     preview ended ran the same build twice.
 *   • A run that has ended (the server restarted with a new token, so
 *     the page's cookie is refused, or it is gone): one banner, no
 *     "Render failed: forbidden" or "Build failed", nothing more tried,
 *     and F5 reloads the page instead of re-rendering.
 *   • Focus: Dismiss on a build's error card goes to that card's Build
 *     button, closing Diagnostics from inside it goes to its summary, a
 *     control whose words change (the Diagnostics summary) keeps focus
 *     across a redraw, and the Find box keeps its caret.
 *   • A console whose run began above the lines shown still names it.
 *   • The drop overlay draws above an open dialog.
 *   • Another window's build shows as running here, and its result
 *     shows when it ends.
 *
 * Most of it stubs one route with page.route (a held preview, a build
 * that answers at once or fails) against the real server; the build seen
 * from another window, and the run that has ended, are real.
 *
 * ISOLATION: a throwaway copy of the project with only the templates in
 * data/, as in test_studio_ui.js.
 *
 * REQUIREMENTS: Playwright's Chromium; skips cleanly without it.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawn } = require('child_process');
const { assertEq, assertTrue, fail, report } = require('./_framework');
const { copyProject, realDistFingerprint } = require('./_project');

const READY_PREFIX = '\x1eSTUDIO_READY ';
const ENDED = 'This Studio run has ended';
const sleep = ms => new Promise(res => setTimeout(res, ms));

function startServer(projectDir) {
  const env = { ...process.env };
  for (const k of ['RESUME_DATA_SOURCE', 'RESUME_DATA_FILE', 'LETTER_DATA_FILE', 'STUDIO_PORT']) {
    delete env[k];
  }
  const child = spawn(process.execPath,
    [path.join(projectDir, 'build', 'studio_server.js'), '--port', '0', '--exit-with-parent', '--open-dry-run'],
    { cwd: projectDir, env, stdio: ['pipe', 'pipe', 'pipe'] });
  const log = [];
  return new Promise((resolve, reject) => {
    let buffer = '';
    const timer = setTimeout(() => reject(new Error(`server did not start:\n${log.slice(-20).join('\n')}`)), 60000);
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
          const { port, token, url } = JSON.parse(line.slice(READY_PREFIX.length));
          resolve({ child, log, port, token, url });
        }
      }
    });
    child.stderr.setEncoding('utf-8');
    child.stderr.on('data', chunk => log.push(...String(chunk).split('\n')));
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`server exited (${code}) before ready:\n${log.slice(-20).join('\n')}`));
    });
  });
}

/** A page that records every CSP violation and page error into `violations`. */
async function newPage(ctx, violations) {
  const page = await ctx.newPage();
  await page.exposeFunction('__cspViolation', v => violations.push(v));
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation',
      e => window.__cspViolation(`${e.violatedDirective} ${e.blockedURI}`));
  });
  page.on('console', (msg) => {
    if (/Content.Security.Policy/i.test(msg.text())) violations.push(msg.text());
  });
  page.on('pageerror', err => violations.push(`page error: ${err.message}`));
  return page;
}

/**
 * The page's event stream, cut off: the first one ends at once, and the
 * page's reconnect waits until the returned function is called, then
 * goes to the real server.
 */
async function holdStream(page) {
  let n = 0, release;
  const gate = new Promise((res) => { release = res; });
  await page.route('**/api/events', async (route) => {
    if (++n === 1) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'retry: 100\n\n' });
    }
    await gate;
    return route.continue().catch(() => {});
  });
  return release;
}

const card = (page, i) => page.locator('.doc').nth(i);
const buildButton = (page, i) => card(page, i).locator('.doc-actions button');
const focused = page => page.evaluate(() => {
  const a = document.activeElement;
  return a ? { tag: a.tagName, id: a.id, text: a.textContent.trim().slice(0, 60), sel: a.selectionStart == null ? null : a.selectionStart } : null;
});

async function run(project, { port, token, url }, chromium) {
  const base = `http://127.0.0.1:${port}/`;
  const violations = [];
  const browser = await chromium.launch();
  try {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await newPage(ctx, violations);
    await page.goto(url);
    await page.waitForSelector('.sheet img', { timeout: 90000 });
    const previewOf = doc => r => r.url().endsWith('/api/preview') && r.request().method() === 'POST'
      && JSON.parse(r.request().postData()).doc === doc;

    /* ── the build queue ─────────────────────────────────────── */
    // Builds answer at once here (no PDF, so the page renders instead);
    // what is checked is which builds the page asks for, and in what order.
    const builds = [];
    await page.route('**/api/build', async (route) => {
      const doc = JSON.parse(route.request().postData()).doc;
      builds.push(doc);
      await sleep(200);
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ doc, ms: 1, pdf: { exists: false }, render: null }) });
    });
    let holdPreview = false;
    await page.route('**/api/preview', async (route) => {
      if (holdPreview) { holdPreview = false; await sleep(1500); }
      await route.continue().catch(() => {});
    });
    const settle = async (n) => {
      for (let i = 0; i < 200 && builds.length < n; i++) await sleep(100);
      // Long enough for a second run of the same build to have started.
      await sleep(1500);
      await page.waitForFunction(() => !document.querySelector('.doc .is-busy'), null, { timeout: 30000 }).catch(() => null);
    };

    // Two documents queued during one preview: both build, in that order.
    holdPreview = true;
    await Promise.all([
      page.waitForRequest(r => r.url().endsWith('/api/preview'), { timeout: 5000 }),
      page.click('#menuRefresh'),
    ]);
    await buildButton(page, 0).click();
    const queued = await buildButton(page, 0).evaluate(b => [b.textContent, b.classList.contains('is-busy')]);
    assertTrue(/queued/i.test(queued[0]) && queued[1],
      `build queue: a queued build says so on its button (${JSON.stringify(queued)})`);
    await buildButton(page, 1).click();
    await settle(2);
    assertEq(builds, ['resume', 'letter'], 'build queue: two builds pressed during a preview both run, in order');

    // Pressed again in the moment after the preview ended, while its
    // status read is still out: the queued build still runs once.
    builds.length = 0;
    let holdStatus = false, statusHeld;
    const statusOut = new Promise((res) => { statusHeld = res; });
    await page.route('**/api/status', async (route) => {
      if (holdStatus) { holdStatus = false; statusHeld(); await sleep(1000); }
      await route.continue().catch(() => {});
    });
    holdPreview = true;
    await Promise.all([
      page.waitForRequest(r => r.url().endsWith('/api/preview'), { timeout: 5000 }),
      page.click('#menuRefresh'),
    ]);
    await buildButton(page, 0).click();
    holdStatus = true;
    await statusOut;
    // A click as a pointer makes it: Playwright would wait for the button
    // to stop being aria-disabled, which a person does not.
    await buildButton(page, 0).evaluate(b => b.click());
    await settle(1);
    assertEq(builds, ['resume'], 'build queue: a second press for a queued build does not run it twice');
    await page.unroute('**/api/status');
    await page.unroute('**/api/preview');

    /* ── focus: an error card's Dismiss, Diagnostics, the Find box ── */
    await page.unroute('**/api/build');
    await page.route('**/api/build', route => route.fulfill({ status: 500, contentType: 'application/json',
      body: JSON.stringify({ error: 'resume.js failed', failures: ['❌ Layout: a stubbed failure'] }) }));
    await buildButton(page, 0).focus();
    await page.keyboard.press('Enter');
    await page.waitForSelector('.doc .errcard button', { timeout: 10000 });
    await page.focus('.doc .errcard button');
    await page.keyboard.press('Enter');
    await page.waitForSelector('.doc .errcard', { state: 'detached', timeout: 3000 });
    const afterDismiss = await focused(page);
    assertTrue(afterDismiss && afterDismiss.tag === 'BUTTON' && /^Build Resume PDF/.test(afterDismiss.text),
      `Dismiss: focus goes to that card's Build button (${JSON.stringify(afterDismiss)})`);
    await page.unroute('**/api/build');

    const diagOpen = () => page.evaluate(() => document.getElementById('diagDet').open);
    if (!(await diagOpen())) await page.keyboard.press('Control+Shift+D');
    await page.focus('#logFind');
    await page.keyboard.press('Control+Shift+D');
    assertEq(await diagOpen(), false, 'Diagnostics: (Ctrl+Shift+D from the Find box closes it)');
    const afterClose = await focused(page);
    assertEq(afterClose && afterClose.tag, 'SUMMARY', 'Diagnostics: closed from inside, focus goes to its summary');
    await page.keyboard.press('Control+Shift+D');

    // The Find box is rebuilt with the Inspector on every render.
    await page.focus('#logFind');
    await page.keyboard.type('xyz');
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowLeft');
    await Promise.all([
      page.waitForResponse(previewOf('resume'), { timeout: 30000 }),
      page.keyboard.press('F5'),
    ]);
    await sleep(300);
    const caret = await focused(page);
    assertEq([caret && caret.id, caret && caret.sel], ['logFind', 1],
      'Find: a redraw keeps the focus and the caret where they were');
    await page.fill('#logFind', '');

    /* ── the drop overlay is above an open dialog ──────────────── */
    await page.focus('#menuRefresh');
    await page.keyboard.press('Control+o');
    await page.waitForSelector('#filesDlg');
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['x: 1\n'], 'x.yml'));
      window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true, cancelable: true }));
    });
    assertTrue(await page.evaluate(() => Boolean(document.querySelector('.drop'))
      && Boolean(document.elementFromPoint(8, 8).closest('.drop'))),
      'drop: a file dragged in over a dialog shows its prompt on top, not under the backdrop');
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['x: 1\n'], 'x.yml'));
      window.dispatchEvent(new DragEvent('dragleave', { dataTransfer: dt, bubbles: true, cancelable: true }));
    });
    await page.keyboard.press('Escape');
    await page.waitForSelector('#filesDlg', { state: 'detached', timeout: 3000 });

    /* ── a build, seen from another window; focus kept by key ──── */
    const other = await newPage(ctx, violations);
    await other.goto(base);
    await other.waitForSelector('.sheet img', { timeout: 90000 });
    const letterStatus = p => card(p, 1).locator('.status').textContent();
    await page.keyboard.press('Control+2');
    await page.waitForResponse(previewOf('letter'), { timeout: 30000 }).catch(() => null);
    await page.waitForFunction(() => !document.querySelector('.doc .track'), null, { timeout: 30000 });
    if (!(await diagOpen())) await page.keyboard.press('Control+Shift+D');
    await page.focus('#diagDet > summary');
    const seenBuilding = other.waitForFunction(
      () => /Building/.test(document.querySelectorAll('.doc')[1].querySelector('.status').textContent),
      null, { timeout: 60000, polling: 50 }).then(() => true, () => false);
    const [builtReply] = await Promise.all([
      page.waitForResponse(r => r.url().endsWith('/api/build'), { timeout: 180000 }),
      page.keyboard.press('Control+Shift+B'),
    ]);
    assertEq(builtReply.status(), 200, 'build: (the cover letter built)');
    assertTrue(await seenBuilding, 'another window: shows the build as running');
    await other.waitForFunction(
      () => /^Built/.test(document.querySelectorAll('.doc')[1].querySelector('.status').textContent),
      null, { timeout: 15000 }).catch(() => null);
    assertTrue(/^Built/.test(await letterStatus(other)),
      `another window: ...and shows it built when it ends (${await letterStatus(other)})`);
    await page.waitForFunction(() => !document.querySelector('.doc .is-busy'), null, { timeout: 30000 });
    await sleep(300);
    const onSummary = await focused(page);
    assertEq(onSummary && onSummary.tag, 'SUMMARY',
      'focus: the Diagnostics summary keeps focus though its words and the controls before it changed');
    await other.close();

    /* ── the console names a run that began above its lines ────── */
    const logged = await newPage(ctx, violations);
    let openStream;
    const streamGate = new Promise((res) => { openStream = res; });
    await logged.route('**/api/events', async (route) => {
      await streamGate;
      const lines = Array.from({ length: 200 }, (_, i) => `event: log\ndata: ${JSON.stringify({ line: `line ${i}` })}\n\n`);
      return route.fulfill({ status: 200, contentType: 'text/event-stream',
        body: `retry: 600000\n\n${lines.join('')}` }).catch(() => {});
    });
    await logged.goto(base);
    await logged.waitForSelector('.sheet img', { timeout: 90000 });
    openStream();
    if (!(await logged.evaluate(() => document.getElementById('diagDet').open))) {
      await logged.keyboard.press('Control+Shift+D');
    }
    await logged.waitForFunction(() => /line 199/.test(document.getElementById('logBox').textContent),
      null, { timeout: 10000 });
    assertEq(await logged.textContent('#logBox .lg-runhead .lg-what'), 'Preview · Resume',
      'console: lines of a run whose header scrolled away are still under its name');
    await logged.close();

    /* ── a run that has ended ──────────────────────────────────── */
    // A restarted server has a new token: the page's cookie is refused
    // with 403 {error:'forbidden'}, which is what clearing it gives here.
    const ctx2 = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const first = await newPage(ctx2, violations);
    const releaseFirst = await holdStream(first);
    await first.goto(url);
    await first.waitForSelector('.sheet img', { timeout: 90000 });
    const second = await newPage(ctx2, violations);
    const releaseSecond = await holdStream(second);
    await second.goto(base);
    await second.waitForSelector('.sheet img', { timeout: 90000 });
    await ctx2.clearCookies();
    const ended = p => p.waitForFunction(t => document.body.innerText.indexOf(t) !== -1, ENDED, { timeout: 10000 })
      .then(() => true, () => false);

    const diagWas = await first.evaluate(() => document.getElementById('diagDet').open);
    await first.keyboard.press('F5');
    assertTrue(await ended(first), 'ended: a render refused for its token says the run has ended');
    assertTrue(await first.evaluate(was => document.getElementById('errOverlay').hidden
      && document.getElementById('alertSay').textContent === ''
      && document.getElementById('diagDet').open === was, diagWas),
      'ended: ...and is not shown as a failed render, nor opens Diagnostics');
    const tried = first.waitForRequest(r => r.url().endsWith('/api/build'), { timeout: 1500 }).then(() => true, () => false);
    await buildButton(first, 0).click();
    assertEq(await tried, false, 'ended: Build does not try');
    await sleep(500);
    assertTrue(!(await first.$('.doc .errcard')) && !/Build failed/.test(await first.textContent('#toasts')),
      'ended: ...and nothing says "Build failed"');
    const reloaded = first.waitForNavigation({ timeout: 10000 }).then(() => true, () => false);
    await first.keyboard.press('F5');
    assertTrue(await reloaded, 'ended: F5 reloads the page');
    assertEq(Boolean(await first.$('#docList')), false, "ended: ...which is now the server's page saying where to open the Studio");

    releaseSecond();
    assertTrue(await ended(second), 'ended: an event stream refused for its token says the run has ended too');
    releaseFirst();
    await ctx2.close();

    // The server gone: a request that cannot connect while the event
    // stream is down.
    const gone = await newPage(ctx, violations);
    const releaseGone = await holdStream(gone);
    await gone.goto(base);
    await gone.waitForSelector('.sheet img', { timeout: 90000 });
    await gone.route('**/api/preview', route => route.abort('connectionrefused'));
    await gone.keyboard.press('F5');
    assertTrue(await ended(gone), 'ended: a server that cannot be reached at all ends the run as well');
    assertTrue(await gone.evaluate(() => document.getElementById('errOverlay').hidden),
      'ended: ...not "Render failed: Failed to fetch"');
    releaseGone();
    await gone.close();

    await page.waitForTimeout(300);
    // A refused request is logged by the browser as a failed resource
    // load, which is not a policy violation.
    assertEq(violations, [], "CSP: none of it breaks a rule of the page's policy");
  } finally {
    await browser.close();
  }
}

(async () => {
  let chromium;
  try {
    ({ chromium } = require('playwright'));
    const b = await chromium.launch();
    await b.close();
  } catch (err) {
    console.log(`SKIP studio_ui_state: Chromium unavailable (${String(err.message).split('\n')[0]})`);
    process.exit(0);
  }
  const before = realDistFingerprint();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-ui-state-test-'));
  const project = path.join(tmp, 'project');
  let srv = null;
  try {
    copyProject(project);
    srv = await startServer(project);
    await run(project, srv, chromium);
  } catch (err) {
    fail('studio ui state run', { error: `${err.stack || err.message}\n${srv ? srv.log.slice(-30).join('\n') : ''}` });
  } finally {
    if (srv) {
      const exited = new Promise(res => srv.child.once('exit', res));
      srv.child.stdin.end();
      const done = await Promise.race([exited.then(() => true), sleep(8000).then(() => false)]);
      if (!done) { try { srv.child.kill('SIGKILL'); } catch { /* gone */ } }
    }
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* best effort */ }
  }
  assertEq(realDistFingerprint(), before, "isolation: nothing was written to this checkout's dist/");
  report();
})();
