# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Every source file says its license."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parent.parent
SPDX = "# SPDX-License-Identifier: AGPL-3.0-or-later"


def test_every_python_file_has_spdx_header():
    missing = []
    for top in ("src", "tests"):
        for path in sorted((REPO / top).rglob("*.py")):
            lines = path.read_text().splitlines()
            first = lines[1] if lines and lines[0].startswith("#!") else (lines[0] if lines else "")
            if first != SPDX:
                missing.append(str(path.relative_to(REPO)))
    assert not missing, missing


def test_license_is_agpl3():
    text = (REPO / "LICENSE").read_text()
    assert text.lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE")
    assert "Version 3, 19 November 2007" in text
