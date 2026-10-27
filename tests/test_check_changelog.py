# SPDX-License-Identifier: AGPL-3.0-or-later
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_changelog", ROOT / "scripts/check_changelog.py")
check_changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_changelog)


def test_app_change_needs_a_changelog_line():
    msg = check_changelog.problem(["src/wirenote/cli.py", "tests/test_x.py"], [])
    assert msg and "src/wirenote/cli.py" in msg and "no-changelog" in msg
    assert check_changelog.problem(["src/wirenote/cli.py", "CHANGELOG.md"], []) is None


def test_label_or_no_app_change_passes():
    assert check_changelog.problem(["src/wirenote/cli.py"], ["no-changelog"]) is None
    assert check_changelog.problem(["docs/usage.md", ".github/workflows/ci.yml", "uv.lock"], []) is None
