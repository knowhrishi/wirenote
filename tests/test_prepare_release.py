# SPDX-License-Identifier: AGPL-3.0-or-later
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare_release = _load("prepare_release")
release_notes = _load("release_notes")

CHANGELOG = """# Changelog

Intro text.

## [Unreleased]

### Fixed

- A fix.

## [0.1.0] - 2026-11-02

First public release.

[0.1.0]: https://github.com/knowhrishi/wirenote/releases/tag/v0.1.0
"""


def test_unreleased_becomes_the_dated_release_with_its_link():
    text = prepare_release.changelog(CHANGELOG, "0.1.1", "2026-11-20")
    assert "## [Unreleased]\n\n## [0.1.1] - 2026-11-20\n\n### Fixed\n\n- A fix.\n\n## [0.1.0]" in text
    assert text.endswith("[0.1.1]: https://github.com/knowhrishi/wirenote/releases/tag/v0.1.1\n"
                         "[0.1.0]: https://github.com/knowhrishi/wirenote/releases/tag/v0.1.0\n")
    notes = release_notes.notes(text, "0.1.1", attested=False)   # what the release job checks
    assert notes.startswith("### Fixed\n\n- A fix.\n")


def test_the_new_unreleased_section_starts_empty():
    text = prepare_release.changelog(CHANGELOG, "0.1.1", "2026-11-20")
    with pytest.raises(ValueError, match="nothing under"):
        prepare_release.changelog(text, "0.1.2", "2026-11-21")


@pytest.mark.parametrize("text, error", [
    (CHANGELOG.replace("## [Unreleased]", "## Next"), "no ## \\[Unreleased\\]"),
    (CHANGELOG.replace("### Fixed\n\n- A fix.\n", ""), "nothing under"),
])
def test_no_release_without_notes(text, error):
    with pytest.raises(ValueError, match=error):
        prepare_release.changelog(text, "0.1.1", "2026-11-20")


def test_a_version_is_released_once():
    with pytest.raises(ValueError, match="already has a section"):
        prepare_release.changelog(CHANGELOG, "0.1.0", "2026-11-20")


def test_init_version_is_replaced():
    text = '"""Doc."""\n\n__version__ = "0.1.0"\n'
    assert prepare_release.init_py(text, "0.2.0") == '"""Doc."""\n\n__version__ = "0.2.0"\n'
    with pytest.raises(ValueError):
        prepare_release.init_py('"""Doc."""\n', "0.2.0")


def test_main_refuses_a_version_pyproject_does_not_have(tmp_path, monkeypatch, capsys):
    (tmp_path / "src/wirenote").mkdir(parents=True)
    (tmp_path / "src/wirenote/__init__.py").write_text('__version__ = "0.1.0"\n')
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.1"\n')
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    monkeypatch.chdir(tmp_path)
    assert prepare_release.main(["0.1.2", "2026-11-20"]) == 1
    assert "pyproject.toml says 0.1.1" in capsys.readouterr().err
    assert prepare_release.main(["0.1.1", "2026-11-20"]) == 0
    assert (tmp_path / "src/wirenote/__init__.py").read_text() == '__version__ = "0.1.1"\n'
    assert "## [0.1.1] - 2026-11-20" in (tmp_path / "CHANGELOG.md").read_text()
