/**
 * studio_server.js — the Studio app's backend, on localhost.
 *
 * WHY A SERVER INSIDE A DESKTOP APP
 * ---------------------------------
 * The obvious Tauri design puts all of this in Rust: spawn the engine,
 * frame its stdio, expose #[tauri::command]s. It works, and it means
 * the entire app can only be run, tested or debugged by someone with a
 * Rust toolchain and the platform's webview dev packages installed.
 *
 * Putting the app behind a small HTTP server instead leaves the Rust
 * layer with one job — start this, show that URL — measured in dozens
 * of lines rather than hundreds. The whole application is then
 * testable with `npm run ui` and a browser, the UI can be iterated on
 * without recompiling anything, and the desktop build is a wrapper
 * rather than a rewrite.
 *
 * It listens on 127.0.0.1 only, on an ephemeral port by default, and
 * prints the chosen URL as a framed line the Rust side reads. Every
 * request must name this server in its Host header, an Origin must be
 * its own, and POST bodies must be JSON — see checkRequest.
 *
 * ENDPOINTS
 *   GET  /                 the UI
 *   GET  /api/status       engine + data-source state
 *   GET  /api/datafiles    what's in data/
 *   GET  /api/datafile     ?name= -> one file's contents
 *   GET  /api/events       SSE: log lines, render/build state, and the
 *                          `page` events of a streamed render
 *   POST /api/preview      {doc, scale, pages, from, known, stream,
 *                          renderId, order} -> rasterized PDF pages;
 *                          from:'built' reads the last Build's PDF
 *                          instead of re-rendering; known ({page: hash})
 *                          returns unchanged pages without a PNG;
 *                          stream:true sends each page on /api/events as
 *                          it is ready (most wanted first, per `order`)
 *                          and leaves those PNGs out of the reply
 *   POST /api/build        {doc, variants, snapshot, tests} -> shells out to the
 *                          CLI, and returns the built PDF rasterized
 *   POST /api/datasource   {source: 'default'|'mine'}
 *   POST /api/pick         {doc, name} -> read that data/ file in place
 *   POST /api/adopt        {filename, content} -> works out which document
 *                          it is from its contents, then reads it if it is
 *                          already here, else saves it under its own name;
 *                          reports a conflict rather than ever replacing
 *   POST /api/adopt-as     {doc, name, content} -> save under a chosen free name
 *   POST /api/reveal       {path} -> show it in the OS file manager
 *   POST /api/shutdown
 *
 * Run standalone:  node build/studio_server.js [--port 4173] [--open]
 */

// First, before anything heavy is required: V8 reuses the compiled form
// of every module loaded after this line, which is most of what a cold
// start spends its time on (playwright's require alone was ~290 ms).
// Silently a no-op on Node < 22.8 or when the cache cannot be written.
// See build/_compile_cache.js for where the cache lives.
const compileCache = require('./_compile_cache');
compileCache.enable();

const http = require('http');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const { createEngine } = require('./engine');
const {
  outputPaths, outputPattern, RETIRED_GRAYSCALE_SUFFIXES, DOC_SUFFIX, SEPARATOR,
} = require('./_output_name');
const {
  ENV_RESUME_SNAPSHOT,
  ENV_RESUME_TESTS,
  ENV_RESUME_DATA_SOURCE,
  ENV_RESUME_DATA_FILE,
  ENV_LETTER_DATA_FILE,
} = require('./_env_contract');

const ROOT = path.join(__dirname, '..');
const UI_DIR = path.join(ROOT, 'ui');
const DATA_DIR = path.join(ROOT, 'data');
const DIST = path.join(ROOT, 'dist');

// Printed on stdout when listening, for the Tauri shell to read.
const READY_PREFIX = '\x1eSTUDIO_READY ';

/*
 * SAVES, AND FILES CAUGHT HALF-WRITTEN
 * ------------------------------------
 * A save reaches the preview through the watcher below, after
 * WATCH_DEBOUNCE_MS of quiet. Editors often report one save as two or
 * three events (write, then rename or touch); 20 ms collects those while
 * adding next to nothing to every save.
 *
 * The cost of a short debounce is that the render can start while the
 * editor is still writing: an editor that truncates the file and then
 * writes it can be caught in between, and the YAML then reads as empty
 * or cut off mid-line. On Windows the editor may also still hold the
 * file open, and reading it fails with a sharing violation. Rather than
 * flash that error, a preview whose data could not be read as YAML —
 * or read as nothing at all, or not read because the file was locked —
 * within HALF_WRITTEN_WINDOW_MS of a change is tried once more after
 * HALF_WRITTEN_RETRY_MS. A file that really is broken fails the second
 * time too and is reported as before, 50 ms later; a schema error in a
 * file that parsed is never retried.
 */
const WATCH_DEBOUNCE_MS = 20;
const HALF_WRITTEN_WINDOW_MS = 1000;
const HALF_WRITTEN_RETRY_MS = 50;
const HALF_WRITTEN_MESSAGE = /could not be read as YAML|is empty or not a YAML mapping/;

/** True for a build failure that a half-written data file would cause. */
function looksHalfWritten(err) {
  if (!err) return false;
  // build/worker.py reports a PermissionError as 'locked_file'.
  if (err.kind === 'locked_file') return true;
  return err.kind === 'build_failed' && HALF_WRITTEN_MESSAGE.test(String(err.message || ''));
}

/**
 * Run `render`; if it fails the way a half-written file would, within
 * HALF_WRITTEN_WINDOW_MS of the last change (`changedAt`, a Date.now()
 * value or null), wait HALF_WRITTEN_RETRY_MS and run it once more.
 * `now` and `sleep` are injectable for tests.
 */
async function retryIfHalfWritten(render, {
  changedAt = null,
  now = Date.now,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  onRetry = null,
} = {}) {
  const startedAt = now();
  try {
    return await render();
  } catch (err) {
    if (!looksHalfWritten(err) || changedAt === null
        || startedAt - changedAt > HALF_WRITTEN_WINDOW_MS) {
      throw err;
    }
    if (onRetry) onRetry(err);
    await sleep(HALF_WRITTEN_RETRY_MS);
    return await render();
  }
}

/**
 * The two documents this app knows about, and how each one is built.
 *
 * Both are live now: build/pipeline.js drives each, with `variant`
 * selecting the paths and the build phase. They are still listed
 * separately because everything else about them differs — their data
 * file, their output names, their CLI entry point — and because the
 * separation between them is the point: building one never touches
 * the other's files.
 */
const DOCS = {
  resume: {
    label: 'Resume',
    script: 'resume.js',
    variant: 'resume',
    live: true,
    meta: path.join(DIST, 'pdf_meta.json'),
    myData: path.join(DATA_DIR, 'resume.yml'),
    defaultData: path.join(DATA_DIR, 'resume_default.yml'),
  },
  letter: {
    label: 'Cover Letter',
    script: 'letter.js',
    variant: 'letter',
    live: true,
    meta: path.join(DIST, 'letter_meta.json'),
    myData: path.join(DATA_DIR, 'letter.yml'),
    defaultData: path.join(DATA_DIR, 'letter_default.yml'),
  },
};

/*
 * `pdf` is the path the last real Build wrote.
 *
 * The built PDFs are named after you — Gaius_Caesar_Resume.pdf — so
 * their paths depend on data this server never parses. build.py writes
 * the stem into the document's metadata JSON, and _output_name.js
 * reads it back.
 *
 * They used to be read from that JSON on every access. But a live
 * preview runs the same build.py and rewrites the same JSON, so
 * previewing a different data file renamed the tray's idea of the
 * built PDF to one that does not exist — "Not built", beside a PDF
 * sitting in dist/. So the paths are remembered instead: captured from
 * the metadata at the moment a Build completes (inside the engine's
 * queue, before any preview can rewrite it), and discovered once from
 * dist/ when the server starts. Nothing extra is written to disk.
 *
 * Deliberately non-enumerable so JSON.stringify(DOCS) stays a
 * description of configuration rather than a filesystem snapshot.
 */
const builtOutputs = {};

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf-8')); } catch { return null; }
}

/**
 * What a document's built PDFs are called, as of now.
 *
 * The metadata's name wins when a PDF by that name exists. Otherwise
 * dist/ is searched with the document's own output pattern — a Build
 * deletes every other file matching it, so what is left is the last
 * Build's — newest first. With nothing built, the metadata's name is
 * still returned so the tray's tooltip has a path to show.
 */
function discoverBuilt(doc) {
  const fromMeta = outputPaths(DIST, doc.meta, doc.variant);
  let chosen = fromMeta;
  if (!fs.existsSync(fromMeta.pdf)) {
    // A current output's stem is the bare document suffix or ends in
    // "<separator><suffix>"; anything else the pattern admits (a
    // retired grayscale spelling, from back when a second PDF was
    // built) is not something a Build writes now.
    const suffix = DOC_SUFFIX[doc.variant];
    const isStem = (stem) => stem === suffix || stem.endsWith(`${SEPARATOR}${suffix}`);
    const retired = RETIRED_GRAYSCALE_SUFFIXES.map(x => `${x}.pdf`);
    let newest = null;
    let entries = [];
    try { entries = fs.readdirSync(DIST); } catch { /* nothing built */ }
    for (const name of entries) {
      if (!outputPattern(doc.variant).test(name)) continue;
      if (retired.some(x => name.endsWith(x))) continue;
      const stem = name.slice(0, -'.pdf'.length);
      if (!isStem(stem)) continue;
      let mtime;
      try { mtime = fs.statSync(path.join(DIST, name)).mtimeMs; } catch { continue; }
      if (!newest || mtime > newest.mtime) newest = { mtime, stem };
    }
    if (newest) {
      chosen = { stem: newest.stem, pdf: path.join(DIST, `${newest.stem}.pdf`) };
    }
  }
  const meta = readJson(doc.meta);
  return { ...chosen, meta: meta && meta.output_stem === chosen.stem ? meta : null };
}

function builtPaths(doc) {
  if (!builtOutputs[doc.variant]) builtOutputs[doc.variant] = discoverBuilt(doc);
  return builtOutputs[doc.variant];
}

/** Call only right after a Build, inside the engine's queue. */
function rememberBuilt(doc) {
  const paths = outputPaths(DIST, doc.meta, doc.variant);
  builtOutputs[doc.variant] = { ...paths, meta: readJson(doc.meta) };
  return builtOutputs[doc.variant];
}

for (const doc of Object.values(DOCS)) {
  Object.defineProperties(doc, {
    pdf: {
      get() { return builtPaths(this).pdf; },
    },
  });
}


/* ─── Log fan-out ─────────────────────────────────────────────── */

/**
 * The engine and the pipeline both log through _console, which writes
 * to process.stdout. Rather than give them a second logging channel to
 * keep in sync with the first, tee stdout: the terminal still sees
 * everything, and every line is also pushed to connected SSE clients.
 *
 * Frames (lines starting with ASCII RS) are protocol, not log, and are
 * passed through without being broadcast.
 */
const clients = new Set();

function broadcast(event, data) {
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of clients) {
    try { res.write(payload); } catch { clients.delete(res); }
  }
}

function teeStdout() {
  // Nobody may be reading stdout. Under the desktop shell it is a pipe,
  // and if that pipe's read end goes away every subsequent write raises
  // EPIPE — which, unhandled, is an 'error' event on the socket and
  // takes the whole process down. A build server must not die because
  // its log has no audience.
  //
  // The listener stops the throw; the try/catch stops a synchronous one.
  // Either way the SSE clients above still get the line, which is where
  // the log the user actually looks at comes from.
  process.stdout.on('error', (err) => {
    if (err && (err.code === 'EPIPE' || err.code === 'ERR_STREAM_DESTROYED')) return;
    throw err;
  });

  const original = process.stdout.write.bind(process.stdout);
  process.stdout.write = (chunk, enc, cb) => {
    const text = typeof chunk === 'string' ? chunk : String(chunk);
    if (!text.startsWith('\x1e')) {
      text.split('\n').filter(l => l.trim()).forEach(line => broadcast('log', { line }));
    }
    try {
      return original(chunk, enc, cb);
    } catch (err) {
      if (err && (err.code === 'EPIPE' || err.code === 'ERR_STREAM_DESTROYED')) return true;
      throw err;
    }
  };
}


/* ─── Small HTTP helpers ──────────────────────────────────────── */

function json(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(text),
    'cache-control': 'no-store',
  });
  res.end(text);
}

function readBody(req, limitBytes = 8 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limitBytes) {
        reject(new Error(`request body over ${Math.round(limitBytes / 1024)} KB`));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf-8');
      if (!raw) return resolve({});
      try { resolve(JSON.parse(raw)); } catch (err) { reject(err); }
    });
    req.on('error', reject);
  });
}

/**
 * The data file a document will actually read on the next build.
 *
 * This has to agree with build.py's loader, because the card built from
 * it is a claim about what the build does. Getting it wrong is worse
 * than showing nothing: it was previously hard-wired to "yours if the
 * file exists", so choosing `default` in the inspector left the card
 * still naming your resume while the build read the template.
 *
 * The rules, mirroring load_data():
 *   picked    → that file, read in place (*_DATA_FILE)
 *   'default' → the shipped template, and it is an error if absent
 *   'mine'    → your own file, and it is an error if absent
 *   unset     → yours if present, else the template
 *
 * Each document's selection is its own. `source` is the resume card's
 * RESUME_DATA_SOURCE and is ignored for the letter: the letter reads
 * the file picked on its own card, else letter.yml, else
 * letter_default.yml. renderEnv below is what makes that true of the
 * build — it never hands the resume's source to a letter render.
 */
function dataFileInfo(doc, source, pickedPath) {
  const effective = doc.variant === 'letter' ? null : source;

  let target;
  if (pickedPath) target = pickedPath;
  else if (effective === 'default') target = doc.defaultData;
  else if (effective === 'mine') target = doc.myData;
  else target = fs.existsSync(doc.myData) ? doc.myData : doc.defaultData;

  const info = {
    path: path.relative(ROOT, target).replace(/\\/g, '/'),
    name: path.basename(target),
    isMine: target === doc.myData,
    forced: Boolean(effective),
    picked: Boolean(pickedPath),
    missing: !fs.existsSync(target),
  };
  if (!info.missing) {
    const st = fs.statSync(target);
    info.mtimeMs = st.mtimeMs;
    info.bytes = st.size;
  }
  return info;
}

/**
 * The data-selection variables for one document's render or build.
 *
 * Every variable is stated, including the ones that are off (null
 * unsets it), so the choice on the card is the whole story: a
 * RESUME_DATA_SOURCE or *_DATA_FILE inherited from the shell that
 * started the server cannot change what a render reads behind the
 * card's back, and the resume card's source never reaches the letter.
 * build_letter.py does honor RESUME_DATA_SOURCE when it is set, which
 * is exactly why the letter's renders clear it.
 */
function renderEnv(id, { dataSource = null, picked = {} } = {}) {
  if (id === 'letter') {
    return {
      [ENV_RESUME_DATA_SOURCE]: null,
      [ENV_LETTER_DATA_FILE]: picked.letter || null,
    };
  }
  return {
    [ENV_RESUME_DATA_SOURCE]: dataSource || null,
    [ENV_RESUME_DATA_FILE]: picked.resume || null,
  };
}

/**
 * Is this request addressed to this server, from this server's page?
 *
 * Loopback binding keeps other machines out, but not other web pages
 * in the user's own browser: a page on any site can send requests to
 * 127.0.0.1, and a DNS-rebinding page can even make them look
 * same-origin. So every request must carry a Host naming this server
 * exactly (127.0.0.1 or localhost, on the port actually listened on),
 * and an Origin, when a browser sends one, must be this server's own —
 * same scheme, host and port, compared exactly rather than by prefix.
 * The desktop window loads http://127.0.0.1:<port>/, so it passes.
 *
 * Returns null when allowed, or [status, message] when not.
 */
function checkRequest(req, port, extraHosts = []) {
  const hosts = new Set(['127.0.0.1', 'localhost', ...extraHosts]
    .map(h => `${h}:${port}`));
  const host = String(req.headers.host || '').toLowerCase();
  if (!hosts.has(host)) {
    return [403, 'requests must be addressed to this server (Host header)'];
  }
  const origin = req.headers.origin;
  if (origin !== undefined) {
    const origins = new Set([...hosts].map(h => `http://${h}`));
    if (!origins.has(String(origin).toLowerCase())) {
      return [403, 'cross-origin requests are not accepted'];
    }
  }
  if (req.method === 'POST') {
    const type = String(req.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
    if (type !== 'application/json') {
      return [415, 'POST bodies must be sent as application/json'];
    }
  }
  return null;
}

/**
 * Write a file that must not exist yet.
 *
 * `wx` makes the existence check and the write one operation, so a file
 * that appears between a check and a write — another tab, another
 * drop — is reported rather than replaced. Returns false when the name
 * is taken.
 */
function writeNew(target, content) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  try {
    fs.writeFileSync(target, content, { encoding: 'utf-8', flag: 'wx' });
    return true;
  } catch (err) {
    if (err && err.code === 'EEXIST') return false;
    throw err;
  }
}

/**
 * Resolve a client-supplied path, or null if it escapes the project.
 *
 * Separate from its caller so it can be tested directly. It is the
 * only thing standing between `POST /api/reveal` and "ask the
 * operating system to open any path on this machine", and a check
 * like that wants a test more than it wants brevity.
 *
 * path.resolve collapses `..` before the comparison, so `../../etc`
 * is rejected on its resolved form rather than by looking for the
 * literal dots. The `+ path.sep` matters too: without it, a sibling
 * directory whose name merely starts with the project's — say
 * `resume-studio-backup` — would pass a plain startsWith.
 */
function resolveInsideRoot(raw) {
  const root = path.resolve(ROOT);
  const target = path.resolve(root, String(raw || '').trim());
  if (target !== root && !target.startsWith(root + path.sep)) return null;
  return target;
}

/**
 * Is this entry in data/ a document the app can build?
 *
 * Files whose names start with an underscore are support files, not
 * documents. Right now that means _profile.yml — the shared block of
 * name, contact and language that every build merges underneath the
 * document it is building.
 *
 * They are still watched, because editing your phone number should
 * re-render the preview like any other change. They are just never
 * offered as something to *build*, because building _profile.yml is
 * not a coherent request: it has no sidebar, no jobs and no letter.
 */
function isDocumentFile(name) {
  return /\.ya?ml$/i.test(name) && !name.startsWith('_');
}

/**
 * Work out whether a dropped file is a resume or a cover letter by
 * reading it, not by reading its name.
 *
 * WHY NOT THE FILENAME
 * --------------------
 * The app used to guess from the name (/cover|letter/i) and then ask
 * you to confirm. The guess is wrong often enough to be worth the
 * question — "letter" appears in plenty of resume filenames, a file
 * called `acme-2026.yml` says nothing at all — and the question was
 * being asked every single time to cover a case that is actually rare.
 *
 * The contents are unambiguous. The two documents have disjoint
 * required keys, and these are the exact keys their validators demand:
 *
 *   resume  build.py              needs `sidebar` and `mainColumn`
 *   letter  build_letter.py needs `letter`
 *
 * So this is not a heuristic dressed up as detection — a file that
 * satisfies one validator cannot satisfy the other.
 *
 * WHY A LINE SCAN RATHER THAN A YAML PARSE
 * ----------------------------------------
 * This is Node; the real loader is Python (build/_yaml_loader.py), and
 * round-tripping through the worker to learn one bit would be slower
 * and could fail on a file the user is midway through fixing. Only
 * top-level keys matter, and in YAML those are the ones at column 0 —
 * block scalar bodies and nested mappings are all indented, so a
 * column-anchored scan cannot mistake `letter:` inside a bullet for a
 * top-level key.
 *
 * Returns { doc, reason } where doc is 'resume' | 'letter' | null.
 * null means "do not guess": either nothing matched, or both did, and
 * the caller should ask rather than pick.
 */
function detectDoc(content) {
  const top = new Set();
  for (const line of String(content).split('\n')) {
    const m = /^([A-Za-z_][\w-]*)\s*:/.exec(line);
    if (m) top.add(m[1]);
  }

  const resumeKeys = ['sidebar', 'mainColumn'].filter(k => top.has(k));
  const letterKeys = ['letter'].filter(k => top.has(k));

  if (resumeKeys.length && letterKeys.length) {
    return {
      doc: null,
      reason: `it has both ${resumeKeys.join(' and ')} and letter — that is `
            + `not a shape either builder accepts`,
    };
  }
  if (resumeKeys.length) {
    return { doc: 'resume', reason: `it has ${resumeKeys.join(' and ')}` };
  }
  if (letterKeys.length) {
    return { doc: 'letter', reason: 'it has letter' };
  }
  return {
    doc: null,
    reason: 'no sidebar, mainColumn or letter at the top level'
          + (top.has('name') || top.has('contact')
              ? ' — this looks like a shared profile, which is merged into '
              + 'every build rather than opened as a document'
              : ''),
  };
}

/**
 * The document files in data/, each labeled with which document it
 * is, so the inspector can offer the resume card only resume files.
 *
 * Classified by reading the file (see detectDoc), not by its name.
 * The name is a poor signal — `acme-2026.yml` says nothing — and the
 * point of dividing the list is that picking from it cannot produce a
 * build that fails on the first line.
 *
 * `doc: null` means detectDoc would not commit: the file matches both
 * document shapes or neither. Those stay visible to both cards rather
 * than vanishing, because a file you deliberately put in data/ going
 * missing from the UI is worse than one that turns out not to build.
 *
 * Reading every file on each call is a few hundred KB of line-scanning
 * and has not been worth caching; if data/ ever grows large enough to
 * notice, key a cache on (mtimeMs, size).
 */
function listDataFiles() {
  try {
    return fs.readdirSync(DATA_DIR)
      .filter(isDocumentFile)
      .sort((a, b) => a.localeCompare(b))
      .map((name) => {
        let doc = null;
        try {
          doc = detectDoc(fs.readFileSync(path.join(DATA_DIR, name), 'utf-8')).doc;
        } catch { /* unreadable mid-write; treat as unclassified */ }
        return { name, doc };
      });
  } catch {
    return [];
  }
}

function freeName(base) {
  const ext = path.extname(base);
  const stem = base.slice(0, -ext.length) || base;
  for (let n = 2; n < 100; n += 1) {
    const candidate = `${stem}-${n}${ext}`;
    if (!fs.existsSync(path.resolve(DATA_DIR, candidate))) return candidate;
  }
  return `${stem}-${Date.now()}${ext}`;
}

/**
 * Describe one built PDF for the app's tray.
 *
 * `path` is included whether or not the file exists, and is the reason
 * this returns it at all: the outputs are named after you now, so the
 * app can no longer assemble 'dist/resume-color.pdf' from the document
 * id and the variant. It asks. The path is project-relative with
 * forward slashes, which is both what the tooltip should show and what
 * POST /api/reveal expects back.
 */
function pdfInfo(p) {
  const rel = path.relative(ROOT, p).replace(/\\/g, '/');
  try {
    const st = fs.statSync(p);
    return { path: rel, exists: true, bytes: st.size, mtimeMs: st.mtimeMs };
  } catch {
    return { path: rel, exists: false };
  }
}


/* ─── The app ─────────────────────────────────────────────────── */

async function start({ port = 0, host = '127.0.0.1' } = {}) {
  teeStdout();

  /*
   * The engine starts alongside the HTTP listener, not before it.
   *
   * Warm: Python, Chromium and Sass start together, so the first preview
   * does not wait for each of them in turn. But they still take a few
   * hundred milliseconds between them, and this used to be awaited
   * before the socket was even opened — which meant the desktop shell,
   * which waits for the READY line to create its window, could not so
   * much as start loading the UI until Chromium was up.
   *
   * So the listener comes up first and READY is printed as soon as the
   * port is known. The window, the page, its stylesheet and its fonts
   * then load while Chromium and Sass are still starting. Every request
   * that needs the engine waits for `engineReady` (see handle), so
   * nothing can reach a half-built engine; the only difference is that
   * the waiting now happens with the UI on screen instead of in front
   * of a blank window.
   */
  let engine = null;
  const engineReady = createEngine({ root: ROOT, warm: true })
    .then((e) => { engine = e; return e; });
  // A request or the awaits below report it; this only stops Node from
  // treating an early failure as an unhandled rejection.
  engineReady.catch(() => {});

  // Which YAML the next render reads. Mirrors RESUME_DATA_SOURCE:
  // null means build.py's own rule (local if present, else default).
  let dataSource = null;
  let busy = false;

  // A file the user has picked for a document, read in place.
  //
  // Nothing is copied and nothing in data/ is modified: the path is
  // passed to the build, which reads it directly. An earlier version of
  // this app implemented "load a file" by overwriting the user's
  // data/resume.yml with it, which is a different and much worse
  // operation than the one anyone asked for.
  const picked = {};

  /**
   * The data selection for one document's render or build — its own
   * card's choice and nothing else. See renderEnv.
   *
   * RESUME_DATA_SOURCE is the resume card's setting. build_letter.py
   * would honor it too, so the letter's renders clear it: with it
   * passed through, forcing the resume to its template built the
   * letter from letter_default.yml and the build then deleted the PDF
   * of your real letter as stale. The UI offers the modes only on the
   * resume card.
   */
  function envForRender(id) {
    return renderEnv(id, { dataSource, picked });
  }

  /**
   * Show a document by rasterizing what is already in dist/, instead of
   * re-running the pipeline to arrive at an identical file.
   *
   * WHY THIS EXISTS
   * ---------------
   * A Build shells out to the CLI, which measures, solves, renders,
   * prints and writes the document's PDF into dist/ under your own
   * name. The app then refreshed the
   * pane by calling renderPreview(), which did all of that a second
   * time, into a temp file, to reach the same pixels — roughly 600 ms
   * of a ~1.1 s round trip spent recomputing a result already on disk.
   *
   * So after a build the pane reads the built file. What it shows is
   * not an approximation of the deliverable, it *is* the deliverable:
   * the exact bytes the CLI just wrote, cropped and stamped. If
   * anything that is the more honest thing to put on screen.
   *
   * This is also the only way to show a document with no live pipeline
   * (see DOCS[].live), and the fallback when a render is unavailable.
   */
  async function renderFromBuilt(id, { scale, pages } = {}) {
    const doc = DOCS[id];
    const started = Date.now();
    const built = builtPaths(doc);

    const pdfPath = fs.existsSync(doc.pdf) ? doc.pdf : null;

    if (!pdfPath) {
      return {
        doc: id, mode: 'built', built: false,
        images: [], pages: 0, timings: {}, totalMs: 0,
        note: 'Not built yet — press Build to produce it.',
      };
    }

    const result = await engine.pipeline.python.raster({
      pdfPath, scale: scale || 2.0, pages: pages || null,
    });
    const readBuilt = Date.now() - started;

    // The metadata as the Build that wrote this PDF left it — not the
    // file on disk, which a later preview may have rewritten for other
    // data. Null for a PDF from before this server started whose
    // metadata has since been replaced.
    const meta = built.meta || null;

    return {
      doc: id,
      mode: 'built',
      built: true,
      source: path.relative(ROOT, pdfPath).replace(/\\/g, '/'),
      images: result.images,
      pages: result.pageCount,
      meta,
      dataSource: meta ? meta.data_source : null,
      // Not an assumption: resume.js fails the build when an invariant
      // is violated, so a PDF that exists is one that passed.
      invariants: { ok: true },
      timings: { readBuilt },
      totalMs: readBuilt,
      note: null,
    };
  }

  const routes = {
    'GET /api/status': async () => ({
      ...engine.status(),
      dataSource,
      busy,
      // Every .yml in data/, so the inspector can offer them directly
      // rather than making you open a dialog to find out what exists.
      dataFiles: listDataFiles(),
      documents: Object.fromEntries(Object.entries(DOCS).map(([id, d]) => [id, {
        label: d.label,
        live: d.live,
        pdf: pdfInfo(d.pdf),
        hasMyData: fs.existsSync(d.myData),
        hasDefaultData: fs.existsSync(d.defaultData),
        dataFile: dataFileInfo(d, dataSource, picked[id]),
        picked: picked[id] ? path.basename(picked[id]) : null,
      }])),
    }),

    'GET /api/datafiles': async () => {
      let names = [];
      try {
        names = fs.readdirSync(DATA_DIR).filter(isDocumentFile);
      } catch { /* no data dir yet */ }
      return {
        files: names.map((name) => {
          const st = fs.statSync(path.join(DATA_DIR, name));
          return {
            name,
            bytes: st.size,
            mtimeMs: st.mtimeMs,
            doc: detectDoc(fs.readFileSync(path.join(DATA_DIR, name), 'utf-8')).doc
                 || 'resume',
            // Which files leave this machine if the project is pushed.
            //
            // This used to read `name.includes('.local.')`, which was a
            // true statement about the old naming and is now false about
            // everything — the private files lost their suffix and the
            // shipped ones gained one. It has to agree with .gitignore,
            // which is an allowlist of exactly the *_default.yml files,
            // because the badge it feeds says "private" or "committed"
            // and a wrong answer there is the kind that ends with a
            // phone number on GitHub.
            isTemplate: /_default\.ya?ml$/i.test(name),
          };
        }).sort((a, b) => b.mtimeMs - a.mtimeMs),
      };
    },

    /**
     * Read one file out of data/ so the file dialog can load it.
     *
     * Name only, resolved against data/ and checked to still be inside
     * it — this exists to serve a picker listing that directory, not to
     * read arbitrary files off the machine.
     */
    'GET /api/datafile': async (_body, url) => {
      const name = url.searchParams.get('name') || '';
      const target = path.resolve(DATA_DIR, name);
      if (path.dirname(target) !== path.resolve(DATA_DIR) || !/\.ya?ml$/i.test(target)) {
        throw new Error('that is not a data file');
      }
      if (!fs.existsSync(target)) throw new Error(`no such file: ${name}`);
      return { name, content: fs.readFileSync(target, 'utf-8') };
    },

    'POST /api/preview': async (body) => {
      const id = DOCS[body.doc] ? body.doc : 'resume';
      const doc = DOCS[id];
      // from:'built' asks for the file in dist/ rather than a fresh
      // render — what the app wants immediately after a Build, and the
      // only thing it can show for a document with no live pipeline.
      //
      // On the engine's queue, like every other use of the worker: it
      // reads what a running build is still writing.
      if (!doc.live || body.from === 'built') {
        return await engine.exclusive(
          () => renderFromBuilt(id, { scale: body.scale, pages: body.pages }));
      }
      busy = true;
      broadcast('render', { state: 'start', doc: id });
      try {
        // `known`: page hashes the page already holds, so unchanged pages
        // come back without their PNG. See renderPreview in engine.js.
        const known = body.known && typeof body.known === 'object' && !Array.isArray(body.known)
          ? body.known : undefined;

        /*
         * Streaming: the pages as they are ready, on the event stream.
         *
         * A render used to reach the UI in one JSON body, so page 1 —
         * the one being looked at — waited for every page behind it to
         * be rendered and encoded. A UI that asks for streaming tells us
         * which pages are on screen and in what order (`order`), and
         * each page is broadcast the moment the worker has it.
         *
         * The reply is still one JSON body listing every page, so the
         * request/response shape callers rely on is unchanged; what is
         * different is that pages already sent carry `sent: true` and no
         * PNG, because the page has them. A UI that missed an event sees
         * a page with neither a PNG nor `unchanged` and asks again
         * without streaming. `renderId` is echoed back so a page can
         * drop events from a render its request has already superseded.
         */
        const streaming = body.stream === true;
        const renderId = typeof body.renderId === 'string'
          ? body.renderId.slice(0, 64) : null;
        const order = streaming && Array.isArray(body.order)
          ? body.order.filter(n => Number.isInteger(n) && n > 0).slice(0, 64)
          : null;
        const sent = new Set();

        const result = await retryIfHalfWritten(() => engine.renderPreview({
          doc: id, scale: body.scale, pages: body.pages, env: envForRender(id), known,
          order: streaming ? order : undefined,
          onPage: streaming ? (im) => {
            sent.add(im.page);
            broadcast('page', { doc: id, renderId, image: im });
          } : undefined,
        }), {
          changedAt: lastChangeAt,
          onRetry: () => console.log('  (the data file may have been caught mid-save; reading it again)'),
        });
        broadcast('render', { state: 'done', doc: id, ms: result.totalMs });
        if (!streaming) return { mode: 'live', ...result };
        const images = result.images.map((im) => {
          if (!im.png || !sent.has(im.page)) return im;
          const { png, ...rest } = im;
          return { ...rest, sent: true };
        });
        return { mode: 'live', ...result, images, renderId, streamed: true };
      } finally {
        busy = false;
      }
    },

    'POST /api/build': async (body) => {
      const id = DOCS[body.doc] ? body.doc : 'resume';
      const doc = DOCS[id];

      busy = true;
      broadcast('build', { state: 'start', doc: id });
      try {
        // The snapshot check is off unless the document's checkbox asks
        // for it, and even then a difference is reported rather than
        // fatal — the PDFs are written before it runs. Only the resume
        // has fixtures; letter.js has no snapshot phase at all.
        const buildEnv = {
          ...envForRender(id),
          [ENV_RESUME_SNAPSHOT]: body.snapshot ? 'on' : 'off',
          // Off unless asked. The suites test the pipeline, which has
          // not changed between two saves of your resume; the checks
          // that test your data run inside the build regardless.
          [ENV_RESUME_TESTS]: body.tests ? 'on' : 'off',
        };
        let result;
        try {
          result = await engine.build({
            script: doc.script,
            env: buildEnv,
            // Inside the build's own queue slot, so no preview can rewrite
            // the metadata between the build finishing and this reading it.
            after: async () => {
              rememberBuilt(doc);

              // Hand the built PDF back rasterized, in the same round trip.
              //
              // The build has just produced exactly what the preview pane
              // exists to show; rendering it again here would recompute a
              // file already on disk. See renderFromBuilt.
              //
              // Best effort, deliberately: a build that succeeded must not
              // be reported as failed because the pane could not be
              // refreshed.
              try {
                return await renderFromBuilt(id, { scale: body.scale });
              } catch (err) {
                console.log(`  (built, but could not rasterize it for the preview: ${err.message})`);
                return null;
              }
            },
          });
        } catch (err) {
          // A failed build may have written (or pruned) PDFs before it
          // failed. Keep what was remembered while it still exists;
          // otherwise look again.
          const known = builtOutputs[doc.variant];
          if (known && !fs.existsSync(known.pdf)) {
            delete builtOutputs[doc.variant];
          }
          throw err;
        }
        broadcast('build', { state: 'done', doc: id, ms: result.ms });
        const render = result.after || null;

        return {
          doc: id,
          ms: result.ms,
          pdf: pdfInfo(doc.pdf),
          render,
        };
      } catch (err) {
        broadcast('build', { state: 'failed', doc: id, message: err.message });
        // Carry the CLI's own diagnostics through, so the app can say
        // what went wrong rather than that something did.
        err.failures = err.failures || [];
        throw err;
      } finally {
        busy = false;
      }
    },

    'POST /api/datasource': async (body) => {
      if (![null, 'default', 'mine'].includes(body.source ?? null)) {
        throw new Error("source must be 'default', 'mine' or null");
      }
      dataSource = body.source ?? null;
      return { dataSource };
    },

    /**
     * Use a file in data/ for this document, without touching anything.
     *
     * The path is handed to the build through RESUME_DATA_FILE and read
     * in place. No copying, no overwriting, no backups needed — picking
     * a different file to look at should not be a write operation, and
     * making it one is how a preview feature ends up replacing the file
     * someone spent the afternoon editing.
     *
     * Passing null clears the pick and returns to the normal
     * local/default resolution.
     */
    'POST /api/pick': async (body) => {
      const id = DOCS[body.doc] ? body.doc : 'resume';
      if (!body.name) {
        delete picked[id];
        return { doc: id, picked: null };
      }
      const target = path.resolve(DATA_DIR, body.name);
      if (path.dirname(target) !== path.resolve(DATA_DIR) || !/\.ya?ml$/i.test(target)) {
        throw new Error('that is not a data file');
      }
      if (!isDocumentFile(path.basename(target))) {
        throw new Error(
          `${path.basename(target)} is a shared profile, not a document — ` +
          `it is merged into every build rather than built on its own`);
      }
      if (!fs.existsSync(target)) throw new Error(`no such file: ${body.name}`);
      // Refuse a file that is plainly the other document.
      //
      // The inspector no longer offers letter files under the resume
      // card, so this should be unreachable from the UI — but /api/pick
      // is also the endpoint the conflict dialog uses, and "the build
      // failed on line 1" is a much worse way to learn you picked the
      // wrong file than being told here.
      const asked = detectDoc(fs.readFileSync(target, 'utf-8')).doc;
      if (asked && asked !== id) {
        throw new Error(
          `${path.basename(target)} is a ${DOCS[asked].label.toLowerCase()}, `
          + `not a ${DOCS[id].label.toLowerCase()}`);
      }
      picked[id] = target;
      return { doc: id, picked: path.relative(ROOT, target).replace(/\\/g, '/') };
    },

    /**
     * Take a dropped file: read it if it is already here, otherwise save
     * it under its own name and read that.
     *
     * A webview hands over a dropped file's bytes but never its path, so
     * there is nowhere to read it from in place — the only way to keep
     * an external file is to write it into data/.
     *
     * Three cases, and the middle one is the one an earlier version got
     * wrong by refusing outright:
     *
     *   name free            → save it, read it
     *   name taken, same     → nothing to save; just read the file that
     *                          is already there. Dropping a file you
     *                          already have is not an error.
     *   name taken, differs  → do not guess. Report the conflict and let
     *                          the caller choose between reading what is
     *                          on disk and saving under a free name.
     *                          Replacing is never offered here.
     */
    'POST /api/adopt': async (body) => {
      if (typeof body.content !== 'string' || !body.content.trim()) {
        throw new Error('no YAML content received');
      }

      // Which document this is, decided by reading it.
      //
      // `body.doc` is only a fallback for the case detection refuses
      // to call — the file's own contents outrank whichever card you
      // happened to drop it on, because that is what the builders will
      // do with it anyway. See detectDoc.
      const detected = detectDoc(body.content);
      const id = detected.doc || (DOCS[body.doc] ? body.doc : null);
      if (!id) {
        const err = new Error(
          `could not tell whether that is a resume or a cover letter: `
          + `${detected.reason}`);
        err.ambiguous = true;
        err.reason = detected.reason;
        throw err;
      }

      const base = path.basename(body.filename || 'dropped.yml');
      if (!/\.ya?ml$/i.test(base)) throw new Error('only .yml or .yaml files');
      // The same rule /api/pick applies: an underscore name is a support
      // file (_profile.yml), merged into every build, never a document.
      if (!isDocumentFile(base)) {
        throw new Error(`${base} starts with an underscore, which marks a shared `
          + `profile rather than a document — rename it to open it here`);
      }

      const target = path.resolve(DATA_DIR, base);
      if (path.dirname(target) !== path.resolve(DATA_DIR)) {
        throw new Error('that filename is not allowed');
      }

      // Write first, exclusively; a taken name is the "already here" case.
      if (!writeNew(target, body.content)) {
        const onDisk = fs.readFileSync(target, 'utf-8');
        if (onDisk === body.content) {
          picked[id] = target;
          return { doc: id, detectedBy: detected.reason, saved: null,
                   existed: true, identical: true, picked: `data/${base}` };
        }
        // Same name, different contents. The caller decides.
        return {
          doc: id, detectedBy: detected.reason, conflict: true, name: base,
          suggestion: freeName(base),
          picked: null,
        };
      }

      picked[id] = target;
      return { doc: id, detectedBy: detected.reason, saved: `data/${base}`,
               existed: false, picked: `data/${base}` };
    },

    /**
     * Save a dropped file under a name the caller has chosen, which must
     * be free. Used to resolve the conflict above without ever replacing
     * what is already on disk.
     */
    'POST /api/adopt-as': async (body) => {
      const id = DOCS[body.doc] ? body.doc : 'resume';
      const base = path.basename(body.name || '');
      if (!/\.ya?ml$/i.test(base)) throw new Error('only .yml or .yaml files');
      if (!isDocumentFile(base)) {
        throw new Error(`${base} starts with an underscore, which marks a shared `
          + `profile rather than a document — choose another name`);
      }

      const target = path.resolve(DATA_DIR, base);
      if (path.dirname(target) !== path.resolve(DATA_DIR)) {
        throw new Error('that filename is not allowed');
      }
      if (typeof body.content !== 'string' || !body.content.trim()) {
        throw new Error('no YAML content received');
      }

      if (!writeNew(target, body.content)) {
        throw new Error(`data/${base} already exists too`);
      }
      picked[id] = target;
      return { doc: id, saved: `data/${base}`, picked: `data/${base}` };
    },

    /**
     * Show a built file in the OS file manager, selected.
     *
     * The UI has called this since the first version and the server
     * never implemented it — the endpoint was listed in the docblock
     * above and nowhere else. It went unnoticed because the only way
     * to reach it was clicking a file size that did not look like a
     * button; renaming that to "Show (217 KB)" made the dead route
     * visible immediately.
     *
     * CONTAINMENT
     * -----------
     * `path` arrives from the page, and this endpoint asks the
     * operating system to open something. Loopback binding and the
     * same-origin check are not enough on their own — a stray tab in
     * an ordinary browser is exactly the thing they are there to stop,
     * and "open any path on the machine" is too useful a primitive to
     * leave open behind one check. So the resolved path must sit
     * inside the project and must already exist.
     *
     * PLATFORMS
     * ---------
     * `explorer.exe /select,<path>` must arrive as a single token, and
     * Node quotes any argv entry containing a space — which turns it
     * into something explorer reads as a different argument, and it
     * opens Documents instead. The first version worked around that
     * with `shell: true`, which on Windows runs the command through
     * cmd.exe: correct, but it starts a whole extra process before
     * explorer is even asked, and process creation on Windows is not
     * cheap. `windowsVerbatimArguments` gets the same exact command
     * line without the shell in between, so the cmd.exe hop is gone.
     *
     * A double quote cannot appear in a Windows filename, so quoting
     * the path inside the token is safe here in a way it would not be
     * on a POSIX system — which is why macOS and Linux pass argv
     * entries plainly and need no such flag.
     *
     * explorer.exe exits 1 even when it succeeds, so nothing here
     * waits on or reads the exit code.
     *
     * WHAT THIS STILL DOES NOT FIX
     * ----------------------------
     * The window tends to open behind Studio rather than in front.
     * That is Windows' foreground-activation rule, not a timing bug:
     * a process may only raise a window if it owns the foreground or
     * handled the last input event. The click landed in the Tauri
     * window, but the reveal is performed several processes away — an
     * HTTP request into this server, which detaches a child — and by
     * then nothing in the chain holds that right, so Windows declines
     * and flashes the taskbar button instead. Fixing it properly means
     * performing the reveal in the process that was actually clicked,
     * i.e. a Tauri command in src-tauri calling
     * SHOpenFolderAndSelectItems, with this endpoint kept as the
     * fallback for `npm run ui` in an ordinary browser.
     */
    'POST /api/reveal': async (body) => {
      const raw = String(body.path || '').trim();
      if (!raw) throw new Error('no path given');

      const target = resolveInsideRoot(raw);
      if (!target) throw new Error('that path is outside the project');
      const root = path.resolve(ROOT);
      if (!fs.existsSync(target)) {
        throw new Error(`nothing at ${path.relative(root, target) || '.'}`);
      }
      const isDir = fs.statSync(target).isDirectory();

      let child;
      if (process.platform === 'win32') {
        child = spawn('explorer.exe',
                      [isDir ? `"${target}"` : `/select,"${target}"`],
                      { windowsVerbatimArguments: true,
                        detached: true, stdio: 'ignore' });
      } else if (process.platform === 'darwin') {
        child = spawn('open', isDir ? [target] : ['-R', target],
                      { detached: true, stdio: 'ignore' });
      } else {
        child = spawn('xdg-open', [isDir ? target : path.dirname(target)],
                      { detached: true, stdio: 'ignore' });
      }
      // Never let a missing file manager take the server down with it.
      child.on('error', (err) => {
        console.log(`  (could not open a file manager: ${err.message})`);
      });
      child.unref();

      return { revealed: path.relative(root, target).replace(/\\/g, '/') || '.' };
    },

    'POST /api/shutdown': async () => {
      setTimeout(() => { shutdown('request'); }, 50);
      return { bye: true };
    },
  };

  /**
   * Watch the inputs and tell the UI when they change.
   *
   * This is what makes the preview live. There is no editor in the app
   * — you edit the YAML in whatever editor you already use, and the
   * rendered PDF in the pane follows.
   *
   * TWO MECHANISMS, ON PURPOSE
   * --------------------------
   * fs.watch is the fast path: near-instant, cheap, and unreliable in
   * ways that vary by platform, filesystem and editor. Many editors
   * save atomically — write a temp file, then rename over the original
   * — and depending on the platform the event that surfaces may name
   * the temp file, may name nothing at all, or may not fire.
   *
   * So a poll runs alongside it. It stats the handful of files in
   * data/ and styles/ once a second and compares size and mtime. That
   * is a few dozen stat calls a second on maybe ten files, which costs
   * nothing measurable, and it fires regardless of how the editor
   * wrote the file.
   *
   * Both paths funnel into the same debounced broadcast, so a save seen
   * by both produces one render, not two. A live preview that silently
   * stops following your edits is worse than not having one, which is
   * why this is belt and braces rather than one clever watcher.
   */
  const WATCH_DIRS = [DATA_DIR, path.join(ROOT, 'styles')];
  const watchers = [];
  let watchTimer = null;
  let pollTimer = null;
  let lastSeen = new Map();
  // When the watcher last reported a change; see retryIfHalfWritten.
  let lastChangeAt = null;

  function inputFiles() {
    const found = new Map();
    for (const dir of WATCH_DIRS) {
      let names;
      try {
        names = fs.readdirSync(dir);
      } catch {
        continue;
      }
      for (const name of names) {
        if (!/\.(ya?ml|scss)$/i.test(name)) continue;
        const full = path.join(dir, name);
        try {
          const st = fs.statSync(full);
          found.set(full, `${st.mtimeMs}:${st.size}`);
        } catch { /* vanished between readdir and stat */ }
      }
    }
    return found;
  }

  // How long a save has to be quiet before it triggers a render — see
  // WATCH_DEBOUNCE_MS at the top of this file, and retryIfHalfWritten
  // for a save caught halfway. A change that still lands while a render
  // is running is not lost: the app queues one more render for it (see
  // preview() in ui/index.html).
  function noteChange(file) {
    clearTimeout(watchTimer);
    watchTimer = setTimeout(() => {
      lastSeen = inputFiles();   // resync so the poll doesn't re-fire
      lastChangeAt = Date.now();
      broadcast('changed', { file: file || null });
    }, WATCH_DEBOUNCE_MS);
  }

  function watchInputs() {
    for (const dir of WATCH_DIRS) {
      if (!fs.existsSync(dir)) continue;
      try {
        const w = fs.watch(dir, { persistent: false }, (_event, filename) => {
          // No extension filter. An atomic save can surface under the
          // temp file's name, and these directories hold nothing but
          // inputs anyway — a spurious render costs half a second.
          noteChange(filename ? path.join(dir, filename) : null);
        });
        watchers.push(w);
      } catch (err) {
        // fs.watch is best-effort across platforms and filesystems.
        // The poll below covers its absence, so this is informational.
        console.log(`  (fs.watch unavailable for ${path.relative(ROOT, dir)}: ${err.message}; polling instead)`);
      }
    }

    lastSeen = inputFiles();
    pollTimer = setInterval(() => {
      const now = inputFiles();
      if (now.size !== lastSeen.size) {
        lastSeen = now;
        noteChange(null);
        return;
      }
      for (const [file, stamp] of now) {
        if (lastSeen.get(file) !== stamp) {
          lastSeen = now;
          noteChange(file);
          return;
        }
      }
    }, 1000);
    pollTimer.unref();
  }
  watchInputs();

  // Set once listening; every request is checked against it.
  let listeningPort = null;

  async function handle(req, res) {
    // Same server, same page only — see checkRequest. The Host check
    // comes first and applies to every request, the UI page included,
    // so a rebinding page cannot even read it.
    const refused = checkRequest(req, listeningPort,
      host === '127.0.0.1' || host === 'localhost' ? [] : [host]);
    if (refused) return json(res, refused[0], { error: refused[1] });

    // A fixed base: the Host header is client input and parsing it as a
    // URL throws on a malformed one. Only the path and query are used.
    const url = new URL(req.url, 'http://127.0.0.1');
    const key = `${req.method} ${url.pathname}`;

    if (key === 'GET /api/events') {
      res.writeHead(200, {
        'content-type': 'text/event-stream',
        'cache-control': 'no-cache',
        connection: 'keep-alive',
      });
      res.write(': connected\n\n');
      clients.add(res);
      req.on('close', () => clients.delete(res));
      return;
    }

    if (url.pathname === '/' || url.pathname === '/index.html') {
      const file = path.join(UI_DIR, 'index.html');
      if (!fs.existsSync(file)) {
        return json(res, 500, { error: `UI missing at ${path.relative(ROOT, file)}` });
      }
      const html = fs.readFileSync(file);
      // The header says whether the engine was up when the page was
      // served. Nothing in the app reads it; it is how a test can show
      // that the page does not wait for the engine without timing two
      // requests against each other and hoping the machine cooperates.
      res.writeHead(200, {
        'content-type': 'text/html; charset=utf-8',
        'cache-control': 'no-store',
        'x-studio-engine': engine ? 'ready' : 'starting',
      });
      return res.end(html);
    }

    const handler = routes[key];
    if (!handler) return json(res, 404, { error: `no route for ${key}` });

    try {
      // Everything below this line reads or drives the engine, and the
      // engine may still be starting. Waiting here rather than before
      // the listener is what lets the UI load meanwhile; a failure to
      // start is reported as this request's error.
      if (!engine) await engineReady;
      const body = req.method === 'POST' ? await readBody(req) : {};
      const result = await handler(body, url);
      json(res, 200, result);
    } catch (err) {
      json(res, 500, {
        error: err.message,
        kind: err.kind || 'failed',
        failures: err.failures || undefined,
        ambiguous: err.ambiguous || undefined,
        reason: err.reason || undefined,
      });
    }
  }

  const server = http.createServer((req, res) => {
    // Whatever goes wrong with one request stays with that request. An
    // exception escaping here would otherwise take the server — and the
    // desktop app's whole backend — down with it.
    Promise.resolve().then(() => handle(req, res)).catch((err) => {
      try {
        if (!res.headersSent) json(res, 500, { error: err && err.message ? err.message : 'internal error' });
        else res.end();
      } catch { /* the socket is already gone */ }
    });
  });
  server.on('clientError', (err, socket) => {
    try { socket.end('HTTP/1.1 400 Bad Request\r\n\r\n'); } catch { /* gone */ }
  });

  /**
   * Stop everything, once.
   *
   * The engine owns a Chromium instance and a Python worker, neither of
   * which reliably dies just because this process does. Every exit path
   * — the shutdown endpoint, the parent pipe closing, Ctrl-C, SIGTERM —
   * goes through here, because an earlier version called process.exit()
   * directly on one of those paths and left a Python worker running
   * with nothing attached to it.
   *
   * The timeout is the backstop: if disposal hangs (a wedged browser,
   * a worker mid-render), exiting late is better than not exiting.
   */
  let shuttingDown = false;
  async function shutdown(reason) {
    if (shuttingDown) return;
    shuttingDown = true;
    clearInterval(pollTimer);
    clearTimeout(watchTimer);
    for (const w of watchers) { try { w.close(); } catch { /* already closed */ } }
    for (const res of clients) { try { res.end(); } catch { /* already gone */ } }
    try { server.close(); } catch { /* not listening */ }
    const forced = setTimeout(() => process.exit(0), 5000);
    forced.unref();
    // A shutdown during startup: wait for the engine it is disposing of,
    // so a Chromium or a worker that is still being born is not orphaned.
    try { await engineReady; } catch { /* never started; nothing to dispose */ }
    try { if (engine) await engine.dispose(); } catch { /* best effort */ }
    clearTimeout(forced);
    process.exit(0);
  }

  await new Promise((resolve) => server.listen(port, host, resolve));
  const actualPort = server.address().port;
  listeningPort = actualPort;
  const url = `http://${host}:${actualPort}/`;

  // Framed so the Tauri shell can find it without parsing log text.
  // Printed before the engine is up on purpose — see engineReady above.
  process.stdout.write(`${READY_PREFIX}${JSON.stringify({ url, port: actualPort })}\n`);

  // The engine is still the thing this server exists to drive: if it
  // cannot start, this is a failed start, exactly as it was when the
  // listener came up second.
  try {
    await engineReady;
  } catch (err) {
    try { server.close(); } catch { /* not listening */ }
    throw err;
  }

  return { server, engine, url, port: actualPort, shutdown };
}


if (require.main === module) {
  const argv = process.argv.slice(2);
  const portArg = argv.indexOf('--port');
  const port = portArg !== -1 ? Number(argv[portArg + 1]) : Number(process.env.STUDIO_PORT || 0);

  start({ port, }).then(({ url, shutdown }) => {
    /**
     * --exit-with-parent: stop when stdin closes.
     *
     * The desktop shell holds this process's stdin open as a pipe. If
     * the shell is killed in a way that skips its own cleanup (a force
     * quit, a crash, a terminated session), the pipe closes and this
     * fires. Without it, a warm Chromium and a Python worker are left
     * running with no window attached to them.
     *
     * Wired here rather than before start() so it can call the real
     * shutdown routine — an earlier version exited the process directly
     * and leaked the worker every time.
     */
    if (argv.includes('--exit-with-parent')) {
      process.stdin.resume();
      process.stdin.on('end', () => shutdown('parent-exit'));
      process.stdin.on('close', () => shutdown('parent-exit'));
    }
    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('SIGTERM', () => shutdown('SIGTERM'));

    console.log(`Resume Studio → ${url}`);
    // Where the compiled-code cache lives, said once: it is outside the
    // project by design and a user who wants to clear it should not have
    // to read the source to find it.
    const cacheLine = compileCache.describe();
    if (cacheLine) console.log(`  ${cacheLine}`);
    if (argv.includes('--open')) {
      const opener = process.platform === 'win32' ? 'explorer'
        : process.platform === 'darwin' ? 'open' : 'xdg-open';
      try {
        require('child_process').spawn(opener, [url], { detached: true, stdio: 'ignore' }).unref();
      } catch { /* the URL is printed above either way */ }
    }
  }).catch((err) => {
    console.error(`Studio failed to start: ${err.message}`);
    process.exitCode = 1;
  });
}

module.exports = { start, DOCS, READY_PREFIX, detectDoc, isDocumentFile,
                   listDataFiles, resolveInsideRoot, dataFileInfo, renderEnv,
                   checkRequest, writeNew, looksHalfWritten, retryIfHalfWritten,
                   WATCH_DEBOUNCE_MS, HALF_WRITTEN_WINDOW_MS, HALF_WRITTEN_RETRY_MS };
