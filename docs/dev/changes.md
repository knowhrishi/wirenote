# Changing Wirenote safely

Wirenote runs unattended on people's computers, records other people, and keeps months of notes. A careless change can cost someone a meeting, show a recording to someone else, or make their old notes unreadable. These rules keep changes from doing that as the project grows. They come on top of [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Before you start

- **One change per pull request.** A feature and the refactor it needs are two pull requests, the refactor first, with no change in behaviour.
- **Changes that touch a contract (below), the privacy promises or more than one milestone area get a short design note first**, in `docs/dev/`, like the existing ones: what changes, what stays, open questions.
- **Thresholds and defaults come from docs/dev/PLAN.md › Defaults.** A new one goes into that table, and the code cites it.

## Contracts: what other people's data and tools depend on

Code can change freely. What has been written to disk, or what someone else calls, cannot. Each row says what must keep working after an upgrade.

| Contract | Where | Rule |
|---|---|---|
| `config.toml` keys | `config/settings.py` | Unknown keys are refused, so **renaming or removing a key breaks every config that sets it.** Add keys with a default equal to the old behaviour. To rename one, accept both names for at least one release and say so in CHANGELOG.md. |
| `apps.toml` fields | `config/apps.py` | Same as config keys. Users' copies are never rewritten, so a change to the default rules reaches only new installs; say so in the changelog. |
| `manifest.json`, `transcript.json`, `summary.json` | `capture/recorder.py`, `process/worker.py` | Add fields; never rename or remove one, and never change a field's meaning. Readers use `.get()` with a default. A breaking change raises `"version"` in the manifest and comes with code that reads the old version. |
| `jobs.db` | `process/jobs.py` | `PRAGMA user_version` counts the steps in `MIGRATIONS` that have run. **Change the schema by appending a step; never edit one that has shipped.** Reads name their columns, so add columns rather than rename or drop them: an older Wirenote then still reads a newer database. |
| Note files | `process/note.py` | People edit notes and point other tools at them. Front matter keys and section headings stay; new keys go at the end. Notes are never deleted, not even by `uninstall --purge`. |
| Folders | `config/settings.py`, `config/migrate.py` | A move goes through a migration like `migrate.py`: copy, check, then delete the old folder. |
| CLI commands and flags, control-socket commands | `cli.py`, `detector/daemon.py` | Scripts, desktop shortcuts and the web app call these. Keep old names working; mark them deprecated in `--help` for one release before removing them. |
| systemd unit names, `.desktop` file | `data/`, `tools/install.py` | `setup install` must clean up old names (see `OLD_UNITS`). |
| `detector.jsonl` events | `detector/daemon.py` | The tests, `doctor` and bug reports read them. Add events and fields freely. Rename one only together with every reader. |
| Web API | `ui/server.py` | Only the bundled page uses it, so it may change, but together with `app.js` in the same pull request. |

**`tests/test_compat.py` enforces the stored-data rows.** `tests/compat/<version>/` keeps what each release wrote: a recording without audio, `jobs.db`, a config that sets every key, and the default `apps.toml`. Every release adds its own (docs/dev/releasing.md). A failure there means users who upgrade would lose something. Fix the code, not the snapshot. Snapshots are never edited after their release.

## Privacy and security gates

Check these on every pull request. On every push, the end-to-end test checks part of them for one real flow: what reaches the summary backend, that the log holds none of the call's words, and the file modes. Unit tests in `test_peer.py` and `test_ui.py` cover the refusals.

- **Nothing new leaves the computer.** If something must, it is opt-in, off by default, listed in PRIVACY.md and in the app's disclosure text, and the call is marked in the UI ("Local" or "Sent to Claude").
- **Logs never hold room names, page or calendar titles, transcript text, note paths or speaker names.** Use the hashes and counts the daemon already uses (`room_hash`).
- **Files stay private to the user** (umask 077, `config/perms.py`).
- **The summary backend gets only loopback servers owned by this user, or root, or a system account** (`net/peer.py`).
- **The web app serves only this user,** behind the one-time login and the write token.
- **Text from outside reaches notifications, notes and HTML only escaped.** Outside text means page titles, calendar events and model output.
- **Recording needs a click,** and the "Recording" notice stays up for as long as it runs.

## New behaviour: ship it switched off, then switch it on

A change in what Wirenote does on its own (when it prompts, what it records, what it deletes, where it sends data) goes out in two releases:

1. **The code ships behind a setting whose default keeps the old behaviour.** The changelog says how to try it.
2. **A later release flips the default,** once the detector logs or issue reports show it works.

Opt-in features, such as a new summary backend, simply stay off by default. This means a bad idea costs one setting, not a hotfix.

## Which test each change needs

| You change… | Add or update |
|---|---|
| a rule inside one module | a unit test that fails without the change |
| detection: a call app, a desktop, a PipeWire quirk | a replay trace in `tests/replay/` (`scripts/make_replay.py`) and a replay test |
| the daemon, the recorder, the worker or the web app's flow | the end-to-end test, if the change is on its path; see docs/dev/testing.md |
| anything stored on disk, or a setting | `tests/test_compat.py` must still pass, untouched |
| what goes into the wheel (data files, static files, dependencies) | `scripts/test_wheel.sh` locally; CI runs it too |
| a dependency of the speech pipeline | run the real-model test: `uv run pytest -m models`, or the "Real models" workflow by hand |

## Dependencies

- **Everything is locked in `uv.lock`.** CI installs with `--locked`, so a change to `pyproject.toml` without `uv lock` fails.
- **Dependabot proposes updates weekly.** A pull request for faster-whisper, CTranslate2, onnxruntime or sherpa-onnx should not be merged until the "Real models" workflow has passed on its branch (run it from the Actions tab).
- **`pip-audit` blocks known vulnerabilities, and gitleaks blocks secrets,** on every push.
- **A new dependency needs a reason in the pull request,** its license added to THIRD_PARTY.md, and no network use at import time.

## Releases

The release workflow re-runs every check on the tagged commit, plus the real models, then builds the wheel and tests that exact file on Python 3.11 to 3.14. Only then does it publish, after your approval (docs/dev/releasing.md).

- **Risky releases go out as a release candidate first** (`X.Y.Zrc1`): PyPI marks it as a pre-release, and plain installs skip it.
- **Before tagging, do a dry run:** Actions › Release › Run workflow, on `main`. It runs everything except publishing.
- **There is no rollback on PyPI,** because a version number can be uploaded only once. Fix forward with the next patch version, and yank the broken one so new installs skip it.
- **Every user-visible change gets a line under `## [Unreleased]` in CHANGELOG.md in the same pull request.** The release only dates the section.
