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
 *   • text in a tracked cut ('Manrope Tracked', its letter-spacing
 *     built into the font) has no letter-spacing of its own on top;
 *   • every @font-face in the stylesheet points at a woff2 in fonts/
 *     that exists, and no face is left unused.
 *
 * tests/test_pdf_fonts.py checks the other half: that the PDFs embed
 * them as TrueType, not Type 3.
 *
 * Runs in a throwaway copy of the project (tests/_project.js). Needs
 * Chromium; without it this prints the runner's SKIP marker.
 */

const path = require('path');
const fs = require('fs');
const { pathToFileURL } = require('url');
const { assertEq, assertTrue, fail, report } = require('./_framework');
const { tempProject, realDistFingerprint } = require('./_project');

const SUITE = 'test_font_faces';

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
      const m = /^Newsreader opsz([\d.]+)$/.exec(u.family);
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

    browser = await chromium.launch();
    const page = await browser.newPage();
    const usedFaces = new Set();
    await checkPage(page, path.join(project.dist, 'index.html'), usedFaces);
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
