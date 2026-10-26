# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Per-app state machine (docs/dev/PLAN.md › State machine, Mermaid diagram).

Pure: no I/O, no clock of its own. The daemon calls update() after every
PipeWire batch and on the 1 s reconcile tick with the apps currently on the
mic; user clicks come in through record(), ignore() and stop(). Every call
returns a list of actions for the daemon to carry out and log.

Two refinements of the diagram, both to avoid surprising prompts:
- StopPending stays put while a stop guard (recording < 30 s, app runtime
  < 20 s) blocks, and re-checks on every update, instead of bouncing back to
  Recording each tick. It returns to Recording only when the app comes back.
- Manual Stop and the 4 h cap go to Suppressed, not Idle, so a call that is
  still running does not prompt again right away.
Prompted also waits out the grace before withdrawing, so a mic reopen (Chrome
closes and reopens it at Join, docs/dev/m0-findings.md) does not flash the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..config.settings import Settings


MANUAL_KEY = "manual"


class State(StrEnum):
    IDLE = "idle"
    CANDIDATE = "candidate"
    PROMPTED = "prompted"
    RECORDING = "recording"
    STOP_PENDING = "stop_pending"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class Observation:
    """One app on the mic right now: all of its running mic streams."""
    key: str
    name: str
    kind: str                     # "meeting" or "browser", after title rules
    streams: frozenset[int]       # object.serial of each running stream
    service: str | None = None    # e.g. "Jitsi", from a title rule
    room: str | None = None


# Actions -------------------------------------------------------------------

@dataclass(frozen=True)
class Transition:
    key: str
    old: State
    new: State
    reason: str


@dataclass(frozen=True)
class Note:
    key: str
    message: str


@dataclass(frozen=True)
class ShowPrompt:
    key: str
    name: str
    kind: str
    service: str | None
    room: str | None


@dataclass(frozen=True)
class WithdrawPrompt:
    key: str


@dataclass(frozen=True)
class StartRecording:
    key: str
    name: str


@dataclass(frozen=True)
class StopRecording:
    key: str
    reason: str
    duration_s: float


@dataclass(frozen=True)
class ShowNudge:
    key: str
    name: str
    dismisses: int


Action = Transition | Note | ShowPrompt | WithdrawPrompt | StartRecording | StopRecording | ShowNudge


@dataclass
class AppTrack:
    key: str
    state: State = State.IDLE
    obs: Observation | None = None
    session_start: float | None = None   # first mic use of this session
    gone_since: float | None = None      # set while the app is off the mic
    candidate_since: float | None = None
    recording_since: float | None = None
    note: str | None = None              # last Note, so it is emitted once
    manual: bool = False                 # started with "Record a meeting": no app to leave


class Tracker:
    def __init__(self, settings: Settings, dismisses: dict[str, int] | None = None) -> None:
        self.s = settings
        self.apps: dict[str, AppTrack] = {}
        self.dismisses: dict[str, int] = dict(dismisses or {})
        self.asleep = False

    # Queries ---------------------------------------------------------------

    @property
    def recording_key(self) -> str | None:
        return next((t.key for t in self.apps.values()
                     if t.state in (State.RECORDING, State.STOP_PENDING)), None)

    def snapshot(self, now: float) -> dict:
        out = {}
        for t in self.apps.values():
            entry = {"state": str(t.state), "name": t.obs.name if t.obs else t.key}
            if t.session_start is not None:
                entry["on_mic_for_s"] = round(now - t.session_start, 1)
            if t.recording_since is not None:
                entry["recording_for_s"] = round(now - t.recording_since, 1)
            if t.gone_since is not None:
                entry["gone_for_s"] = round(now - t.gone_since, 1)
            out[t.key] = entry
        return out

    # Inputs ----------------------------------------------------------------

    def update(self, now: float, observations: dict[str, Observation]) -> list[Action]:
        if self.asleep:
            return []
        actions: list[Action] = []
        for key in sorted(set(self.apps) | set(observations)):
            t = self.apps.setdefault(key, AppTrack(key))
            obs = observations.get(key)
            if obs is not None:
                t.obs = obs
                t.gone_since = None
            elif t.gone_since is None:
                t.gone_since = now
            self._step(t, now, obs is not None, actions)
            if t.state is State.IDLE:
                del self.apps[key]
        return actions

    def record(self, key: str, now: float) -> list[Action]:
        t = self.apps.get(key)
        if t is None or t.state is not State.PROMPTED:
            return [Note(key, "record ignored: no prompt showing")]
        if self.recording_key is not None:
            return [Note(key, f"record refused: {self.recording_key} is recording")]
        actions: list[Action] = []
        t.recording_since = now
        self._go(t, State.RECORDING, "user clicked Record", actions)
        actions.append(StartRecording(key, t.obs.name if t.obs else key))
        return actions

    def start_manual(self, now: float, key: str = MANUAL_KEY,
                     name: str = "In-person meeting") -> list[Action]:
        """Record without a call app (docs/dev/features-2-design.md › 2). Only Stop,
        the 4 h cap and sleep end it."""
        if self.recording_key is not None:
            return [Note(key, f"record refused: {self.recording_key} is recording")]
        actions: list[Action] = []
        t = self.apps[key] = AppTrack(key, obs=Observation(key, name, "meeting", frozenset()),
                                      session_start=now, recording_since=now, manual=True)
        self._go(t, State.RECORDING, "user started a recording", actions)
        actions.append(StartRecording(key, name))
        return actions

    def ignore(self, key: str, now: float) -> list[Action]:
        t = self.apps.get(key)
        if t is None or t.state is not State.PROMPTED:
            return [Note(key, "ignore ignored: no prompt showing")]
        actions: list[Action] = []
        self._go(t, State.SUPPRESSED, "user ignored the prompt", actions)
        n = self.dismisses[key] = self.dismisses.get(key, 0) + 1
        if n == self.s.nudge_after_dismisses:
            actions.append(ShowNudge(key, t.obs.name if t.obs else key, n))
        return actions

    def stop(self, now: float, reason: str = "manual stop") -> list[Action]:
        key = self.recording_key
        if key is None:
            return []
        actions: list[Action] = []
        self._stop(self.apps[key], now, State.SUPPRESSED, reason, actions)
        return actions

    def sleep(self, now: float) -> list[Action]:
        actions: list[Action] = []
        for t in list(self.apps.values()):
            if t.state is State.PROMPTED:
                actions.append(WithdrawPrompt(t.key))
            if t.state in (State.RECORDING, State.STOP_PENDING):
                self._stop(t, now, State.IDLE, "system sleep", actions)
            elif t.state is not State.IDLE:
                self._go(t, State.IDLE, "system sleep", actions)
        self.apps.clear()
        self.asleep = True
        return actions

    def resume(self, now: float, observations: dict[str, Observation]) -> list[Action]:
        """Apps already on the mic after wake count as seen: no prompt."""
        self.asleep = False
        actions: list[Action] = []
        for key, obs in sorted(observations.items()):
            t = self.apps.setdefault(key, AppTrack(key, obs=obs, session_start=now))
            self._go(t, State.SUPPRESSED, "on the mic at resume", actions)
        return actions

    # Internals -------------------------------------------------------------

    def _step(self, t: AppTrack, now: float, present: bool, actions: list[Action]) -> None:
        s = self.s
        gone_for = now - t.gone_since if t.gone_since is not None else 0.0

        if t.state is State.IDLE:
            if present:
                t.session_start = t.candidate_since = now
                self._go(t, State.CANDIDATE, "app opened the mic", actions)

        elif t.state is State.CANDIDATE:
            if not present:
                self._go(t, State.IDLE, "mic closed before dwell", actions)
                return
            other = self.recording_key
            if other is not None:
                self._note(t, f"prompt held: {other} is recording", actions)
                return
            dwell = s.dwell_meeting_s if t.obs.kind == "meeting" else s.dwell_browser_s
            if now - t.candidate_since >= dwell:
                self._go(t, State.PROMPTED, f"on the mic for {dwell:g} s", actions)
                o = t.obs
                actions.append(ShowPrompt(t.key, o.name, o.kind, o.service, o.room))

        elif t.state is State.PROMPTED:
            if not present and gone_for >= s.grace_s:
                self._go(t, State.IDLE, "app left the mic (prompt withdrawn)", actions)
                actions.append(WithdrawPrompt(t.key))

        elif t.state is State.SUPPRESSED:
            if not present and gone_for >= max(s.cooldown_s, s.grace_s):
                self._go(t, State.IDLE, f"stream ended {s.cooldown_s:g} s ago", actions)

        elif t.state is State.RECORDING:
            if now - t.recording_since >= s.max_recording_s:
                self._stop(t, now, State.SUPPRESSED, "4 h cap", actions)
            elif t.manual:
                pass
            elif not present and gone_for >= s.grace_s:
                self._go(t, State.STOP_PENDING, f"app gone > {s.grace_s:g} s", actions)
                self._check_stop(t, now, actions)

        elif t.state is State.STOP_PENDING:
            if present:
                self._go(t, State.RECORDING, "app back on the mic", actions)
            elif now - t.recording_since >= s.max_recording_s:
                self._stop(t, now, State.SUPPRESSED, "4 h cap", actions)
            else:
                self._check_stop(t, now, actions)

    def _check_stop(self, t: AppTrack, now: float, actions: list[Action]) -> None:
        s = self.s
        if now - t.recording_since < s.min_recording_age_s:
            self._note(t, f"stop held: recording younger than {s.min_recording_age_s:g} s", actions)
        elif now - t.session_start < s.min_app_runtime_s:
            self._note(t, f"stop held: app on the mic less than {s.min_app_runtime_s:g} s", actions)
        else:
            self._stop(t, now, State.IDLE, "app left the mic", actions)

    def _stop(self, t: AppTrack, now: float, new: State, reason: str,
              actions: list[Action]) -> None:
        duration = now - t.recording_since if t.recording_since is not None else 0.0
        self._go(t, new, reason, actions)
        actions.append(StopRecording(t.key, reason, round(duration, 1)))
        t.recording_since = None

    def _go(self, t: AppTrack, new: State, reason: str, actions: list[Action]) -> None:
        actions.append(Transition(t.key, t.state, new, reason))
        t.state = new
        t.note = None

    def _note(self, t: AppTrack, message: str, actions: list[Action]) -> None:
        if t.note != message:
            t.note = message
            actions.append(Note(t.key, message))
