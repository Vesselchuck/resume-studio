/**
 * test_loading_page.js — the desktop window's loading page shows what
 * the shell gives it.
 *
 * The Tauri shell (src-tauri/src/main.rs) shows a failed start, or a
 * server that stopped once the Studio was open, by evaluating
 * `window.showStartupError(message, heading)` in ui/loading.html. Nothing
 * runs that page under test otherwise: the CI type-checks the shell but
 * does not run it. So this runs the page's script against a stand-in
 * document, and reads main.rs as text for the headings it passes.
 */

const path = require('path');
const fs = require('fs');
const vm = require('vm');

const { assertEq, assertTrue, test, report } =
  require(path.join(__dirname, '_framework'));

const ROOT = path.join(__dirname, '..');
const PAGE = fs.readFileSync(path.join(ROOT, 'ui', 'loading.html'), 'utf-8');
const MAIN_RS = fs.readFileSync(path.join(ROOT, 'src-tauri', 'src', 'main.rs'), 'utf-8');

/** The text of the element with `id` in the page as written. */
function written(id) {
  const found = PAGE.match(new RegExp(`<[a-z0-9]+ id="${id}">([^<]*)<`));
  assertTrue(found !== null, `loading.html has an element with id "${id}"`);
  return found ? found[1] : undefined;
}

/** The page's inline script run against a stand-in document. */
function loadPage() {
  // Any case, attributes and whitespace in either tag: a script written
  // `<SCRIPT>` or `</script >` still counts (CodeQL js/bad-tag-filter).
  const scripts = [...PAGE.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\b[^>]*>/gi)].map(m => m[1]);
  assertEq(scripts.length, 1, 'loading.html has one inline script');
  const elements = {
    heading: { textContent: written('heading') },
    message: { textContent: '' },
  };
  const classes = new Set();
  const window = {};
  const document = {
    getElementById: id => elements[id],
    body: { classList: { add: name => classes.add(name) } },
  };
  vm.runInNewContext(scripts[0], { window, document, String });
  return { window, elements, classes };
}

/** The value of `const NAME: &str = "…";` in main.rs (plain text only). */
function rustConst(name) {
  const found = MAIN_RS.match(new RegExp(`^const ${name}: &str = "([^"\\\\]*)";`, 'm'));
  assertTrue(found !== null, `main.rs declares ${name}`);
  return found ? found[1] : undefined;
}


test('a failed start shows its reason under the heading the shell gives', () => {
  const { window, elements, classes } = loadPage();
  assertEq(typeof window.showStartupError, 'function', 'the page defines showStartupError');
  window.showStartupError('The server exited before it was ready.', 'The Studio stopped');
  assertEq(elements.message.textContent, 'The server exited before it was ready.', 'the reason');
  assertEq(elements.heading.textContent, 'The Studio stopped', 'the heading');
  assertTrue(classes.has('failed'), 'the error is on screen');
});

test('without a heading, the one in the page stays', () => {
  const { window, elements } = loadPage();
  window.showStartupError('Could not start Node.');
  assertEq(elements.heading.textContent, 'Resume Studio could not start', 'the default heading');
  assertEq(elements.message.textContent, 'Could not start Node.', 'the reason');
});

test("the page's own heading is the shell's for a failed start", () => {
  assertEq(written('heading'), rustConst('NOT_STARTED'), 'NOT_STARTED');
  const stopped = rustConst('STOPPED');
  assertTrue(typeof stopped === 'string' && stopped !== written('heading'), 'STOPPED is a heading of its own');
});

test('the shell passes the heading after the message', () => {
  // The order showStartupError takes them in (startup_error_script).
  assertTrue(/window\.showStartupError\(\{\}, \{\}\)/.test(MAIN_RS)
    && /json\(message\),\s*json\(heading\)/.test(MAIN_RS),
  'main.rs calls showStartupError(message, heading)');
});

report('loading_page');
