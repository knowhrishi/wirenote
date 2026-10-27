# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""The meetings app's scripts parse: one syntax error leaves the whole app blank."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import quickjs

from wirenote import ui

# The installed package's copy, so a run against the wheel checks the scripts that ship.
STATIC = Path(ui.__file__).parent / "static"
SCRIPTS = sorted(STATIC.glob("*.js"))

# Comments and a "use strict" directive, which must stay first to keep strict mode.
PROLOGUE = re.compile(r"""(?:\s+|//[^\n]*|/\*.*?\*/|(["'])use strict\1;?)*""", re.S)
STOP = 'throw "parsed"; '


def parse(source: str) -> None:
    # A script is parsed in full before its first statement runs, so stopping there checks
    # the grammar browsers use (no top-level return) without running code that needs a DOM.
    at = PROLOGUE.match(source).end()
    try:
        quickjs.Context().eval(source[:at] + STOP + source[at:])
    except quickjs.JSException as e:
        if str(e).splitlines()[0] != "parsed":
            raise


def test_the_app_script_ships():
    assert STATIC / "app.js" in SCRIPTS


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_parses(script):
    parse(script.read_text(encoding="utf-8"))


@pytest.mark.parametrize("source", [
    'const hint = "Run "ant auth login" in a terminal.";',
    "// a top-level return parses in a function body, not in a script\nreturn;",
    '"use strict";\nwith (Math) { max(1, 2); }',
])
def test_syntax_errors_are_caught(source):
    with pytest.raises(quickjs.JSException, match="SyntaxError"):
        parse(source)
