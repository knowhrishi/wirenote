# Contributing to Wirenote

Thank you for helping. Bug reports, tested fixes, support for more call apps and desktops, and clearer docs are all welcome.

## Before you start

- **Bugs:** open an issue with your distribution, desktop, PipeWire version (`pipewire --version`) and the output of `wirenote doctor`. Never attach recordings, transcripts, calendar links or full logs: they contain other people's words and names.
- **Larger changes:** open an issue first, so we can agree on the approach before you spend time on it.
- **Security problems:** do not open an issue; see [SECURITY.md](SECURITY.md).

## Development setup

```sh
git clone https://github.com/knowhrishi/wirenote
cd wirenote
uv sync
uv run pytest -q
uv run pyflakes src tests
```

Run your checkout as the real thing with `uv tool install --editable --force .` and `wirenote setup install`.

The design notes in [docs/dev/](docs/dev/) explain why the detector, the capture and the processing work the way they do; [docs/dev/PLAN.md](docs/dev/PLAN.md) has the defaults table every threshold in the code refers to.

## Pull requests

- Keep each pull request to one change, with a test that fails without it. The tests run without PipeWire, a GPU or network access; keep it that way (see the fakes in `tests/`).
- Every Python file starts with the license header:
  ```python
  # SPDX-License-Identifier: AGPL-3.0-or-later
  # Copyright (c) 2026 Hrishikesh Jadhav
  ```
  If you add a file, use the same header; you keep the copyright in your changes (see the CLA below).
- If people using Wirenote would notice the change, add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md). It becomes the release notes.
- Anything that sends data off the computer must be opt-in and listed in [PRIVACY.md](PRIVACY.md) and in the app's disclosure text.
- To add a call app, add a rule to the defaults in `src/wirenote/config/apps.py` and say how you verified it (`wirenote dump-streams` during a call shows what the detector sees).

## Contributor License Agreement

Before your first pull request is merged, you sign the [CLA](CLA.md) once; a bot asks you on the pull request. It lets the maintainer also offer Wirenote under other terms (for example a supported edition for companies), while every accepted contribution stays available under an open source license.

## Code of conduct

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).
