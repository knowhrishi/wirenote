# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Synchronous detector: graph mirror -> mic streams -> app identity -> tracker.

No I/O besides the injected ProcReader, so tests replay recorded batches
against a fake clock. The asyncio daemon wraps this.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..config.apps import AppRules
from ..config.settings import Settings
from .graph import Graph
from .identity import Identity, ProcReader, gather_evidence, resolve
from .streams import MicStream, mic_streams
from .tracker import Action, Observation, Tracker


@dataclass(frozen=True)
class Identified:
    """First sight of a client on the mic, with the evidence behind its identity."""
    client_serial: int | None
    key: str | None          # None: no rule matched, so it never prompts
    kind: str | None
    matched_by: str | None
    evidence: dict


DetectorEvent = Action | Identified

IDENTITY_CACHE_MAX = 256


class Detector:
    def __init__(self, settings: Settings, rules: AppRules, proc: ProcReader,
                 own_pids: set[int], ignored: set[str] | None = None,
                 dismisses: dict[str, int] | None = None) -> None:
        self.settings = settings
        self.rules = rules
        self.proc = proc
        self.own_pids = own_pids
        self.ignored: set[str] = set(ignored or ())
        self.graph = Graph()
        self.tracker = Tracker(settings, dismisses)
        self._identities: dict[int | None, Identity] = {}
        # Capture sources each app on the mic is linked to (node.name), for the recorder.
        self.app_sources: dict[str, tuple[str, ...]] = {}
        # True while a calendar event with a call link is in its window: browsers
        # then get the meeting dwell (docs/dev/PLAN.md › Browsers).
        self.calendar_hint: Callable[[], bool] = lambda: False

    def apply_batch(self, batch: list[dict], now: float) -> list[DetectorEvent]:
        self.graph.apply(batch)
        return self.tick(now)

    def reset_graph(self) -> None:
        """pw-dump restarted; its first batch will be the full graph again."""
        self.graph.clear()

    def tick(self, now: float) -> list[DetectorEvent]:
        events: list[DetectorEvent] = []
        obs = self.observations(events)
        events += self.tracker.update(now, obs)
        if len(self._identities) > IDENTITY_CACHE_MAX:
            self._prune_identities()
        return events

    def _prune_identities(self) -> None:
        """Forget clients that left the graph; the daemon runs for weeks."""
        live = {self.graph.props(c["id"]).get("object.serial")
                for c in self.graph.of_type(":Client")}
        self._identities = {k: v for k, v in self._identities.items() if k in live}

    def observations(self, events: list[DetectorEvent] | None = None) -> dict[str, Observation]:
        by_app: dict[str, list[MicStream]] = {}
        for stream in mic_streams(self.graph, self.own_pids, self.settings.own_node_prefix):
            ident = self._identify(stream, events)
            if ident.rule is None or ident.rule.kind == "ignore" or ident.key in self.ignored:
                continue
            by_app.setdefault(ident.key, []).append(stream)

        out = {}
        hint = None
        for key, streams in by_app.items():
            rule = self.rules.get(key)
            kind, service, room = rule.kind, None, None
            for s in streams:
                hit = rule.title_match(s.media_name)
                if hit:
                    kind, (service, room) = "meeting", hit
                    break
            if kind == "browser":
                hint = self.calendar_hint() if hint is None else hint
                if hint:
                    kind = "meeting"
            out[key] = Observation(key, rule.name, kind,
                                   frozenset(s.serial for s in streams), service, room)
        self.app_sources = {key: tuple(sorted({n for s in streams for n in s.source_names}))
                            for key, streams in by_app.items()}
        return out

    def _identify(self, stream: MicStream, events: list[DetectorEvent] | None) -> Identity:
        cache_key = stream.client_serial if stream.client_serial is not None else -stream.serial
        ident = self._identities.get(cache_key)
        if ident is None:
            ev = gather_evidence(stream.client_props, stream.node_props, self.proc)
            ident = self._identities[cache_key] = resolve(ev, self.rules)
            if events is not None:
                events.append(Identified(stream.client_serial, ident.key,
                                         ident.rule.kind if ident.rule else None,
                                         ident.matched_by, ev.as_dict()))
        return ident
