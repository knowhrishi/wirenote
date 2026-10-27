# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Stand-in for `pw-dump --monitor`: replays a recorded trace from tests/replay/.

Environment:
  WIRENOTE_E2E_TRACE    the trace file
  WIRENOTE_E2E_HOLD_AT  replay batches up to this many seconds into the trace (the
                        call is running), then wait
  WIRENOTE_E2E_HANGUP   a path; once it exists, replay the rest up to STOP_AT
  WIRENOTE_E2E_STOP_AT  last trace second to replay (the call has ended by then)

Batches are written the way pw-dump writes them: pretty-printed JSON arrays, each
closing "]" alone at column 0. The fake exits when its parent dies, so a test that
kills the daemon leaves nothing running.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> None:
    trace = json.loads(Path(os.environ["WIRENOTE_E2E_TRACE"]).read_text())
    hold_at = float(os.environ["WIRENOTE_E2E_HOLD_AT"])
    stop_at = float(os.environ["WIRENOTE_E2E_STOP_AT"])
    hangup = Path(os.environ["WIRENOTE_E2E_HANGUP"])
    parent = os.getppid()

    def emit(batch: list) -> None:
        sys.stdout.write(json.dumps(batch, indent=2) + "\n")
        sys.stdout.flush()

    def wait(until) -> None:
        while not until():
            if os.getppid() != parent:
                sys.exit(0)
            time.sleep(0.05)

    batches = trace["batches"]
    for b in batches:
        if b["rel_s"] <= hold_at:
            emit(b["batch"])
    wait(hangup.exists)
    for b in batches:
        if hold_at < b["rel_s"] <= stop_at:
            emit(b["batch"])
    wait(lambda: False)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
