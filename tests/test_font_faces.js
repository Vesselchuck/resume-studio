/**
 * test_font_faces.js — every piece of text is drawn from a font file
 * cut for it.
 *
 * WHAT THIS GUARDS
 * ----------------
 * The fonts are static: one file per weight in use, and for Newsreader
 * one per size in use, cut from the variable fonts by
 * build/make_static_fonts.py (see styles/_fonts.scss for why). CSS
 * never complains about a weight or a size that has no file. It draws
 * the nearest weight it has, or thickens one artificially, and a
 * Newsreader face cut for another size simply looks slightly wrong.
 * So a stylesheet change that asks for, say, weight 700 on body text
 * would pass every other test and quietly change the document. This
 * suite builds both documents from the templates and checks, for every
 * visible piece of text:
 *
 *   • its first font family has an @font-face with exactly its weight,
 *     and that face is loaded (not a system font, not a fallback);
 *   • Newsreader text uses the face cut for its size: the family is
 *     named after its optical size, which must equal the font size in
 *     CSS px (what Chromium set the axis to with the variable font);
 *   • text in a tracked cut ('Manrope Tracked' and the headings'
 *     Newsreader, their letter-spacing built into the font) has no
 *     letter-spacing of its own on top;
 *   • every @font-face in the stylesheet points at a woff2 in fonts/
 *     that exists, and no face is left unused;
 *   • no woff2 in fonts/ kerns any glyph against the space or the
 *     no-break space, in GPOS or in a legacy kern table: kerned, the
 *     letter overlaps the space and pdftotext -raw read "ESL
 *     Classrooms" as "ESLClassrooms" (build/make_static_fonts.py, NO
 *     KERNING AGAINST A SPACE). The files are read with Node's own
 *     brotli, so this needs no fontTools.
 *
 * The résumé is the template plus one details block, the example the
 * template's comments give: the template itself has none, and without
 * one the labels' face ('Manrope Tracked' 600) would go unchecked and
 * count as unused.
 *
 * tests/test_pdf_fonts.py checks the other half: that the PDFs embed
 * them as TrueType, not Type 3.
 *
 * Runs in a throwaway copy of the project (tests/_project.js). Needs
 * Chromium; without it this prints the runner's SKIP marker.
 */

const path = require('path');
const fs = require('fs');
const zlib = require('zlib');
const { pathToFileURL } = require('url');
const { assertEq, assertTrue, fail, report } = require('./_framework');
const { tempProject, realDistFingerprint } = require('./_project');

const SUITE = 'test_font_faces';

/** A details block, put first in the sidebar of the template's data. */
const DETAILS = [
  '    - id: online',
  '      type: details',
  '      heading: Online',
  '      rows:',
  '        - label: LinkedIn',
  '          value: linkedin.com/in/gcaesar',
  '          href: "https://linkedin.com/in/gcaesar"',
  '        - label: Security Clearance',
  '          value: Secret',
];

/** Write data/resume.yml in the copy: the template plus DETAILS. */
function withDetailsBlock(root) {
  const data = path.join(root, 'data');
  const yml = fs.readFileSync(path.join(data, 'resume_default.yml'), 'utf-8');
  const eol = yml.includes('\r\n') ? '\r\n' : '\n';
  const out = yml.replace(/^sidebar:\r?\n {2}blocks:\r?\n/m, m => m + DETAILS.join(eol) + eol);
  if (out === yml) throw new Error("resume_default.yml has no 'sidebar:' / 'blocks:' to add a details block to");
  // resume.yml wins over the template when no data source is set.
  fs.writeFileSync(path.join(data, 'resume.yml'), out);
}

/** What the page draws its text with, and what faces it has. */
function inspect() {
  const uses = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (!node.textContent.trim()) continue;
    const el = node.parentElement;
    const c = getComputedStyle(el);
    if (c.display === 'none' || c.visibility === 'hidden') continue;
    uses.push({
      where: `${el.tagName.toLowerCase()}${el.className ? '.' + String(el.className).split(' ').join('.') : ''}`,
      family: c.fontFamily.split(',')[0].trim().replace(/^['"]|['"]$/g, ''),
      weight: c.fontWeight,
      style: c.fontStyle,
      px: parseFloat(c.fontSize),
      letterSpacing: c.letterSpacing,
    });
  }
  const faces = [...document.fonts].map(f => ({
    family: f.family.replace(/^['"]|['"]$/g, ''), weight: f.weight, style: f.style, status: f.status,
  }));
  return { uses, faces };
}

/** WOFF2's known table tags, by the index its table directory stores. */
const WOFF2_TAGS = ('cmap head hhea hmtx maxp name OS/2 post cvt  fpgm glyf loca prep CFF  VORG EBDT EBLC '
  + 'gasp hdmx kern LTSH PCLT VDMX vhea vmtx BASE GDEF GPOS GSUB EBSC JSTF MATH CBDT CBLC COLR CPAL SVG  '
  + 'sbix acnt avar bdat bloc bsln cvar fdsc feat fmtx fvar gvar hsty just lcar mort morx opbd prop trak '
  + 'Zapf Silf Glat Gloc Feat Sill').match(/.{4}\s?/g).map(t => t.slice(0, 4));

/** The tables of a WOFF2 file, by tag (glyf, loca and hmtx may be transformed: not used here). */
function woff2Tables(file) {
  const buf = fs.readFileSync(file);
  if (buf.toString('latin1', 0, 4) !== 'wOF2') throw new Error(`${file} is not WOFF2`);
  const numTables = buf.readUInt16BE(12);
  let at = 48;
  const base128 = () => {
    let value = 0;
    for (let i = 0; i < 5; i++) {
      const b = buf[at++];
      value = value * 128 + (b & 0x7f);
      if (!(b & 0x80)) return value;
    }
    throw new Error(`${file}: bad UIntBase128`);
  };
  const entries = [];
  for (let i = 0; i < numTables; i++) {
    const flags = buf[at++];
    let tag = WOFF2_TAGS[flags & 0x3f];
    if ((flags & 0x3f) === 0x3f) { tag = buf.toString('latin1', at, at + 4); at += 4; }
    const version = flags >> 6;
    let length = base128();
    const transformed = (tag === 'glyf' || tag === 'loca') ? version === 0 : version !== 0;
    if (transformed) length = base128();
    entries.push({ tag, length });
  }
  const data = zlib.brotliDecompressSync(buf.subarray(at, at + buf.readUInt32BE(20)));
  const tables = {};
  let offset = 0;
  for (const { tag, length } of entries) {
    tables[tag] = data.subarray(offset, offset + length);
    offset += length;
  }
  if (offset !== data.length) throw new Error(`${file}: the tables do not add up to the decompressed data`);
  return tables;
}

/** The glyph ID cmap gives a code point (format 4 or 12), or 0. */
function cmapGlyph(cmap, cp) {
  const subtables = [];
  for (let i = 0; i < cmap.readUInt16BE(2); i++) {
    const platform = cmap.readUInt16BE(4 + 8 * i), encoding = cmap.readUInt16BE(4 + 8 * i + 2);
    // Unicode subtables only: platform 0, or Windows BMP (1) and full (10).
    if (platform === 0 || (platform === 3 && (encoding === 1 || encoding === 10))) {
      subtables.push(cmap.readUInt32BE(4 + 8 * i + 4));
    }
  }
  for (const s of subtables) {
    const format = cmap.readUInt16BE(s);
    if (format === 12) {
      for (let g = 0, n = cmap.readUInt32BE(s + 12); g < n; g++) {
        const p = s + 16 + 12 * g;
        const start = cmap.readUInt32BE(p);
        if (cp >= start && cp <= cmap.readUInt32BE(p + 4)) return cmap.readUInt32BE(p + 8) + cp - start;
      }
    } else if (format === 4) {
      const segs = cmap.readUInt16BE(s + 6) / 2;
      const ends = s + 14, starts = ends + 2 * segs + 2, deltas = starts + 2 * segs, ranges = deltas + 2 * segs;
      for (let i = 0; i < segs; i++) {
        if (cp > cmap.readUInt16BE(ends + 2 * i)) continue;
        const start = cmap.readUInt16BE(starts + 2 * i);
        if (cp < start) break;
        const delta = cmap.readUInt16BE(deltas + 2 * i);
        const range = cmap.readUInt16BE(ranges + 2 * i);
        if (!range) return (cp + delta) & 0xffff;
        const g = cmap.readUInt16BE(ranges + 2 * i + range + 2 * (cp - start));
        return g ? (g + delta) & 0xffff : 0;
      }
    }
  }
  return 0;
}

/** The glyphs of an OpenType Coverage table, in coverage order. */
function coverage(t, at) {
  const out = [];
  const n = t.readUInt16BE(at + 2);
  if (t.readUInt16BE(at) === 1) {
    for (let i = 0; i < n; i++) out.push(t.readUInt16BE(at + 4 + 2 * i));
  } else {
    for (let i = 0; i < n; i++) {
      const p = at + 4 + 6 * i;
      for (let g = t.readUInt16BE(p); g <= t.readUInt16BE(p + 2); g++) out.push(g);
    }
  }
  return out;
}

/** An OpenType ClassDef table as a function from glyph to class (0 when not listed). */
function classDef(t, at) {
  const map = new Map();
  if (t.readUInt16BE(at) === 1) {
    const start = t.readUInt16BE(at + 2);
    for (let i = 0, n = t.readUInt16BE(at + 4); i < n; i++) map.set(start + i, t.readUInt16BE(at + 6 + 2 * i));
  } else {
    for (let i = 0, n = t.readUInt16BE(at + 2); i < n; i++) {
      const p = at + 4 + 6 * i;
      for (let g = t.readUInt16BE(p); g <= t.readUInt16BE(p + 2); g++) map.set(g, t.readUInt16BE(p + 4));
    }
  }
  return g => map.get(g) || 0;
}

/**
 * Kerning pairs with any of `glyphs` on either side that move a glyph:
 * GPOS pair adjustments (formats 1 and 2, inside Extension lookups too)
 * and legacy kern format 0 pairs. Also counts the pair subtables read,
 * so a reader that found none cannot pass for a clean font.
 */
function spacePairs(tables, glyphs) {
  const found = [];
  let subtablesRead = 0;
  const gpos = tables.GPOS;
  const words = vf => { let n = 0; for (let f = vf; f; f >>= 1) n += f & 1; return n; };
  const moves = (at, n) => { for (let i = 0; i < n; i++) if (gpos.readUInt16BE(at + 2 * i)) return true; return false; };
  if (gpos) {
    const list = gpos.readUInt16BE(8);
    for (let l = 0, nl = gpos.readUInt16BE(list); l < nl; l++) {
      const lookup = list + gpos.readUInt16BE(list + 2 + 2 * l);
      const type = gpos.readUInt16BE(lookup);
      for (let s = 0, ns = gpos.readUInt16BE(lookup + 4); s < ns; s++) {
        let sub = lookup + gpos.readUInt16BE(lookup + 6 + 2 * s);
        let subType = type;
        if (type === 9) { subType = gpos.readUInt16BE(sub + 2); sub += gpos.readUInt32BE(sub + 4); }
        if (subType !== 2) continue;
        subtablesRead++;
        const format = gpos.readUInt16BE(sub);
        const firsts = coverage(gpos, sub + gpos.readUInt16BE(sub + 2));
        const n1 = words(gpos.readUInt16BE(sub + 4)), n2 = words(gpos.readUInt16BE(sub + 6));
        const record = n1 + n2;
        if (format === 1) {
          firsts.forEach((first, i) => {
            const set = sub + gpos.readUInt16BE(sub + 10 + 2 * i);
            for (let r = 0, nr = gpos.readUInt16BE(set); r < nr; r++) {
              const p = set + 2 + 2 * (1 + record) * r;
              const second = gpos.readUInt16BE(p);
              if ((glyphs.has(first) || glyphs.has(second)) && moves(p + 2, record)) {
                found.push(`lookup ${l}: ${first} ${second}`);
              }
            }
          });
        } else if (format === 2) {
          const class1 = classDef(gpos, sub + gpos.readUInt16BE(sub + 8));
          const class2 = classDef(gpos, sub + gpos.readUInt16BE(sub + 10));
          const c1Count = gpos.readUInt16BE(sub + 12), c2Count = gpos.readUInt16BE(sub + 14);
          const at = (c1, c2) => sub + 16 + 2 * record * (c1 * c2Count + c2);
          const firstClasses = new Set(firsts.map(class1));
          for (const g of glyphs) {
            if (firsts.includes(g)) {
              for (let c2 = 0; c2 < c2Count; c2++) {
                if (moves(at(class1(g), c2), record)) found.push(`lookup ${l}: ${g} before class ${c2}`);
              }
            }
            for (const c1 of firstClasses) {
              if (c1 < c1Count && moves(at(c1, class2(g)), record)) found.push(`lookup ${l}: class ${c1} before ${g}`);
            }
          }
        }
      }
    }
  }
  const kern = tables.kern;
  if (kern && kern.readUInt16BE(0) === 0) {
    let at = 4;
    for (let s = 0, ns = kern.readUInt16BE(2); s < ns; s++) {
      const length = kern.readUInt16BE(at + 2);
      subtablesRead++;
      if (kern.readUInt16BE(at + 4) >> 8 !== 0) {
        found.push(`kern subtable ${s}: a format this test cannot read`);
      } else {
        for (let p = 0, np = kern.readUInt16BE(at + 6); p < np; p++) {
          const q = at + 14 + 6 * p;
          if ((glyphs.has(kern.readUInt16BE(q)) || glyphs.has(kern.readUInt16BE(q + 2))) && kern.readInt16BE(q + 4)) {
            found.push(`kern: ${kern.readUInt16BE(q)} ${kern.readUInt16BE(q + 2)}`);
          }
        }
      }
      at += length;
    }
  } else if (kern) {
    found.push('a kern table of a version this test cannot read');
  }
  return { found, subtablesRead };
}

/** No font in fonts/ kerns anything against a space (build/make_static_fonts.py). */
function checkSpaceKerning(fontsDir) {
  const files = fs.readdirSync(fontsDir).filter(f => f.endsWith('.woff2'));
  assertTrue(files.length > 0, 'fonts/ holds the static cuts');
  for (const file of files) {
    const tables = woff2Tables(path.join(fontsDir, file));
    const spaces = new Set([0x20, 0xa0].map(cp => cmapGlyph(tables.cmap, cp)).filter(Boolean));
    assertTrue(spaces.size > 0, `${file}: cmap maps the space`);
    const { found, subtablesRead } = spacePairs(tables, spaces);
    assertTrue(subtablesRead > 0, `${file}: its kerning was read (${subtablesRead} pair subtables)`);
    assertEq(found.length, 0, `${file}: no glyph is kerned against a space (glyph IDs: ${found.slice(0, 5).join('; ')})`);
  }
}

async function checkPage(page, file, usedFaces) {
  await page.goto(pathToFileURL(file).href);
  await page.evaluate(() => document.fonts.ready);
  const { uses, faces } = await page.evaluate(inspect);
  const name = path.basename(file);
  assertTrue(uses.length > 10, `${name}: text found to check (${uses.length} pieces)`);

  const problems = [];
  const seen = new Set();
  for (const u of uses) {
    const key = `${u.family} ${u.weight} ${u.style} ${u.px}px ${u.letterSpacing}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const face = faces.find(f => f.family === u.family && f.weight === u.weight && f.style === 'normal');
    if (!face) {
      problems.push(`${u.where}: ${u.family} at weight ${u.weight} has no @font-face (${key})`);
      continue;
    }
    usedFaces.add(`${face.family} ${face.weight}`);
    if (face.status !== 'loaded') problems.push(`${u.where}: ${u.family} ${u.weight} is ${face.status}`);
    if (u.style !== 'normal' && u.family !== 'Manrope') {
      problems.push(`${u.where}: ${u.style} ${u.family} would be slanted by the browser`);
    }
    if (u.family.endsWith('Tracked') && !['normal', '0px'].includes(u.letterSpacing)) {
      problems.push(`${u.where}: ${u.family} carries its tracking; letter-spacing ${u.letterSpacing} doubles it`);
    }
    if (u.family.startsWith('Newsreader')) {
      const m = /^Newsreader opsz([\d.]+)(?: Tracked)?$/.exec(u.family);
      if (!m) problems.push(`${u.where}: ${u.family} is not one of the per-size Newsreader faces`);
      else if (Math.abs(Number(m[1]) - u.px) > 0.01) {
        problems.push(`${u.where}: drawn at ${u.px.toFixed(2)}px with the face cut for ${m[1]}px`);
      }
    }
  }
  assertEq(problems, [], `${name}: every text is drawn from a face cut for its weight and size`);
}

(async () => {
  let chromium;
  try {
    ({ chromium } = require('playwright'));
    const b = await chromium.launch();
    await b.close();
  } catch (err) {
    console.log(`SKIP ${SUITE}: chromium unavailable (${String(err.message).split('\n')[0]})`);
    return;
  }

  const before = realDistFingerprint();
  const project = tempProject('font-faces-test');
  let engine;
  let browser;
  try {
    withDetailsBlock(project.root);
    const { createEngine } = project.require('build/engine');
    engine = await createEngine({ root: project.root });
    await engine.renderPreview({ doc: 'resume' });
    await engine.renderPreview({ doc: 'letter' });

    // Every face the stylesheet declares, and its file.
    const css = fs.readFileSync(path.join(project.dist, 'styles.css'), 'utf-8');
    const declared = [...css.matchAll(/@font-face\s*{([^}]*)}/g)].map(([, body]) => ({
      family: /font-family:\s*['"]?([^;'"]+)/.exec(body)[1],
      weight: /font-weight:\s*([^;]+)/.exec(body)[1].trim(),
      src: /url\(['"]?([^'")]+)/.exec(body)[1],
    }));
    assertTrue(declared.length > 0, 'the stylesheet declares its fonts');
    for (const d of declared) {
      const file = path.resolve(project.dist, d.src);
      assertTrue(fs.existsSync(file) && file.endsWith('.woff2'),
        `${d.family} ${d.weight}: ${d.src} is a woff2 in fonts/`);
      assertTrue(/^\d+$/.test(d.weight), `${d.family}: one weight per file, not a range (${d.weight})`);
    }
    checkSpaceKerning(path.join(project.root, 'fonts'));

    browser = await chromium.launch();
    const page = await browser.newPage();
    const usedFaces = new Set();
    await checkPage(page, path.join(project.dist, 'index.html'), usedFaces);
    assertTrue(usedFaces.has('Manrope Tracked 600'), 'the details block drew its labels');
    await checkPage(page, path.join(project.dist, 'letter.html'), usedFaces);

    const unused = declared.map(d => `${d.family} ${d.weight}`).filter(k => !usedFaces.has(k));
    assertEq(unused, [], 'every declared face draws something in one of the documents');
  } catch (err) {
    fail('font faces run', { error: err.stack || err.message });
  } finally {
    if (browser) await browser.close();
    if (engine) await engine.dispose();
    project.remove();
  }
  assertEq(realDistFingerprint(), before, "isolation: nothing was written to this checkout's dist/");
  report();
})();
