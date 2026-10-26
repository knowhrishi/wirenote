# Security

## Reporting a vulnerability

Please report security problems privately through GitHub: the repository's **Security** tab › **Report a vulnerability**. Only the maintainer can read these reports. Do not open a public issue for them.

Include what you found, how to reproduce it, and what an attacker could do with it. The maintainer aims to answer within 14 days. Once a fix is released, the advisory credits you unless you prefer otherwise.

## Supported versions

Only the latest minor release gets security fixes. Wirenote is a 0.x project, so update with `uv tool upgrade wirenote` and then `wirenote setup install`.

## What Wirenote defends against

Wirenote runs on one person's Linux computer. Its threat model:

- **Other users on the same computer.** Recordings, transcripts, notes, settings and secrets are private to your user (mode 600, folders 700). The meetings app listens on 127.0.0.1 but only serves connections from your own user, checked against the kernel's socket table, and only after a login through a one-time code that only your user can create. The daemon's control socket can only be opened by your user (mode 600, in your private runtime folder), and summaries go to a local Ollama only if it runs as your user.
- **Web pages in your browser.** The meetings app refuses cross-site requests and foreign `Host` and `Origin` headers, requires a write token (which only the logged-in page sees) and a `SameSite=Strict` session cookie for every change, and sends a strict Content Security Policy.
- **Calendar invites and call content.** Event titles, invitee names, room names and transcripts are untrusted text. They are escaped in notifications, in the web app and in the Markdown notes, and calendar feeds are limited in size, number of events and recurrences, and must use https.
- **Leaks through logs.** The event log does not contain room names, note paths, speaker names or calendar links.

Out of scope: an attacker who already runs code as your user, or has root, can read everything Wirenote can.

## For contributors

`pip-audit` and `gitleaks` run on every pull request. Never commit recordings, transcripts, calendar links, API keys or log files, even from test calls; test fixtures must be redacted (see `scripts/make_replay.py`).
