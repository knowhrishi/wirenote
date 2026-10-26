# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Replay the scrubbed M0 traces through the whole detector on a fake clock.

Expected timings come from docs/dev/m0-findings.md and docs/dev/PLAN.md › Defaults.
"""

from __future__ import annotations

from wirenote.detector.core import Identified
from wirenote.detector.tracker import ShowPrompt, StartRecording, StopRecording, WithdrawPrompt

from conftest import load_trace, replay


def of_type(timeline, cls):
    return [(t, e) for t, e in timeline if isinstance(e, cls)]


def test_chrome_identity_and_pid_check():
    _, tl = replay(load_trace("jitsi-chrome"))
    ids = [e for _, e in of_type(tl, Identified)]
    assert len(ids) == 1                      # one client: Chrome's audio service
    ident = ids[0]
    assert ident.key == "chrome" and ident.kind == "browser"
    assert ident.matched_by == "exe"
    assert ident.evidence["pid_source"] == "application.process.id"
    assert ident.evidence["pid_ok"] is True
    assert ident.evidence["parents"] == ("chrome", "systemd")   # browser, then systemd --user


def test_chrome_prompt_after_browser_dwell_and_withdraw_after_grace():
    _, tl = replay(load_trace("jitsi-chrome"))
    prompts = of_type(tl, ShowPrompt)
    # Pre-join stream 5.98-9.48 resets at the Join reopen; the call stream
    # runs from 9.60, so the 5 s dwell ends at the first tick after 14.60.
    # The 47 ms stream at 51.6 never reached "running" and must not count.
    assert [t for t, _ in prompts] == [15.0, 185.0]
    assert all(p.name == "Chrome" and p.service is None for _, p in prompts)
    withdraws = of_type(tl, WithdrawPrompt)
    assert [t for t, _ in withdraws] == [31.0, 205.0]   # left at 26.32 / 200.87, grace 4 s


def test_chrome_reopen_within_grace_keeps_one_prompt():
    # jitsi-chrome-marked: the stream closes at 27.67 and reopens at 28.72.
    _, tl = replay(load_trace("jitsi-chrome-marked"))
    assert len(of_type(tl, ShowPrompt)) == 1
    assert [t for t, _ in of_type(tl, WithdrawPrompt)] == [64.0]   # left at 59.53


def test_chrome_second_parallel_stream_during_mute_is_one_app():
    # jitsi-chrome-marked2: stream B runs 33.2-40.6 next to stream A.
    _, tl = replay(load_trace("jitsi-chrome-marked2"))
    assert [t for t, _ in of_type(tl, ShowPrompt)] == [11.0]
    assert [t for t, _ in of_type(tl, WithdrawPrompt)] == [51.0]


def test_firefox_title_makes_a_jitsi_meeting_with_short_dwell():
    _, tl = replay(load_trace("jitsi-firefox"))
    ids = [e for _, e in of_type(tl, Identified)]
    assert {(i.key, i.matched_by) for i in ids} == {("firefox", "exe")}
    prompts = of_type(tl, ShowPrompt)
    # Streams run from 22.35 and 227.73; meeting dwell is 2 s.
    assert [t for t, _ in prompts] == [25.0, 230.0]
    p = prompts[0][1]
    assert (p.kind, p.service, p.room) == ("meeting", "Jitsi", "Room")


def test_firefox_record_stops_within_10s_of_leaving():
    def respond(event, t):
        if isinstance(event, ShowPrompt):
            return (3.0, "record")

    _, tl = replay(load_trace("jitsi-firefox"), respond=respond)
    starts = of_type(tl, StartRecording)
    stops = of_type(tl, StopRecording)
    assert len(starts) == 2 and len(stops) == 2
    for (t, stop), left in zip(stops, (190.93, 306.14)):
        assert stop.reason == "app left the mic"
        assert 0 < t - left <= 10      # docs/dev/PLAN.md › M2 exit: stop within 10 s of leaving


def test_ignored_call_stays_silent():
    def respond(event, t):
        if isinstance(event, ShowPrompt):
            return (1.0, "ignore")

    det, tl = replay(load_trace("jitsi-chrome-marked2"), respond=respond)
    assert len(of_type(tl, ShowPrompt)) == 1
    assert of_type(tl, WithdrawPrompt) == []   # suppressed, not prompted anymore
    assert det.tracker.dismisses == {"chrome": 1}
    assert det.tracker.apps == {}              # back to idle after stream end + 60 s
