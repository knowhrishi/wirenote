# SPDX-License-Identifier: AGPL-3.0-or-later
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts/release_notes.py")
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

Intro text.

## [0.2.0] - 2027-01-15

### Fixed

- Second release.

## [0.1.0] - 2026-11-02

First public release.

### Added

- Call detection.

[0.2.0]: https://github.com/knowhrishi/wirenote/releases/tag/v0.2.0
[0.1.0]: https://github.com/knowhrishi/wirenote/releases/tag/v0.1.0
"""


def test_takes_only_the_section_of_that_version():
    notes = release_notes.notes(CHANGELOG, "0.1.0", attested=False)
    assert "First public release." in notes and "- Call detection." in notes
    assert "Second release" not in notes
    assert "[0.1.0]:" not in notes and "## [0.1.0]" not in notes


def test_a_middle_section_stops_at_the_next_version():
    notes = release_notes.notes(CHANGELOG, "0.2.0", attested=False)
    assert "- Second release." in notes and "Call detection" not in notes


def test_refuses_an_undated_section():
    text = CHANGELOG.replace("## [0.1.0] - 2026-11-02", "## [0.1.0] - unreleased")
    with pytest.raises(ValueError, match="date"):
        release_notes.notes(text, "0.1.0", attested=False)


def test_refuses_a_version_missing_from_the_changelog():
    with pytest.raises(ValueError, match="0.3.0"):
        release_notes.notes(CHANGELOG, "0.3.0", attested=False)


def test_footer_pins_the_version_and_explains_verification():
    plain = release_notes.notes(CHANGELOG, "0.1.0", attested=False)
    assert "uv tool install wirenote==0.1.0" in plain
    assert "sha256sum -c SHA256SUMS" in plain
    assert "gh attestation verify" not in plain
    attested = release_notes.notes(CHANGELOG, "0.1.0", attested=True)
    assert "gh attestation verify wirenote-0.1.0-py3-none-any.whl --repo knowhrishi/wirenote" in attested


def test_the_real_changelog_has_a_section_for_the_current_version():
    import tomllib
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert f"## [{version}] - " in (ROOT / "CHANGELOG.md").read_text()
