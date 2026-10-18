/**
 * test_env_parsing.js — how the Node side reads the env vars a user
 * types by hand.
 *
 * Both of these were bugs that read a variable as "set" rather than
 * reading its value:
 *
 *   • FORCE_COLOR=0 forced color ON (any non-empty string was truthy).
 *   • cmd.exe's `set PYTHON=py && node resume.js` stores "py " with the
 *     trailing space, and spawning "py " fails with ENOENT.
 *
 * And two where the value was taken on trust: cmd.exe's
 * `set PYTHON="C:\…\python.exe"` keeps its quotes, and a Python older
 * than README's 3.10 got as far as a traceback mid-build. detectPython
 * now checks both first.
 *
 * Detection itself: an interpreter under a non-ASCII path came back
 * garbled (or not at all) from the code page Python printed it in.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { assertEq, assertTrue, test, report } = require('./_framework');

const ROOT = path.resolve(__dirname, '..');
const c = require(path.join(ROOT, 'build', '_console'));
const {
  detectPython, detectPythonInfo, pythonOverride, parseVersion, tildify, MIN_PYTHON,
} = require(path.join(ROOT, 'build', 'detect_python'));

const TTY = { isTTY: true };
const PIPE = { isTTY: false };


/* ─── _console.colorEnabled ───────────────────────────────────── */

test('no env: color follows the stream', () => {
  assertEq(c.colorEnabled(TTY, {}), true, 'TTY → color');
  assertEq(c.colorEnabled(PIPE, {}), false, 'pipe → none');
});

test('FORCE_COLOR=0 / false disables color, even on a TTY', () => {
  for (const v of ['0', 'false', 'FALSE', ' 0 ']) {
    assertEq(c.colorEnabled(TTY, { FORCE_COLOR: v }), false, `FORCE_COLOR=${JSON.stringify(v)}`);
  }
});

test('FORCE_COLOR=1/2/3/true (or empty) enables color, even when piped', () => {
  for (const v of ['1', '2', '3', 'true', 'True', '']) {
    assertEq(c.colorEnabled(PIPE, { FORCE_COLOR: v }), true, `FORCE_COLOR=${JSON.stringify(v)}`);
  }
});

test('an unrecognized FORCE_COLOR falls back to the stream', () => {
  assertEq(c.colorEnabled(PIPE, { FORCE_COLOR: 'banana' }), false, 'pipe');
  assertEq(c.colorEnabled(TTY, { FORCE_COLOR: 'banana' }), true, 'TTY');
});

test('NO_COLOR, when non-empty, wins over everything', () => {
  assertEq(c.colorEnabled(TTY, { NO_COLOR: '1' }), false, 'TTY');
  assertEq(c.colorEnabled(PIPE, { NO_COLOR: '1', FORCE_COLOR: '1' }), false, 'over FORCE_COLOR');
});

test('an empty NO_COLOR is not set (no-color.org)', () => {
  assertEq(c.colorEnabled(TTY, { NO_COLOR: '' }), true, 'TTY keeps color');
});


/* ─── detect_python: the PYTHON override ──────────────────────── */

function withPython(value, fn) {
  const had = Object.prototype.hasOwnProperty.call(process.env, 'PYTHON');
  const old = process.env.PYTHON;
  if (value === undefined) delete process.env.PYTHON;
  else process.env.PYTHON = value;
  try {
    return fn();
  } finally {
    if (had) process.env.PYTHON = old;
    else delete process.env.PYTHON;
  }
}

/**
 * A spawnSync stand-in: `versions` maps a command to what `--version`
 * prints, and `seen` collects the commands probed that way. Asked for
 * sys.executable (`-c`), a command answers `exes[cmd]` when given — null
 * makes it fail — and otherwise `/fake/bin/<cmd>`. `calls` records
 * every spawn as {cmd, args, cwd}.
 */
function fakeRun(versions, seen = [], exes = {}, calls = []) {
  return (cmd, args = [], opts = {}) => {
    calls.push({ cmd, args, cwd: opts.cwd });
    if (args[0] === '-c' && cmd in versions) {
      const exe = cmd in exes ? exes[cmd] : `/fake/bin/${cmd}`;
      return exe === null
        ? { status: 1, stdout: '', stderr: 'Traceback' }
        : { status: 0, stdout: `${exe}\n`, stderr: '' };
    }
    seen.push(cmd);
    if (!(cmd in versions)) return { error: new Error(`spawnSync ${cmd} ENOENT`), status: null };
    return { status: 0, stdout: versions[cmd], stderr: '' };
  };
}

/**
 * detectPython with a fake spawn; process.exit becomes a thrown marker.
 * Every path the fake reports exists unless `exists` says otherwise.
 * With `info`, the whole of detectPythonInfo's answer.
 */
function detect(env, versions, seen, exes, calls, { exists = () => true, info = false } = {}) {
  const write = process.stderr.write;
  process.stderr.write = () => true;
  try {
    return (info ? detectPythonInfo : detectPython)({
      env, run: fakeRun(versions, seen, exes, calls), exists,
      exit: (code) => { throw new Error(`exit ${code}`); },
    });
  } catch (err) {
    return err.message;
  } finally {
    process.stderr.write = write;
  }
}

test("cmd.exe's trailing space is trimmed", () => {
  withPython('py ', () => {
    assertEq(pythonOverride(), 'py', 'override');
    const seen = [];
    const calls = [];
    assertEq(detect(process.env, { py: 'Python 3.12.1' }, seen, {}, calls), 'py',
      'detectPython returns it trimmed, as written: PYTHON is not resolved');
    assertEq(seen, ['py'], 'and spawns it trimmed');
    assertEq(calls.length, 1, 'with one --version and nothing else');
  });
  withPython('\tpython3.12 \r\n', () => assertEq(pythonOverride(), 'python3.12', 'all whitespace'));
});

test('a blank PYTHON counts as unset', () => {
  withPython('   ', () => assertEq(pythonOverride(), null, 'whitespace only'));
  withPython('', () => assertEq(pythonOverride(), null, 'empty'));
  withPython(undefined, () => assertEq(pythonOverride(), null, 'absent'));
});

test('a path with inner spaces is kept intact', () => {
  withPython(' C:\\Program Files\\Python312\\python.exe ', () =>
    assertEq(pythonOverride(), 'C:\\Program Files\\Python312\\python.exe', 'inner spaces'));
});

test("cmd.exe's quotes around a path are removed", () => {
  // set PYTHON="C:\Program Files\Python312\python.exe" keeps the quotes,
  // and a program spawned without a shell is looked up with them.
  const exe = 'C:\\Program Files\\Python312\\python.exe';
  assertEq(pythonOverride({ PYTHON: `"${exe}"` }), exe, 'double quotes');
  assertEq(pythonOverride({ PYTHON: ` '${exe}' ` }), exe, 'single quotes, padded');
  assertEq(pythonOverride({ PYTHON: '""' }), null, 'only quotes is unset');
  assertEq(pythonOverride({ PYTHON: '"py' }), '"py', 'an unmatched quote is left alone');
});

test('PYTHON older than README\'s 3.10 is refused, by name', () => {
  assertEq(MIN_PYTHON, [3, 10], "README.md: 'Python 3.10+'");
  assertEq(detect({ PYTHON: 'python3.9' }, { 'python3.9': 'Python 3.9.18' }), 'exit 1', '3.9');
  assertEq(detect({ PYTHON: 'python2' }, { python2: 'Python 2.7.18' }), 'exit 1', '2.7');
  assertEq(detect({ PYTHON: 'python3.10' }, { 'python3.10': 'Python 3.10.0' }), 'python3.10', '3.10');
});

test('a PYTHON that cannot be run stops here, not mid-build', () => {
  assertEq(detect({ PYTHON: 'py -3.12' }, { py: 'Python 3.12.1' }), 'exit 1', 'arguments');
});

test('auto-detection skips an interpreter that is too old', () => {
  const seen = [];
  const got = detect({}, { python3: 'Python 3.8.10', python: 'Python 3.12.1', py: 'Python 3.12.1' }, seen);
  if (process.platform === 'win32') {
    assertEq(got, '/fake/bin/python', 'first candidate on Windows');
  } else {
    assertEq(got, '/fake/bin/python', 'python3 is 3.8, python is 3.12');
    assertEq(seen, ['python3', 'python'], 'tried in order');
  }
  assertEq(detect({}, { python3: 'Python 3.9.1', python: 'Python 2.7.18', py: 'Python 3.9.1' }),
    'exit 1', 'nothing new enough');
});

// The candidates in the order detectPython tries them on this platform.
const FIRST = process.platform === 'win32' ? 'python' : 'python3';
const SECOND = process.platform === 'win32' ? 'py' : 'python';

test('auto-detection needs a Python version, not just exit 0', () => {
  // tooOld(null) is false, so a candidate that printed no version used
  // to be taken as new enough.
  assertEq(detect({}, { [FIRST]: '', [SECOND]: 'Python 3.12.1' }), `/fake/bin/${SECOND}`,
    'one that printed nothing is passed over');
  assertEq(detect({}, { [FIRST]: 'something else 9.9', [SECOND]: 'Python 3.12.1' }),
    `/fake/bin/${SECOND}`, 'and one that printed something other than a version');
  assertEq(detect({}, { python: '', py: '', python3: '' }), 'exit 1', 'none of them: no Python');
});

test('auto-detection returns the absolute path the interpreter reports', () => {
  // On Windows a bare name is looked up in the current directory first
  // (the project root, for the CLIs and the engine): spawned by name,
  // a python.exe dropped there would run instead.
  const all = { python: 'Python 3.12.1', py: 'Python 3.12.1', python3: 'Python 3.12.1' };
  assertEq(detect({}, all, [], { [FIRST]: 'C:\\Python312\\python.exe' }),
    'C:\\Python312\\python.exe', 'its sys.executable');
});

test('a candidate that cannot say where it is runs by its name, not skipped', () => {
  // Skipping it picked the next candidate, another Python, in silence —
  // or none at all. The bare name is what ran before locate existed.
  const all = { python: 'Python 3.12.1', py: 'Python 3.12.1', python3: 'Python 3.12.1' };
  assertEq(detect({}, all, [], { [FIRST]: null }), FIRST, 'one that cannot report it');
  assertEq(detect({}, all, [], { [FIRST]: 'python' }), FIRST, 'one that reports a relative path');
  assertEq(detect({}, all, [], { [FIRST]: 'C:\\Jos\uFFFD\\python.exe' }, [], { exists: () => false }),
    FIRST, 'one that reports a path that is not there (a garbled one, say)');
});

test('the log shows the candidate and its version, not the absolute path', () => {
  // A per-user Python is C:\Users\<account>\…, and a Studio Build's
  // output goes to the in-app log, which ends up in screenshots.
  const all = { python: 'Python 3.12.1', py: 'Python 3.12.1', python3: 'Python 3.12.1' };
  const exe = path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'Python', 'python.exe');
  const got = detect({}, all, [], { [FIRST]: exe }, [], { info: true });
  assertEq(got, { exe, command: FIRST, label: `${FIRST} 3.12.1` },
    'spawned by its path, shown and typed by its name');
  const set = detect({ PYTHON: exe }, { [exe]: 'Python 3.13.0rc1' }, [], {}, [], { info: true });
  assertEq(set.exe, exe, 'PYTHON is spawned as written');
  assertEq(set.command, exe, 'and typed as written: the hint must run');
  assertEq(set.label, `${path.join('~', 'AppData', 'Local', 'Programs', 'Python', 'python.exe')} 3.13.0rc1`,
    'shown with the home directory as ~');
});

test('tildify replaces the home directory and nothing else', () => {
  const home = os.homedir();
  assertEq(tildify(home), '~', 'home itself');
  assertEq(tildify(path.join(home, 'x', 'python')), path.join('~', 'x', 'python'), 'below it');
  assertEq(tildify(`${home}2${path.sep}python`), `${home}2${path.sep}python`, 'a sibling that shares the prefix');
  assertEq(tildify('python3'), 'python3', 'a bare name');
  if (process.platform === 'win32') {
    assertEq(tildify(path.join(home.toUpperCase(), 'p.exe')), path.join('~', 'p.exe'),
      'Windows paths compare without case');
  }
});

/** The interpreter of a venv at `dir`, made from `base` unless it is there. */
function venvPython(base, dir) {
  const exe = process.platform === 'win32'
    ? path.join(dir, 'Scripts', 'python.exe') : path.join(dir, 'bin', 'python3');
  if (fs.existsSync(exe)) return exe;
  const { spawnSync } = require('child_process');
  const r = spawnSync(base, ['-m', 'venv', '--without-pip', dir], { encoding: 'utf-8' });
  if (r.status !== 0) throw new Error(`venv at ${dir}: ${r.stderr || r.error}`);
  return exe;
}

/** detectPython in a fresh Node with `bin` first on PATH and Python's stdout in `encoding`. */
function detectWith(bin, encoding) {
  const { spawnSync } = require('child_process');
  const env = { ...process.env, PYTHONIOENCODING: encoding };
  delete env.PYTHON;
  const key = Object.keys(env).find(k => /^path$/i.test(k)) || 'PATH';
  env[key] = `${bin}${path.delimiter}${env[key] || ''}`;
  const code = `process.stdout.write(JSON.stringify(require(${JSON.stringify(
    path.join(ROOT, 'build', 'detect_python'))}).detectPython()))`;
  const r = spawnSync(process.execPath, ['-e', code], { encoding: 'utf-8', env });
  if (r.status !== 0) throw new Error(`detectPython: ${r.stderr}`);
  return JSON.parse(r.stdout);
}

test('an interpreter under a non-ASCII path is found by that path', () => {
  // sys.executable was printed in Python's stdout encoding, on Windows
  // the ANSI code page, and read as UTF-8: José came back with U+FFFD (a
  // path no spawn finds), and Cyrillic under cp1252 raised, so that
  // Python was skipped and another one picked. PYTHONIOENCODING stands
  // in for the code page here, on any platform.
  const base = withPython(undefined, () => detectPython());
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'python-path-'));
  const same = (a, b) => (process.platform === 'win32'
    ? a.toLowerCase() === b.toLowerCase() : a === b);
  try {
    for (const [name, encoding] of [['José', 'cp1252'], ['Смирнов', 'cp1251'], ['Смирнов', 'cp1252']]) {
      const exe = venvPython(base, path.join(root, name));
      const got = detectWith(path.dirname(exe), encoding);
      assertTrue(same(got, exe), `${name} under ${encoding}: ${JSON.stringify(got)} is ${JSON.stringify(exe)}`);
      assertTrue(fs.existsSync(got), `${name} under ${encoding}: the path exists`);
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('on Windows the candidates are probed from %SystemRoot%, not the current directory', () => {
  const calls = [];
  detect({ SystemRoot: process.env.SystemRoot || os.tmpdir() }, { [FIRST]: 'Python 3.12.1' }, [], {}, calls);
  const want = process.platform === 'win32' ? (process.env.SystemRoot || os.tmpdir()) : undefined;
  assertEq(calls.map(x => x.cwd), calls.map(() => want), 'every probe');
  assertEq(calls.length, 2, '--version, then sys.executable');
});

test('a python.exe in the current directory is never what detection picks', () => {
  // The real interpreter, copied into a directory that is then made
  // current, and with Windows' opt-out of the current-directory search
  // removed for the duration.
  if (process.platform !== 'win32') {
    assertTrue(true, 'Windows only: elsewhere a bare name never searches the current directory');
    return;
  }
  const real = withPython(undefined, () => detectPython());
  assertTrue(path.isAbsolute(real), `detectPython returns an absolute path: ${real}`);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'planted-python-'));
  const cwd = process.cwd();
  const optOut = process.env.NoDefaultCurrentDirectoryInExePath;
  try {
    fs.copyFileSync(real, path.join(dir, 'python.exe'));
    delete process.env.NoDefaultCurrentDirectoryInExePath;
    process.chdir(dir);
    const got = withPython(undefined, () => detectPython());
    assertTrue(path.isAbsolute(got), `absolute: ${got}`);
    assertTrue(path.dirname(got).toLowerCase() !== dir.toLowerCase(),
      `not the planted copy: ${got}`);
  } finally {
    process.chdir(cwd);
    if (optOut !== undefined) process.env.NoDefaultCurrentDirectoryInExePath = optOut;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/* ─── _compile_cache: NODE_COMPILE_CACHE / NODE_DISABLE_COMPILE_CACHE ── */

/**
 * enable() and describe() in a fresh Node with `env` added. With
 * `asNode`, that Node passes for an older one: no getCompileCacheDir
 * (Node 22.8 added it), and process.versions.node says `asNode`.
 */
function compileCacheIn(env, asNode = null) {
  const { spawnSync } = require('child_process');
  const pretend = asNode ? `delete require('node:module').getCompileCacheDir;
    Object.defineProperty(process, 'versions',
      { value: { ...process.versions, node: ${JSON.stringify(asNode)} } });` : '';
  const code = `${pretend}
    const cc = require(${JSON.stringify(path.join(ROOT, 'build', '_compile_cache'))});
    process.stdout.write(JSON.stringify({ r: cc.enable(), d: cc.describe() }));`;
  const clean = { ...process.env };
  delete clean.NODE_COMPILE_CACHE;
  delete clean.NODE_DISABLE_COMPILE_CACHE;
  const r = spawnSync(process.execPath, ['-e', code], { encoding: 'utf-8', env: { ...clean, ...env } });
  return JSON.parse(r.stdout);
}

test('the compile cache is reported off when NODE_DISABLE_COMPILE_CACHE turns it off', () => {
  // With NODE_COMPILE_CACHE also set, enable() took the variable's word
  // for it and describe() named a cache Node was not writing.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'compile-cache-'));
  try {
    const off = compileCacheIn({ NODE_COMPILE_CACHE: dir, NODE_DISABLE_COMPILE_CACHE: '1' });
    assertEq([off.r.enabled, off.d], [false, null], 'off, and nothing to describe');
    const on = compileCacheIn({ NODE_COMPILE_CACHE: dir });
    assertEq(on.r.enabled, true, 'on without the opt-out');
    assertTrue(path.resolve(on.r.dir).startsWith(path.resolve(dir)),
      `in the directory it names: ${on.r.dir}`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('NODE_COMPILE_CACHE on a Node that has no getCompileCacheDir', () => {
  // Node 20.19 to 22.0 ignore the variable, and the Studio reported a
  // cache there all the same. 22.1 to 22.7 read it (Node's CLI docs),
  // with no way to ask.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'compile-cache-'));
  try {
    const old = compileCacheIn({ NODE_COMPILE_CACHE: dir }, '20.19.0');
    assertEq([old.r.enabled, old.d], [false, null], '20.19: off, and nothing to describe');
    assertTrue(/ignores NODE_COMPILE_CACHE/.test(old.r.reason), `says why: ${old.r.reason}`);
    assertEq(compileCacheIn({ NODE_COMPILE_CACHE: dir }, '22.0.0').r.enabled, false, '22.0: off');
    const reads = compileCacheIn({ NODE_COMPILE_CACHE: dir }, '22.5.0');
    assertEq([reads.r.enabled, reads.r.dir], [true, dir], '22.5: on, in the directory it names');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('parseVersion reads stdout (3.x) and stderr (2.x) forms', () => {
  assertEq(parseVersion('Python 3.12.1\n'), [3, 12], '3.12');
  assertEq(parseVersion('Python 3.10.0rc1'), [3, 10], 'rc');
  assertEq(parseVersion(''), null, 'nothing');
});


report();
