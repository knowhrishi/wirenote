# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print the GitHub release notes for one version, taken from CHANGELOG.md.

Usage: python scripts/release_notes.py VERSION [--attested]

Fails if the version has no section, or if its section is not dated yet, so a
release cannot go out with "unreleased" notes.
"""
import re
import sys
from pathlib import Path

REPO = "knowhrishi/wirenote"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def notes(changelog: str, version: str, attested: bool) -> str:
    lines = changelog.splitlines()
    head = f"## [{version}] - "
    start = next((i for i, line in enumerate(lines) if line.startswith(head)), None)
    if start is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    if not _DATE.fullmatch(lines[start][len(head):].strip()):
        raise ValueError(f"the {version} section in CHANGELOG.md needs a release date (YYYY-MM-DD)")
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## ") or re.match(r"\[[^\]]+\]: ", line):
            break
        body.append(line)
    return "\n".join(body).strip() + "\n\n" + _footer(version, attested)


def _footer(version: str, attested: bool) -> str:
    wheel = f"wirenote-{version}-py3-none-any.whl"
    verify = (
        "The files below were built by GitHub Actions from this tag. To check that a file is\n"
        "the one built here, use the GitHub CLI:\n\n"
        f"```sh\ngh attestation verify {wheel} --repo {REPO}\n```\n\n"
    ) if attested else ""
    return (
        "## Install\n\n"
        f"```sh\nuv tool install wirenote=={version}   # or: pipx install wirenote=={version}\n"
        "wirenote setup install\n```\n\n"
        f"Requirements and first steps are in the [README](https://github.com/{REPO}#readme).\n\n"
        "## Verify the download\n\n"
        f"{verify}"
        "`SHA256SUMS` lists the checksum of every file: `sha256sum -c SHA256SUMS`.\n"
    )


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--attested"]
    if len(args) != 1:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    try:
        print(notes(Path("CHANGELOG.md").read_text(), args[0], "--attested" in argv), end="")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
