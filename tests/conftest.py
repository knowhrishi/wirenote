# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wirenote.config.apps import DEFAULT_APPS_TOML, parse_apps
from wirenote.config.settings import Settings
from wirenote.detector.core import Detector

REPLAY_DIR = Path(__file__).parent / "replay"


class FakeProc:
    """ProcReader backed by a dict of pid -> fields, like a replay trace."""

    def __init__(self, procs: dict) -> None:
        self.procs = {int(k): v for k, v in procs.items()}

    def read(self, pid: int) -> dict | None:
        return self.procs.get(pid)


def load_trace(name: str) -> dict:
    return json.loads((REPLAY_DIR / f"{name}.json").read_text())


def replay(trace: dict, settings: Settings | None = None, respond=None, tail_s: float = 120.0):
    """Feed a trace through a Detector on a fake clock with a 1 s reconcile tick.

    respond(event, t) may return (delay_s, "record" | "ignore" | "stop") to
    simulate a click. Returns (detector, [(t, event), ...]).
    """
    settings = settings or Settings()
    det = Detector(settings, parse_apps(DEFAULT_APPS_TOML), FakeProc(trace["processes"]),
                   own_pids=set())
    timeline: list[tuple[float, object]] = []
    clicks: list[tuple[float, str, str]] = []   # (t, action, key)

    def emit(t, events):
        for e in events:
            timeline.append((t, e))
            if respond:
                r = respond(e, t)
                if r:
                    clicks.append((t + r[0], r[1], e.key))
                    clicks.sort()

    def run_clicks(until):
        while clicks and clicks[0][0] <= until:
            t, what, key = clicks.pop(0)
            if what == "record":
                emit(t, det.tracker.record(key, t))
            elif what == "ignore":
                emit(t, det.tracker.ignore(key, t))
            elif what == "stop":
                emit(t, det.tracker.stop(t))

    next_tick = 1.0
    for b in trace["batches"]:
        while next_tick < b["rel_s"]:
            run_clicks(next_tick)
            emit(next_tick, det.tick(next_tick))
            next_tick += settings.reconcile_s
        run_clicks(b["rel_s"])
        emit(b["rel_s"], det.apply_batch(b["batch"], b["rel_s"]))
    end = trace["batches"][-1]["rel_s"] + tail_s
    while next_tick <= end:
        run_clicks(next_tick)
        emit(next_tick, det.tick(next_tick))
        next_tick += settings.reconcile_s
    return det, timeline


@pytest.fixture(autouse=True)
def private_home(tmp_path_factory, monkeypatch):
    """Every test gets its own HOME and XDG folders, so none can read or change the
    developer's real settings, recordings or running daemon. Tests that need other
    folders set them on top of these."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for var, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_STATE_HOME", ".local/state"),
                     ("XDG_DATA_HOME", ".local/share"), ("XDG_CACHE_HOME", ".cache"),
                     ("XDG_RUNTIME_DIR", "run")):
        (home / sub).mkdir(parents=True, mode=0o700)
        monkeypatch.setenv(var, str(home / sub))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture
def settings() -> Settings:
    return Settings()
