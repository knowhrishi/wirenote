# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""State machine against a fake clock (docs/dev/PLAN.md › State machine, › Defaults)."""

from __future__ import annotations

from dataclasses import replace

from wirenote.config.settings import Settings
from wirenote.detector.tracker import (Note, Observation, ShowNudge, ShowPrompt, StartRecording,
                                       State, StopRecording, Tracker, WithdrawPrompt)


def obs(key="discord", kind="meeting", streams=(1,), service=None, room=None):
    return {key: Observation(key, key.title(), kind, frozenset(streams), service, room)}


def run(tracker, start, end, observations, step=1.0):
    """Call update() every `step` s on [start, end] with the same observations."""
    out = []
    t = start
    while t <= end + 1e-9:
        out += [(t, a) for a in tracker.update(t, observations)]
        t += step
    return out


def of_type(timeline, cls):
    return [(t, a) for t, a in timeline if isinstance(a, cls)]


def state(tracker, key="discord"):
    t = tracker.apps.get(key)
    return t.state if t else State.IDLE


def test_blip_shorter_than_dwell_never_prompts():
    tr = Tracker(Settings())
    tl = run(tr, 0, 1, obs())
    tl += run(tr, 2, 10, {})
    assert of_type(tl, ShowPrompt) == []
    assert state(tr) is State.IDLE


def test_meeting_app_prompts_after_2s_dwell():
    tr = Tracker(Settings())
    tl = run(tr, 0, 5, obs(), step=0.5)
    prompts = of_type(tl, ShowPrompt)
    assert [t for t, _ in prompts] == [2.0]
    assert state(tr) is State.PROMPTED


def test_browser_prompts_after_5s_dwell():
    tr = Tracker(Settings())
    tl = run(tr, 0, 8, obs("chrome", kind="browser"), step=0.5)
    assert [t for t, _ in of_type(tl, ShowPrompt)] == [5.0]


def test_closing_mic_before_dwell_resets_it():
    tr = Tracker(Settings())
    tl = run(tr, 0, 4, obs("chrome", kind="browser"))
    tr.update(4.5, {})
    tl += run(tr, 5, 9, obs("chrome", kind="browser"))
    # dwell restarts at 5.0, so no prompt before 10.0
    assert of_type(tl, ShowPrompt) == []


def test_prompt_survives_reopen_within_grace():
    tr = Tracker(Settings())
    tl = run(tr, 0, 3, obs())
    tl += [(3.5, a) for a in tr.update(3.5, {})]
    tl += run(tr, 4, 6, obs(streams=(2,)))   # reopened with a new stream serial
    assert of_type(tl, WithdrawPrompt) == []
    assert len(of_type(tl, ShowPrompt)) == 1
    assert state(tr) is State.PROMPTED


def test_prompt_withdrawn_when_app_leaves_for_longer_than_grace():
    tr = Tracker(Settings())
    tl = run(tr, 0, 3, obs())
    tl += run(tr, 4, 9, {})
    withdrawn = of_type(tl, WithdrawPrompt)
    assert [t for t, _ in withdrawn] == [8.0]   # gone since 4.0, grace 4 s
    assert state(tr) is State.IDLE


def test_ignore_stays_silent_until_stream_ends_plus_cooldown():
    tr = Tracker(Settings())
    run(tr, 0, 3, obs())
    tr.ignore("discord", 3.0)
    tl = run(tr, 4, 100, obs())               # still in the call: no prompt
    assert of_type(tl, ShowPrompt) == []
    tl = run(tr, 101, 150, {})                 # left at 101; cooldown until 161
    tl += run(tr, 151, 155, obs())             # back within the cooldown
    assert of_type(tl, ShowPrompt) == []
    assert state(tr) is State.SUPPRESSED
    tl = run(tr, 156, 230, {})                 # left at 156 -> idle at 216
    assert state(tr) is State.IDLE
    tl = run(tr, 231, 235, obs())
    assert [t for t, _ in of_type(tl, ShowPrompt)] == [233.0]


def test_record_then_auto_stop_after_grace():
    tr = Tracker(Settings())
    run(tr, 0, 2, obs())
    acts = tr.record("discord", 3.0)
    assert any(isinstance(a, StartRecording) for a in acts)
    run(tr, 3, 100, obs())
    tl = run(tr, 101, 110, {})
    stops = of_type(tl, StopRecording)
    assert [(t, a.reason) for t, a in stops] == [(105.0, "app left the mic")]
    assert stops[0][1].duration_s == 102.0
    assert state(tr) is State.IDLE


def test_young_recording_is_not_auto_stopped_before_30s():
    tr = Tracker(Settings())
    run(tr, 0, 2, obs())
    tr.record("discord", 2.0)
    tl = run(tr, 3, 5, obs())
    tl += run(tr, 6, 40, {})                   # gone at 6; grace ends 10; age 30 at 32
    notes = [a.message for _, a in of_type(tl, Note)]
    assert notes == ["stop held: recording younger than 30 s"]   # logged once
    assert [t for t, _ in of_type(tl, StopRecording)] == [32.0]


def test_app_runtime_guard_holds_stop():
    s = replace(Settings(), min_recording_age_s=0, min_app_runtime_s=60)
    tr = Tracker(s)
    run(tr, 0, 2, obs())
    tr.record("discord", 2.0)
    tl = run(tr, 3, 10, obs())
    tl += run(tr, 11, 70, {})
    assert [t for t, _ in of_type(tl, StopRecording)] == [60.0]


def test_app_back_during_stop_pending_keeps_recording():
    s = replace(Settings(), min_recording_age_s=1000)
    tr = Tracker(s)
    run(tr, 0, 2, obs())
    tr.record("discord", 2.0)
    run(tr, 3, 10, {})
    assert state(tr) is State.STOP_PENDING
    run(tr, 11, 12, obs())
    assert state(tr) is State.RECORDING


def test_only_one_recording_other_app_prompt_held():
    tr = Tracker(Settings())
    run(tr, 0, 2, obs("discord"))
    tr.record("discord", 2.0)
    both = {**obs("discord"), **obs("chrome", kind="browser", streams=(7,))}
    tl = run(tr, 3, 20, both)
    assert of_type(tl, ShowPrompt) == []
    assert [a.message for _, a in of_type(tl, Note)] == ["prompt held: discord is recording"]
    tr.stop(21.0)
    tl = run(tr, 21, 22, both)
    assert [a.key for _, a in of_type(tl, ShowPrompt)] == ["chrome"]


def test_manual_stop_does_not_reprompt_during_the_call():
    tr = Tracker(Settings())
    run(tr, 0, 2, obs())
    tr.record("discord", 2.0)
    acts = tr.stop(50.0)
    assert [a.reason for a in acts if isinstance(a, StopRecording)] == ["manual stop"]
    tl = run(tr, 51, 200, obs())
    assert of_type(tl, ShowPrompt) == []
    assert state(tr) is State.SUPPRESSED


def test_four_hour_cap():
    tr = Tracker(Settings())
    run(tr, 0, 2, obs())
    tr.record("discord", 2.0)
    tl = run(tr, 3, 4 * 3600 + 10, obs(), step=60)
    stops = of_type(tl, StopRecording)
    assert len(stops) == 1 and stops[0][1].reason == "4 h cap"
    assert state(tr) is State.SUPPRESSED


def test_sleep_withdraws_prompt_and_stops_recording_resume_does_not_prompt():
    tr = Tracker(Settings())
    both = {**obs("discord"), **obs("chrome", kind="browser", streams=(7,))}
    run(tr, 0, 6, both)
    tr.record("discord", 6.0)
    acts = tr.sleep(10.0)
    assert {type(a) for a in acts} >= {WithdrawPrompt, StopRecording}
    assert tr.update(11.0, both) == []                 # asleep: ignored
    tr.resume(500.0, both)
    tl = run(tr, 501, 600, both)
    assert of_type(tl, ShowPrompt) == []
    assert state(tr, "discord") is State.SUPPRESSED


def test_nudge_after_five_dismisses():
    tr = Tracker(Settings())
    nudges = []
    t = 0.0
    for _ in range(5):
        run(tr, t, t + 2, obs())
        nudges += [a for a in tr.ignore("discord", t + 2) if isinstance(a, ShowNudge)]
        run(tr, t + 3, t + 70, {})
        t += 100
    assert [(n.key, n.dismisses) for n in nudges] == [("discord", 5)]


def test_clicks_without_prompt_are_ignored():
    tr = Tracker(Settings())
    assert isinstance(tr.record("discord", 0.0)[0], Note)
    assert isinstance(tr.ignore("discord", 0.0)[0], Note)
    assert tr.stop(0.0) == []


def test_manual_recording_never_auto_stops(settings):
    from wirenote.detector.tracker import MANUAL_KEY, StartRecording, StopRecording, Tracker

    tr = Tracker(settings)
    acts = tr.start_manual(0.0)
    assert any(isinstance(a, StartRecording) and a.key == MANUAL_KEY for a in acts)
    for t in range(1, 600, 5):
        assert not any(isinstance(a, StopRecording) for a in tr.update(float(t), {}))
    assert tr.recording_key == MANUAL_KEY
    assert tr.start_manual(700.0)[0].message.startswith("record refused")
    stop = [a for a in tr.stop(700.0) if isinstance(a, StopRecording)]
    assert stop and stop[0].duration_s == 700.0
    cap = Tracker(settings)
    cap.start_manual(0.0)
    assert any(isinstance(a, StopRecording) and a.reason == "4 h cap"
               for a in cap.update(settings.max_recording_s + 1, {}))
