# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""No test may read or write the real home folder: a test once ran the migration on
the developer's own data because it redirected only some XDG folders."""

from __future__ import annotations

import os
from pathlib import Path


def test_home_and_every_xdg_folder_are_temporary(tmp_path_factory):
    tmp_root = tmp_path_factory.getbasetemp()
    for var in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        assert Path(os.environ[var]).is_relative_to(tmp_root), var
