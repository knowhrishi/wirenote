# Releasing

A release starts from a button and goes through a pull request:

- **`prepare-release.yml`** (Actions › Prepare release › Run workflow, on `main`):
  - raises the version with `uv version --bump` (patch, minor or major) in `pyproject.toml` and `uv.lock`
  - turns `## [Unreleased]` in `CHANGELOG.md` into the dated section for it (`scripts/prepare_release.py`) and sets `__version__`
  - saves this version's stored data to `tests/compat/X.Y.Z/` for the compatibility test (docs/dev/changes.md › Contracts)
  - opens a "Release X.Y.Z" pull request from `release/vX.Y.Z`, with CI started on it
- **`tag-release.yml`**: when that pull request is merged, tags the merge commit `vX.Y.Z` and starts `release.yml` on the tag.
- **`release.yml`** checks and publishes. Each step runs only if the one before passed:

1. **Checks on the tagged commit:** all of CI (`ci.yml`: tests on Python 3.11 to 3.14 including end to end, pyflakes, the wheel tests, dependency audit, secret scan) and the real-model test (`models.yml`).
2. **Build:** checks that the tag matches the version in `pyproject.toml` and that `CHANGELOG.md` has a dated section for it. It builds the source archive and the wheel from it, and signs their build provenance (public repository only).
3. **Wheel on every Python:** installs the exact wheel that will be published into a fresh environment, on 3.11, 3.12, 3.13 and 3.14, and runs every test against it (`scripts/test_wheel.sh`).
4. **Publish to PyPI:** through Trusted Publishing, so no PyPI token is stored on GitHub. It waits for approval in the `pypi` environment.
5. **GitHub release page:** "Wirenote X.Y.Z", with the changelog section, install and verification steps, both files and `SHA256SUMS`. Versions with `a`, `b` or `rc` are marked as pre-releases.

**Dry run:** Actions › Release › Run workflow, on any branch, runs steps 1 to 3 and publishes nothing. An undated changelog section is only a warning there. Run on a tag, the same workflow is a real release.

## Each release

1. Make sure `## [Unreleased]` in `CHANGELOG.md` lists what changed. Pull requests add their lines there (the `changelog.yml` check makes sure), and the workflow refuses to release without any. Read them as a user would: the wheel ships this file, and the app shows the new sections after an update.
2. Actions › **Prepare release** › Run workflow on `main`, and pick the part to raise: **patch** for fixes (0.2.0 → 0.2.1), **minor** for new features (0.2.1 → 0.3.0), **major** for breaking changes.
3. Review the "Release X.Y.Z" pull request it opens: the version, the notes, the new `tests/compat/X.Y.Z/` folder, and CI. For a risky release, run the dry run on its branch too. To change the notes, edit `CHANGELOG.md` on its branch.
4. Merge it. The merge is tagged and the release starts.
5. In the Actions tab, approve the "Publish to PyPI" job.
6. Check the result: `uv tool install wirenote==X.Y.Z` in a clean account, and the release page. On an older install, `wirenote update --check` should now offer X.Y.Z with its notes.

If Prepare release pushes `release/vX.Y.Z` but fails to open the pull request, GitHub does not let workflows open pull requests yet: turn on the setting in step 4 under "Once, before the first release" and run Prepare release again with the same choice. It finds the branch it pushed and only opens the pull request. If CI does not show up on the release pull request, close and reopen it. If a release fails after the tag exists, fix the cause and run **Release** again from Actions on the tag (Run workflow › Use workflow from › Tags › vX.Y.Z).

Releasing by hand still works: set the version in `pyproject.toml`, `uv.lock` and `src/wirenote/__init__.py`, date the changelog section, and save the stored data (`WIRENOTE_E2E_KEEP=tests/compat/X.Y.Z uv run pytest -q tests/e2e/test_call_to_note.py`, or through `scripts/test_in_linux.sh` on macOS). Merge, then push an annotated `vX.Y.Z` tag.

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
4. *Actions › General › Workflow permissions*: tick "Allow GitHub Actions to create and approve pull requests", so Prepare release can open its pull request.
5. The release pull request's commit is by `github-actions[bot]`, signed off for the DCO check. If the CLA bot asks it to sign, add `github-actions[bot]` to its allowlist.
