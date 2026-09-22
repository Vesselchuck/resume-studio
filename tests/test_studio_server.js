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
 *     page (Origin), with JSON bodies; a malformed Host cannot crash it.
 *   • A dropped file never replaces one in data/, and a support file
 *     name (_profile.yml) is refused.
 *   • Closing the server's stdin (what the desktop shell does) shuts it
 *     down gracefully, worker included.
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


/* ─── Pure helpers ────────────────────────────────────────────── */

function unitTests(tmp) {
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

  // writeNew: never replaces.
  const target = path.join(tmp, 'wn', 'a.yml');
  assertTrue(server.writeNew(target, 'one'), 'writeNew: writes a free name');
  assertTrue(!server.writeNew(target, 'two'), 'writeNew: reports a taken name');
  assertEq(fs.readFileSync(target, 'utf-8'), 'one', 'writeNew: never replaces the file');
}


/* ─── The isolated project ────────────────────────────────────── */

function copyProject(dest) {
  const copyDir = (from, to) => {
    fs.mkdirSync(to, { recursive: true });
    for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
      if (entry.name === '__pycache__') continue;
      const src = path.join(from, entry.name);
      const dst = path.join(to, entry.name);
      if (entry.isDirectory()) copyDir(src, dst);
      else if (entry.isFile()) fs.copyFileSync(src, dst);
    }
  };
  for (const dir of ['build', 'styles', 'templates', 'fonts', 'ui', 'schemas']) {
    if (fs.existsSync(path.join(ROOT, dir))) copyDir(path.join(ROOT, dir), path.join(dest, dir));
  }
  for (const file of ['resume.js', 'letter.js', 'package.json']) {
    fs.copyFileSync(path.join(ROOT, file), path.join(dest, file));
  }
  fs.mkdirSync(path.join(dest, 'data'), { recursive: true });
  for (const file of fs.readdirSync(path.join(ROOT, 'data'))) {
    if (/_default\.ya?ml$/.test(file)) {
      fs.copyFileSync(path.join(ROOT, 'data', file), path.join(dest, 'data', file));
    }
  }
  fs.symlinkSync(path.join(ROOT, 'node_modules'), path.join(dest, 'node_modules'),
    process.platform === 'win32' ? 'junction' : 'dir');
}

function startServer(projectDir) {
  const env = { ...process.env };
  for (const k of ['RESUME_DATA_SOURCE', 'RESUME_DATA_FILE', 'LETTER_DATA_FILE', 'STUDIO_PORT']) {
    delete env[k];
  }
  const child = spawn(process.execPath,
    [path.join(projectDir, 'build', 'studio_server.js'), '--port', '0', '--exit-with-parent'],
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
        resolve({ status: res.statusCode, data, text });
      });
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`${method} ${route} timed out`)));
    req.on('error', reject);
    if (payload !== null) req.write(payload);
    req.end();
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

  // M9 — a malformed Host does not take the server down.
  const bad = await rawRequest(port, `GET /api/status HTTP/1.1\r\nHost: [::bad\r\nConnection: close\r\n\r\n`);
  assertTrue(/^HTTP\/1\.1 (400|403)/.test(bad), `a malformed Host is refused (${bad.split('\r\n')[0]})`);
  const none = await rawRequest(port, `GET /api/status HTTP/1.0\r\n\r\n`);
  assertTrue(/^HTTP\/1\.[01] (400|403)/.test(none), `a request with no Host is refused (${none.split('\r\n')[0]})`);
  assertEq((await api('GET', '/api/status')).status, 200, 'the server is still up afterwards');

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

  // H1 — the letter's data is the letter card's choice alone.
  const mine = letterYml.replace('first: Gaius', 'first: Testy').replace('last: Caesar', 'last: McTest');
  assertTrue(mine !== letterYml, 'fixture: letter.yml differs from the template');
  fs.writeFileSync(path.join(project, 'data', 'letter.yml'), mine);

  await api('POST', '/api/datasource', { source: 'default' });   // the resume card: Template
  let status = (await api('GET', '/api/status')).data;
  assertEq(status.documents.letter.dataFile.name, 'letter.yml',
    'H1: the letter card names letter.yml while the resume is forced to its template');

  r = await api('POST', '/api/build', { doc: 'letter', variants: { color: true }, scale: 1 });
  assertEq(r.status, 200, `H1: the letter builds (${r.data && r.data.error})`);
  assertTrue(r.data && /Testy_McTest_Cover_Letter\.pdf$/.test(r.data.color.path),
    `H1: the build read letter.yml (${r.data && r.data.color.path})`);
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
  assertTrue(/Testy_McTest_Cover_Letter\.pdf$/.test(status.documents.letter.color.path)
    && status.documents.letter.color.exists,
    `H3: the tray still names the built PDF after a preview (${status.documents.letter.color.path})`);
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
  r = await api('POST', '/api/build', { doc: 'letter', variants: { color: true }, scale: 1 },
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

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-server-test-'));
  const project = path.join(tmp, 'project');
  let srv = null;
  try {
    unitTests(tmp);

    copyProject(project);
    srv = await startServer(project);
    const workerPid = await httpTests(project, srv);

    // L-c — closing stdin (what the desktop shell does) is a graceful
    // shutdown: the server exits by itself and takes the worker with it.
    const exited = new Promise(res => srv.child.once('exit', res));
    srv.child.stdin.end();
    const outcome = await Promise.race([exited.then(() => 'exited'),
      new Promise(res => setTimeout(() => res('timeout'), 8000))]);
    assertEq(outcome, 'exited', 'closing stdin shuts the server down');
    assertTrue(await goneWithin(workerPid, 5000), 'the graceful shutdown stopped the Python worker');
    srv = null;
  } catch (err) {
    fail('studio server run', { error: `${err.stack || err.message}\n${srv ? srv.log.slice(-30).join('\n') : ''}` });
  } finally {
    if (srv) { try { srv.child.kill('SIGKILL'); } catch { /* gone */ } }
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* best effort */ }
  }
  report();
})();
