# SPDX-License-Identifier: AGPL-3.0-or-later
"""Make the Unreleased section of CHANGELOG.md the release of VERSION.

Usage: python scripts/prepare_release.py VERSION DATE

Run by .github/workflows/prepare-release.yml after `uv version --bump` has set
VERSION in pyproject.toml and uv.lock. Dates the section, adds its link line and
a new empty Unreleased section, and sets __version__ in src/wirenote/__init__.py.
Fails if Unreleased is missing or empty, so a release always has notes.
"""
import re
import sys
import tomllib
from pathlib import Path

REPO = "knowhrishi/wirenote"
INIT = Path("src/wirenote/__init__.py")
_UNRELEASED = re.compile(r"^## \[Unreleased\]\s*$", re.IGNORECASE)
_LINK = re.compile(r"^\[[^\]]+\]: ")
_VERSION = re.compile(r'^__version__ = "[^"]*"$', re.MULTILINE)


def changelog(text: str, version: str, date: str) -> str:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if _UNRELEASED.match(line)), None)
    if start is None:
        raise ValueError("CHANGELOG.md has no ## [Unreleased] section")
    if any(line.startswith(f"## [{version}]") for line in lines):
        raise ValueError(f"CHANGELOG.md already has a section for {version}")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ") or _LINK.match(lines[i])), len(lines))
    if not "\n".join(lines[start + 1:end]).strip():
        raise ValueError("nothing under ## [Unreleased] in CHANGELOG.md; write the notes first")
    lines[start:start + 1] = ["## [Unreleased]", "", f"## [{version}] - {date}"]
    link = f"[{version}]: https://github.com/{REPO}/releases/tag/v{version}"
    first_link = next((i for i, line in enumerate(lines) if _LINK.match(line)), None)
    if first_link is None:
        lines += ["", link]
    else:
        lines.insert(first_link, link)
    return "\n".join(lines) + "\n"


def init_py(text: str, version: str) -> str:
    new, n = _VERSION.subn(f'__version__ = "{version}"', text)
    if n != 1:
        raise ValueError(f"{INIT} needs exactly one __version__ line")
    return new


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    version, date = argv
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError(f"date {date!r} is not YYYY-MM-DD")
        project = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
        if project != version:
            raise ValueError(f"pyproject.toml says {project}, not {version}")
        log = changelog(Path("CHANGELOG.md").read_text(), version, date)
        init = init_py(INIT.read_text(), version)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    Path("CHANGELOG.md").write_text(log)
    INIT.write_text(init)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
