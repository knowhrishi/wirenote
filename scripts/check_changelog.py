# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail a pull request that changes the app without a line in CHANGELOG.md.

Usage: python scripts/check_changelog.py BASE_SHA [--labels JSON]

The notes are what users read in the app after an update (docs/dev/updates-design.md),
so a change under src/ comes with its CHANGELOG.md line. The `no-changelog` label
skips the check for changes nobody would notice, such as a refactor.
"""
import json
import subprocess
import sys

SKIP_LABEL = "no-changelog"


def problem(changed: list[str], labels: list[str]) -> str | None:
    """Why the pull request needs a changelog line, or None if it is fine."""
    if SKIP_LABEL in labels or "CHANGELOG.md" in changed:
        return None
    app = sorted(f for f in changed if f.startswith("src/"))
    if not app:
        return None
    return (f"{len(app)} file(s) under src/ changed ({app[0]}{', …' if len(app) > 1 else ''}) "
            "but CHANGELOG.md did not. Add a line under ## [Unreleased] saying what users "
            f"notice, or add the '{SKIP_LABEL}' label if they notice nothing.")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    base = argv[0]
    labels = json.loads(argv[argv.index("--labels") + 1]) if "--labels" in argv else []
    changed = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], check=True,
                             capture_output=True, text=True).stdout.split()
    msg = problem(changed, labels)
    if msg:
        print(f"::error title=CHANGELOG.md::{msg}")
        return 1
    print("changelog: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
