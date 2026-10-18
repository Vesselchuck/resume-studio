/**
 * test_shell_frames.js — the desktop shell and the server agree on their
 * frames.
 *
 * The Tauri shell (src-tauri/src/main.rs) learns the server's state from
 * two framed lines on the server's stdout: the ready frame, with the URL
 * to show and the launch token, and the failed frame, with the reason the
 * engine could not start after that. It sends the token back in a header
 * whose name is agreed the same way. The prefixes and the header are
 * written out once in Rust and once
 * in build/studio_server.js, and nothing compiles one against the other.
 * A change to either alone is not an error anywhere: the shell simply
 * never sees the server become ready (the window spins forever) or never
 * sees it fail (the window shows WebView2's "can't reach this page").
 *
 * So this reads main.rs as text and compares. The CI type-checks the
 * shell but does not run it, which is why the check is here rather than
 * in main.rs's own tests. The JS suites that start the server wait for
 * the same ready frame and keep their own copy of it; they are checked
 * against the server too.
 */

const path = require('path');
const fs = require('fs');

const { assertEq, assertTrue, test, report } =
  require(path.join(__dirname, '_framework'));

const ROOT = path.join(__dirname, '..');
const MAIN_RS = path.join(ROOT, 'src-tauri', 'src', 'main.rs');
const server = require(path.join(ROOT, 'build', 'studio_server.js'));

/** The failed frame, as agreed with the server. */
const FAILED_LITERAL = '\x1eSTUDIO_FAILED ';

/** A Rust string literal's value: the escapes main.rs can use. */
function rustString(body) {
  return body.replace(/\\(u\{([0-9a-fA-F]{1,6})\}|x([0-9a-fA-F]{2})|[nrt0\\"'])/g,
    (whole, esc, uni, hex) => {
      if (uni) return String.fromCodePoint(parseInt(uni, 16));
      if (hex) return String.fromCharCode(parseInt(hex, 16));
      return { n: '\n', r: '\r', t: '\t', 0: '\0', '\\': '\\', '"': '"', "'": "'" }[esc];
    });
}

/** The value of `const NAME: &str = "…";` in main.rs. */
function rustConst(name) {
  const source = fs.readFileSync(MAIN_RS, 'utf-8');
  const found = source.match(new RegExp(`^const ${name}: &str = "((?:[^"\\\\]|\\\\.)*)";`, 'm'));
  assertTrue(found !== null, `main.rs declares ${name}`);
  return found ? rustString(found[1]) : undefined;
}


test('the reader of Rust literals reads the escapes main.rs uses', () => {
  assertEq(rustString('\\u{1e}STUDIO_READY '), '\x1eSTUDIO_READY ', '\\u{..}');
  assertEq(rustString('a\\x1eb\\\\c\\"d\\n'), 'a\x1eb\\c"d\n', 'the others');
});

test("main.rs's ready frame is the server's", () => {
  assertEq(typeof server.READY_PREFIX, 'string', 'the server exports READY_PREFIX');
  assertEq(rustConst('READY_PREFIX'), server.READY_PREFIX, 'READY_PREFIX');
});

test("main.rs's failed frame is the server's", () => {
  const rust = rustConst('STARTUP_FAILED_PREFIX');
  assertEq(rust, FAILED_LITERAL, 'STARTUP_FAILED_PREFIX is the agreed frame');
  assertEq(rust, server.STARTUP_FAILED_PREFIX, "STARTUP_FAILED_PREFIX is the server's");
});

test("main.rs's token header is the server's", () => {
  // HTTP header names are not case-sensitive; Node hands the server
  // them in lower case, and main.rs writes the conventional form.
  assertEq(typeof server.TOKEN_HEADER, 'string', 'the server exports TOKEN_HEADER');
  assertEq((rustConst('TOKEN_HEADER') || '').toLowerCase(), server.TOKEN_HEADER, 'TOKEN_HEADER');
});

test('the frames are distinct and start with the record separator', () => {
  const ready = rustConst('READY_PREFIX');
  const failed = rustConst('STARTUP_FAILED_PREFIX');
  assertTrue(ready.startsWith('\x1e') && failed.startsWith('\x1e'), 'both framed with \\x1e');
  assertTrue(!ready.startsWith(failed) && !failed.startsWith(ready), 'neither is a prefix of the other');
});

test("the suites' own copies of the ready frame are the server's", () => {
  // JS escapes in single quotes; the ones these copies use read the same
  // as Rust's.
  for (const name of fs.readdirSync(__dirname)) {
    if (!/^test_.*\.js$/.test(name)) continue;
    const source = fs.readFileSync(path.join(__dirname, name), 'utf-8');
    for (const [, body] of source.matchAll(/^const READY_PREFIX = '((?:[^'\\]|\\.)*)';/gm)) {
      assertEq(rustString(body), server.READY_PREFIX, `${name}'s READY_PREFIX`);
    }
  }
});

report('shell_frames');
