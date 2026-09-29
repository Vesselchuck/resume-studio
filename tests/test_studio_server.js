/**
 * test_studio_server.js — the Studio server over HTTP, end to end.
 *
 * WHAT THIS GUARDS
 * ----------------
 *   • Each document's data selection is its own: the resume card's
 *     RESUME_DATA_SOURCE never reaches a letter build or preview, and a
 *     letter file picked on the letter card is what the preview reads.
 *   • A crashed Python worker fails the request it broke and comes back
 *     on the next one, rather than hanging every preview and build.
 *   • The tray names the PDFs the last Build wrote, however many
 *     previews of other data files have run since.
 *   • Requests must be addressed to this server (Host), from its own
 *     page (Origin, Sec-Fetch-Site), with JSON bodies; a malformed Host
 *     cannot crash it. Every response carries nosniff, CORP and
 *     Referrer-Policy; the page's CSP lets it load and render in a real
 *     browser with no violation.
 *   • A folder named *.yml is not offered as a data file.
 *   • A dropped file never replaces one in data/, and a support file
 *     name (_profile.yml) is refused.
 *   • Closing the server's stdin (what the desktop shell does) shuts it
 *     down gracefully, worker included; so does SIGHUP, Chromium included.
 *   • A save is picked up after a 20 ms debounce, and a preview that
 *     fails the way a half-written data file would, shortly after a
 *     change, is tried once more before the error is shown.
 *   • Editors' own files (swap, lock, backup, atomic-save temps) set off
 *     no render; the atomic save itself sets off exactly one.
 *   • A dropped file named for a Windows device (CON.yml), or with a
 *     ':' (an NTFS stream) or a control character, is refused.
 *   • A preview's scale is clamped and its page list cleaned, so a bad
 *     value can neither fail the worker nor ask it for gigapixels.
 *   • /api/status stays busy while any preview or build is under way.
 *   • An event-stream client that stops reading is dropped, instead of
 *     having every page image queued for it without limit.
 *   • The UI's settings (/api/prefs) keep only known keys with valid
 *     values, under the same Host/Origin/JSON rules as every write, and
 *     the page is served with them (theme on <html>, all in a <meta>)
 *     without its CSP changing.
 *   • /api/open opens only an existing file in data/ or styles/, and
 *     the command it runs is a program and its arguments — never a
 *     shell — with a vscode:// link percent-encoded.
 *
 * ISOLATION
 * ---------
 * The server is started against a throwaway copy of the project in the
 * system temp directory, with only the shipped templates in its data/.
 * It builds, prunes and writes files there, never in this checkout —
 * a test run must not cost anyone their data or their built PDFs.
 *
 * REQUIREMENTS
 * ------------
 * Playwright's Chromium and Python with the build's requirements. Skips
 * cleanly without Chromium, the same way test_engine_equivalence.js does.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const http = require('http');
const net = require('net');
const { spawn } = require('child_process');
const { assertEq, assertTrue, fail, report } = require('./_framework');

const ROOT = path.join(__dirname, '..');
const READY_PREFIX = '\x1eSTUDIO_READY ';

const server = require('../build/studio_server');
const { copyProject, realDistFingerprint } = require('./_project');


/* ─── Pure helpers ────────────────────────────────────────────── */

async function unitTests(tmp) {
  // renderEnv: the letter never gets the resume's source.
  const pickedFile = path.join(tmp, 'x.yml');
  assertEq(server.renderEnv('letter', { dataSource: 'default', picked: {} }),
    { RESUME_DATA_SOURCE: null, LETTER_DATA_FILE: null },
    'renderEnv: letter clears RESUME_DATA_SOURCE even when the resume forces one');
  assertEq(server.renderEnv('letter', { dataSource: 'mine', picked: { letter: pickedFile } }),
    { RESUME_DATA_SOURCE: null, LETTER_DATA_FILE: pickedFile },
    'renderEnv: letter reads the file picked on its own card');
  assertEq(server.renderEnv('resume', { dataSource: 'default', picked: { letter: pickedFile } }),
    { RESUME_DATA_SOURCE: 'default', RESUME_DATA_FILE: null },
    "renderEnv: resume uses its own source and ignores the letter's pick");

  // dataFileInfo: the letter card follows letter.yml → letter_default.yml.
  const fakeDir = path.join(tmp, 'fake-data');
  fs.mkdirSync(fakeDir, { recursive: true });
  const letter = {
    script: 'letter.js', variant: 'letter',
    myData: path.join(fakeDir, 'letter.yml'),
    defaultData: path.join(fakeDir, 'letter_default.yml'),
  };
  fs.writeFileSync(letter.defaultData, 'letter: {}\n');
  assertEq(server.dataFileInfo(letter, 'mine', null).name, 'letter_default.yml',
    'dataFileInfo: letter ignores the resume source (no letter.yml)');
  fs.writeFileSync(letter.myData, 'letter: {}\n');
  assertEq(server.dataFileInfo(letter, 'default', null).name, 'letter.yml',
    'dataFileInfo: letter ignores the resume source (letter.yml present)');
  assertEq(server.dataFileInfo(letter, 'default', null).forced, false,
    'dataFileInfo: letter is never "forced" by the resume source');

  // checkRequest.
  const req = (headers, method = 'GET') => ({ method, headers });
  assertEq(server.checkRequest(req({ host: '127.0.0.1:5000' }), 5000), null,
    'checkRequest: 127.0.0.1:<port> is accepted');
  assertEq(server.checkRequest(req({ host: 'LOCALHOST:5000' }), 5000), null,
    'checkRequest: localhost:<port> is accepted, any case');
  assertEq((server.checkRequest(req({ host: '127.0.0.1:5001' }), 5000) || [])[0], 403,
    'checkRequest: another port in Host is refused');
  assertEq((server.checkRequest(req({ host: 'evil.example:5000' }), 5000) || [])[0], 403,
    'checkRequest: a rebinding host name is refused');
  assertEq((server.checkRequest(req({}), 5000) || [])[0], 403,
    'checkRequest: a missing Host is refused');
  assertEq((server.checkRequest(req({ host: '127.0.0.1:5000', origin: 'http://127.0.0.1:50001' }), 5000) || [])[0], 403,
    'checkRequest: an Origin that merely starts with ours is refused');
  assertEq((server.checkRequest(req({ host: '127.0.0.1:5000', origin: 'null' }), 5000) || [])[0], 403,
    'checkRequest: Origin "null" is refused');
  assertEq(server.checkRequest(req({ host: '127.0.0.1:5000', origin: 'http://localhost:5000',
    'content-type': 'application/json; charset=utf-8' }, 'POST'), 5000), null,
    'checkRequest: a same-origin JSON POST is accepted');
  assertEq((server.checkRequest(req({ host: '127.0.0.1:5000',
    'content-type': 'text/plain' }, 'POST'), 5000) || [])[0], 415,
    'checkRequest: a text/plain POST is refused');

  // checkRequest: Sec-Fetch-Site. A browser says who asked; another site
  // is refused, and a request that says nothing (curl, the desktop
  // shell's own POST) is not.
  const fetchReq = (site, extra = {}, method = 'GET', url = '/api/status') => ({ method, url,
    headers: { host: '127.0.0.1:5000', ...(site ? { 'sec-fetch-site': site } : {}), ...extra } });
  assertEq((server.checkRequest(fetchReq('cross-site'), 5000) || [])[0], 403,
    'checkRequest: Sec-Fetch-Site cross-site is refused');
  assertEq((server.checkRequest(fetchReq('same-site'), 5000) || [])[0], 403,
    'checkRequest: Sec-Fetch-Site same-site (a sibling subdomain) is refused');
  assertEq((server.checkRequest(fetchReq('CROSS-SITE', { 'sec-fetch-mode': 'no-cors' }, 'GET', '/'), 5000) || [])[0], 403,
    'checkRequest: a cross-site subresource load of the page is refused, any case');
  assertEq(server.checkRequest(fetchReq('same-origin'), 5000), null,
    'checkRequest: Sec-Fetch-Site same-origin is accepted');
  assertEq(server.checkRequest(fetchReq('none'), 5000), null,
    'checkRequest: Sec-Fetch-Site none (typed or bookmarked) is accepted');
  assertEq(server.checkRequest(fetchReq(null, { 'content-type': 'application/json' }, 'POST'), 5000), null,
    'checkRequest: a POST with no Sec-Fetch-Site (the shell, curl) is accepted');
  assertEq(server.checkRequest(fetchReq('cross-site',
    { 'sec-fetch-mode': 'navigate', 'sec-fetch-dest': 'document' }, 'GET', '/'), 5000), null,
    'checkRequest: a top-level navigation to the page from elsewhere is accepted');
  assertEq((server.checkRequest(fetchReq('cross-site',
    { 'sec-fetch-mode': 'navigate', 'sec-fetch-dest': 'document' }, 'GET', '/api/status'), 5000) || [])[0], 403,
    'checkRequest: ...but not a navigation to the API');
  assertEq((server.checkRequest(fetchReq('cross-site',
    { 'sec-fetch-mode': 'navigate', 'sec-fetch-dest': 'iframe' }, 'GET', '/'), 5000) || [])[0], 403,
    'checkRequest: ...nor the page loaded into another site\'s frame');

  // The UI's Content-Security-Policy allows its inline script by hash,
  // and only that script.
  const csp = server.uiContentSecurityPolicy('<p>x</p><script>var a = 1;</script><script src="/x.js"></script>');
  const digest = require('crypto').createHash('sha256').update('var a = 1;').digest('base64');
  assertTrue(csp.includes(`script-src 'sha256-${digest}';`),
    `CSP: the inline script is allowed by its hash, the src= one adds none (${csp})`);
  // Line endings: the browser hashes the script after turning CRLF and
  // a lone CR into LF, so a page saved with Windows endings must get the
  // hash of the LF text or nothing on it runs.
  assertEq(server.uiContentSecurityPolicy('<script>\r\nvar a = 1;\r\n</script>'),
    server.uiContentSecurityPolicy('<script>\nvar a = 1;\n</script>'),
    'CSP: a page saved with CRLF gets the hash the browser computes');
  assertEq(server.uiContentSecurityPolicy('<script>\rvar a = 1;\r</script>'),
    server.uiContentSecurityPolicy('<script>\nvar a = 1;\n</script>'),
    'CSP: ...and so does one with lone CRs');
  assertTrue(/frame-ancestors 'none'/.test(csp) && /default-src 'none'/.test(csp)
    && !/unsafe-eval/.test(csp) && !/script-src[^;]*unsafe-inline/.test(csp),
    'CSP: no framing, nothing by default, no unsafe script sources');

  // The save debounce, and a data file caught half-written.
  assertEq(server.WATCH_DEBOUNCE_MS, 20, 'debounce: a save is rendered after 20 ms of quiet');
  assertTrue(server.HALF_WRITTEN_RETRY_MS >= 20 && server.HALF_WRITTEN_RETRY_MS <= 100,
    'half-written: the retry waits tens of milliseconds');
  assertTrue(!server.looksHalfWritten(null) && !server.looksHalfWritten(new Error('x')),
    'half-written: an ordinary error is not one');
  const yamlErr = Object.assign(new Error('❌ data/resume.yml could not be read as YAML.'), { kind: 'build_failed' });
  const emptyErr = Object.assign(new Error('❌ data/resume.yml is empty or not a YAML mapping at the top level (parsed as NoneType).'), { kind: 'build_failed' });
  const schemaErr = Object.assign(new Error('❌ invalid resume data — sidebar is required'), { kind: 'build_failed' });
  assertTrue(server.looksHalfWritten(yamlErr), 'half-written: a YAML syntax error is one');
  assertTrue(server.looksHalfWritten(emptyErr), 'half-written: an empty file is one');
  assertTrue(!server.looksHalfWritten(schemaErr), 'half-written: a schema error in a file that parsed is not');
  assertTrue(server.looksHalfWritten(Object.assign(new Error('Permission denied'), { kind: 'locked_file' })),
    'half-written: a file still locked by the editor (Windows) is one');

  const clock = { t: 10000 };
  const slept = [];
  const opts = (changedAt) => ({
    changedAt, now: () => clock.t, sleep: async (ms) => { slept.push(ms); clock.t += ms; },
  });
  const failingThen = (errors, value) => {
    let calls = 0;
    const fn = async () => {
      calls++;
      if (errors.length) throw errors.shift();
      return value;
    };
    fn.calls = () => calls;
    return fn;
  };
  let fn = failingThen([yamlErr], 'rendered');
  assertEq(await server.retryIfHalfWritten(fn, opts(clock.t - 100)), 'rendered',
    'half-written: a YAML error just after a change is retried, and the retry is returned');
  assertEq([fn.calls(), slept[slept.length - 1]], [2, server.HALF_WRITTEN_RETRY_MS],
    'half-written: ...once, after the retry delay');

  const rejects = async (promise) => { try { await promise; return null; } catch (e) { return e; } };
  fn = failingThen([yamlErr, yamlErr], 'never');
  assertTrue(await rejects(server.retryIfHalfWritten(fn, opts(clock.t - 100))) === yamlErr,
    'half-written: a file still broken on the retry reports its error');
  assertEq(fn.calls(), 2, 'half-written: ...after exactly one retry');

  fn = failingThen([yamlErr], 'never');
  assertTrue(await rejects(server.retryIfHalfWritten(fn, opts(clock.t - server.HALF_WRITTEN_WINDOW_MS - 1))) === yamlErr,
    'half-written: long after a change, the error is shown at once');
  assertEq(fn.calls(), 1, 'half-written: ...without a retry');

  fn = failingThen([yamlErr], 'never');
  assertTrue(await rejects(server.retryIfHalfWritten(fn, opts(null))) === yamlErr,
    'half-written: with no change seen, the error is shown at once');

  fn = failingThen([schemaErr], 'never');
  assertTrue(await rejects(server.retryIfHalfWritten(fn, opts(clock.t))) === schemaErr,
    'half-written: a schema error is not retried');
  assertEq(fn.calls(), 1, 'half-written: ...not even once');

  // Names a dropped file may not have.
  for (const name of ['CON.yml', 'con.yml', 'nul.backup.yml', 'COM1.yaml', 'LPT9.yml', 'COM¹.yml',
    'aux .yml', 'x.yml:y.yml', 'tab\there.yml', 'a<b.yml', 'x.yml.']) {
    assertTrue(server.unportableName(name) !== null, `unportableName: ${JSON.stringify(name)} is refused`);
  }
  for (const name of ['CONSOLE.yml', 'com10.yml', 'my con.yml', 'résumé 2026.yml', 'resume_default.yml']) {
    assertEq(server.unportableName(name), null, `unportableName: ${JSON.stringify(name)} is allowed`);
  }

  // Preview parameters.
  assertEq([server.previewScale(2), server.previewScale(100), server.previewScale(-1), server.previewScale(0.01)],
    [2, 4, 0.25, 0.25], 'previewScale: clamped to 0.25–4');
  assertEq([server.previewScale('2'), server.previewScale(NaN), server.previewScale(null)],
    [undefined, undefined, undefined], 'previewScale: not a finite number is the default');
  assertEq(server.previewPages([2, 1, 1, 'x', 1.5, 0, -3, 65]), [1, 2],
    'previewPages: whole page numbers from 1, each once');
  assertEq([server.previewPages(3), server.previewPages(['x']), server.previewPages(null)], [null, null, null],
    'previewPages: anything else is every page');

  // What the watcher renders for.
  for (const name of ['resume.yml', 'letter.yaml', '_profile.yml', 'styles.scss', '_base.scss']) {
    assertTrue(server.isWatchedInput(name), `isWatchedInput: ${name} is an input`);
  }
  for (const name of ['.resume.yml.swp', '.resume.yml.swx', '4913', 'resume.yml~', '.#resume.yml',
    '#resume.yml#', 'resume.yml___jb_tmp___', 'resume.yml___jb_old___', 'resume.yml.tmp',
    'resume.yml.4f2a.tmp', '~$sume.yml', '.~lock.resume.yml#']) {
    assertTrue(!server.isWatchedInput(name), `isWatchedInput: ${name} is not`);
  }

  // Settings: only known keys, each checked.
  assertEq(server.sanitizePrefs({ theme: 'dark', zoom: 1000, fit: 'bogus', snapshot: true,
    tests: 'yes', highlight: false, evil: '<x>', __proto__: { theme: 'light' } }),
  { theme: 'dark', zoom: 200, snapshot: true, highlight: false },
  'sanitizePrefs: known keys with valid values only, zoom clamped');
  assertEq([server.sanitizePrefs(null), server.sanitizePrefs([1]), server.sanitizePrefs('x')], [{}, {}, {}],
    'sanitizePrefs: anything but an object is nothing');
  assertEq(server.sanitizePrefs({ theme: 'dark" onload="x' }), {},
    'sanitizePrefs: a theme is one of three words, nothing else');
  const prefsFile = path.join(tmp, 'prefs', 'p.json');
  assertEq(server.writePrefs({ theme: 'light', bogus: 1 }, prefsFile), { theme: 'light' },
    'writePrefs: writes what passes');
  assertEq(server.writePrefs({ zoom: 150 }, prefsFile), { theme: 'light', zoom: 150 },
    'writePrefs: merges into what is there');
  fs.writeFileSync(prefsFile, '{not json');
  assertEq(server.readPrefs(prefsFile), {}, 'readPrefs: a damaged file is no settings, not an error');
  const served = server.pageWithPrefs('<html lang="en"><head><title>t</title></head></html>',
    { theme: 'dark', note: '"><script>alert(1)</script>' });
  assertTrue(served.includes('<html lang="en" data-theme="dark">'), 'pageWithPrefs: the theme is on <html>');
  assertTrue(!served.includes('<script>') && served.includes('&quot;&gt;&lt;script&gt;'),
    'pageWithPrefs: the settings JSON is escaped into its attribute');
  assertTrue(!server.pageWithPrefs('<html lang="en"><head></head>', { theme: 'system' }).includes('data-theme'),
    'pageWithPrefs: System sets no theme attribute');

  // Opening a file: what is run, and what may be opened.
  const win = server.openCommand('C:\\Users\\A B\\res#ume\\data\\r%s.yml',
    { line: 42, col: 5, vscode: true, platform: 'win32' });
  assertTrue(/explorer\.exe$/i.test(win.cmd) && path.win32.isAbsolute(win.cmd), `openCommand: vscode through explorer.exe by full path (${win.cmd})`);
  assertEq(win.args, ['vscode://file/C:/Users/A%20B/res%23ume/data/r%25s.yml:42:5'],
    'openCommand: the vscode:// link is percent-encoded, drive colon kept, line and column last');
  const plain = server.openCommand('C:\\x\\data\\a.yml', { platform: 'win32' });
  assertTrue(/explorer\.exe$/i.test(plain.cmd) && plain.args.length === 1 && plain.editor === 'default',
    'openCommand: without VS Code, explorer.exe opens the file itself');
  assertEq(server.openCommand('/p/data/a.yml', { platform: 'darwin' }), { editor: 'default', cmd: 'open', args: ['/p/data/a.yml'] },
    'openCommand: macOS uses open');
  assertEq(server.openCommand('/p/data/a.yml', { platform: 'linux' }), { editor: 'default', cmd: 'xdg-open', args: ['/p/data/a.yml'] },
    'openCommand: Linux uses xdg-open');
  // explorer.exe splits its command line at commas, quoted or not: a
  // file named `run.bat,x.yml` would open (run) run.bat.
  assertEq([server.openCommand('C:\\x\\data\\run.bat,x.yml', { platform: 'win32' }),
    server.openCommand('C:\\x, y\\data\\a.yml', { platform: 'win32' })], [null, null],
    'openCommand: explorer.exe is never handed a path with a comma');
  assertTrue(server.openCommand('C:\\x\\data\\run.bat,x.yml', { platform: 'win32', vscode: true }).args[0].includes('%2C'),
    'openCommand: ...while the vscode:// link encodes it');
  assertEq(server.openCommand('\\\\srv\\share\\data\\a.yml', { platform: 'win32', vscode: true }).editor, 'default',
    'openCommand: a UNC path, which has no vscode://file/ form, goes to its default program');
  for (const platform of ['win32', 'darwin', 'linux']) {
    for (const vscode of [true, false]) {
      const c = server.openCommand('/p/data/a b;&|.yml', { platform, vscode });
      assertTrue(!/(^|[\\/])(cmd|powershell|pwsh|sh|bash)(\.exe)?$/i.test(c.cmd),
        `openCommand: never a shell (${platform}, vscode ${vscode}: ${c.cmd})`);
    }
  }
  assertTrue(server.resolveOpenable('data/resume_default.yml') !== null, 'resolveOpenable: a data file is openable');
  assertTrue(server.resolveOpenable('styles/styles.scss') !== null, 'resolveOpenable: a stylesheet is openable');
  for (const bad of ['package.json', 'build/engine.js', '../package.json', 'data/../package.json',
    'data', 'data/', 'data/no-such.yml', '/etc/passwd', 'styles/../build/engine.js', '']) {
    assertEq(server.resolveOpenable(bad), null, `resolveOpenable: ${JSON.stringify(bad)} is refused`);
  }

  // writeNew: never replaces.
  const target = path.join(tmp, 'wn', 'a.yml');
  assertTrue(server.writeNew(target, 'one'), 'writeNew: writes a free name');
  assertTrue(!server.writeNew(target, 'two'), 'writeNew: reports a taken name');
  assertEq(fs.readFileSync(target, 'utf-8'), 'one', 'writeNew: never replaces the file');
}



function startServer(projectDir) {
  const env = { ...process.env };
  for (const k of ['RESUME_DATA_SOURCE', 'RESUME_DATA_FILE', 'LETTER_DATA_FILE', 'STUDIO_PORT']) {
    delete env[k];
  }
  // --open-dry-run: /api/open reports what it would run instead of
  // starting an editor.
  const child = spawn(process.execPath,
    [path.join(projectDir, 'build', 'studio_server.js'), '--port', '0', '--exit-with-parent', '--open-dry-run'],
    { cwd: projectDir, env, stdio: ['pipe', 'pipe', 'pipe'] });
  const log = [];
  return new Promise((resolve, reject) => {
    let buffer = '';
    const timer = setTimeout(() => reject(new Error(
      `server did not report ready in 60 s:\n${log.slice(-20).join('\n')}`)), 60000);
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
    child.stderr.on('data', (chunk) => log.push(...String(chunk).split('\n')));
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`server exited (${code}) before ready:\n${log.slice(-20).join('\n')}`));
    });
  });
}

function request(port, method, route, { body, headers = {}, timeoutMs = 120000 } = {}) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : JSON.stringify(body);
    const req = http.request({
      host: '127.0.0.1', port, method, path: route,
      headers: {
        host: `127.0.0.1:${port}`,
        ...(payload !== null ? { 'content-type': 'application/json',
                                 'content-length': Buffer.byteLength(payload) } : {}),
        ...headers,
      },
    }, (res) => {
      let text = '';
      res.setEncoding('utf-8');
      res.on('data', (c) => { text += c; });
      res.on('end', () => {
        let data = null;
        try { data = JSON.parse(text); } catch { /* not JSON */ }
        resolve({ status: res.statusCode, data, text, headers: res.headers });
      });
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`${method} ${route} timed out`)));
    req.on('error', reject);
    if (payload !== null) req.write(payload);
    req.end();
  });
}

/**
 * Listen on /api/events and collect the named events.
 *
 * The preview's streamed pages arrive here, which is the only way to
 * see them: they are deliberately not in the reply.
 */
function events(port, onEvent) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/api/events',
      headers: { host: `127.0.0.1:${port}` } }, (res) => {
      let buffer = '';
      res.setEncoding('utf-8');
      res.on('data', (chunk) => {
        buffer += chunk;
        let idx;
        while ((idx = buffer.indexOf('\n\n')) !== -1) {
          const block = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const name = /^event: (.+)$/m.exec(block);
          const data = /^data: (.+)$/m.exec(block);
          if (!name || !data) continue;
          try { onEvent(name[1], JSON.parse(data[1])); } catch { /* not ours */ }
        }
      });
      resolve({ close: () => req.destroy() });
    });
    req.on('error', () => resolve({ close: () => {} }));
  });
}

function rawRequest(port, text) {
  return new Promise((resolve) => {
    const sock = net.connect(port, '127.0.0.1', () => sock.write(text));
    let got = '';
    sock.setEncoding('utf-8');
    sock.on('data', (c) => { got += c; });
    sock.on('end', () => resolve(got));
    sock.on('close', () => resolve(got));
    sock.on('error', () => resolve(got));
    sock.setTimeout(10000, () => { sock.destroy(); resolve(got); });
  });
}

// A zombie (exited, not yet reaped by whoever inherited it) counts as gone.
function isAlive(pid) {
  try { process.kill(pid, 0); } catch { return false; }
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf-8');
    return stat.slice(stat.lastIndexOf(')') + 2)[0] !== 'Z';
  } catch { return true; }
}

async function goneWithin(pid, ms) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    if (!isAlive(pid)) return true;
    await new Promise(res => setTimeout(res, 100));
  }
  return !isAlive(pid);
}


// Every process below `pid`, from /proc (Linux). Chromium is started by
// Playwright's Node side, so it is a child of the server, not of us.
function descendants(pid) {
  const parent = new Map();
  for (const entry of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const stat = fs.readFileSync(`/proc/${entry}/stat`, 'utf-8');
      parent.set(Number(entry), Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1]));
    } catch { /* gone meanwhile */ }
  }
  const out = [];
  const walk = (p) => {
    for (const [child, pp] of parent) if (pp === p) { out.push(child); walk(child); }
  };
  walk(pid);
  return out;
}

function commandOf(pid) {
  try { return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf-8').replace(/\0/g, ' '); } catch { return ''; }
}


/**
 * The page in a real browser under its own Content-Security-Policy:
 * it loads, draws a page of the preview, and nothing it does is blocked.
 */
async function cspTests(port, chromium) {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    const violations = [];
    await page.exposeFunction('__cspViolation', v => violations.push(v));
    await page.addInitScript(() => {
      document.addEventListener('securitypolicyviolation',
        e => window.__cspViolation(`${e.violatedDirective} ${e.blockedURI}`));
    });
    page.on('console', (msg) => {
      if (/Content.Security.Policy/i.test(msg.text())) violations.push(msg.text());
    });
    const resp = await page.goto(`http://127.0.0.1:${port}/`);
    assertTrue(/frame-ancestors 'none'/.test(resp.headers()['content-security-policy'] || ''),
      'CSP: the browser receives the policy');
    await page.waitForSelector('.sheet img', { timeout: 90000 });
    await page.evaluate(() => document.fonts.ready);
    const fonts = await page.evaluate(() => [...document.fonts].filter(f => f.status === 'loaded')
      .map(f => f.family.replace(/["']/g, '')));
    assertTrue(fonts.includes('Manrope'), `CSP: the UI font loads under the policy (${fonts.join(', ')})`);
    await page.waitForTimeout(300);
    assertEq(violations, [], 'CSP: loading and drawing the preview breaks no rule of the policy');
  } finally {
    await browser.close();
  }
}


/**
 * A keyboard user who presses Build keeps focus on the button: while
 * the build runs (the button is unavailable, not removed from the tab
 * order) and after it ends, so Enter builds again.
 */
async function buildFocusTests(port, chromium) {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${port}/`);
    await page.waitForSelector('.sheet img', { timeout: 90000 });
    // Unavailable either way a button can say so, so a regression back
    // to `disabled` fails on focus rather than on a timeout.
    const isBusy = (want) => want === [...document.querySelectorAll('button')]
      .some(b => /^Build .* PDF$/.test(b.textContent.trim())
        && (b.disabled || b.getAttribute('aria-disabled') === 'true'));
    const busy = () => page.evaluate(isBusy, true);
    const focused = () => page.evaluate(() => document.activeElement.textContent.trim());
    const button = page.locator('button', { hasText: /^Build Cover Letter PDF$/ });
    await button.focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(isBusy, true, { timeout: 5000 });
    assertEq(await focused(), 'Build Cover Letter PDF', 'focus: the Build button keeps focus while it builds');
    await page.waitForFunction(isBusy, false, { timeout: 90000 });
    assertEq(await focused(), 'Build Cover Letter PDF', 'focus: ...and after the build');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(100);
    assertTrue(await busy(), 'focus: Enter on it builds again');
    await page.waitForFunction(isBusy, false, { timeout: 90000 });
  } finally {
    await browser.close();
  }
}


/* ─── Endpoint tests ──────────────────────────────────────────── */

async function httpTests(project, srv) {
  const { port } = srv;
  const api = (method, route, body, extra) => request(port, method, route, { body, ...extra });

  // M8 — Host, Origin, content type.
  assertEq((await api('GET', '/api/status')).status, 200, 'status: 200 from 127.0.0.1');
  assertEq((await api('GET', '/', undefined, { headers: { host: `localhost:${port}` } })).status, 200,
    'the UI is served to localhost:<port>');
  assertEq((await api('GET', '/', undefined, { headers: { host: `evil.example:${port}` } })).status, 403,
    'the UI is refused to a foreign Host (DNS rebinding)');

  // The UI loads nothing from the network: its fonts come from fonts/.
  const ui = (await api('GET', '/')).text;
  assertEq((ui.match(/(?:src|href)\s*=\s*["']?https?:|url\(\s*["']?https?:/gi) || []).length, 0,
    'the UI references no remote stylesheet, script, font or image');
  const faces = ui.match(/\/fonts\/variable\/[\w-]+\.woff2/g) || [];
  assertEq(faces.length, 2, 'the UI declares its two typefaces');
  for (const face of faces) {
    assertEq((await api('GET', face)).status, 200, `the UI's font ${face} is served`);
  }
  assertEq((await api('GET', '/fonts/variable/..%2F..%2Fpackage.woff2')).status, 404,
    'the font route serves nothing outside fonts/variable/');
  // HEAD gets the headers GET would; any other method is not a page load.
  const headFont = await api('HEAD', faces[0] || '/fonts/variable/Manrope.woff2');
  assertEq([headFont.status, headFont.headers['content-type']], [200, 'font/woff2'],
    'HEAD on a font answers as GET does');
  assertEq((await api('HEAD', '/')).status, 200, 'HEAD on the page answers as GET does');
  assertEq((await api('POST', '/', {})).status, 404, 'a POST to the page is not answered with it');
  assertEq((await api('DELETE', '/index.html')).status, 404, '...nor a DELETE');
  assertEq((await api('POST', '/api/datasource', { source: null },
    { headers: { origin: 'http://evil.example' } })).status, 403,
    'a cross-origin POST is refused');
  assertEq((await api('POST', '/api/datasource', { source: null },
    { headers: { origin: `http://127.0.0.1:${port}0` } })).status, 403,
    'an Origin on another port is refused');
  assertEq((await api('POST', '/api/datasource', { source: null },
    { headers: { 'content-type': 'text/plain' } })).status, 415,
    'a non-JSON POST is refused');
  assertEq((await api('POST', '/api/datasource', { source: null },
    { headers: { origin: `http://127.0.0.1:${port}` } })).status, 200,
    'a same-origin JSON POST is accepted');

  // Sec-Fetch-Site over HTTP: another site's fetch gets nothing.
  assertEq((await api('GET', '/api/status', undefined,
    { headers: { 'sec-fetch-site': 'cross-site', 'sec-fetch-mode': 'no-cors' } })).status, 403,
    'a cross-site GET of the API is refused');
  assertEq((await api('POST', '/api/datasource', { source: null },
    { headers: { 'sec-fetch-site': 'same-site' } })).status, 403,
    'a same-site POST is refused');
  assertEq((await api('GET', '/api/status', undefined,
    { headers: { 'sec-fetch-site': 'same-origin' } })).status, 200,
    'a same-origin GET is accepted');

  // Headers on every response, and the page's policy.
  const want = { 'x-content-type-options': 'nosniff', 'cross-origin-resource-policy': 'same-origin',
    'referrer-policy': 'no-referrer' };
  const pick = h => Object.fromEntries(Object.keys(want).map(k => [k, h[k]]));
  const page = await api('GET', '/');
  for (const [what, resp] of [['the page', page], ['the API', await api('GET', '/api/status')],
    ['a font', await api('GET', faces[0] || '/fonts/variable/Manrope.woff2')],
    ['a 404', await api('GET', '/nope')],
    ['a refusal', await api('GET', '/api/status', undefined, { headers: { 'sec-fetch-site': 'cross-site' } })]]) {
    assertEq(pick(resp.headers), want, `security headers are on ${what}`);
  }
  const pagePolicy = page.headers['content-security-policy'] || '';
  assertTrue(/frame-ancestors 'none'/.test(pagePolicy) && /script-src 'sha256-/.test(pagePolicy),
    `the page is sent with its Content-Security-Policy (${pagePolicy})`);
  assertEq(pagePolicy, server.uiContentSecurityPolicy(page.text),
    "the page's policy hashes the page as served");

  // Settings over HTTP: kept in the project's dist/, filtered, and
  // served back inside the page.
  let pr = await api('GET', '/api/prefs');
  assertEq([pr.status, pr.data.prefs], [200, {}], 'prefs: a fresh project has none saved');
  assertEq((await api('PUT', '/api/prefs', { theme: 'dark' },
    { headers: { 'content-type': 'text/plain' } })).status, 415, 'prefs: a non-JSON PUT is refused');
  assertEq((await api('PUT', '/api/prefs', { theme: 'dark' },
    { headers: { origin: 'http://evil.example' } })).status, 403, 'prefs: a cross-origin PUT is refused');
  assertEq((await api('PUT', '/api/prefs', { theme: 'dark' },
    { headers: { 'sec-fetch-site': 'cross-site' } })).status, 403, 'prefs: a cross-site PUT is refused');
  assertEq((await api('PUT', '/api/prefs', [1, 2])).status, 500, 'prefs: a PUT that is not an object is refused');
  assertEq((await api('PUT', '/api/prefs', { theme: 'dark', pad: 'x'.repeat(64 * 1024) })).status, 413,
    'prefs: an oversized PUT is refused (a few settings are a few hundred bytes)');
  pr = await api('PUT', '/api/prefs', { theme: 'dark', zoom: 125, fit: 'none', evil: 'x', snapshot: 'yes' });
  assertEq([pr.status, pr.data.prefs, pr.data.ignored.sort()],
    [200, { theme: 'dark', zoom: 125, fit: 'none' }, ['evil', 'snapshot']],
    'prefs: known keys with valid values are kept; the rest are named as ignored');
  assertEq(JSON.parse(fs.readFileSync(path.join(project, 'dist', '.studio-prefs.json'), 'utf-8')),
    { theme: 'dark', zoom: 125, fit: 'none' }, "prefs: written to the project's dist/.studio-prefs.json");
  const themed = await api('GET', '/');
  assertTrue(/<html lang="en" data-theme="dark">/.test(themed.text),
    'prefs: the page is served in the saved theme (no flash of the other one)');
  const metaMatch = /<meta name="studio-prefs" content="([^"]*)">/.exec(themed.text);
  assertEq(metaMatch && JSON.parse(metaMatch[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&')),
    { theme: 'dark', zoom: 125, fit: 'none' }, 'prefs: ...with every saved setting in its <meta>');
  assertEq(themed.headers['content-security-policy'], pagePolicy,
    "prefs: ...and the same Content-Security-Policy (the script's hash is unchanged)");
  await api('PUT', '/api/prefs', { theme: 'system' });
  assertTrue(!/<html[^>]*data-theme=/.test((await api('GET', '/')).text), 'prefs: System leaves the theme to the OS');
  fs.rmSync(path.join(project, 'dist', '.studio-prefs.json'));

  // Opening a file in the editor: inputs only, and never through a shell.
  const editor = (await api('GET', '/api/open')).data.editor;
  if (process.platform !== 'win32') {
    assertEq(editor, 'default', 'open: off Windows there is no vscode:// handler to look for');
  }
  let op = await api('POST', '/api/open', { path: 'data/resume_default.yml', line: 12, col: 3 });
  assertEq([op.status, op.data.opened, op.data.editor], [200, 'data/resume_default.yml', editor],
    `open: a data file is opened (${op.data && op.data.error})`);
  assertTrue(Array.isArray(op.data.command) && (editor === 'vscode'
    ? /^vscode:\/\/file\/.*resume_default\.yml:12:3$/.test(op.data.command[1])
    : op.data.command[1] === fs.realpathSync.native(path.join(project, 'data', 'resume_default.yml'))
      && op.data.command.length === 2),
    `open: ...as a program and its arguments, the file named in full (${JSON.stringify(op.data.command)})`);
  op = await api('POST', '/api/open', { path: 'styles/_base.scss', line: 'x', col: -4 });
  assertEq(op.status, 200, 'open: a stylesheet is opened, a bad line or column is just 1');
  for (const bad of ['../../../etc/passwd', 'build/engine.js', 'package.json', 'data/../package.json',
    'data', 'data/missing.yml', path.join(project, 'build', 'engine.js'), '']) {
    op = await api('POST', '/api/open', { path: bad, line: 1 });
    assertEq(op.status, 500, `open: ${JSON.stringify(bad)} is refused`);
  }
  // Only the inputs' kinds: "open" hands a file to its default program,
  // which runs a .bat, .exe, .desktop or .sh. The file a link finally
  // leads to must be one of them too; a link to a .yml elsewhere is fine.
  {
    const dataDir = path.join(project, 'data');
    const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-open-outside-'));
    const made = [];
    const make = (name, fn) => {
      const f = path.join(dataDir, name);
      try { fn(f); made.push(f); return true; } catch { return false; }
    };
    try {
      for (const name of ['x.bat', 'x.sh', 'x.desktop', 'x.yml.exe', 'x.YML.lnk']) {
        make(name, f => fs.writeFileSync(f, ''));
        op = await api('POST', '/api/open', { path: `data/${name}` });
        assertEq(op.status, 500, `open: data/${name} is not an input, refused`);
      }
      fs.writeFileSync(path.join(outside, 'tool.exe'), '');
      fs.writeFileSync(path.join(outside, 'synced.yml'), 'a: 1\n');
      const linkType = process.platform === 'win32' ? 'junction' : 'dir';
      if (make('prog.yml', f => fs.symlinkSync(path.join(outside, 'tool.exe'), f))) {
        op = await api('POST', '/api/open', { path: 'data/prog.yml' });
        assertEq(op.status, 500, 'open: a .yml link to a program elsewhere is refused');
      }
      if (make('outdir', f => fs.symlinkSync(outside, f, linkType))) {
        op = await api('POST', '/api/open', { path: 'data/outdir/tool.exe' });
        assertEq(op.status, 500, 'open: a directory link out of data/ does not reach a program');
      }
      if (make('synced.yml', f => fs.symlinkSync(path.join(outside, 'synced.yml'), f))) {
        op = await api('POST', '/api/open', { path: 'data/synced.yml' });
        assertEq([op.status, op.data.opened], [200, 'data/synced.yml'],
          'open: a data file linked from another folder opens (the app reads it too)');
      }
    } finally {
      for (const f of made) fs.rmSync(f, { force: true });
      fs.rmSync(outside, { recursive: true, force: true });
    }
  }
  assertEq((await api('POST', '/api/open', { path: 'data/resume_default.yml' },
    { headers: { origin: 'http://evil.example' } })).status, 403, 'open: a cross-origin request is refused');
  assertEq((await api('POST', '/api/open', { path: 'data/resume_default.yml' },
    { headers: { 'content-type': 'text/plain' } })).status, 415, 'open: a non-JSON body is refused');

  // M9 — a malformed Host does not take the server down.
  const bad = await rawRequest(port, `GET /api/status HTTP/1.1\r\nHost: [::bad\r\nConnection: close\r\n\r\n`);
  assertTrue(/^HTTP\/1\.1 (400|403)/.test(bad), `a malformed Host is refused (${bad.split('\r\n')[0]})`);
  const none = await rawRequest(port, `GET /api/status HTTP/1.0\r\n\r\n`);
  assertTrue(/^HTTP\/1\.[01] (400|403)/.test(none), `a request with no Host is refused (${none.split('\r\n')[0]})`);
  assertEq((await api('GET', '/api/status')).status, 200, 'the server is still up afterwards');

  // A folder named *.yml is not a data file the inspector can offer.
  fs.mkdirSync(path.join(project, 'data', 'folder.yml'));
  try {
    const listed = ((await api('GET', '/api/status')).data.dataFiles || []).map(f => f.name);
    assertTrue(listed.length > 0 && !listed.includes('folder.yml'),
      `status: a folder named folder.yml is not listed as a data file (${listed.join(', ')})`);
    const files = ((await api('GET', '/api/datafiles')).data.files || []).map(f => f.name);
    assertTrue(!files.includes('folder.yml'), 'datafiles: ...nor in the Data files dialog');
  } finally {
    fs.rmSync(path.join(project, 'data', 'folder.yml'), { recursive: true, force: true });
  }

  // L-b — adopting a dropped file.
  const letterYml = fs.readFileSync(path.join(project, 'data', 'letter_default.yml'), 'utf-8');
  let r = await api('POST', '/api/adopt', { filename: '_profile.yml', content: letterYml });
  assertTrue(r.status === 500 && /underscore/.test(r.data.error),
    'adopt: an underscore name is refused');
  assertTrue(!fs.existsSync(path.join(project, 'data', '_profile.yml')), 'adopt: _profile.yml was not written');
  r = await api('POST', '/api/adopt-as', { doc: 'letter', name: '_x.yml', content: letterYml });
  assertTrue(r.status === 500 && /underscore/.test(r.data.error), 'adopt-as: an underscore name is refused');

  r = await api('POST', '/api/adopt', { filename: 'dropped.yml', content: letterYml });
  assertEq([r.status, r.data.saved, r.data.doc], [200, 'data/dropped.yml', 'letter'], 'adopt: saves a free name');
  r = await api('POST', '/api/adopt', { filename: 'dropped.yml', content: letterYml });
  assertEq([r.data.identical, r.data.picked], [true, 'data/dropped.yml'], 'adopt: the same file is just read');
  r = await api('POST', '/api/adopt', { filename: 'dropped.yml', content: letterYml + '\n# changed\n' });
  assertEq([r.data.conflict, r.data.suggestion], [true, 'dropped-2.yml'], 'adopt: different contents are a conflict');
  assertEq(fs.readFileSync(path.join(project, 'data', 'dropped.yml'), 'utf-8'), letterYml,
    'adopt: the conflict left the file on disk alone');
  r = await api('POST', '/api/adopt-as', { doc: 'letter', name: 'dropped.yml', content: 'x: 1\n' });
  assertTrue(r.status === 500 && /already exists/.test(r.data.error), 'adopt-as: a taken name is refused');
  assertEq(fs.readFileSync(path.join(project, 'data', 'dropped.yml'), 'utf-8'), letterYml,
    'adopt-as: the taken file is unchanged');
  await api('POST', '/api/pick', { doc: 'letter', name: null });
  fs.rmSync(path.join(project, 'data', 'dropped.yml'));

  // Names Windows reads as something else are refused before anything
  // is written: CON.yml is the console, x.yml:y.yml a hidden stream.
  for (const name of ['CON.yml', 'nul.backup.yml', 'x.yml:y.yml']) {
    r = await api('POST', '/api/adopt', { filename: name, content: letterYml });
    assertTrue(r.status === 500 && /reserves|not allowed/.test(r.data.error),
      `adopt: ${name} is refused (${r.status} ${r.data && r.data.error})`);
    assertTrue(!fs.existsSync(path.join(project, 'data', name)), `adopt: ${name} was not written`);
  }
  r = await api('POST', '/api/adopt-as', { doc: 'letter', name: 'COM1.yaml', content: letterYml });
  assertTrue(r.status === 500 && /reserves/.test(r.data.error), 'adopt-as: COM1.yaml is refused');
  if (process.platform === 'win32') {
    r = await api('POST', '/api/pick', { doc: 'letter', name: 'AUX.yml' });
    assertTrue(r.status === 500 && /reserves/.test(r.data.error), 'pick: AUX.yml is refused');
  } else {
    // An existing file is readable under the name it already has here.
    for (const name of ['aux.yml', 'Resume 9:2026.yml']) {
      fs.writeFileSync(path.join(project, 'data', name), letterYml);
      r = await api('POST', '/api/pick', { doc: 'letter', name });
      assertEq(r.status, 200, `pick: an existing ${name} opens (${r.data && r.data.error})`);
      r = await api('GET', `/api/datafile?name=${encodeURIComponent(name)}`);
      assertEq(r.status, 200, `datafile: an existing ${name} is read (${r.data && r.data.error})`);
      fs.rmSync(path.join(project, 'data', name));
    }
    await api('POST', '/api/pick', { doc: 'letter', name: null });
  }
  assertTrue(server.unreadableName('AUX.yml', 'win32') !== null, 'unreadableName: AUX.yml on Windows');
  assertEq(server.unreadableName('Resume 9:2026.yml', 'darwin'), null, 'unreadableName: an existing macOS name');

  // Preview parameters: a bad scale or page list renders with defaults
  // rather than failing in the worker, and a huge scale is clamped.
  r = await api('POST', '/api/preview', { doc: 'letter', scale: 'abc', pages: 3 });
  assertEq([r.status, r.data.images && r.data.images.length], [200, 1],
    `preview: a scale that is not a number and a bare page number render anyway (${r.data && r.data.error})`);
  r = await api('POST', '/api/preview', { doc: 'letter', scale: 50, pages: ['x'] });
  assertEq([r.status, r.data.images && r.data.images[0].width], [200, 8.5 * 72 * 4],
    `preview: scale 50 is rendered at 4 (${r.data && r.data.error})`);

  // Busy while anything is under way. The build waits behind the
  // preview on the engine's queue; once the preview is done, the build
  // still runs, and a single flag used to say idle then.
  const previewing = api('POST', '/api/preview', { doc: 'letter', scale: 1.5 });
  const building = api('POST', '/api/build', { doc: 'letter', scale: 1 });
  await previewing;
  let busyNow = (await api('GET', '/api/status')).data.busy;
  const buildDone = (await building).status;
  assertEq([busyNow, buildDone], [true, 200], 'status: busy after a preview ends while a build still runs');
  busyNow = (await api('GET', '/api/status')).data.busy;
  assertEq(busyNow, false, 'status: ...and idle once both are done');

  // Editors' own files. vim's swap file and Emacs's lock and autosave
  // appear while typing, before any save; none of them may render. An
  // atomic save (JetBrains' safe write: temp file, old copy, rename over
  // the input) renders once, for the input.
  const changes = [];
  const watch = await events(port, (name, data) => {
    if (name === 'changed') changes.push(data.file ? path.basename(data.file) : null);
  });
  try {
    const dataDir = path.join(project, 'data');
    const target = path.join(dataDir, 'letter_default.yml');
    const settle = () => new Promise(res => setTimeout(res, 1300));   // debounce and a poll
    await settle();
    changes.length = 0;
    fs.writeFileSync(path.join(dataDir, '.letter_default.yml.swp'), 'b0VIM 9.0');
    fs.writeFileSync(path.join(dataDir, '4913'), '');
    fs.rmSync(path.join(dataDir, '4913'));
    fs.writeFileSync(path.join(dataDir, '#letter_default.yml#'), 'autosave');
    fs.writeFileSync(path.join(dataDir, '~$tter_default.yml'), 'lock');
    await settle();
    assertEq(changes, [], 'watch: swap, probe, autosave and lock files render nothing');
    for (const f of ['.letter_default.yml.swp', '#letter_default.yml#', '~$tter_default.yml']) {
      fs.rmSync(path.join(dataDir, f));
    }
    await settle();
    changes.length = 0;
    const before = fs.readFileSync(target, 'utf-8');
    fs.writeFileSync(`${target}___jb_tmp___`, `${before}\n# saved\n`);
    fs.renameSync(target, `${target}___jb_old___`);
    fs.renameSync(`${target}___jb_tmp___`, target);
    fs.rmSync(`${target}___jb_old___`);
    await settle();
    assertEq(changes, ['letter_default.yml'], 'watch: an atomic save renders once, for the input');
    fs.writeFileSync(target, before);
    await settle();
  } finally {
    watch.close();
  }

  // An event-stream client that stops reading. Streamed renders at the
  // largest scale queue a few MB each for it; past the limit it is
  // dropped, which its EventSource would answer by reconnecting.
  const stalled = await new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/api/events',
      headers: { host: `127.0.0.1:${port}` } }, (res) => {
      res.pause();
      req.socket.pause();
      resolve({ req, res });
    });
  });
  for (let i = 0; i < 12; i++) {
    r = await api('POST', '/api/preview',
      { doc: 'resume', scale: 3.9 + i / 100, stream: true, renderId: `stall-${i}` });
    assertEq(r.status, 200, `SSE: streamed render ${i} succeeds with a stalled client (${r.data && r.data.error})`);
  }
  const closedByServer = await new Promise((resolve) => {
    stalled.res.on('end', () => resolve(true));
    stalled.res.on('error', () => resolve(true));
    stalled.res.on('close', () => resolve(true));
    stalled.res.on('data', () => {});
    stalled.req.socket.resume();
    stalled.res.resume();
    setTimeout(() => resolve(false), 5000);
  });
  stalled.req.destroy();
  assertTrue(closedByServer, 'SSE: a client that stopped reading is dropped rather than buffered for');

  // H1 — the letter's data is the letter card's choice alone.
  const mine = letterYml.replace('first: Gaius', 'first: Testy').replace('last: Caesar', 'last: McTest');
  assertTrue(mine !== letterYml, 'fixture: letter.yml differs from the template');
  fs.writeFileSync(path.join(project, 'data', 'letter.yml'), mine);

  await api('POST', '/api/datasource', { source: 'default' });   // the resume card: Template
  let status = (await api('GET', '/api/status')).data;
  assertEq(status.documents.letter.dataFile.name, 'letter.yml',
    'H1: the letter card names letter.yml while the resume is forced to its template');

  r = await api('POST', '/api/build', { doc: 'letter', scale: 1 });
  assertEq(r.status, 200, `H1: the letter builds (${r.data && r.data.error})`);
  assertTrue(r.data && /Testy_McTest_Cover_Letter\.pdf$/.test(r.data.pdf.path),
    `H1: the build read letter.yml (${r.data && r.data.pdf.path})`);
  assertTrue(fs.existsSync(path.join(project, 'dist', 'Testy_McTest_Cover_Letter.pdf')),
    'H1: the real letter PDF exists after the build');
  assertEq(r.data && r.data.render && r.data.render.meta && r.data.render.meta.author, 'Testy McTest',
    'H1: the built render describes the file that was built');

  r = await api('POST', '/api/preview', { doc: 'letter', scale: 1 });
  assertEq(r.data && r.data.meta && r.data.meta.author, 'Testy McTest',
    'H1: the preview reads letter.yml too, not the resume source');

  r = await api('POST', '/api/pick', { doc: 'letter', name: 'letter_default.yml' });
  assertEq(r.status, 200, 'H1: letter_default.yml can be picked for the letter');
  status = (await api('GET', '/api/status')).data;
  assertEq(status.documents.letter.dataFile.name, 'letter_default.yml', 'H1: the card names the picked file');
  r = await api('POST', '/api/preview', { doc: 'letter', scale: 1 });
  assertEq(r.data && r.data.meta && r.data.meta.author, 'Gaius Caesar',
    'H1: the preview honors the file picked on the letter card');
  const firstImages = (r.data && r.data.images) || [];

  // H3 — that preview rewrote dist/letter_meta.json for other data.
  status = (await api('GET', '/api/status')).data;
  assertTrue(/Testy_McTest_Cover_Letter\.pdf$/.test(status.documents.letter.pdf.path)
    && status.documents.letter.pdf.exists,
    `H3: the tray still names the built PDF after a preview (${status.documents.letter.pdf.path})`);
  r = await api('POST', '/api/preview', { doc: 'letter', from: 'built', scale: 1 });
  assertTrue(r.data && r.data.built === true && /Testy_McTest/.test(r.data.source),
    'H3: from:"built" shows the PDF the Build wrote');
  assertEq(r.data && r.data.meta && r.data.meta.author, 'Testy McTest',
    'H3: from:"built" reports the metadata of the build, not of the preview');

  // L-a — pages the caller already holds come back without a PNG.
  const known = Object.fromEntries(firstImages.map(im => [im.page, im.hash]));
  r = await api('POST', '/api/preview', { doc: 'letter', scale: 1, known });
  const again = (r.data && r.data.images) || [];
  assertTrue(again.length > 0 && again.every(im => im.unchanged === true && !im.png
    && im.hash === known[im.page]),
    'L-a: unchanged pages are sent as {hash, unchanged} without the PNG');
  r = await api('POST', '/api/preview', { doc: 'letter', scale: 1 });
  assertTrue(((r.data && r.data.images) || []).every(im => im.png && !im.unchanged),
    'L-a: a preview that names no known pages gets every PNG');

  // L-d — a streamed render: the pages on the event stream, the rest of
  // the render in the reply.
  //
  // The pane asks for the pages it is showing, in the order it shows
  // them, and applies each as it arrives instead of waiting for the
  // whole render. What must hold over HTTP is that the two halves fit
  // back together, that each page names the render it belongs to (so a
  // superseded render's pages can be dropped), and that a caller which
  // asks for none of this still gets exactly one reply with everything
  // in it.
  await api('POST', '/api/pick', { doc: 'letter', name: null });
  const streamData = path.join(project, 'data', 'stream.yml');
  fs.copyFileSync(path.join(project, 'data', 'resume_default.yml'), streamData);
  await api('POST', '/api/pick', { doc: 'resume', name: 'stream.yml' });
  const bump = (n) => fs.writeFileSync(streamData,
    fs.readFileSync(streamData, 'utf-8')
      .replace(/Phasellusx* scelerisque/, `Phasellus${'x'.repeat(n)} scelerisque`));

  const seen = [];
  const stream = await events(port, (name, data) => {
    if (name === 'page') seen.push(data);
  });
  try {
    bump(1);
    seen.length = 0;
    r = await api('POST', '/api/preview',
      { doc: 'resume', scale: 1, stream: true, renderId: 'rid-one' });
    assertEq(r.status, 200, `L-d: a streamed preview succeeds (${r.data && r.data.error})`);
    const one = r.data;
    assertEq(one.renderId, 'rid-one', 'L-d: the reply echoes the render id');
    assertTrue(one.streamed === true, 'L-d: ...and says it was streamed');
    // Give the last event a moment: it is a different socket.
    for (let i = 0; i < 50 && seen.length < one.images.length; i++) {
      await new Promise(res => setTimeout(res, 20));
    }
    assertTrue(seen.length > 0 && seen.every(e => e.renderId === 'rid-one' && e.doc === 'resume'),
      'L-d: every page event names its render and its document');
    assertEq(seen.map(e => e.image.page), one.images.map(im => im.page),
      'L-d: with no order asked for, the pages arrive in page order');
    // The reply leaves out what the events carried, and the two put
    // together are the whole render.
    assertTrue(one.images.some(im => im.sent) && one.images.every(im => !(im.sent && im.png)),
      'L-d: a page already sent carries no PNG in the reply');
    const assembled = one.images.map((im) => {
      if (!im.sent) return im;
      const ev = seen.find(e => e.image.page === im.page);
      return ev && ev.image.hash === im.hash ? ev.image : null;
    });
    assertTrue(assembled.every(im => im && im.png),
      'L-d: the events fill in every page the reply left out');

    // Order: the page on screen first. Page 2 named alone must arrive
    // before page 1.
    if (one.images.length > 1) {
      bump(2);
      seen.length = 0;
      r = await api('POST', '/api/preview',
        { doc: 'resume', scale: 1, stream: true, renderId: 'rid-two', order: [2] });
      for (let i = 0; i < 50 && seen.length < r.data.images.length; i++) {
        await new Promise(res => setTimeout(res, 20));
      }
      assertEq(seen.map(e => e.image.page), [2, 1],
        'L-d: the page the caller asked for first arrives first');
      assertTrue(seen.every(e => e.renderId === 'rid-two'),
        'L-d: ...all tagged with the new render, none with the old one');
    }

    // A caller that does not ask for streaming is answered exactly as
    // before: one reply, every page, every PNG, and nothing broadcast.
    bump(3);
    seen.length = 0;
    r = await api('POST', '/api/preview', { doc: 'resume', scale: 1 });
    assertEq(r.status, 200, `L-d: a plain preview still works (${r.data && r.data.error})`);
    assertTrue(r.data.images.length > 0 && r.data.images.every(im => im.png && !im.sent),
      'L-d: a non-streaming caller gets every PNG in the reply');
    assertTrue(r.data.renderId === undefined && r.data.streamed === undefined,
      'L-d: ...and no streaming fields');
    await new Promise(res => setTimeout(res, 150));
    assertEq(seen.length, 0, 'L-d: ...and no page events were sent');

    // The unchanged-page fast path, over HTTP, with streaming on: an
    // edit on page 1 leaves page 2 unrendered.
    bump(4);
    seen.length = 0;
    r = await api('POST', '/api/preview',
      { doc: 'resume', scale: 1, stream: true, renderId: 'rid-three' });
    assertTrue(r.data.timings && r.data.timings.renderedPages < r.data.images.length,
      `L-d: an edit on one page still leaves the others unrendered `
      + `(${r.data.timings && r.data.timings.renderedPages} of ${r.data.images.length})`);
  } finally {
    stream.close();
    await api('POST', '/api/pick', { doc: 'resume', name: null });
    fs.rmSync(streamData, { force: true });
  }

  // A data file that does not parse: the error the preview reports is
  // the one looksHalfWritten() recognizes (so a save caught halfway is
  // retried), and a file that stays broken is still reported.
  fs.writeFileSync(path.join(project, 'data', 'broken.yml'), 'letter:\n  body: [unclosed\n');
  r = await api('POST', '/api/pick', { doc: 'letter', name: 'broken.yml' });
  assertEq(r.status, 200, 'half-written: a broken letter file can be picked');
  r = await api('POST', '/api/preview', { doc: 'letter', scale: 1 });
  assertTrue(r.status === 500 && r.data && r.data.kind === 'build_failed'
    && server.looksHalfWritten({ kind: r.data.kind, message: r.data.error }),
    `half-written: a YAML error is reported as one (${r.data && r.data.error})`);
  fs.writeFileSync(path.join(project, 'data', 'broken.yml'), '');
  r = await api('POST', '/api/preview', { doc: 'letter', scale: 1 });
  assertTrue(r.status === 500 && server.looksHalfWritten({ kind: r.data.kind, message: r.data.error }),
    `half-written: an empty file is reported as one (${r.data && r.data.error})`);
  await api('POST', '/api/pick', { doc: 'letter', name: null });
  fs.rmSync(path.join(project, 'data', 'broken.yml'));

  // H2 — a crashed worker fails fast and comes back.
  const pid = status.worker.pid;
  assertTrue(isAlive(pid), 'H2: the worker pid in /api/status is a live process');
  process.kill(pid, 'SIGKILL');
  await new Promise(res => setTimeout(res, 200));
  const t0 = Date.now();
  r = await api('POST', '/api/preview', { doc: 'letter', scale: 1 }, { timeoutMs: 30000 });
  assertTrue(Date.now() - t0 < 30000, 'H2: the preview after a crash answers instead of hanging');
  r = await api('POST', '/api/preview', { doc: 'resume', scale: 1 }, { timeoutMs: 60000 });
  assertEq(r.status, 200, `H2: the next preview succeeds (${r.data && r.data.error})`);
  status = (await api('GET', '/api/status')).data;
  assertTrue(status.worker.pid !== pid && isAlive(status.worker.pid),
    'H2: /api/status reports the restarted worker');

  // Kill it again, then build: the shared queue must not be wedged.
  process.kill(status.worker.pid, 'SIGKILL');
  await new Promise(res => setTimeout(res, 200));
  r = await api('POST', '/api/build', { doc: 'letter', scale: 1 },
    { timeoutMs: 60000 });
  assertEq(r.status, 200, `H2: a build after a crash completes (${r.data && r.data.error})`);

  return (await api('GET', '/api/status')).data.worker.pid;
}


/* ─── Main ────────────────────────────────────────────────────── */

(async () => {
  let chromium;
  try {
    ({ chromium } = require('playwright'));
    const browser = await chromium.launch();
    await browser.close();
  } catch (err) {
    console.log(`SKIP studio_server: Chromium unavailable (${String(err.message).split('\n')[0]})`);
    process.exit(0);
  }

  const before = realDistFingerprint();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-server-test-'));
  const project = path.join(tmp, 'project');
  let srv = null;
  try {
    await unitTests(tmp);

    copyProject(project);
    srv = await startServer(project);
    const workerPid = await httpTests(project, srv);
    await cspTests(srv.port, chromium);
    await buildFocusTests(srv.port, chromium);

    // L-c — closing stdin (what the desktop shell does) is a graceful
    // shutdown: the server exits by itself and takes the worker with it.
    const exited = new Promise(res => srv.child.once('exit', res));
    srv.child.stdin.end();
    const outcome = await Promise.race([exited.then(() => 'exited'),
      new Promise(res => setTimeout(() => res('timeout'), 8000))]);
    assertEq(outcome, 'exited', 'closing stdin shuts the server down');
    assertTrue(await goneWithin(workerPid, 5000), 'the graceful shutdown stopped the Python worker');
    srv = null;

    // SIGHUP (the terminal it runs in is closed) is a graceful shutdown
    // too: Chromium and the worker go with the server.
    if (process.platform === 'linux') {
      srv = await startServer(project);
      const warm = await request(srv.port, 'POST', '/api/preview', { body: { doc: 'letter', scale: 1 } });
      assertEq(warm.status, 200, `SIGHUP: a preview first, so Chromium is up (${warm.data && warm.data.error})`);
      const family = descendants(srv.child.pid);
      assertTrue(family.some(pid => /chrom/i.test(commandOf(pid))) && family.some(pid => /worker\.py/.test(commandOf(pid))),
        `SIGHUP: the server has a Chromium and a worker running (${family.length} processes)`);
      const hupExit = new Promise(res => srv.child.once('exit', (code, signal) => res({ code, signal })));
      srv.child.kill('SIGHUP');
      const hup = await Promise.race([hupExit, new Promise(res => setTimeout(() => res('timeout'), 8000))]);
      assertEq(hup, { code: 0, signal: null }, 'SIGHUP: the server runs its shutdown and exits 0');
      const left = [];
      for (const pid of family) if (!(await goneWithin(pid, 5000))) left.push(`${pid} ${commandOf(pid).slice(0, 60)}`);
      assertEq(left, [], 'SIGHUP: no Chromium or worker process is left behind');
      srv = null;
    }
  } catch (err) {
    fail('studio server run', { error: `${err.stack || err.message}\n${srv ? srv.log.slice(-30).join('\n') : ''}` });
  } finally {
    if (srv) { try { srv.child.kill('SIGKILL'); } catch { /* gone */ } }
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* best effort */ }
  }
  assertEq(realDistFingerprint(), before, "isolation: nothing was written to this checkout's dist/");
  report();
})();
