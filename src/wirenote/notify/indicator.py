# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""What Wirenote shows in the desktop's panel: the recording state and a short menu
(docs/usage.md › Tray icon).

The daemon builds an IndicatorState whenever its status changes and hands it to an
Indicator. The state and the menu live here; notify/tray.py draws them on Linux, and
any other desktop's indicator draws the same ones. A click on a menu item calls
on_menu(key) with one of MENU_KEYS.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

# record and ignore answer the prompt showing; record-now records without a call app
MENU_KEYS = ("record", "ignore", "stop", "flag", "record-now", "open")


@dataclass(frozen=True)
class IndicatorState:
    recording: str | None = None                # what is being recorded, e.g. "Jitsi call"
    recording_since: str | None = None          # its start, ISO 8601 with the UTC offset
    prompt: tuple[str, str] | None = None       # (app key, app name) of the prompt showing
    flags: int = 0                              # flagged moments in this recording


@dataclass(frozen=True)
class MenuItem:
    key: str | None      # what a click sends to on_menu; None for a line that only informs
    label: str


def _hhmm(iso: str | None) -> str | None:
    try:
        return dt.datetime.fromisoformat(iso).strftime("%H:%M") if iso else None
    except ValueError:
        return None


def describe(s: IndicatorState) -> str:
    """One line for a tooltip. Names in it are untrusted: escape them for the place they go."""
    if s.recording is not None:
        since = _hhmm(s.recording_since)
        return f"Recording {s.recording}" + (f" since {since}" if since else "")
    if s.prompt is not None:
        return f"{s.prompt[1]} is using your mic"
    return "Not recording"


def menu_for(s: IndicatorState) -> list[MenuItem]:
    if s.recording is not None:
        line = describe(s)
        if s.flags:
            line += f", {s.flags} flag" + ("s" if s.flags > 1 else "")
        return [MenuItem(None, line), MenuItem("flag", "Flag this moment"),
                MenuItem("stop", "Stop"), MenuItem("open", "Open Wirenote")]
    if s.prompt is not None:
        return [MenuItem("record", f"Record {s.prompt[1]}"), MenuItem("ignore", "Ignore")]
    return [MenuItem("record-now", "Record a meeting"), MenuItem("open", "Open Wirenote")]


class Indicator(Protocol):
    on_menu: Callable[[str], None]

    async def start(self) -> bool:
        """True once the desktop shows it; False where it cannot, and nothing is shown."""
        ...

    def update(self, s: IndicatorState) -> None:
        """Show s. Never raises: the daemon must keep detecting whatever the panel does."""
        ...


class NullIndicator:
    """For desktops without a tray, and when the tray icon is turned off."""

    def __init__(self) -> None:
        self.on_menu: Callable[[str], None] = lambda key: None

    async def start(self) -> bool:
        return False

    def update(self, s: IndicatorState) -> None:
        pass
