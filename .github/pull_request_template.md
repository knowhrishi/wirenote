## What and why

<!-- One change per pull request. Link the issue it fixes, if any. -->

## Checklist

- [ ] A test fails without this change and passes with it (`uv run pytest -q`); [docs/dev/testing.md](../docs/dev/testing.md) says which tier.
- [ ] `uv run pyflakes src tests scripts` is clean.
- [ ] Stored data, settings, commands and notes from older versions still work ([docs/dev/changes.md › Contracts](../docs/dev/changes.md)); `tests/compat/` is untouched.
- [ ] New behaviour that acts on its own is behind a setting that defaults to the old behaviour.
- [ ] CHANGELOG.md has a line under `## [Unreleased]`, if a user would notice the change.
- [ ] New Python files start with the SPDX license header.
- [ ] Nothing new leaves the computer, or it is opt-in and listed in PRIVACY.md and the app's disclosure.
- [ ] No recordings, transcripts, calendar links, keys or real names in the diff.
