# Resume

Version 0.1.0. See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

A resume written as a single web page: the content is typed directly
into `index.html`, and `styles.css` lays it out on screen and on paper.
There is no build step, no data file and nothing to install.

This is the design every later version reproduces. It first existed
only as a PDF made with an online resume builder; this version
recreates it in HTML and CSS so that the project's history starts from
source files.

The content is a placeholder (Gaius Caesar, with Latin filler text).
Replace it with your own by editing `index.html`.

## Files

```
.
├── index.html     The resume: name, sidebar and main column, in HTML
├── styles.css     Layout, typography, responsive rules and print rules
├── print.pdf      The page printed to PDF (A4, 2 pages)
├── CHANGELOG.md   What changed in each release
└── LICENSE        MIT
```

## Viewing it

Open `index.html` in a browser. The typeface, Montserrat, is loaded
from Google Fonts, so the page needs an internet connection to look as
designed. Offline, the browser falls back to Helvetica Neue or Arial.

On screen the resume is a white A4-wide sheet with two columns,
separated by a thin vertical rule:

- **Sidebar** — Details (address, phone, email), Key Skills and
  Languages.
- **Main column** — Summary, Work Experience, Education and
  Professional Development, separated by thin horizontal rules.

The name is set in capitals on two lines above a full-width rule.
Section headings are uppercase and letter-spaced, with a short dark
bar under each one. Below 780px wide the columns stack, and below
520px the type gets smaller and job locations move under the titles.

## Printing it

Print the page from the browser, or save it as PDF from the print
dialog. The print rules in `styles.css` set:

- A4 paper with a 10mm top margin, an 8mm bottom margin and 16mm side
  margins,
- a white background and exact colors (`print-color-adjust: exact`),
- no page break inside a sidebar block, a heading or a bullet.

Turn off the browser's own headers and footers in the print dialog;
the page does not suppress them. The browser decides where pages
break, so the break points can move when the content or the browser
changes.

`print.pdf` is the result of printing the current `index.html` with
Chromium.

## Editing

Everything is in `index.html`:

- **Name** — the two `<span>`s in `<h1 class="name">`, one per line.
- **Sidebar** — the Details list (`<dl class="details">`, a label and
  a value per row), and the Key Skills and Languages lists
  (`.plain-list`).
- **Main column** — the summary paragraph, one `<article class="job">`
  per position, the education entry and the professional development
  list (`.pd-list`).

Each position has a title, an optional location (`.job-location`,
shown on the right) and a date (`.job-date`). A position with no
bullets, such as a career break, uses `<article class="job job--note">`
and simply leaves the list out. `<strong>` inside a bullet sets the
key figure in bold.

The design tokens (colors, type sizes, spacing, column widths) are
CSS custom properties at the top of `styles.css`.

## License

MIT. See [`LICENSE`](LICENSE).
