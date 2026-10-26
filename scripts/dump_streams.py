#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Shim so `python3 scripts/dump_streams.py` keeps working; same as `wirenote dump-streams`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wirenote.tools.dump_streams import main  # noqa: E402

sys.exit(main())
