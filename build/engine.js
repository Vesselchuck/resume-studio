/**
 * engine.js — The warm build engine behind the desktop app.
 *
 * WHAT IT IS
 * ----------
 * A long-lived process that holds open the three things a cold build
 * pays for on every run, and then re-runs the real pipeline against
 * them as fast as you can type:
 *
 *   • one Chromium instance and one page (Playwright)
 *   • one build/worker.py process (Python, imports done once)
 *   • the compiled dist/styles.css, recompiled only when a .scss changes
 *
 * A cold `node resume.js` spends most of its wall-clock on startup:
 * launching Chromium and starting four separate Python interpreters.
 * Measured on the reference machine, the Python half alone drops from
 * ~468 ms to ~179 ms per cycle once the interpreter stops restarting.
 * That is the whole reason this file exists — not a faster pipeline, a
 * pipeline that has stopped paying to be born.
 *
 * WHAT IT IS NOT
 * --------------
 * It is not a second implementation of the build. Every phase comes
 * from build/pipeline.js, the same module resume.js drives. The only
 * difference between the CLI and this engine is what they hold open
 * and how they reach Python.
 *
 * It also does not write the PDFs in dist/. Live previews render one
 * color PDF to a temp file and rasterize it. A real Build shells out
 * to `node resume.js`, so the deliverables are only ever produced by
 * the path that also runs the unit tests and the snapshot diff. That
 * keeps exactly one blessed way to produce a PDF you would send to
 * someone.
 *
 * THE PREVIEW IS THE PDF
 * ----------------------
 * renderPreview() does not screenshot the page. It prints a real PDF,
 * crops it through crop_pdf.py, and rasterizes the cropped file with
 * the same function the visual-regression test uses. What the app
 * displays is the output, post-crop, at true 612 × 792 pt — not an
 * approximation of it.
 *
 * ORDERING
 * --------
 * Every preview runs the full measure → solve → final sequence. It is
 * tempting to skip re-solving when "only a little" changed; don't.
 * dist/placement.json is solved for one specific set of ids, and
 * rendering final mode against a stale one is the one way this design
 * can produce a wrong document. See op_build in build/worker.py.
 *
 * PROTOCOL (when run as a process)
 * --------------------------------
 * Same shape as build/worker.py: newline-delimited JSON in, frames out,
 * each frame prefixed with ASCII RS (0x1e) so that _console output
 * interleaved on the same stdout is log noise rather than a parse
 * error.
 *
 *   {"id":1,"op":"preview","scale":2}
 *   {"id":2,"op":"build"}           — shells out to the CLI
 *   {"id":3,"op":"status"}
 *   {"id":4,"op":"shutdown"}
 *
 * Usage as a module:
 *
 *   const { createEngine } = require('./build/engine');
 *   const engine = await createEngine({ root: __dirname });
 *   const shot = await engine.renderPreview({ scale: 2 });
 *   await engine.dispose();
 */

const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawn } = require('child_process');

const { createPipeline, disposeSass, warmUpSass } = require('./pipeline');
const { detectPython } = require('./detect_python');
const c = require('./_console');

const FRAME_PREFIX = '\x1e';
const WORKER = path.join(__dirname, 'worker.py');


/* ─── The warm Python adapter ─────────────────────────────────── */

/**
 * Wraps one build/worker.py process in the two-method adapter shape
 * build/pipeline.js expects.
 *
 * The CLI's adapter spawns a fresh interpreter per call; this one
 * writes a JSON line and waits for the matching frame. The pipeline
 * cannot tell them apart, and tests/test_worker_equivalence.py asserts
 * they produce identical bytes.
 */
class PythonWorker {
  constructor({ root, python }) {
    this.root = root;
    this.python = python;
    this.seq = 0;
    this.pending = new Map();
    this.buffer = '';
    this.ready = null;
    // Per-render env overrides (RESUME_DATA_SOURCE). Set by
    // renderPreview around a call and cleared after, so a data-source
    // switch never leaks into the next render.
    this.buildEnv = null;
    this.proc = null;
    this.lastLog = [];
  }

  start() {
    this.proc = spawn(this.python, ['-B', WORKER], {
      cwd: this.root,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    });
    this.proc.stdout.setEncoding('utf-8');
    this.proc.stderr.setEncoding('utf-8');

    this.ready = new Promise((resolve, reject) => {
      this._resolveReady = resolve;
      this.proc.once('error', reject);
      this.proc.once('exit', (code) => {
        const err = new Error(`build/worker.py exited (code ${code})`);
        for (const { reject: rj } of this.pending.values()) rj(err);
        this.pending.clear();
        if (this._resolveReady) reject(err);
      });
    });

    this.proc.stdout.on('data', (chunk) => this._consume(chunk));
    // The worker keeps its own stderr clean of protocol data; anything
    // arriving here is a hard crash (a traceback that escaped the
    // per-request handler), so surface it rather than swallowing it.
    this.proc.stderr.on('data', (chunk) => {
      String(chunk).split('\n').filter(Boolean).forEach(line => c.detail(`[worker] ${line}`));
    });

    return this.ready;
  }

  _consume(chunk) {
    this.buffer += chunk;
    let idx;
    while ((idx = this.buffer.indexOf('\n')) !== -1) {
      const line = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 1);
      if (!line.startsWith(FRAME_PREFIX)) {
        if (line.trim()) c.detail(`[worker] ${line}`);
        continue;
      }
      let frame;
      try {
        frame = JSON.parse(line.slice(1));
      } catch {
        continue;
      }
      if (frame.op === 'hello') {
        const r = this._resolveReady;
        this._resolveReady = null;
        if (r) r(frame.result);
        continue;
      }
      const waiter = this.pending.get(frame.id);
      if (!waiter) continue;
      this.pending.delete(frame.id);
      waiter.resolve(frame);
    }
  }

  call(req) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.proc.stdin.write(JSON.stringify({ ...req, id }) + '\n');
    });
  }

  /**
   * Run an op and turn a failure frame into a thrown Error carrying the
   * worker's own explanation, already printed through _console so the
   * app's log shows what the CLI would have shown.
   */
  async run(req) {
    const frame = await this.call(req);
    this.lastLog = frame.log || [];
    (frame.log || []).forEach(line => process.stdout.write(line + '\n'));
    if (!frame.ok) {
      c.err(frame.error.message);
      (frame.error.detail || []).forEach(line => c.detail(line));
      const err = new Error(frame.error.message);
      err.alreadyReported = true;
      err.kind = frame.error.kind;
      throw err;
    }
    return frame;
  }

  // --- the adapter surface build/pipeline.js consumes ---

  async buildHtml(mode) {
    await this.run({ op: 'build', mode, env: this.buildEnv || undefined });
  }

  async buildLetter() {
    await this.run({ op: 'build_letter' });
  }

  async cropPdf({ input, output, meta, quiet }) {
    const frame = await this.run({ op: 'crop', input, output, meta: meta || null });
    if (!quiet) {
      const r = frame.result;
      c.ok_pair('Cropped',
        `${r.pages} ${r.pages === 1 ? 'page' : 'pages'}, ` +
        `${(r.widthPt / 72).toFixed(1)} × ${(r.heightPt / 72).toFixed(0)} in (US Letter)`);
    }
    return frame.result;
  }

  async compare({ a, b, scale }) {
    const frame = await this.run({ op: 'compare', a, b, scale });
    return frame.result;
  }

  async raster({ pdfPath, scale, pages }) {
    const frame = await this.run({ op: 'raster', path: pdfPath, scale, pages: pages || null });
    return frame.result;
  }

  async stop() {
    if (!this.proc || this.proc.exitCode !== null) return;
    try {
      await this.call({ op: 'shutdown' });
    } catch {
      // Already gone.
    }
    this.proc.kill();
  }
}


/* ─── The engine ──────────────────────────────────────────────── */

/**
 * @param {object}  opts
 * @param {string}  opts.root   — project root
 * @param {string}  opts.python — interpreter; detected when omitted
 * @param {boolean} opts.warm   — start everything a first preview needs
 *   right away and in parallel, instead of on first use: the Python
 *   worker, Chromium and the Sass compiler all boot at once. Studio sets
 *   it; a caller that only wants build() should not.
 */
async function createEngine({ root, python, warm = false } = {}) {
  root = root || path.join(__dirname, '..');
  const interpreter = python || process.env.PYTHON || detectPython();

  const worker = new PythonWorker({ root, python: interpreter });
  const workerReady = worker.start();

  // 'fonts' instead of the CLI's 'networkidle': the fonts are vendored,
  // so waiting 500 ms for network silence on a file:// document with no
  // remote assets is idling. See openDocument in build/pipeline.js.
  //
  // One pipeline per document. They share the worker and the browser —
  // the only thing that differs is which paths and which build phase
  // apply, which is exactly what `variant` selects.
  const pipelines = {
    resume: createPipeline({ root, python: worker, navWait: 'fonts', variant: 'resume', warmSass: true }),
    letter: createPipeline({ root, python: worker, navWait: 'fonts', variant: 'letter', warmSass: true }),
  };
  const pipeline = pipelines.resume;

  // Lazy so that a caller who only ever wants `build()` (which shells
  // out to the CLI) never pays for a browser it will not use — unless
  // `warm` asks for it up front.
  let browser = null;
  let page = null;
  let pagePromise = null;

  // One launch, however many callers ask at once: a warm start may still
  // be launching Chromium when the first preview arrives, and that
  // preview must wait for the same launch rather than start a second.
  function browserPage() {
    if (!pagePromise) {
      pagePromise = launchBrowser().catch((err) => {
        pagePromise = null;
        throw err;
      });
    }
    return pagePromise;
  }

  async function launchBrowser() {
    const { chromium } = require('playwright');
    try {
      browser = await chromium.launch();
    } catch (err) {
      c.err('Chromium launch failed');
      err.message.split('\n').forEach(line => c.detail(line));
      c.detail('');
      c.detail('Is Chromium installed? Run:  npx playwright install chromium');
      err.alreadyReported = true;
      throw err;
    }
    const ctx = await browser.newContext();
    page = await ctx.newPage();
    return page;
  }

  // Warm start. The three are independent processes, so booting them
  // side by side costs about as long as the slowest of them rather than
  // their sum. Chromium's launch runs in the background; a failure is
  // left for the first preview to report, where it has somewhere to go.
  if (warm) {
    browserPage().catch(() => { /* reported again by the first preview */ });
  }

  const hello = await workerReady;

  // Sass last: its API is synchronous and holds this thread while it
  // starts, so it waits until Python is up and Chromium's launch is
  // under way — both keep starting in their own processes meanwhile.
  // It compiles the stylesheet too, when it is stale, so the first
  // preview does not have to.
  if (warm) {
    try {
      warmUpSass();
      if (pipeline.stylesAreStale()) pipeline.compileSass();
    } catch {
      // Already reported by compileSass; the first preview will try again.
    }
  }

  // One page, one placement file: overlapping renders would interleave
  // on both. Serialize every operation through this chain so a burst of
  // keystrokes queues instead of corrupting a render in flight.
  let chain = Promise.resolve();
  function serial(fn) {
    const run = chain.then(fn, fn);
    chain = run.then(() => {}, () => {});
    return run;
  }

  /**
   * Render the document and hand back rasterized pages of the real PDF.
   *
   * @param {object}  opts
   * @param {string}  opts.doc        — 'resume' (default) or 'letter'
   * @param {number}  opts.scale      — raster scale; 2.0 ≈ 144 dpi
   * @param {?number[]} opts.pages    — 1-based page numbers, or null for all
   * @param {boolean} opts.recompileStyles — force a Sass rebuild
   * @param {?object} opts.env — env overrides for the Python build,
   *   e.g. { RESUME_DATA_SOURCE: 'default' }
   */
  function renderPreview(opts = {}) {
    return serial(async () => {
      const doc = opts.doc === 'letter' ? 'letter' : 'resume';
      const pl = pipelines[doc];
      const started = Date.now();
      const timings = {};
      const mark = (name, t0) => { timings[name] = Date.now() - t0; };

      worker.buildEnv = opts.env && Object.keys(opts.env).length ? opts.env : null;

      let t = Date.now();
      if (opts.recompileStyles || pl.stylesAreStale()) {
        pl.compileSass();
        timings.sass = Date.now() - t;
      } else {
        timings.sass = 0;
      }

      let placement = null;
      let invariants = { ok: true };

      if (doc === 'letter') {
        // One flowing column: no measurement pass, no solver, no
        // invariant check. Skipping them here is not an optimization,
        // it is what the document is — see letter.js.
        t = Date.now();
        await pl.buildLetter();
        mark('letterHtml', t);
      } else {
        t = Date.now();
        await pl.buildMeasurement();
        mark('measurementHtml', t);
      }

      // Timed separately because the first render of a session pays a
      // cold Chromium launch here and every later one pays nothing.
      // Folding it into another bucket would make the first render look
      // like a mysteriously slow measurement step.
      t = Date.now();
      const hadBrowser = Boolean(page);
      const pg = await browserPage();
      timings.browserLaunch = hadBrowser ? 0 : Date.now() - t;

      t = Date.now();
      await pl.openDocument(pg);
      if (doc === 'resume') {
        const measurements = await pl.getMeasurements(pg);
        mark('measure', t);

        t = Date.now();
        placement = pl.solveAndWritePlacement(measurements);
        mark('solve', t);

        t = Date.now();
        await pl.buildFinal();
        mark('finalHtml', t);

        // Invariants are a real check, not a formality — they catch
        // content overflowing its page. In a live preview a violation is
        // information rather than a reason to show nothing, so report it
        // and still return the render. A Build (which goes through the
        // CLI) treats the same violation as fatal.
        t = Date.now();
        try {
          await pl.verifyInvariants(pg, placement.pages.length);
        } catch (err) {
          invariants = { ok: false, message: err.message };
        }
        mark('invariants', t);
      } else {
        mark('load', t);
      }

      const tmpPdf = path.join(os.tmpdir(), `studio-preview-${doc}-${process.pid}.pdf`);
      t = Date.now();
      await pl.printPdfs(pg, { color: tmpPdf, grayscale: null, quiet: true });
      mark('print', t);

      t = Date.now();
      const raster = await worker.raster({
        pdfPath: tmpPdf, scale: opts.scale || 2.0, pages: opts.pages,
      });
      mark('raster', t);

      // Best-effort. On Windows a rasterizer that still holds the file
      // open makes this throw EBUSY, and losing a preview because a
      // scratch file outlived it would be absurd. build/snapshot_pdf.py
      // now closes its PdfDocument, which removes the usual cause; this
      // stays as the belt to that suspenders.
      try {
        fs.rmSync(tmpPdf, { force: true });
      } catch (err) {
        c.detail(`(could not remove the preview scratch file: ${err.code || err.message})`);
      }

      const meta = JSON.parse(fs.readFileSync(pl.paths.pdfMeta, 'utf-8'));
      worker.buildEnv = null;

      return {
        doc,
        pages: placement ? placement.pages.length : raster.pageCount,
        dataSource: meta.data_source,
        meta,
        invariants,
        images: raster.images,
        timings,
        totalMs: Date.now() - started,
      };
    });
  }

  /**
   * Produce the real deliverables by running the CLI.
   *
   * Deliberately a subprocess rather than an in-process call. `npm run
   * resume` runs the unit tests first and the snapshot diff last, and
   * it is the only path that writes the PDFs in dist/. Routing the
   * app's Build button through it means there is exactly one way to
   * produce a PDF worth sending, and the app cannot quietly grow a
   * second one.
   *
   * Rejects with `failures` carrying the child's stderr lines, so the
   * caller can say why rather than just that.
   *
   * @param {string} script — 'resume.js' or 'letter.js'
   */
  function build({ script = 'resume.js', env = {} } = {}) {
    return serial(() => new Promise((resolve, reject) => {
      const started = Date.now();

      // Piped, not inherited.
      //
      // With stdio:'inherit' the child writes straight to this
      // process's file descriptors, which bypasses the log tee in
      // build/studio_server.js — so the app's console pane showed
      // preview output and nothing at all from a build, and a failed
      // build could only report "resume.js failed" because the reason
      // never reached it. Piping and writing each line through
      // process.stdout puts the build's output where both the terminal
      // and the app's log can see it.
      const child = spawn(process.execPath, [path.join(root, script)], {
        cwd: root,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: { ...process.env, ...env },
      });

      const failures = [];
      const forward = (stream, keep) => {
        let buffer = '';
        stream.setEncoding('utf-8');
        stream.on('data', (chunk) => {
          buffer += chunk;
          let idx;
          while ((idx = buffer.indexOf('\n')) !== -1) {
            const line = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 1);
            process.stdout.write(line + '\n');
            if (keep && line.trim()) failures.push(line.trim());
          }
        });
        stream.on('end', () => {
          if (buffer.trim()) {
            process.stdout.write(buffer + '\n');
            if (keep) failures.push(buffer.trim());
          }
        });
      };
      // _console sends errors and warnings to stderr, so that stream is
      // where a failure explains itself.
      forward(child.stdout, false);
      forward(child.stderr, true);

      child.once('error', (err) => {
        const e = new Error(`could not run ${script}: ${err.message}`);
        e.alreadyReported = true;
        reject(e);
      });

      child.once('close', (code) => {
        if (code === 0) {
          resolve({ script, ms: Date.now() - started });
          return;
        }
        const e = new Error(`${script} failed (exit ${code})`);
        e.alreadyReported = true;
        e.failures = failures.slice(-12);
        reject(e);
      });
    }));
  }

  async function dispose() {
    disposeSass();
    // A warm start may still be launching Chromium; wait for it so the
    // browser it produces is closed rather than orphaned.
    if (pagePromise) {
      try { await pagePromise; } catch { /* never launched */ }
    }
    if (browser) {
      try { await browser.close(); } catch { /* already gone */ }
      browser = null;
      page = null;
      pagePromise = null;
    }
    await worker.stop();
  }

  return {
    root,
    python: interpreter,
    workerInfo: hello,
    pipeline,
    pipelines,
    renderPreview,
    build,
    dispose,
    status: () => ({
      root,
      python: interpreter,
      worker: hello,
      browserOpen: Boolean(page),
      stylesStale: pipeline.stylesAreStale(),
    }),
  };
}


/* ─── Line-protocol server ────────────────────────────────────── */

function send(payload) {
  process.stdout.write(FRAME_PREFIX + JSON.stringify(payload) + '\n');
}

async function serve() {
  let engine;
  try {
    engine = await createEngine({ root: path.join(__dirname, '..') });
  } catch (err) {
    send({ id: null, op: 'hello', ok: false,
           error: { kind: 'startup', message: err.message, detail: [] } });
    process.exitCode = 1;
    return;
  }
  send({ id: null, op: 'hello', ok: true, result: engine.status() });

  const ops = {
    preview: (req) => engine.renderPreview({
      doc: req.doc, scale: req.scale, pages: req.pages,
      recompileStyles: req.recompileStyles, env: req.env,
    }),
    build: (req) => engine.build({ script: req.script, env: req.env }),
    status: async () => engine.status(),
    shutdown: async () => ({ bye: true }),
  };

  let buffer = '';
  process.stdin.setEncoding('utf-8');
  for await (const chunk of process.stdin) {
    buffer += chunk;
    let idx;
    while ((idx = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;

      let req;
      try {
        req = JSON.parse(line);
      } catch (err) {
        send({ id: null, op: null, ok: false,
               error: { kind: 'bad_request', message: `malformed JSON: ${err.message}`, detail: [] } });
        continue;
      }

      const fn = ops[req.op];
      if (!fn) {
        send({ id: req.id ?? null, op: req.op, ok: false,
               error: { kind: 'unknown_op', message: `unknown op ${req.op}`,
                        detail: [`known ops: ${Object.keys(ops).join(', ')}`] } });
        continue;
      }

      const started = Date.now();
      try {
        const result = await fn(req);
        send({ id: req.id ?? null, op: req.op, ok: true, result, ms: Date.now() - started });
      } catch (err) {
        send({ id: req.id ?? null, op: req.op, ok: false,
               error: { kind: err.kind || 'failed', message: err.message,
                        detail: err.alreadyReported ? [] : String(err.stack || '').split('\n').slice(0, 5) },
               ms: Date.now() - started });
      }

      if (req.op === 'shutdown') {
        await engine.dispose();
        return;
      }
    }
  }
  await engine.dispose();
}


module.exports = { createEngine, PythonWorker, FRAME_PREFIX };

if (require.main === module) {
  serve();
}
