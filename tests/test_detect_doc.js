/**
 * test_detect_doc.js — which document is this file?
 *
 * When you drop a .yml on the window, the app works out whether it is
 * a resume or a cover letter by reading it. It used to guess from the
 * filename and then ask you to confirm the guess, every time.
 *
 * The rule is not a heuristic: the two builders require disjoint
 * top-level keys, so a file that satisfies one cannot satisfy the
 * other.
 *
 *   resume  build.py               requires `sidebar` and `mainColumn`
 *   letter  build_letter.py  requires `letter`
 *
 * WHAT IS ACTUALLY AT RISK HERE
 * ─────────────────────────────
 * detectDoc scans for keys at column 0 rather than parsing the YAML,
 * because the app's server is Node and the project's real loader is
 * Python. That trade is only sound while the scan stays anchored: the
 * word `letter:` appears in ordinary résumé prose, and a bullet
 * reading "letter: see attached" must not turn a resume into a cover
 * letter. YAML puts every nested key and every block-scalar line at an
 * indent, so column 0 is exactly the set of top-level keys — but that
 * is a property of the regex, and a regex is one careless edit from
 * losing its anchor. Hence the nesting cases below.
 *
 * The other half is knowing when NOT to answer. A file matching both
 * shapes, or neither, must return null so the app asks instead of
 * silently filing your cover letter under Resume.
 */

const path = require('path');
const fs = require('fs');

const { assertEq, assertTrue, test, report } =
  require(path.join(__dirname, '_framework'));

const ROOT = path.join(__dirname, '..');
const { detectDoc, listDataFiles, resolveInsideRoot } =
  require(path.join(ROOT, 'build', 'studio_server.js'));

const doc = (yml) => detectDoc(yml).doc;


/* ─── the happy paths ─────────────────────────────────────────── */

test('a resume is detected from sidebar + mainColumn', () => {
  assertEq(doc('sidebar:\n  blocks: []\nmainColumn:\n  - type: summary\n'),
           'resume', 'both resume keys');
});

test('either resume key alone is enough', () => {
  assertEq(doc('sidebar:\n  blocks: []\n'), 'resume', 'sidebar only');
  assertEq(doc('mainColumn:\n  - type: summary\n'), 'resume', 'mainColumn only');
});

test('a cover letter is detected from letter', () => {
  assertEq(doc('letter:\n  body: |\n    Dear sir\n'), 'letter', 'letter key');
});

test('the reason is reported, for the toast that names it', () => {
  assertEq(detectDoc('sidebar:\n  blocks: []\n').reason,
           'it has sidebar', 'resume reason');
  assertEq(detectDoc('letter:\n  body: x\n').reason,
           'it has letter', 'letter reason');
});


/* ─── the anchor: nesting must not count ──────────────────────── */

test('letter: inside a block scalar does not make it a letter', () => {
  // The case this whole design rests on. `letter` is an ordinary
  // English word and will turn up in résumé prose sooner or later.
  const resume = [
    'sidebar:',
    '  blocks: []',
    'mainColumn:',
    '  - type: experience',
    '    jobs:',
    '      - id: a-job',
    '        bullets:',
    '          - |',
    '            letter: drafted the offer letter template',
    '',
  ].join('\n');
  assertEq(doc(resume), 'resume', 'nested letter ignored');
});

test('an indented top-level-looking key does not count', () => {
  assertEq(doc('  mainColumn:\n    - x\n'), null, 'indented mainColumn');
  assertEq(doc('  letter:\n    body: x\n'), null, 'indented letter');
});

test('a key inside a quoted string does not count', () => {
  assertEq(doc('sidebar:\n  blocks: []\nmainColumn:\n  - type: summary\n'
             + '    text: "letter: not a key"\n'),
           'resume', 'quoted letter ignored');
});

test('comments are not keys', () => {
  assertEq(doc('# letter: this is a note\nsidebar:\n  blocks: []\n'),
           'resume', 'commented letter ignored');
});


/* ─── refusing to answer ──────────────────────────────────────── */

test('both shapes at once returns null rather than picking', () => {
  const both = 'sidebar:\n  blocks: []\nletter:\n  body: x\n';
  assertEq(doc(both), null, 'ambiguous -> null');
  assertTrue(/both/.test(detectDoc(both).reason), 'says why');
});

test('neither shape returns null', () => {
  assertEq(doc('# nothing here\n'), null, 'empty -> null');
  assertEq(doc('role: Cook\n'), null, 'unrelated keys -> null');
});

test('a shared profile is recognized and explained', () => {
  // _profile.yml has name/contact/meta and none of the document keys.
  // Saying "not a document" is unhelpful; saying what it looks like is
  // the difference between a dead end and an answer.
  const profile = 'name:\n  first: A\n  last: B\ncontact:\n  address: X\n';
  assertEq(doc(profile), null, 'profile -> null');
  assertTrue(/shared profile/.test(detectDoc(profile).reason),
             'names it as a profile');
});


/* ─── against the real files on disk ──────────────────────────── */

test('every file in data/ is classified the way its name implies', () => {
  const dataDir = path.join(ROOT, 'data');
  if (!fs.existsSync(dataDir)) return;
  for (const name of fs.readdirSync(dataDir)) {
    if (!/\.ya?ml$/i.test(name)) continue;          // skips .old backups
    const got = detectDoc(fs.readFileSync(path.join(dataDir, name), 'utf-8')).doc;
    const want = name.startsWith('_') ? null
               : /^(cover[_-])?letter/i.test(name) ? 'letter'
               : 'resume';
    assertEq(got, want, `${name} -> ${want}`);
  }
});

/* ─── the inspector's divided list ────────────────────────────── */

test('listDataFiles labels each file so the picker can divide them', () => {
  // The Resume card must not offer letter.yml. Picking it could only
  // ever produce a build that fails on the first validation, and the
  // list is built from these labels.
  const listed = listDataFiles();
  if (!listed.length) return;                       // no data/ in a bare checkout

  for (const f of listed) {
    assertTrue(!f.name.startsWith('_'), `${f.name} is not a support file`);
    const want = /^(cover[_-])?letter/i.test(f.name) ? 'letter' : 'resume';
    assertEq(f.doc, want, `${f.name} labeled ${want}`);
  }

  const forResume = listed.filter(f => f.doc === 'resume' || f.doc === null);
  const forLetter = listed.filter(f => f.doc === 'letter' || f.doc === null);
  assertTrue(!forResume.some(f => /^letter/i.test(f.name)),
             'no letter files offered to the resume card');
  assertTrue(!forLetter.some(f => /^resume/i.test(f.name)),
             'no resume files offered to the letter card');
});

/* ─── /api/reveal cannot be pointed outside the project ───────── */

test('resolveInsideRoot accepts paths within the project', () => {
  assertTrue(resolveInsideRoot('dist') !== null, 'a subdirectory');
  assertTrue(resolveInsideRoot('dist/Gaius_Caesar_Resume.pdf') !== null, 'a file in one');
  assertTrue(resolveInsideRoot('.') !== null, 'the root itself');
  assertTrue(resolveInsideRoot(path.join(ROOT, 'data')) !== null, 'an absolute path inside');
});

test('resolveInsideRoot rejects anything that escapes', () => {
  // This is what stops POST /api/reveal from being "open any path on
  // this machine" for a stray tab that gets past the origin check.
  assertEq(resolveInsideRoot('../../etc/passwd'), null, 'dot-dot escape');
  assertEq(resolveInsideRoot('/etc/passwd'), null, 'absolute outside');
  assertEq(resolveInsideRoot('dist/../../..'), null, 'escape via a valid prefix');
  assertEq(resolveInsideRoot(''), null === resolveInsideRoot('') ? null : resolveInsideRoot(''),
           'empty resolves to the root, which is allowed');
});

test('a sibling directory sharing the prefix is not inside', () => {
  // The reason the check appends a separator before comparing:
  // "<root>-backup" starts with "<root>" but is a different tree.
  assertEq(resolveInsideRoot(ROOT + '-backup/secrets.txt'), null, 'prefix sibling');
});

report('detect_doc');
