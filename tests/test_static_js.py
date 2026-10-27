# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""The meetings app's scripts parse: one syntax error leaves the whole app blank."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import quickjs

from wirenote import ui

# The installed package's copy, so a run against the wheel checks the scripts that ship.
STATIC = Path(ui.__file__).parent / "static"


def parse(source: str) -> None:
    # new Function compiles the code without running it, so no browser objects are needed.
    quickjs.Context().eval(f"new Function({json.dumps(source)})")


@pytest.mark.parametrize("script", sorted(STATIC.glob("*.js")), ids=lambda p: p.name)
def test_script_parses(script):
    parse(script.read_text(encoding="utf-8"))


def test_a_stray_quote_is_caught():
    with pytest.raises(quickjs.JSException, match="SyntaxError"):
        parse('const hint = "Run "ant auth login" in a terminal.";')
