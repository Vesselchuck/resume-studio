# Resume

Version 0.2.0. See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

A resume written as a single web page: the content is typed directly
into `index.html`, and `styles.css` lays it out on screen and on paper.
There is no build step, no data file and nothing to install.

The content in this version is a placeholder (Gaius Caesar, with Latin
filler text). Replace it with your own by editing `index.html`.

## Files

```
.
├── index.html     The resume: header, sidebar and main column, in HTML
├── styles.css     Layout, typography, responsive rules and print rules
├── print.pdf      The page printed to PDF (US Letter, 3 pages)
├── CHANGELOG.md   What changed in each release
└── LICENSE        MIT
```

## Viewing it

Open `index.html` in a browser. The fonts, Fraunces for the name and
headings and Manrope for everything else, are loaded from Google
Fonts, so the page needs an internet connection to look as designed.
Offline, the browser falls back to Georgia and to Helvetica Neue or
Arial.

On screen the resume is a two-column grid: a sidebar with key skills
and languages, and a main column with the summary, work experience,
education and professional development. Below 780px wide the columns
stack, and below 520px the spacing tightens further.

## Printing it

Print the page from the browser, or save it as PDF from the print
dialog. The print rules in `styles.css` set:

- US Letter paper with 0.5in margins,
- a white background and exact colors (`print-color-adjust: exact`),
- 10.5pt body text and a tighter two-column grid.

Turn off the browser's own headers and footers in the print dialog;
the page does not suppress them. The browser decides where pages
break, so the break points can move when the content or the browser
changes.

`print.pdf` is the result of printing the current `index.html` with
Chromium.

## Editing

Everything is in `index.html`:

- **Header** — the name, and the location, phone and email rows in
  the `<address>` block.
- **Sidebar** — the Key Skills list (`.plain-list`) and the Languages
  list (`.lang-list`, a name and a level per entry).
- **Main column** — the summary paragraph, one `<article class="job">`
  per position, the education entry and the professional development
  list (`.pd-list`).

A position with no bullets, such as a career break, uses
`<article class="job job--note">`, which sets the title in muted
italics.

The design tokens (colors, type sizes, spacing) are CSS custom
properties at the top of `styles.css`.

## License

MIT. See [`LICENSE`](LICENSE).
