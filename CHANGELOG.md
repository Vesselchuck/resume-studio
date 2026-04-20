# Changelog

Every release of this project, newest first.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and version numbers follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html):
**MAJOR.MINOR.PATCH**.

The project is still in **initial development**, which SemVer marks
with a major version of **0**: the interface below can still change,
and nothing about it is promised yet. While the major version is 0,
this project follows the usual convention for that range:

- **MINOR** goes up when something you rely on stops working unless
  you change it,
- **PATCH** goes up for everything else — new features, fixes and
  adjustments that break nothing.

**1.0.0** will mean the interface is settled. From then on the rules
become the standard ones: a breaking change takes a new major version,
a new feature a minor one, and a fix a patch.

"Something you rely on" is this project's public interface, and it
has four parts:

1. **The data format** — the keys and structure of the YAML files you
   write.
2. **The commands** — what you run: the `node …` and `python …`
   invocations.
3. **The environment variables** the build reads.
4. **The outputs** — where the PDFs are written and what they are
   called.

A change to how the PDF *looks*, however large, is not a change to
that interface. Neither is an internal refactor.

Headings under each release: **Added** for new capability, **Changed**
for existing behavior that now works differently, **Removed** for what
is gone, **Fixed** for bugs, **Security** for what used to be exposed.
**Breaking** marks the changes that forced a new minor version.

---

## [0.1.0] — 2026-04-20

*The starting point: the resume design, as a hand-written web page.*

- **Added.** `index.html`, with the resume written directly into the
  markup, and `styles.css`: a two-column A4 layout in Montserrat. The
  sidebar holds details, key skills and languages; the main column
  holds a summary, work experience, education and professional
  development. Section headings are uppercase with a short rule under
  them, and each job shows its location on the right. Below 780px the
  columns stack.
- **Added.** Print CSS for A4. The browser decides where pages break.
- **Added.** `print.pdf`, printed from the page.

The design first existed only as a PDF made with an online resume
builder. This version recreates it in HTML and CSS, so the history
starts from source files.

[0.1.0]: #010--2026-04-20
