# Security policy

## Supported versions

Only the latest release gets security fixes. Resume Studio is in initial
development (0.x), so a fix ships as a new release rather than a patch
to an older one.

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
- a POST body is sent as `application/json`.

Worth reporting, for example:

- a way for a web page, a dropped file or a data file to run code or
  read files through the Studio;
- a way for real data from `data/` to end up in files that are
  committed or in the published PDFs' metadata unexpectedly;
- a vulnerable dependency that the project actually exercises.

Known issues that affect only a dependency's unused code are tracked
through Dependabot and don't need a report.
