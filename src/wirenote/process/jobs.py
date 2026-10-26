# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Processing jobs in SQLite (docs/dev/PLAN.md › Processing: a crash never loses a meeting).

One row per recording. States: queued -> transcribing -> summarizing -> done,
or failed after MAX_ATTEMPTS. The daemon runs one worker process at a time;
the worker updates the row as it goes.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MAX_ATTEMPTS = 3
ACTIVE = ("transcribing", "summarizing")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    recording TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    summary_only INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    note TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
)
"""


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Job:
    id: int
    recording: Path
    state: str
    attempts: int
    summary_only: bool
    error: str | None
    note: str | None
    created: str
    updated: str


class Jobs:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The UI server shares one connection across threads behind its own lock.
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add(self, recording: Path, summary_only: bool = False) -> int:
        """Queue a recording; re-queues it (attempts reset) if it already has a row."""
        now = _now()
        self.db.execute(
            "INSERT INTO jobs (recording, state, summary_only, created, updated) "
            "VALUES (?, 'queued', ?, ?, ?) ON CONFLICT(recording) DO UPDATE SET "
            "state='queued', attempts=0, error=NULL, summary_only=excluded.summary_only, "
            "updated=excluded.updated",
            (str(recording), int(summary_only), now, now))
        return self.by_recording(recording).id

    def get(self, job_id: int) -> Job | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job(row)

    def by_recording(self, recording: Path) -> Job | None:
        row = self.db.execute("SELECT * FROM jobs WHERE recording=?",
                              (str(recording),)).fetchone()
        return _job(row)

    def next_queued(self) -> Job | None:
        row = self.db.execute("SELECT * FROM jobs WHERE state='queued' "
                              "ORDER BY id LIMIT 1").fetchone()
        return _job(row)

    def recent(self, n: int = 10) -> list[Job]:
        return [_job(r) for r in self.db.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (n,))]

    def counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state"))

    def set_state(self, job_id: int, state: str, note: str | None = None) -> None:
        self.db.execute("UPDATE jobs SET state=?, note=COALESCE(?, note), error=NULL, "
                        "updated=? WHERE id=?", (state, note, _now(), job_id))

    def fail(self, job_id: int, error: str) -> Job:
        """Count a failed attempt; back to queued unless attempts are used up."""
        job = self.get(job_id)
        attempts = job.attempts + 1
        state = "failed" if attempts >= MAX_ATTEMPTS else "queued"
        self.db.execute("UPDATE jobs SET state=?, attempts=?, error=?, updated=? WHERE id=?",
                        (state, attempts, error[-2000:], _now(), job_id))
        return self.get(job_id)

    def delete(self, recording: Path) -> None:
        self.db.execute("DELETE FROM jobs WHERE recording=?", (str(recording),))

    def requeue_interrupted(self) -> list[Job]:
        """Jobs a dead worker left mid-way count one failed attempt."""
        stuck = [_job(r) for r in self.db.execute(
            f"SELECT * FROM jobs WHERE state IN {ACTIVE}")]
        return [self.fail(j.id, f"interrupted while {j.state}") for j in stuck]


def _job(row) -> Job | None:
    if row is None:
        return None
    id_, rec, state, attempts, summary_only, error, note, created, updated = row
    return Job(id_, Path(rec), state, attempts, bool(summary_only), error, note, created, updated)
