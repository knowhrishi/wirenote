# AGENTS.md

Instructions for AI coding agents working on Wirenote, in the [AGENTS.md](https://agents.md) format. They match [CONTRIBUTING.md](CONTRIBUTING.md), which human contributors read first; where the two differ, CONTRIBUTING.md wins.

## Project overview

Wirenote is a Linux daemon and a local web app that turn calls into meeting notes. It notices when an app opens the microphone (PipeWire), records the mic and the system audio as two tracks, transcribes them with faster-whisper, tells speakers apart with sherpa-onnx, and summarizes with a local model through Ollama or, opt-in, Claude. Everything runs on the user's computer by default.

- Python 3.11 to 3.14, Linux only, packaged with uv and hatchling.
- License: AGPL-3.0-or-later. Contributions need the one-time [CLA](CLA.md).

| Path | What it holds |
| --- | --- |
| `src/wirenote/cli.py` | The `wirenote` command and its subcommands |
| `src/wirenote/detector/` | Call detection on PipeWire and the Record / Ignore state machine |
| `src/wirenote/capture/` | Two-track recording to WAV |
| `src/wirenote/process/` | Transcription, speakers, merging the tracks, summaries, notes, the job worker |
| `src/wirenote/ui/` | The meetings web app: server, data access, `static/` front end |
| `src/wirenote/config/` | Settings, call-app rules (`apps.py`), secrets, file permissions, migrations |
| `src/wirenote/calendar/`, `notify/`, `net/` | ICS calendars, desktop notifications and clipboard, loopback peer checks |
| `src/wirenote/tools/` | `setup`, `doctor`, `calendar`, `dump-streams` |
| `src/wirenote/data/` | systemd unit and desktop entry templates |
| `tests/` | pytest suite; `tests/replay/` holds scrubbed PipeWire traces |
| `scripts/` | Replay trace maker, release notes check |
| `docs/usage.md` | User documentation |
| `docs/dev/` | Design notes; the defaults table in `docs/dev/PLAN.md` is what thresholds in the code refer to |

## Setup and commands

```sh
uv sync                              # install the project and the dev group
uv run pytest -q                     # all tests
uv run pytest tests/test_tracker.py  # one file
uv run pyflakes src tests scripts    # lint, the same paths as CI
uv build                             # wheel and source archive
```

CI (`.github/workflows/ci.yml`) runs the tests on Python 3.11 to 3.14, pyflakes, `pip-audit` on the locked dependencies and `gitleaks` on the whole history. Run the tests and pyflakes before every commit.

- Change dependencies with `uv add` / `uv remove` and commit `uv.lock`; never edit `uv.lock` by hand.
- `wirenote setup install` and `wirenote setup uninstall --purge` change the developer's own systemd services and data. Do not run them, or the daemon, unless asked.

## Code style

- Every Python file starts with the license header; `tests/test_license_headers.py` checks it:
  ```python
  # SPDX-License-Identifier: AGPL-3.0-or-later
  # Copyright (c) 2026 Hrishikesh Jadhav
  ```
- Then a module docstring saying what the module does, pointing to its design note where there is one (for example `docs/dev/PLAN.md › Processing`), and `from __future__ import annotations`.
- Type hints on public functions; dataclasses for plain records.
- No formatter is enforced. Match the surrounding code, keep lines under 120 characters and keep pyflakes clean.
- Comments say why, not what.
- A new default or threshold goes into the defaults table in `docs/dev/PLAN.md` in the same change.
- Docs and user-facing text use short, plain sentences and speak to the user as "you".

## Testing

- Every change comes with a test that fails without it.
- Tests run without PipeWire, a GPU, the network or the real home folder. An autouse fixture in `tests/conftest.py` gives each test its own `HOME` and XDG folders and sets `HF_HUB_OFFLINE=1`; never work around it. Use the fakes in `tests/conftest.py` (`FakeProc`, `replay()`).
- For detector changes, replay a trace from `tests/replay/`. New traces are made with `scripts/make_replay.py`; raw captures stay in the ignored `tests/fixtures/` because they hold host, user and device names.
- `tests/test_docs.py` checks that the tracked tree has no private words and that `PRIVACY.md` contains the app's cloud disclosure text.

## Privacy and security

Wirenote handles other people's voices and words, so these rules come before any feature:

- Nothing leaves the computer by default. Anything that sends data out must be opt-in and listed in `PRIVACY.md` and in `CLOUD_DISCLOSURE` in `src/wirenote/ui/data.py`.
- Never commit recordings, transcripts, notes, calendar links, API keys, real names, host names or room names, in code, tests, docs or commit messages. Use invented data.
- Keep files private to their user (umask 077, `config/perms.py`), the web app on 127.0.0.1 serving only its own user, and secrets out of command lines and logs.
- Report security problems privately as described in [SECURITY.md](SECURITY.md), never in a public issue.

## Branch names

Name each branch for the change it carries, not for who or what made it:

```
<type>/<short-description>
<type>/<issue>-<short-description>
```

| Type | For |
| --- | --- |
| `feat` | A new feature or user-visible behaviour |
| `fix` | A bug fix |
| `docs` | Documentation only |
| `refactor` | A code change that neither fixes a bug nor adds a feature |
| `perf` | A performance improvement |
| `test` | Tests only |
| `ci` | GitHub workflows and other CI configuration |
| `build` | Packaging and dependencies |
| `chore` | Anything else that changes no shipped behaviour |
| `release` | Preparing a release, for example `release/0.3.0` |

- The description is 2 to 5 lowercase words joined by hyphens; use only `a-z`, `0-9` and `-`, and keep the whole name under 50 characters.
- No tool, agent, model or person in the name (`claude`, `codex`, `copilot`, `cursor`, a username), and no random suffixes, hashes or dates.
- Examples: `feat/open-call-on-stop`, `fix/notes-stale-tab-overwrite`, `fix/42-pipewire-reconnect`, `ci/e2e-release-pipeline`.

Tools often create branches named after themselves, such as `claude/some-name-1a2b3c`. Rename the branch before its first push:

```sh
git branch -m <type>/<short-description>
```

If it is already pushed but has no pull request, push the new name and delete the old one with `git push origin --delete <old-name>`. Once a pull request is open, keep the name: deleting a pull request's branch closes it.

## Commit messages

- **Subject:** one line under 72 characters saying what changes, without a trailing period. An area prefix is welcome (`Notes: …`, `CI: …`, `README: …`). Release commits read `Release X.Y.Z: <headline>`.
- **Body:** after a blank line, wrapped at 72 characters: why the change was needed and what the user notices.
- **Trailers:** sign off with `git commit -s`. A commit an agent wrote also carries `Co-Authored-By:` naming the model.
- One logical change per commit.

## Pull requests

- One change per pull request, into `main`. Fill in the checklist in `.github/pull_request_template.md`.
- A user-visible change gets an entry in `CHANGELOG.md` under `## [Unreleased]` (add the heading if it is missing), in the Keep a Changelog groups: Added, Changed, Fixed, Security. Users read these in the app after an update, so write what they notice, not how it was done, and add "What you need to do: …" when they must act. Update `docs/usage.md` when commands, settings or behaviour change.
- CI (`changelog.yml`) fails a pull request that changes `src/` without changing `CHANGELOG.md`, unless it has the `no-changelog` label.
- To support a new call app, add a rule to the defaults in `src/wirenote/config/apps.py` and say how you checked it (`wirenote dump-streams` during a call).
- Agents do not push to `main`, create tags or publish releases. Releases are the maintainer's job (`docs/dev/releasing.md`).
