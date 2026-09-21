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
 * prints the chosen URL as a framed line the Rust side reads.
 *
 * ENDPOINTS
 *   GET  /                 the UI
 *   GET  /api/status       engine + data-source state
 *   GET  /api/datafiles    what's in data/
 *   GET  /api/datafile     ?name= -> one file's contents
 *   GET  /api/events       SSE: log lines and render/build state
 *   POST /api/preview      {doc, scale, pages, from} -> rasterized PDF
 *                          pages; from:'built' reads dist/ instead of
 *                          re-rendering
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

const http = require('http');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const { createEngine } = require('./engine');
const { outputPaths } = require('./_output_name');
const {
  ENV_RESUME_VARIANTS,
  ENV_RESUME_SNAPSHOT,
  ENV_RESUME_TESTS,
  ENV_RESUME_DATA_FILE,
  ENV_LETTER_DATA_FILE,
} = require('./_env_contract');

const ROOT = path.join(__dirname, '..');
const UI_DIR = path.join(ROOT, 'ui');
const DATA_DIR = path.join(ROOT, 'data');
const DIST = path.join(ROOT, 'dist');

// Printed on stdout when listening, for the Tauri shell to read.
const READY_PREFIX = '\x1eSTUDIO_READY ';

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
 * `pdf` and `grayscalePdf` are read, not stored.
 *
 * The built PDFs are named after you — Gaius_Iulius_Resume.pdf — so
 * their paths depend on data this server never parses. build.py writes
 * the stem into the document's metadata JSON, and _output_name.js
 * reads it back; defining them as getters means every existing
 * `doc.pdf` call site below picks up the current name without knowing
 * any of that, including across a rebuild that changed it.
 *
 * Deliberately non-enumerable so JSON.stringify(DOCS) stays a
 * description of configuration rather than a filesystem snapshot.
 */
for (const doc of Object.values(DOCS)) {
  Object.defineProperties(doc, {
    pdf: {
      get() { return outputPaths(DIST, this.meta, this.variant).colorPdf; },
    },
    grayscalePdf: {
      get() { return outputPaths(DIST, this.meta, this.variant).grayscalePdf; },
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
 *   'default' → the shipped template, and it is an error if absent
 *   'mine'    → your own file, and it is an error if absent
 *   unset     → yours if present, else the template
 *
 * RESUME_DATA_SOURCE only governs the resume; build_letter.py has
 * its own yours-if-present rule and no override, so `source` is ignored
 * for the letter.
 */
function dataFileInfo(doc, source, pickedPath) {
  const effective = doc.script === 'letter.js' ? null : source;

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

  // Warm: Python, Chromium and Sass start together now, so the first
  // preview does not wait for each of them in turn.
  const engine = await createEngine({ root: ROOT, warm: true });

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

  function dataFileEnv(id) {
    if (!picked[id]) return {};
    return id === 'letter'
      ? { [ENV_LETTER_DATA_FILE]: picked[id] }
      : { [ENV_RESUME_DATA_FILE]: picked[id] };
  }

  /**
   * RESUME_DATA_SOURCE, when the user has forced one.
   *
   * Only the resume's loader honors it — build_letter.py has its
   * own local-if-present rule and no override — so passing it on a
   * letter render is harmless but meaningless. The UI hides the control
   * for the letter rather than offering a switch that does nothing.
   */
  function envForRender(id) {
    return {
      ...(dataSource ? { RESUME_DATA_SOURCE: dataSource } : {}),
      ...dataFileEnv(id),
    };
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
   *
   * Color first; grayscale only when that is the sole variant built —
   * a grayscale-only build should show grayscale, and say why.
   */
  async function renderFromBuilt(id, { scale, pages } = {}) {
    const doc = DOCS[id];
    const started = Date.now();

    const color = fs.existsSync(doc.pdf);
    const pdfPath = color
      ? doc.pdf
      : (fs.existsSync(doc.grayscalePdf) ? doc.grayscalePdf : null);

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

    // build.py writes this next to the PDF, so it describes the file on
    // screen rather than whatever was rendered most recently.
    let meta = null;
    try {
      meta = JSON.parse(fs.readFileSync(engine.pipelines[id].paths.pdfMeta, 'utf-8'));
    } catch { /* an older build, or a variant that writes none */ }

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
      note: color
        ? null
        : 'Showing the grayscale variant — it is the only one this build produced.',
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
        color: pdfInfo(d.pdf),
        grayscale: pdfInfo(d.grayscalePdf),
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
      if (!doc.live || body.from === 'built') {
        return await renderFromBuilt(id, { scale: body.scale, pages: body.pages });
      }
      busy = true;
      broadcast('render', { state: 'start', doc: id });
      try {
        const result = await engine.renderPreview({
          doc: id, scale: body.scale, pages: body.pages, env: envForRender(id),
        });
        broadcast('render', { state: 'done', doc: id, ms: result.totalMs });
        return { mode: 'live', ...result };
      } finally {
        busy = false;
      }
    },

    'POST /api/build': async (body) => {
      const id = DOCS[body.doc] ? body.doc : 'resume';
      const doc = DOCS[id];

      // Which variants to produce. The CLI defaults to both when the
      // variable is unset; Studio always states its choice explicitly so
      // what the checkboxes say is what the build does.
      const wanted = [];
      if (body.variants?.color !== false) wanted.push('color');
      if (body.variants?.grayscale) wanted.push('grayscale');
      if (!wanted.length) throw new Error('select at least one variant');

      busy = true;
      broadcast('build', { state: 'start', doc: id });
      try {
        // The snapshot check is off unless the document's checkbox asks
        // for it, and even then a difference is reported rather than
        // fatal — the PDFs are written before it runs. Only the resume
        // has fixtures; letter.js has no snapshot phase at all.
        const buildEnv = {
          ...envForRender(id),
          [ENV_RESUME_VARIANTS]: wanted.join(','),
          [ENV_RESUME_SNAPSHOT]: body.snapshot ? 'on' : 'off',
          // Off unless asked. The suites test the pipeline, which has
          // not changed between two saves of your resume; the checks
          // that test your data run inside the build regardless.
          [ENV_RESUME_TESTS]: body.tests ? 'on' : 'off',
        };
        const result = await engine.build({ script: doc.script, env: buildEnv });
        broadcast('build', { state: 'done', doc: id, ms: result.ms });

        // Hand the built PDF back rasterized, in the same round trip.
        //
        // The build has just produced exactly what the preview pane
        // exists to show; rendering it again here would recompute a
        // file already on disk. See renderFromBuilt.
        //
        // Best effort, deliberately: a build that succeeded must not be
        // reported as failed because the pane could not be refreshed.
        let render = null;
        try {
          render = await renderFromBuilt(id, { scale: body.scale });
        } catch (err) {
          console.log(`  (built, but could not rasterize it for the preview: ${err.message})`);
        }

        return {
          doc: id,
          ms: result.ms,
          color: pdfInfo(doc.pdf),
          grayscale: pdfInfo(doc.grayscalePdf),
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

      const target = path.resolve(DATA_DIR, base);
      if (path.dirname(target) !== path.resolve(DATA_DIR)) {
        throw new Error('that filename is not allowed');
      }

      if (fs.existsSync(target)) {
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

      fs.mkdirSync(DATA_DIR, { recursive: true });
      fs.writeFileSync(target, body.content, 'utf-8');
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

      const target = path.resolve(DATA_DIR, base);
      if (path.dirname(target) !== path.resolve(DATA_DIR)) {
        throw new Error('that filename is not allowed');
      }
      if (fs.existsSync(target)) throw new Error(`data/${base} already exists too`);
      if (typeof body.content !== 'string' || !body.content.trim()) {
        throw new Error('no YAML content received');
      }

      fs.mkdirSync(DATA_DIR, { recursive: true });
      fs.writeFileSync(target, body.content, 'utf-8');
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

  function noteChange(file) {
    clearTimeout(watchTimer);
    watchTimer = setTimeout(() => {
      lastSeen = inputFiles();   // resync so the poll doesn't re-fire
      broadcast('changed', { file: file || null });
    }, 250);
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

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    const key = `${req.method} ${url.pathname}`;

    // Same-origin only. The server is bound to loopback, but a page in
    // the user's ordinary browser could still POST here; requiring a
    // JSON content-type and rejecting cross-origin requests keeps a
    // stray tab from driving builds.
    if (req.method === 'POST') {
      const origin = req.headers.origin;
      if (origin && !origin.startsWith(`http://${host}:`) && !origin.startsWith('http://localhost:')) {
        return json(res, 403, { error: 'cross-origin requests are not accepted' });
      }
    }

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
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
      return res.end(html);
    }

    const handler = routes[key];
    if (!handler) return json(res, 404, { error: `no route for ${key}` });

    try {
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
    try { await engine.dispose(); } catch { /* best effort */ }
    clearTimeout(forced);
    process.exit(0);
  }

  await new Promise((resolve) => server.listen(port, host, resolve));
  const actualPort = server.address().port;
  const url = `http://${host}:${actualPort}/`;

  // Framed so the Tauri shell can find it without parsing log text.
  process.stdout.write(`${READY_PREFIX}${JSON.stringify({ url, port: actualPort })}\n`);

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
                   listDataFiles, resolveInsideRoot };
