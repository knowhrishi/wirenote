#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run the tests in a throwaway Linux container, with ffmpeg and espeak-ng installed.
# For working on macOS (most tests need Linux), or for a clean check before a release.
#
# Usage: scripts/test_in_linux.sh [pytest args...]
#   scripts/test_in_linux.sh                    # everything CI runs
#   scripts/test_in_linux.sh tests/e2e -m e2e   # end to end, real models included
#   WIRENOTE_E2E_KEEP=tests/compat/X.Y.Z scripts/test_in_linux.sh tests/e2e/test_call_to_note.py
#                                               # save this version's stored data (releasing.md)
#
# The checkout is mounted read-only and copied, so the container writes only to the
# WIRENOTE_E2E_KEEP folder, when one is given.
set -eu

# uv's image with Python 3.13 on Debian bookworm, pinned by digest (checked 2026-10-26).
image=ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

keep_mount=/dev/null:/keep-unused
keep_env=WIRENOTE_E2E_KEEP=
if [ -n "${WIRENOTE_E2E_KEEP:-}" ]; then
  mkdir -p "$(dirname "$WIRENOTE_E2E_KEEP")"
  keep_mount="$(cd "$(dirname "$WIRENOTE_E2E_KEEP")" && pwd):/keep"
  keep_env="WIRENOTE_E2E_KEEP=/keep/$(basename "$WIRENOTE_E2E_KEEP")"
fi

exec docker run --rm -v "$PWD":/src:ro -v "$keep_mount" -e "$keep_env" -w /work "$image" sh -c '
  set -eu
  apt-get update -qq >/dev/null
  apt-get install -y -qq --no-install-recommends ffmpeg espeak-ng git >/dev/null
  cp -r /src/. /work
  rm -rf .venv
  uv sync --locked --quiet
  uv run pytest "$@"
' sh "$@"
