# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""The public tree carries no private names, paths or internal references."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
# The source archive on PyPI has no git history; these checks guard the repository.
needs_git = pytest.mark.skipif(not (REPO / ".git").exists(), reason="needs a git checkout")
# Words that must never appear in the published tree (a company, a user name, a
# private folder, a product we do not compare with), stored as SHA-256 hashes so
# this file does not publish them either.
PRIVATE_WORDS = {
    "31f61ed3e6e210c5f3f729345968bdbcf267cf0fbfec31eac02ed9966ba1d067",
    "09a36d05357e07a2f79e78bf1e954c423d223d7e110dedb8a69130bc35fb3b66",
    "01a9f91f2791ea9afa16649018663cd01d430225c4cb772b9562d3e660ba7a29",
    "13879d586271db46f545166cf67ef5aa585fe99b6121b7324467f9c751a8d93b",
    "ecfba4c8a1d477ada499f7ca1677566a33475b9f8ddf972059d8c8d522f4152b",
    "c4599aab5cbf87f8189071b86ce3c16a7dc83fd6a365466c4374939ef6b04812",
}


def _tracked_text() -> dict[str, str]:
    names = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                           check=True).stdout.decode().split("\0")
    out = {}
    for name in filter(None, names):
        try:
            data = (REPO / name).read_bytes()
        except OSError:
            continue
        if b"\0" not in data:
            out[name] = data.decode("utf-8", "replace")
    return out


@needs_git
def test_no_private_text_in_tree():
    hits = sorted(name for name, text in _tracked_text().items()
                  if any(hashlib.sha256(w.encode()).hexdigest() in PRIVATE_WORDS
                         for w in set(re.findall(r"[a-z0-9]+", text.lower()))))
    assert hits == []


@needs_git
def test_no_ai_working_files_in_tree():
    """Plans, specs and agent instructions stay local (.gitignore)."""
    tracked = _tracked_text()
    assert not [n for n in tracked if n.startswith(("docs/superpowers/", ".claude/", ".superpowers/"))
                or Path(n).name in ("CLAUDE.md", "AGENTS.md")]


def test_public_docs_exist():
    for name in ("README.md", "PRIVACY.md", "SECURITY.md", "CONTRIBUTING.md",
                 "CODE_OF_CONDUCT.md", "THIRD_PARTY.md", "CHANGELOG.md", "docs/usage.md"):
        assert (REPO / name).is_file(), name


def test_privacy_lists_the_cloud_disclosure():
    from wirenote.ui.data import CLOUD_DISCLOSURE

    assert CLOUD_DISCLOSURE in " ".join((REPO / "PRIVACY.md").read_text().split())
