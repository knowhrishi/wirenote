# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Copy text to the clipboard on any desktop: Klipper (KDE) over D-Bus, else
wl-copy (Wayland), xclip or xsel (X11). The text goes in on stdin, never on the
command line, where other users could read it in the process list."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable

# (name, argv, the environment variable that must be set for it to work)
TOOLS = (("wl-copy", ["wl-copy"], "WAYLAND_DISPLAY"),
         ("xclip", ["xclip", "-selection", "clipboard"], "DISPLAY"),
         ("xsel", ["xsel", "-b", "-i"], "DISPLAY"))


def available_tools() -> list[str]:
    return [name for name, argv, _env in TOOLS if shutil.which(argv[0])]


async def copy(text: str, klipper: Callable[[str], Awaitable[None]] | None = None) -> str | None:
    """The name of the tool that copied the text, or None if nothing could."""
    if klipper is not None:
        try:
            await klipper(text)
            return "klipper"
        except Exception:  # noqa: BLE001 - not KDE, or Klipper not running
            pass
    for name, argv, env in TOOLS:
        if not os.environ.get(env) or not shutil.which(argv[0]):
            continue
        try:
            await asyncio.to_thread(subprocess.run, argv, input=text.encode(), timeout=5,
                                    check=True, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            return name
        except (OSError, subprocess.SubprocessError):
            continue
    return None
