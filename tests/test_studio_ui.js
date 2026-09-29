/**
 * test_studio_ui.js — the Studio page in a real browser: its keyboard
 * shortcuts, the error overlay, the change highlight and the settings
 * that must survive a new launch.
 *
 * WHAT THIS GUARDS
 * ----------------
 *   • Settings outlive the origin. The desktop app serves the page from
 *     a new port each launch — a new origin, with empty localStorage —
 *     so a theme or zoom chosen in one window must come back in a page
 *     opened from another origin (here: 127.0.0.1 then localhost).
 *   • F5 and Ctrl+R re-render instead of reloading the page; the other
 *     shortcuts do what the help dialog (F1) says they do, and none of
 *     them fires while a dialog is open.
 *   • A failed render puts its location first, over the dimmed last good
 *     render: file:line:col for a schema error and for a YAML syntax
 *     error, Copy location (clipboard + a screen-reader message) and Open
 *     (POST /api/open); a first render that fails shows what to do.
 *   • A render outlines what changed, the outlines go after four
 *     seconds, and turning the highlight off stops them.
 *   • None of it breaks the page's Content-Security-Policy.
 *
 * ISOLATION: a throwaway copy of the project with only the templates in
 * data/, as in test_studio_server.js. /api/open runs dry (it reports the
 * command) so no editor is started.
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
          resolve({ child, log, port: JSON.parse(line.slice(READY_PREFIX.length)).port });
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

/** A page that records every CSP violation into `violations`. */
async function openPage(ctx, url, violations) {
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
  await page.goto(url);
  return page;
}

async function run(project, port, chromium) {
  const base = `http://127.0.0.1:${port}/`;
  const violations = [];
  const browser = await chromium.launch();
  try {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base.slice(0, -1) });
    const dataFile = path.join(project, 'data', 'resume.yml');
    const template = fs.readFileSync(path.join(project, 'data', 'resume_default.yml'), 'utf-8');
    const lines = template.split('\n');
    const idLine = lines.findIndex(l => /^\s*- id: languages\s*$/.test(l));
    const broken = template.replace('- id: languages', '- id: Bad_languages');

    /* ── a first render that fails: what to do, and where ───────── */
    fs.writeFileSync(dataFile, broken);
    let page = await openPage(ctx, base, violations);
    await page.waitForSelector('#errOverlay', { state: 'visible', timeout: 90000 });
    // The build names the file the way the platform writes paths
    // (data\resume.yml on Windows), and the overlay shows it as given.
    const rel = path.join('data', 'resume.yml');
    const where = `${rel}:${idLine + 1}:${lines[idLine].indexOf('id:') + 1}`;
    assertEq(await page.textContent('#eoLoc'), where, 'overlay: the location comes first, file:line:col');
    assertTrue(/kebab-case/.test(await page.textContent('#eoMsg')), 'overlay: ...then the message');
    assertEq(await page.textContent('#eoHeld'), 'Nothing rendered yet', 'overlay: says there is no earlier render to show');
    const empty = await page.textContent('.placeholder');
    assertTrue(!/press Build/i.test(empty) && /save/i.test(empty),
      `empty state: says to fix and save, not to press Build (${empty.trim()})`);
    assertTrue(Boolean(await page.$('.placeholder button')), 'empty state: offers to open the data file');
    assertTrue(/Render failed at data[\\/]resume\.yml, line \d+, column \d+/.test(await page.textContent('#alertSay')),
      'overlay: a screen reader is told the location in words');

    /* ── a good render, then a broken one: the last good one stays ── */
    fs.writeFileSync(dataFile, template);
    await page.waitForSelector('#errOverlay', { state: 'hidden', timeout: 30000 });
    await page.waitForSelector('.sheet img', { timeout: 30000 });
    assertEq(await page.textContent('#alertSay'), '', 'overlay: recovery clears the alert');
    fs.writeFileSync(dataFile, broken);
    await page.waitForSelector('#errOverlay', { state: 'visible', timeout: 30000 });
    assertTrue(/^Showing last good render · /.test(await page.textContent('#eoHeld')),
      'overlay: the last good render stays underneath, with its time');
    assertTrue((await page.$$('.sheet.held img')).length > 0, 'overlay: ...dimmed');
    await page.click('#eoCopy');
    await page.waitForFunction(w => document.getElementById('say').textContent === `Copied ${w}`, where, { timeout: 3000 });
    assertEq(await page.evaluate(() => navigator.clipboard.readText()), where,
      'Copy location: the clipboard holds file:line:col, and a screen reader hears it');
    const [opened] = await Promise.all([
      page.waitForResponse(r => r.url().endsWith('/api/open') && r.request().method() === 'POST'),
      page.click('#eoOpen'),
    ]);
    const openedBody = await opened.json();
    assertEq([opened.status(), openedBody.opened], [200, 'data/resume.yml'],
      'Open: asks the server to open the file the error is in');
    assertEq(JSON.parse(opened.request().postData()),
      { path: rel, line: idLine + 1, col: lines[idLine].indexOf('id:') + 1 },
      'Open: ...at the line and column');
    assertEq(await page.textContent('#eoOpen'), openedBody.editor === 'vscode' ? 'Open in VS Code' : 'Open file',
      'Open: the button says what it will open the file in');

    // A YAML syntax error: PyYAML's "line N, column M" becomes the location.
    fs.writeFileSync(dataFile, `${template}\nsidebar: [unclosed\n`);
    await page.waitForFunction(() => /^Not valid YAML/.test(document.getElementById('eoMsg').textContent),
      null, { timeout: 30000 });
    assertTrue(/^data[\\/]resume\.yml:\d+:\d+$/.test(await page.textContent('#eoLoc')),
      `overlay: a YAML syntax error is located too (${await page.textContent('#eoLoc')})`);
    fs.writeFileSync(dataFile, template);
    await page.waitForSelector('#errOverlay', { state: 'hidden', timeout: 30000 });

    /* ── what changed is outlined, then fades ──────────────────── */
    await sleep(500);
    fs.writeFileSync(dataFile, template.replace('Sed gravida elit velit', 'Sed gravida elit velit, and more words'));
    await page.waitForSelector('.chg', { timeout: 30000 });
    assertTrue((await page.$$('.chg')).length >= 1, 'highlight: the change is outlined');
    const box = await page.$eval('.chg', n => [n.getAttribute('aria-hidden'), n.parentElement.className]);
    assertEq(box, ['true', 'sheet'], 'highlight: ...on the page, hidden from screen readers');
    await page.waitForFunction(() => !document.querySelector('.chg'), null, { timeout: 8000 });
    assertTrue(true, 'highlight: ...and gone after four seconds');
    // A one-word edit outlines that word, not the page: both renders'
    // pages are shrunk the same way (two different downscales of one PNG
    // differ nearly everywhere, which outlined whole pages).
    fs.writeFileSync(dataFile, template.replace('Sed gravida elit velit', 'Sed gravida elit velit, and more words')
      .replace('Lingua (Gradus Scientiae)', 'Lingua (Gradus Scientia)'));
    await page.waitForSelector('.chg', { timeout: 30000 });
    const sizes = await page.$$eval('.chg', ns => ns.map(n => [n.offsetWidth / n.parentElement.offsetWidth,
      n.offsetHeight / n.parentElement.offsetHeight]));
    assertTrue(sizes.length === 1 && sizes[0][0] < 0.1 && sizes[0][1] < 0.05,
      `highlight: a one-word edit outlines about that word (${JSON.stringify(sizes)})`);
    await page.waitForFunction(() => !document.querySelector('.chg'), null, { timeout: 8000 });
    await page.click('#hlBtn');
    assertEq(await page.getAttribute('#hlBtn', 'aria-pressed'), 'false', 'highlight: the toggle turns it off');
    fs.writeFileSync(dataFile, template.replace('Sed gravida elit velit', 'Sed gravida elit velit, other words'));
    await page.waitForResponse(r => r.url().endsWith('/api/preview'), { timeout: 30000 });
    await sleep(1200);
    assertEq((await page.$$('.chg')).length, 0, 'highlight: ...and nothing is outlined while it is off');
    await page.click('#hlBtn');

    /* ── keyboard shortcuts ─────────────────────────────────────── */
    await page.evaluate(() => { window.__notReloaded = true; });
    for (const key of ['F5', 'Control+r']) {
      // The response is waited for from the start: a fast render can
      // answer before a wait set up after the request would begin.
      const answered = page.waitForResponse(r => r.url().endsWith('/api/preview'), { timeout: 30000 });
      const [req] = await Promise.all([
        page.waitForRequest(r => r.url().endsWith('/api/preview'), { timeout: 10000 }),
        page.keyboard.press(key),
      ]);
      assertTrue(Boolean(req) && await page.evaluate(() => window.__notReloaded === true),
        `${key}: re-renders, and does not reload the page`);
      await answered;
    }
    await page.keyboard.press('Control+Tab');
    await page.waitForFunction(() => document.getElementById('canvasDoc').textContent === 'Cover Letter');
    await page.waitForFunction(() => document.getElementById('say').textContent === 'Showing Cover Letter');
    assertTrue(true, 'Ctrl+Tab: switches document, and says so');
    await page.keyboard.press('Control+Shift+Tab');
    await page.waitForFunction(() => document.getElementById('canvasDoc').textContent === 'Resume');
    assertTrue(true, 'Ctrl+Shift+Tab: switches back');
    await page.keyboard.press('Control+2');
    await page.waitForFunction(() => document.getElementById('canvasDoc').textContent === 'Cover Letter');
    assertTrue(true, 'Ctrl+2: shows the Cover Letter');
    await page.keyboard.press('Control+2');
    await sleep(150);
    assertEq(await page.textContent('#canvasDoc'), 'Cover Letter', 'Ctrl+2: pressed again, stays (not a toggle)');
    await page.keyboard.press('Control+Alt+1');
    await sleep(150);
    assertEq(await page.textContent('#canvasDoc'), 'Cover Letter', 'Ctrl+Alt+1 (AltGr): not a shortcut');
    await page.keyboard.press('Control+1');
    await page.waitForFunction(() => document.getElementById('canvasDoc').textContent === 'Resume');
    assertTrue(true, 'Ctrl+1: shows the Resume');
    assertEq(await page.getAttribute('.doc-pick >> nth=0', 'aria-keyshortcuts'), 'Control+1 Control+Tab',
      'Ctrl+1: named on the Resume card for assistive technology');
    await page.keyboard.press('Control+Shift+P');
    assertEq(await page.getAttribute('#pauseBtn', 'aria-pressed'), 'true', 'Ctrl+Shift+P: pauses');
    await page.keyboard.press('Control+Shift+P');
    assertEq(await page.getAttribute('#pauseBtn', 'aria-pressed'), 'false', 'Ctrl+Shift+P: ...and resumes');

    await page.focus('#menuRefresh');
    // AltGr is Ctrl+Alt on Windows: AltGr+O (ó on a Polish keyboard) is
    // not Ctrl+O.
    await page.keyboard.press('Control+Alt+o');
    await sleep(200);
    assertEq(await page.$('[role="dialog"]'), null, 'shortcuts: Ctrl+Alt (AltGr) combinations are not shortcuts');
    await page.keyboard.press('Control+o');
    await page.waitForSelector('[role="dialog"]');
    assertTrue(/Data files/.test(await page.textContent('[role="dialog"] h3')), 'Ctrl+O: opens Data files');
    await page.keyboard.press('Control+Shift+L');
    assertEq(await page.getAttribute('html', 'data-theme'), null, 'shortcuts: none fires while a dialog is open');
    await page.keyboard.press('Escape');
    assertEq(await page.evaluate(() => document.activeElement.id), 'menuRefresh',
      'Ctrl+O: Escape closes it and focus goes back where it was');

    const diagOpen = () => page.evaluate(() => document.getElementById('diagDet').open);
    const before = await diagOpen();
    await page.keyboard.press('Control+Shift+D');
    assertEq(await diagOpen(), !before, 'Ctrl+Shift+D: toggles Diagnostics');
    await page.keyboard.press('Control+Shift+D');
    assertEq(await diagOpen(), before, 'Ctrl+Shift+D: ...both ways');

    await page.keyboard.press('Control+Shift+L');
    assertEq(await page.getAttribute('html', 'data-theme'), 'light', 'Ctrl+Shift+L: System → Light');
    await page.waitForFunction(() => document.getElementById('say').textContent === 'Theme: Light');
    assertTrue(true, 'Ctrl+Shift+L: ...announced');
    await page.keyboard.press('Control+Shift+L');
    assertEq(await page.getAttribute('html', 'data-theme'), 'dark', 'Ctrl+Shift+L: Light → Dark');
    assertEq(await page.getAttribute('[data-theme-choice="dark"]', 'aria-checked'), 'true',
      'Ctrl+Shift+L: ...and the radio group follows');

    for (const key of ['F1', 'Control+/']) {
      await page.keyboard.press(key);
      await page.waitForSelector('#helpDlg', { timeout: 3000 });
      const rows = await page.$$eval('#helpDlg tbody tr', trs => trs.map(tr => tr.textContent));
      assertTrue(rows.length >= 12 && rows.some(r => /Ctrl\+Shift\+B/.test(r.replace(/\s/g, ''))),
        `${key}: the help dialog lists the shortcuts (${rows.length})`);
      await page.keyboard.press('Escape');
      await page.waitForSelector('#helpDlg', { state: 'detached', timeout: 3000 });
    }

    await page.keyboard.press('Control+9');
    assertTrue(/^Fit page/.test(await page.textContent('#zoomVal')), 'Ctrl+9: fit page');
    await page.keyboard.press('Control+8');
    assertTrue(/^Fit width/.test(await page.textContent('#zoomVal')), 'Ctrl+8: fit width');
    // Fit width shows two pages side by side when both fit at 70% or
    // more, and one page across when they don't.
    const tops = () => page.$$eval('.sheet-wrap', ns => ns.map(n => Math.round(n.getBoundingClientRect().top)));
    await page.setViewportSize({ width: 2560, height: 1000 });
    await page.waitForFunction(() => {
      const w = [...document.querySelectorAll('.sheet-wrap')].map(n => n.getBoundingClientRect());
      return w.length === 2 && Math.round(w[0].top) === Math.round(w[1].top);
    }, null, { timeout: 3000 });
    const [t1, t2] = await tops();
    assertEq(t1, t2, 'fit width on a wide window: the two pages side by side');
    const two = await page.$eval('.sheet-wrap', n => n.getBoundingClientRect().width);
    assertTrue(two >= 571 && two <= 1224, `...each at 70%–150% (${Math.round(two)} px)`);
    await page.setViewportSize({ width: 1100, height: 800 });
    await page.waitForFunction(() => {
      const w = [...document.querySelectorAll('.sheet-wrap')].map(n => n.getBoundingClientRect());
      return w.length === 2 && w[1].top > w[0].bottom - 1;
    }, null, { timeout: 3000 });
    assertTrue(/^Fit width/.test(await page.textContent('#zoomVal')),
      'fit width on a narrow window: one page across, the second below it');
    // Still capped at the raster's own width (1224 px, 150%) on a very
    // wide screen; zooming past it is there for the asking.
    await page.setViewportSize({ width: 3840, height: 1000 });
    await page.waitForFunction(() => document.getElementById('zoomVal').textContent === 'Fit width · 150%',
      null, { timeout: 3000 });
    assertEq(await page.$eval('.sheet-wrap', n => Math.round(n.getBoundingClientRect().width)), 1224,
      'fit width on a very wide window: no wider than the raster (1224 px, 150%)');
    await page.focus('#scroller');
    await page.keyboard.press('+');
    assertEq(await page.textContent('#zoomVal'), '175%', '...while zooming in past it is still allowed');
    await page.keyboard.press('Control+8');
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.focus('#scroller');
    await page.keyboard.press('0');
    assertEq(await page.textContent('#zoomVal'), '100%', '0 in the preview: actual size');
    const width = () => page.$eval('.sheet-wrap', n => Math.round(n.getBoundingClientRect().width));
    assertEq(await width(), 816, '100% is 8.5in, 816 CSS px');
    await page.keyboard.press('+');
    assertEq(await page.textContent('#zoomVal'), '110%', '+ in the preview: zooms in one step');
    await page.keyboard.press('-');
    await page.keyboard.press('-');
    assertEq(await page.textContent('#zoomVal'), '90%', '- in the preview: zooms out');
    await page.focus('#dataChoice');
    await page.keyboard.press('+');
    assertEq(await page.textContent('#zoomVal'), '90%', '+ in a select: nothing (it is not a shortcut there)');
    const sb = await page.$eval('#scroller', n => { const r = n.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + 200 }; });
    await page.mouse.move(sb.x, sb.y);
    await page.keyboard.down('Control');
    await page.mouse.wheel(0, -120);
    await page.keyboard.up('Control');
    await page.waitForFunction(() => document.getElementById('zoomVal').textContent === '100%', null, { timeout: 3000 });
    assertTrue(true, 'Ctrl+wheel over the pages: zooms the preview');

    // F6: Documents → Preview → Inspector, and back.
    await page.focus('#menuRefresh');
    const pane = () => page.evaluate(() => document.activeElement.id);
    const seen = [];
    for (let i = 0; i < 3; i++) { await page.keyboard.press('F6'); seen.push(await pane()); }
    await page.keyboard.press('Shift+F6');
    seen.push(await pane());
    assertEq(seen, ['rail', 'scroller', 'inspector', 'scroller'], 'F6 / Shift+F6: move between the three panes');

    // Ctrl+Shift+B builds what is on screen.
    const built = page.waitForResponse(r => r.url().endsWith('/api/build'), { timeout: 120000 });
    const [build] = await Promise.all([
      page.waitForRequest(r => r.url().endsWith('/api/build'), { timeout: 5000 }),
      page.keyboard.press('Control+Shift+B'),
    ]);
    assertEq(JSON.parse(build.postData()).doc, 'resume', 'Ctrl+Shift+B: builds the document on screen');
    await built;
    await page.waitForFunction(() => ![...document.querySelectorAll('button')]
      .some(b => b.getAttribute('aria-disabled') === 'true' && /^Build/.test(b.textContent)), null, { timeout: 30000 });
    assertTrue(/last page \d+% full/.test(await page.textContent('.doc >> nth=0')),
      'Build: the last-page fill stays on the card (a built PDF read back has none of its own)');

    // Build the other document: the view follows it.
    await page.click('text=Build Cover Letter PDF');
    await page.waitForFunction(() => document.getElementById('canvasDoc').textContent === 'Cover Letter',
      null, { timeout: 120000 });
    assertTrue(true, 'Build of the document not on screen: shows it when it is built');

    /* ── settings outlive the origin ───────────────────────────── */
    await Promise.all([
      page.waitForResponse(r => r.url().endsWith('/api/prefs') && r.request().method() === 'PUT'),
      page.click('#fitPage'),
    ]);
    await sleep(200);
    const other = await openPage(ctx, `http://localhost:${port}/`, violations);
    await other.waitForSelector('.sheet img', { timeout: 90000 });
    assertEq(await other.getAttribute('html', 'data-theme'), 'dark',
      'settings: a page from another origin (a new launch) opens in the saved theme');
    assertTrue(/^Fit page/.test(await other.textContent('#zoomVal')),
      'settings: ...and at the saved zoom');
    assertEq(await other.evaluate(() => localStorage.getItem('studio.prefs')), null,
      "settings: ...which did not come from that origin's localStorage");
    await Promise.all([
      other.waitForResponse(r => r.url().endsWith('/api/prefs') && r.request().method() === 'PUT'),
      other.click('[data-theme-choice="system"]'),
    ]);
    await other.close();

    await page.waitForTimeout(300);
    assertEq(violations, [], 'CSP: none of it breaks a rule of the page\'s policy');
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
    console.log(`SKIP studio_ui: Chromium unavailable (${String(err.message).split('\n')[0]})`);
    process.exit(0);
  }
  const before = realDistFingerprint();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-ui-test-'));
  const project = path.join(tmp, 'project');
  let srv = null;
  try {
    copyProject(project);
    srv = await startServer(project);
    await run(project, srv.port, chromium);
  } catch (err) {
    fail('studio ui run', { error: `${err.stack || err.message}\n${srv ? srv.log.slice(-30).join('\n') : ''}` });
  } finally {
    // Closing stdin is the graceful shutdown (Chromium and the worker
    // go with it); SIGKILL only if that does not end it.
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
