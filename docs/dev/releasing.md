# Releasing

A release is a pushed tag. `.github/workflows/release.yml` does the rest:

1. **Build:** checks that the tag matches the version in `pyproject.toml` and that `CHANGELOG.md` has a dated section for it, runs the tests, builds the wheel and the source archive, and signs their build provenance (public repository only).
2. **Publish to PyPI:** through Trusted Publishing, so no PyPI token is stored on GitHub. It waits for approval in the `pypi` environment.
3. **GitHub release page:** "Wirenote X.Y.Z", with the changelog section, install and verification steps, both files and `SHA256SUMS`. Versions with `a`, `b` or `rc` are marked as pre-releases.

## Each release

1. In `pyproject.toml`, set `version`.
2. In `CHANGELOG.md`, give the section its date (`## [X.Y.Z] - YYYY-MM-DD`), and add the link line at the bottom (`[X.Y.Z]: https://github.com/knowhrishi/wirenote/releases/tag/vX.Y.Z`).
3. Check the notes: `python scripts/release_notes.py X.Y.Z`.
4. Commit, push `main`, and wait for CI to pass.
5. Tag and push the tag:

   ```sh
   git tag -a vX.Y.Z -m "Wirenote X.Y.Z"
   git push origin vX.Y.Z
   ```

6. In the Actions tab, approve the "Publish to PyPI" job.
7. Check the result: `uv tool install wirenote==X.Y.Z` in a clean account, and the release page.

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
