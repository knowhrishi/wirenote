# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Untrusted text for the desktop's own surfaces: the launcher (notify/search_provider.py) and
the tray icon (notify/tray.py). docs/usage.md › Tray icon and › Find a meeting from the launcher.
"""

from __future__ import annotations

import unicodedata
from typing import Any


def plain(text: Any, limit: int | None = None) -> str:
    """Calendar titles, app and people names are untrusted: one line, with no control, format
    or direction characters (Unicode category C), cut to limit characters if given."""
    one = " ".join(str(text).split())
    one = "".join(c for c in one if not unicodedata.category(c).startswith("C"))
    return one if limit is None or len(one) <= limit else one[:limit - 1].rstrip() + "…"
