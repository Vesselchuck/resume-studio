/**
 * test_output_name.js — the Node half of the output-naming contract.
 *
 * The PDFs are named after you: Gaius_Caesar_Resume.pdf. That name is
 * derived from YAML, which only Python in this project can read, so
 * Node never computes it — build.py writes `output_stem` into
 * dist/pdf_meta.json and this module reads it back. Everything here
 * tests that one-way trip and what happens when it fails, which it
 * will: the app polls for tray status before anything is built, and
 * an interrupted build can leave a half-written metadata file.
 *
 * WHAT IS ACTUALLY AT RISK HERE
 * ─────────────────────────────
 * pruneStale() deletes files. It exists because the set of filenames a
 * build occupies moves when your name does, and a stranded PDF in
 * dist/ is not clutter — it is a complete, plausible-looking resume
 * with the wrong contents sitting in the directory you open when you
 * need to attach one. But a delete that is one careless regex edit
 * from matching too much is worse than the problem it solves, so the
 * bulk of this file is about what pruneStale must NOT touch: the
 * other document's outputs, the build's own JSON and HTML, a PDF you
 * put in dist/ yourself, and above all the files the current build
 * just wrote.
 */

const path = require('path');
const fs = require('fs');
const os = require('os');

const { assertEq, assertTrue, test, report } =
  require(path.join(__dirname, '_framework'));

const ROOT = path.join(__dirname, '..');
const on = require(path.join(ROOT, 'build', '_output_name.js'));


/** A throwaway directory, removed when `fn` returns. */
function withTempDir(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'outputname-'));
  try {
    return fn(dir);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

function writeMeta(dir, obj) {
  const p = path.join(dir, 'pdf_meta.json');
  fs.writeFileSync(p, typeof obj === 'string' ? obj : JSON.stringify(obj), 'utf-8');
  return p;
}

function touch(dir, ...names) {
  for (const n of names) fs.writeFileSync(path.join(dir, n), 'x', 'utf-8');
}

const listing = dir => fs.readdirSync(dir).sort();


/* ─── reading the stem back out of the metadata ───────────────── */

test('the stem comes from the build metadata, not from a guess', () => {
  withTempDir((dir) => {
    const meta = writeMeta(dir, { output_stem: 'Gaius_Caesar_Resume' });
    const p = on.outputPaths(dir, meta, 'resume');
    assertEq(p.stem, 'Gaius_Caesar_Resume', 'stem');
    assertEq(path.basename(p.colorPdf), 'Gaius_Caesar_Resume.pdf', 'color');
    assertEq(path.basename(p.grayscalePdf),
             'Gaius_Caesar_Resume_Grayscale.pdf', 'grayscale');
  });
});

test('the color variant takes the bare stem', () => {
  // The file you attach to an application gets the clean name; only
  // the black-and-white one is suffixed. This is the user's choice
  // and the reason the two paths are not symmetric.
  withTempDir((dir) => {
    const meta = writeMeta(dir, { output_stem: 'A_B_Cover_Letter' });
    const p = on.outputPaths(dir, meta, 'letter');
    assertEq(path.basename(p.colorPdf), 'A_B_Cover_Letter.pdf', 'no suffix');
    assertTrue(path.basename(p.grayscalePdf).endsWith(on.GRAYSCALE_SUFFIX + '.pdf'),
               'grayscale suffixed');
  });
});

test('every broken-metadata case falls back instead of throwing', () => {
  // These getters are hit on every status poll the app makes,
  // including before the first build and during one. Throwing here
  // would take out the tray, not just the name.
  withTempDir((dir) => {
    assertEq(on.readStem(path.join(dir, 'nope.json'), 'resume'),
             'Resume', 'missing file');
    assertEq(on.readStem(writeMeta(dir, '{ not json'), 'resume'),
             'Resume', 'corrupt file');
    assertEq(on.readStem(writeMeta(dir, { title: 'x' }), 'resume'),
             'Resume', 'field absent (an older build)');
    assertEq(on.readStem(writeMeta(dir, { output_stem: '' }), 'resume'),
             'Resume', 'empty field');
    assertEq(on.readStem(writeMeta(dir, { output_stem: 42 }), 'letter'),
             'Cover_Letter', 'wrong type');
  });
});

test('the fallback names a file that correctly does not exist', () => {
  // The bootstrap case is not an error to report — nothing has been
  // built, so "not built" is the honest answer, and it is what the
  // absent fallback path produces.
  withTempDir((dir) => {
    const p = on.outputPaths(dir, path.join(dir, 'pdf_meta.json'), 'resume');
    assertEq(path.basename(p.colorPdf), 'Resume.pdf', 'fallback name');
    assertTrue(!fs.existsSync(p.colorPdf), 'and it is absent');
  });
});

test('an unknown variant is refused rather than guessed at', () => {
  let threw = false;
  try { on.readStem('whatever.json', 'invoice'); } catch { threw = true; }
  assertTrue(threw, 'readStem rejects');

  threw = false;
  try { on.outputPattern('invoice'); } catch { threw = true; }
  assertTrue(threw, 'outputPattern rejects');
});


/* ─── the pattern: what counts as this project's output ───────── */

test('the pattern matches this document\'s outputs, named or not', () => {
  const re = on.outputPattern('resume');
  assertTrue(re.test('Gaius_Caesar_Resume.pdf'), 'named color');
  assertTrue(re.test('Gaius_Caesar_Resume_Grayscale.pdf'), 'named grayscale');
  assertTrue(re.test('Resume.pdf'), 'the no-name fallback');
  assertTrue(re.test('Anne_Marie_Smith_Jones_Resume.pdf'), 'many parts');
});

test('the pattern does not reach past its own document', () => {
  const resume = on.outputPattern('resume');
  assertTrue(!resume.test('Gaius_Caesar_Cover_Letter.pdf'), 'the other document');
  assertTrue(!resume.test('pdf_meta.json'), 'build metadata');
  assertTrue(!resume.test('index.html'), 'the rendered HTML');
  assertTrue(!resume.test('styles.css'), 'the stylesheet');
  assertTrue(!resume.test('Resume.pdf.bak'), 'not anchored at the end');
  assertTrue(!resume.test('old_Resume.pdf.txt'), 'wrong extension');
  assertTrue(!resume.test('Resumes.pdf'), 'a longer word');
  assertTrue(!resume.test('My_Resume_Draft.pdf'), 'suffix must be last');

  const letter = on.outputPattern('letter');
  assertTrue(letter.test('Gaius_Caesar_Cover_Letter_Grayscale.pdf'), 'its own');
  assertTrue(!letter.test('Gaius_Caesar_Resume.pdf'), 'the other document');
});


/* ─── pruning: the part that deletes ──────────────────────────── */

test('pruneStale removes the previous name and keeps the current one', () => {
  withTempDir((dir) => {
    touch(dir,
      'Gaius_Caesar_Resume.pdf',            // this build
      'Gaius_Caesar_Resume_Grayscale.pdf',  // this build
      'Gaius_Julius_Resume.pdf');           // yesterday's spelling

    const keep = [
      path.join(dir, 'Gaius_Caesar_Resume.pdf'),
      path.join(dir, 'Gaius_Caesar_Resume_Grayscale.pdf'),
    ];
    const removed = on.pruneStale(dir, 'resume', keep);

    assertEq(removed.join(','), 'Gaius_Julius_Resume.pdf', 'removed the stale one');
    assertEq(listing(dir).join(','),
             'Gaius_Caesar_Resume.pdf,Gaius_Caesar_Resume_Grayscale.pdf',
             'the current pair survives');
  });
});

test('pruneStale removes the pre-rename legacy filenames', () => {
  // The migration case: everyone has a resume-color.pdf from before
  // outputs were named after people.
  withTempDir((dir) => {
    touch(dir, 'resume-color.pdf', 'resume-grayscale.pdf', 'Gaius_Caesar_Resume.pdf');
    on.pruneStale(dir, 'resume', [path.join(dir, 'Gaius_Caesar_Resume.pdf')]);
    assertEq(listing(dir).join(','), 'Gaius_Caesar_Resume.pdf', 'only today\'s left');
  });
});

test('pruneStale removes a variant written under the retired suffix', () => {
  // The grayscale marker was respelled ('-grayscale' → '_Grayscale').
  // A file written the old way is not the file this build wrote and
  // is not overwritten by it, so if the pattern stopped matching it,
  // it would stay in dist/ looking current forever.
  withTempDir((dir) => {
    touch(dir, 'Gaius_Caesar_Resume.pdf',
               'Gaius_Caesar_Resume_Grayscale.pdf',
               'Gaius_Caesar_Resume-grayscale.pdf');   // yesterday's spelling
    const keep = ['Gaius_Caesar_Resume.pdf', 'Gaius_Caesar_Resume_Grayscale.pdf']
      .map(n => path.join(dir, n));
    const removed = on.pruneStale(dir, 'resume', keep);
    assertEq(removed.join(','), 'Gaius_Caesar_Resume-grayscale.pdf', 'the old one');
    assertEq(listing(dir).length, 2, 'the current pair survives');
  });
});

test('the retired suffixes widen the pattern and nothing else', () => {
  // They must never reach outputPaths — a build writes the current
  // spelling only.
  withTempDir((dir) => {
    const meta = writeMeta(dir, { output_stem: 'A_B_Resume' });
    assertEq(path.basename(on.outputPaths(dir, meta, 'resume').grayscalePdf),
             'A_B_Resume' + on.GRAYSCALE_SUFFIX + '.pdf', 'writes the current spelling');
  });
  const re = on.outputPattern('resume');
  for (const retired of on.RETIRED_GRAYSCALE_SUFFIXES) {
    assertTrue(re.test(`A_B_Resume${retired}.pdf`), `matches ${retired}`);
    assertTrue(retired !== on.GRAYSCALE_SUFFIX, `${retired} is actually retired`);
  }
});

test('pruneStale never touches the other document', () => {
  // Building a resume must not delete the cover letter you built an
  // hour ago. The two documents share dist/ and nothing else.
  withTempDir((dir) => {
    touch(dir,
      'Gaius_Caesar_Resume.pdf',
      'Gaius_Caesar_Cover_Letter.pdf',
      'Gaius_Caesar_Cover_Letter_Grayscale.pdf',
      'letter-color.pdf');
    on.pruneStale(dir, 'resume', [path.join(dir, 'Gaius_Caesar_Resume.pdf')]);
    assertEq(listing(dir).length, 4, 'nothing was removed');
  });
});

test('pruneStale never touches anything outside its own pattern', () => {
  withTempDir((dir) => {
    touch(dir,
      'Gaius_Caesar_Resume.pdf',
      'pdf_meta.json', 'placement.json', 'index.html', 'styles.css',
      'favicon.svg',
      'Some_Other_Document.pdf',   // a PDF the user dropped in dist/
      'notes.txt');
    on.pruneStale(dir, 'resume', [path.join(dir, 'Gaius_Caesar_Resume.pdf')]);
    assertEq(listing(dir).length, 8, 'everything survives');
  });
});

test('a variant this build chose not to make is removed, not orphaned', () => {
  // RESUME_VARIANTS=color leaves the grayscale file out of `keep`, so
  // the previous run's grayscale PDF must go. Otherwise "the file
  // exists" stops meaning "this build made it" — which the snapshot
  // test and the app both rely on.
  withTempDir((dir) => {
    touch(dir, 'Gaius_Caesar_Resume.pdf', 'Gaius_Caesar_Resume_Grayscale.pdf');
    on.pruneStale(dir, 'resume', [path.join(dir, 'Gaius_Caesar_Resume.pdf')]);
    assertEq(listing(dir).join(','), 'Gaius_Caesar_Resume.pdf', 'grayscale gone');
  });
});

test('pruneStale compares resolved paths, not the strings it was given', () => {
  withTempDir((dir) => {
    touch(dir, 'Gaius_Caesar_Resume.pdf');
    // The same file, spelled the long way round.
    const awkward = path.join(dir, 'sub', '..', 'Gaius_Caesar_Resume.pdf');
    fs.mkdirSync(path.join(dir, 'sub'));
    const removed = on.pruneStale(dir, 'resume', [awkward]);
    assertEq(removed.length, 0, 'recognized as the kept file');
    assertTrue(fs.existsSync(path.join(dir, 'Gaius_Caesar_Resume.pdf')), 'survived');
  });
});

/* ─── case-insensitive filesystems ─────────────────────────────
 *
 * On NTFS / APFS, writing Gaius_CAESAR_Resume.pdf over an existing
 * Gaius_Caesar_Resume.pdf keeps the old spelling on disk. The keep-list
 * then names a path that readdir never returns, and a string compare
 * would delete the PDF the build just wrote. */

test('pruneStale keeps a file that IS a kept path under another spelling', () => {
  // Simulated on any OS with a hard link: dist/ holds the old-case name
  // only; the keep-list names the new-case path, which (as on a
  // case-insensitive volume) is the same file — same dev and inode.
  withTempDir((dir) => {
    const dist = path.join(dir, 'dist');
    const elsewhere = path.join(dir, 'other');
    fs.mkdirSync(dist);
    fs.mkdirSync(elsewhere);
    touch(dist, 'Gaius_Caesar_Resume.pdf');
    const newCase = path.join(elsewhere, 'Gaius_CAESAR_Resume.pdf');
    try {
      fs.linkSync(path.join(dist, 'Gaius_Caesar_Resume.pdf'), newCase);
    } catch (err) {
      // No hard links on this filesystem: the stubbed test below still
      // covers the logic.
      assertTrue(true, `hard links unavailable (${err.code}) — covered by the stub test`);
      return;
    }
    const removed = on.pruneStale(dist, 'resume', [newCase]);
    assertEq(removed.length, 0, 'the same file is recognized by identity');
    assertTrue(fs.existsSync(path.join(dist, 'Gaius_Caesar_Resume.pdf')),
               'the PDF the build just wrote survives');
  });
});

/** A one-file case-insensitive fs with no inodes (ino 0), like some network shares. */
function caseInsensitiveFs(dist, onDiskName) {
  const onDisk = path.join(dist, onDiskName).toLowerCase();
  const deleted = [];
  const exists = p => path.resolve(p).toLowerCase() === onDisk && !deleted.length;
  const enoent = (p) => Object.assign(new Error(`ENOENT: ${p}`), { code: 'ENOENT' });
  return {
    deleted,
    fs: {
      readdirSync: d => (path.resolve(d) === path.resolve(dist) && !deleted.length
        ? [onDiskName] : []),
      statSync: (p, opts) => {
        if (!exists(p)) throw enoent(p);
        const zero = opts && opts.bigint ? 0n : 0;
        return { dev: zero, ino: zero, isFile: () => true };
      },
      rmSync: (p) => { deleted.push(path.basename(p)); },
    },
  };
}

test('with no inode, win32/darwin fall back to a case-insensitive compare', () => {
  const dist = path.resolve('/virtual/dist');
  for (const platform of ['win32', 'darwin']) {
    const stub = caseInsensitiveFs(dist, 'Gaius_Caesar_Resume.pdf');
    const removed = on.pruneStale(dist, 'resume',
      [path.join(dist, 'Gaius_CAESAR_Resume.pdf')], null, null,
      { fs: stub.fs, platform });
    assertEq(removed, [], `${platform}: nothing removed`);
    assertEq(stub.deleted, [], `${platform}: the just-written PDF is not deleted`);
  }
});

test('with no inode on a case-sensitive platform, a differently-cased name is stale', () => {
  // Linux: Gaius_CAESAR_Resume.pdf and Gaius_Caesar_Resume.pdf are two
  // files, and the old one really is stale. ino 0 must not make every
  // file look identical (dev:0 / ino:0 is "unknown", not a match).
  const dist = path.resolve('/virtual/dist');
  const stub = caseInsensitiveFs(dist, 'Gaius_Caesar_Resume.pdf');
  const removed = on.pruneStale(dist, 'resume',
    [path.join(dist, 'Gaius_CAESAR_Resume.pdf')], null, null,
    { fs: stub.fs, platform: 'linux' });
  assertEq(removed, ['Gaius_Caesar_Resume.pdf'], 'the old spelling is pruned');
});

test('identity never spares an unrelated stale file', () => {
  withTempDir((dir) => {
    touch(dir, 'Gaius_Caesar_Resume.pdf', 'Gaius_Julius_Resume.pdf');
    const removed = on.pruneStale(dir, 'resume', [path.join(dir, 'Gaius_Caesar_Resume.pdf')]);
    assertEq(removed, ['Gaius_Julius_Resume.pdf'], 'a different file is still removed');
  });
});

test('a directory sharing the naming pattern is not deleted', () => {
  withTempDir((dir) => {
    fs.mkdirSync(path.join(dir, 'Gaius_Caesar_Resume.pdf'));
    on.pruneStale(dir, 'resume', []);
    assertTrue(fs.existsSync(path.join(dir, 'Gaius_Caesar_Resume.pdf')),
               'directories are skipped');
  });
});

test('a missing dist/ is not an error', () => {
  withTempDir((dir) => {
    const removed = on.pruneStale(path.join(dir, 'no-such-dir'), 'resume', []);
    assertEq(removed.length, 0, 'returns empty rather than throwing');
  });
});

test('every removal is reported, so a build says what it deleted', () => {
  // Silent deletion of a file that looks like your resume is not
  // acceptable even when it is the right call.
  withTempDir((dir) => {
    touch(dir, 'Old_Name_Resume.pdf', 'resume-color.pdf');
    const seen = [];
    const removed = on.pruneStale(dir, 'resume', [], name => seen.push(name));
    assertEq(seen.sort().join(','), 'Old_Name_Resume.pdf,resume-color.pdf',
             'both announced');
    assertEq(seen.length, removed.length, 'one callback per removal');
  });
});


/* ─── against the real project ────────────────────────────────── */

test('the constants the two languages share are present and sane', () => {
  assertEq(on.DOC_SUFFIX.resume, 'Resume', 'resume suffix');
  assertEq(on.DOC_SUFFIX.letter, 'Cover_Letter', 'letter suffix');
  assertEq(on.SEPARATOR, '_', 'separator');
  assertEq(on.GRAYSCALE_SUFFIX, '_Grayscale', 'grayscale suffix');
  // The grayscale marker shares the separator with the name parts.
  // That is safe only because DOC_SUFFIX always ends the stem, so the
  // marker can only ever appear after it — never inside a name.
  assertTrue(!on.DOC_SUFFIX.resume.includes(on.GRAYSCALE_SUFFIX),
             'the suffixes do not overlap');
  assertTrue(!on.DOC_SUFFIX.letter.includes(on.GRAYSCALE_SUFFIX),
             'nor for the letter');
  assertTrue(!on.RETIRED_GRAYSCALE_SUFFIXES.includes(on.GRAYSCALE_SUFFIX),
             'the current spelling is not listed as retired');
});

test('the pipeline hands back paths that match this module', () => {
  // The integration point: pipeline.paths.colorPdf is a getter over
  // outputPaths, and resume.js prunes with the result. If these two
  // ever disagreed, a build would delete the file it just wrote.
  const { createPipeline } = require(path.join(ROOT, 'build', 'pipeline.js'));
  const noPython = { buildHtml() {}, cropPdf() {} };
  for (const variant of ['resume', 'letter']) {
    const p = createPipeline({ root: ROOT, python: noPython, variant }).paths;
    const expected = on.outputPaths(path.join(ROOT, 'dist'), p.pdfMeta, variant);
    assertEq(p.colorPdf, expected.colorPdf, `${variant} color`);
    assertEq(p.grayscalePdf, expected.grayscalePdf, `${variant} grayscale`);
    assertTrue(on.outputPattern(variant).test(path.basename(p.colorPdf)),
               `${variant} color matches the prune pattern`);
    assertTrue(on.outputPattern(variant).test(path.basename(p.grayscalePdf)),
               `${variant} grayscale matches the prune pattern`);
  }
});

report('output_name');
