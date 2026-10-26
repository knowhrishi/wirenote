# Releasing

A release is a pushed tag. `.github/workflows/release.yml` does the rest, and each step runs only if the one before passed:

1. **Checks on the tagged commit:** all of CI (`ci.yml`: tests on Python 3.11 to 3.14 including end to end, pyflakes, the wheel tests, dependency audit, secret scan) and the real-model test (`models.yml`).
2. **Build:** checks that the tag matches the version in `pyproject.toml` and that `CHANGELOG.md` has a dated section for it. It builds the source archive and the wheel from it, and signs their build provenance (public repository only).
3. **Wheel on every Python:** installs the exact wheel that will be published into a fresh environment, on 3.11, 3.12, 3.13 and 3.14, and runs every test against it (`scripts/test_wheel.sh`).
4. **Publish to PyPI:** through Trusted Publishing, so no PyPI token is stored on GitHub. It waits for approval in the `pypi` environment.
5. **GitHub release page:** "Wirenote X.Y.Z", with the changelog section, install and verification steps, both files and `SHA256SUMS`. Versions with `a`, `b` or `rc` are marked as pre-releases.

**Dry run:** Actions › Release › Run workflow, on any branch, runs steps 1 to 3 and publishes nothing. An undated changelog section is only a warning there.

## Each release

1. In `pyproject.toml`, set `version`.
2. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, start a new empty `## [Unreleased]` above it, and add the link line at the bottom (`[X.Y.Z]: https://github.com/knowhrishi/wirenote/releases/tag/vX.Y.Z`).
3. Check the notes: `python scripts/release_notes.py X.Y.Z`.
4. **Save this version's stored data** for the compatibility test (docs/dev/changes.md › Contracts):

   ```sh
   WIRENOTE_E2E_KEEP=tests/compat/X.Y.Z uv run pytest -q tests/e2e/test_call_to_note.py
   # on macOS: WIRENOTE_E2E_KEEP=tests/compat/X.Y.Z scripts/test_in_linux.sh tests/e2e/test_call_to_note.py
   ```

   `tests/test_compat.py` fails until every dated version in the changelog has its folder.
5. Commit, push `main`, and wait for CI to pass. For a risky release, run the dry run too, or release `X.Y.Zrc1` first.
6. Tag and push the tag:

   ```sh
   git tag -a vX.Y.Z -m "Wirenote X.Y.Z"
   git push origin vX.Y.Z
   ```

7. In the Actions tab, approve the "Publish to PyPI" job.
8. Check the result: `uv tool install wirenote==X.Y.Z` in a clean account, and the release page.

A version number can be uploaded to PyPI only once. If a release is broken, fix it and release the next patch version; "yank" the broken one on PyPI so new installs skip it.

## Once, before the first release

**PyPI**

1. Create an account at <https://pypi.org/account/register/> and confirm the email address.
2. Turn on two-factor authentication (an authenticator app or a security key) and keep the recovery codes.
3. Under *Your account › Publishing*, add a pending GitHub publisher:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `wirenote` |
   | Owner | `knowhrishi` |
   | Repository name | `wirenote` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   A pending publisher does not reserve the name; the first release does.

**GitHub** (repository settings)

1. *Environments › New environment* `pypi`: add yourself as a required reviewer, and under deployment branches and tags allow only tags matching `v*`.
2. *Rules › Rulesets*: protect `main` (require the CI checks, block force pushes and deletion) and tags `v*` (block updates and deletion).
3. *Security*: turn on private vulnerability reporting, Dependabot alerts and secret scanning.
