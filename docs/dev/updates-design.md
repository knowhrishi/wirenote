# Design note: updates and "What's new"

Status: **chosen 2026-10-27**. Releasing was already one button (docs/dev/releasing.md); this note covers the user's side: learning that a release exists, installing it, and seeing what changed.

## What changes

1. **"What's new" after an update.** The wheel carries `CHANGELOG.md` (`wirenote/data/CHANGELOG.md`), so the installed app reads its own notes offline. The web app remembers the last version you saw (`~/.local/state/wirenote/seen-version`). When it starts on a newer version, a card lists the Added, Changed, Fixed and Security points of every release in between, once. **Updates** in the sidebar footer shows them again.
   - No `seen-version` yet (the first version with this feature): if there are recordings, this is an upgrade and the card shows the current release; without recordings it is a new install and shows nothing.
2. **`wirenote update`.** One command for the whole update:
   - Refuses while a call records (`--wait` waits for it to end, up to the 4 h recording cap).
   - Finds how Wirenote was installed: `uv tool` (`uv-receipt.toml` in the environment), pipx (`pipx_metadata.json`), or a source checkout (an editable or path install), which it refuses and explains.
   - Installs the newest version: `uv tool install --upgrade wirenote`, which also clears an earlier pin (`uv tool upgrade` does nothing on a pinned install). `--to X.Y.Z` installs that version: a rollback, with a warning, because an older version may not read what a newer one wrote.
   - Restarts the services through the new version's `setup install`, waits for them, runs `doctor`, and prints the new notes (`wirenote changelog --since OLD` of the new version).
   - Progress goes to `~/.local/state/wirenote/update.json`, which the web app reads.
   - `wirenote update --check` only checks and prints what is new.
3. **Update check, opt-in.** Setting `update_check`, off by default (docs/dev/changes.md › New behaviour). `setup install` asks once, in a terminal; Settings › Updates has the switch.
   - When on, the daemon asks PyPI once a day for the newest version (`https://pypi.org/pypi/wirenote/json`). If it is newer, it fetches that release's `CHANGELOG.md` from GitHub (`raw.githubusercontent.com`) for the notes. The result is cached in `~/.local/state/wirenote/update-check.json`.
   - The requests carry nothing about the user: no version, no ID, a fixed `User-Agent: wirenote`. They do reveal the IP address to PyPI and GitHub, which PRIVACY.md says.
   - A newer version shows as a banner in the web app ("Wirenote 0.3.0 is available"), a line in `doctor`, and one desktop notification per version, never while recording.
   - Pre-releases are offered only to someone already running one; yanked versions never.
4. **Update button.** In the banner and on the Updates page. The web server starts `wirenote update --yes --wait` through `systemd-run --user` as its own unit, so restarting the services does not kill the update. The page follows `update.json`, waits for the server to come back on the new version and reloads into "What's new".

## Release side

- **Changelog check in CI** (`.github/workflows/changelog.yml`, `scripts/check_changelog.py`): a pull request that changes `src/` must change `CHANGELOG.md`, unless it has the `no-changelog` label.
- CONTRIBUTING.md asks for entries written for users, since they now appear in the app.

## What stays

- No network use unless the setting is on, or the user runs `update` / `update --check` or clicks Check now or Update.
- Data formats are untouched. The new files are small state files; losing one only means a card shows again or a check runs again.
- Source checkouts update with git, as before.

## Defaults (docs/dev/PLAN.md › Defaults)

| Parameter | Value |
| --- | --- |
| Update check | off; when on, once a day |
| Update notification | once per version, never while recording |
| Waiting for a call before updating | at most 4 h, the recording cap |
