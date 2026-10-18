# Security policy

## Supported versions

Only the latest release gets security fixes. A fix ships as a new
release rather than a patch to an older one.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately instead:

1. Go to the repository's [**Security** tab](https://github.com/Vesselchuck/resume-studio/security).
2. Click **Report a vulnerability**.

Include what you found, how to reproduce it, and which version you
tested. You'll get a reply once the report has been read, and credit in
the CHANGELOG entry for the fix if you'd like it.

## What is in scope

Resume Studio runs on your own computer, and your data files never
leave the machine. Its server listens on 127.0.0.1 only, and it
answers a request only when:

- the `Host` header is `127.0.0.1:<port>` or `localhost:<port>` for
  the port it is listening on, which stops a DNS-rebinding page from
  reading it;
- the `Origin` header, if sent, is that same address and port, so a
  page from any other site or local port can't drive it;
- the `Sec-Fetch-Site` header, if a browser sends it, doesn't say
  `cross-site` or `same-site`, so another site, or a page on another
  port of 127.0.0.1, can't read even a plain GET; the one exception is
  a top-level link to the page itself, which gets no further than the
  page saying where to open the Studio from, even when the browser
  holds the Studio's cookie (see the token below);
- a POST or PUT body is sent as `application/json`.

These checks stop web pages, but a program that isn't a browser can
send whatever headers it likes. So the server also makes a random
token each time it starts (32 bytes from a cryptographically secure
random source) and refuses every request without it, apart from the
app's two fonts, which are the repository's own files. It compares
the token in constant time and answers a missing or wrong one with a
plain 403. The server never writes the token to a file; it prints it
on its standard output:

- the desktop app reads it from there through a pipe, opens the
  Studio's page with it and sends it with its own requests;
- `npm run ui` prints the page's address with the token in your
  terminal, and opens that address in your browser by passing it on
  the command line of the system's opener (`explorer.exe`, `open` or
  `xdg-open`).

Opening that address gives the browser a cookie holding the token
(`HttpOnly`, so the page's scripts can't read it) and takes the token
out of the address bar. The cookie is also `SameSite=Strict`, but that
does less than it sounds: the port is not part of a site, so to the
browser every server on 127.0.0.1 is the same site as the Studio, and
a link from a page on another local port does bring the cookie. That
is why the `Sec-Fetch-Site` check above refuses such a page load
whatever cookie it carries. It is a session cookie, and it is no use
once the Studio stops: the next start has a new token and refuses the
old one. The Studio's address without the token shows only a page
saying where to open it from.

The token stops programs that can reach 127.0.0.1 but can't read the
Studio's output or memory. Without the token they could use the
Studio the way the app does: read your data files, save new files into
`data/`, build, and change the app's settings. It doesn't stop
malware running under your own account or as an administrator, which
can read the token from the app's memory, the server's output or your
browser's cookies, and could read your data files directly anyway.

Programs run by another account on the same computer are stopped only
if that account can't open the project folder either. The token keeps
them out of the Studio, not out of the folder: an account that can
read `data/` reads your data files directly, and one that can write
the project's code changes what the Studio runs next time. On Windows
a folder made outside the user profiles, on a second drive for
instance, usually inherits `Authenticated Users: Modify` from the
drive, which lets every account on the computer do both. A folder
under your user profile (`%USERPROFILE%`) is open only to you,
administrators and the system. To check a folder, run
`icacls <folder>` in a terminal: if any group other than your own
account, `Administrators` and `SYSTEM` is listed with `(R)`, `(RX)`,
`(M)`, `(W)` or `(F)`, such as `Authenticated Users`, `Users` or
`Everyone`, other accounts can read the folder, and with `(M)`, `(W)`
or `(F)` change it. Keep the project under your profile, or restrict
the folder's inheritance, if that matters on your computer.

Two limits apply to `npm run ui` in an ordinary browser, not to the
desktop app:

- Cookies are kept per host, not per port. While the cookie lasts, the
  browser also sends it with requests to any other server on
  127.0.0.1, so a local server that isn't yours could learn the token
  that way. The desktop app's window goes only to the Studio.
- On Linux and macOS any account can usually see other accounts'
  command lines, and so the address that `npm run ui` hands to your browser,
  token included. Windows shows a command line only to its own account
  and administrators. `npm run ui:serve` opens nothing; open the
  address it prints yourself.

Worth reporting, for example:

- a way for a web page, a dropped file or a data file to run code or
  read files through the Studio;
- a way for real data from `data/` to end up in files that are
  committed or in the published PDFs' metadata unexpectedly;
- a vulnerable dependency that the project actually exercises.

Known issues that affect only a dependency's unused code are tracked
through Dependabot and don't need a report.
