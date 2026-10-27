# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Daemon glue: notification clicks and closes, with a fake notification server."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from wirenote.config.apps import DEFAULT_APPS_TOML, parse_apps
from wirenote.config.settings import Settings
from wirenote.detector.daemon import Daemon
from wirenote.detector.tracker import Observation, State
from wirenote.notify.dbus import CLOSED_BY_CALL, CLOSED_DISMISSED

from test_capture import FakeChild

DISCORD = {"discord": Observation("discord", "Discord", "meeting", frozenset({1}))}


class FakeNotifier:
    has_actions = True
    server = "fake"

    def __init__(self) -> None:
        self.shown: dict[int, tuple] = {}
        self.closed: list[int] = []
        self._next = 100

    async def notify(self, summary, body="", actions=(), timeout_ms=0, urgency=1):
        self._next += 1
        self.shown[self._next] = (summary, body, [k for k, _ in actions], timeout_ms)
        return self._next

    async def close(self, nid):
        self.closed.append(nid)


def make(tmp_path):
    settings = replace(Settings(), recordings_dir=str(tmp_path / "recordings"))
    d = Daemon(settings, parse_apps(DEFAULT_APPS_TOML), tmp_path)
    d.notifier = FakeNotifier()
    d.notify_ok = True
    d.children = []

    async def spawn(argv):
        d.children.append(FakeChild(argv))
        return d.children[-1]

    d.spawn = spawn
    return d


async def drain(d):
    while not (d._ops.empty() and d._rec_ops.empty()):
        for q in (d._rec_ops, d._ops):
            while not q.empty():
                fn, arg = q.get_nowait()
                await fn(arg)


def prompt(d, t=0.0):
    for now in (t, t + 2.0):
        d._handle(d.detector.tracker.update(now, DISCORD))
    asyncio.run(drain(d))
    (nid, (summary, _, actions, timeout)), = [
        (k, v) for k, v in d.notifier.shown.items() if v[0] == "Discord is using your mic"]
    assert actions == ["record", "ignore"] and timeout == 0
    return nid


def test_record_click_records_and_auto_stop_queues_a_job(tmp_path):
    d = make(tmp_path)
    nid = prompt(d)
    d._on_action(nid, "record")
    asyncio.run(drain(d))
    rec = [k for k, v in d.notifier.shown.items() if v[0] == "Recording Discord"]
    assert len(rec) == 1 and d.notifier.shown[rec[0]][2] == ["stop"]
    assert d.recording is not None and len(d.children) == 2
    rec_dir = d.recording.dir
    d._on_closed(nid, CLOSED_DISMISSED)          # server closes the prompt after the click
    assert d.detector.tracker.dismisses == {}    # an answered prompt is not a dismiss

    tr = d.detector.tracker
    tr.apps["discord"].recording_since = -100.0  # pretend the recording is old enough
    tr.apps["discord"].session_start = -100.0
    for now in (10.0, 15.0):
        d._handle(tr.update(now, {}))
    asyncio.run(drain(d))
    assert rec[0] in d.notifier.closed
    saved = [v for v in d.notifier.shown.values() if v[0] == "Recording saved"]
    assert saved and "app left the mic" in saved[0][1]
    assert d.recording is None
    assert d.jobs.next_queued().recording == rec_dir
    log = [json.loads(line)["event"] for line in (tmp_path / "detector.jsonl").open()]
    assert log.count("capture_start") == 1 and log.count("capture_stop") == 1


def test_control_socket_record_and_stop(tmp_path):
    d = make(tmp_path)
    assert d._control("stop") == {"ok": False, "error": "not recording"}
    nid = prompt(d)
    assert d._control("record") == {"ok": True, "app": "discord"}
    asyncio.run(drain(d))
    assert nid in d.notifier.closed and d.recording is not None
    assert d._control("stop") == {"ok": True, "app": "discord"}
    asyncio.run(drain(d))
    assert d.recording is None and d.jobs.next_queued() is not None
    assert d.detector.tracker.apps["discord"].state is State.SUPPRESSED


def test_closing_prompt_without_action_counts_as_ignore(tmp_path):
    d = make(tmp_path)
    nid = prompt(d)
    d._on_closed(nid, CLOSED_DISMISSED)
    assert d.detector.tracker.apps["discord"].state is State.SUPPRESSED
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["dismisses"] == {"discord": 1}


def test_withdrawn_prompt_close_is_not_a_dismiss(tmp_path):
    d = make(tmp_path)
    nid = prompt(d)
    for now in (3.0, 8.0):                       # left the mic, grace passes
        d._handle(d.detector.tracker.update(now, {}))
    asyncio.run(drain(d))
    assert nid in d.notifier.closed
    d._on_closed(nid, CLOSED_BY_CALL)
    assert d.detector.tracker.dismisses == {}


def test_always_ignore_from_nudge_is_persisted(tmp_path):
    d = make(tmp_path)
    d.detector.tracker.dismisses["discord"] = 4
    nid = prompt(d)
    d._on_action(nid, "ignore")
    asyncio.run(drain(d))
    nudge = [k for k, v in d.notifier.shown.items() if v[2] == ["always-ignore", "keep"]]
    assert len(nudge) == 1
    d._on_action(nudge[0], "always-ignore")
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved == {"dismisses": {"discord": 5}, "ignored": ["discord"]}
    assert "discord" in d.detector.ignored


# Notification markup ----------------------------------------------------

def test_notification_text_is_escaped():
    import asyncio

    from wirenote.notify.dbus import Notifier, escape_markup

    assert escape_markup('<a href="x">Click</a> & go') == '&lt;a href="x"&gt;Click&lt;/a&gt; &amp; go'
    sent = []

    class Iface:
        async def call_notify(self, app, rid, icon, summary, body, actions, hints, timeout):
            sent.append((summary, body))
            return 1
    n = Notifier()
    n._iface = Iface()
    asyncio.run(n.notify("<b>Room</b>", '<a href="https://evil">Record</a> | Jitsi Meet'))
    assert sent == [("&lt;b&gt;Room&lt;/b&gt;", '&lt;a href="https://evil"&gt;Record&lt;/a&gt; | Jitsi Meet')]


# Logs without private data ----------------------------------------------

def test_prompt_log_has_no_room_or_title():
    import asyncio

    from wirenote.detector.daemon import Daemon, room_hash
    from wirenote.detector.tracker import ShowPrompt

    lines = []

    class Log:
        def write(self, event, **data):
            lines.append((event, data))

    class Event:
        title = "Secret board meeting"

    fake = type("D", (), {})()
    fake.log = Log()
    fake._prompt_events = {}
    fake._ops = asyncio.Queue()
    fake._calendar_event = lambda service: Event()
    fake._write_status = lambda force=False: None
    fake._show_prompt = None
    Daemon._handle(fake, [ShowPrompt("firefox", "Firefox", "browser", "Jitsi", "Secret Room")])
    (event, data), = [l for l in lines if l[0] == "prompt"]
    flat = repr(data)
    assert "Secret Room" not in flat and "Secret board" not in flat
    assert data["room_hash"] == room_hash("Secret Room") and len(data["room_hash"]) == 8
    assert data["calendar"] is True


# Desktops without Klipper or notification buttons --------------------------------

class _FakeNotifier:
    def __init__(self, actions=True):
        self.sent = []
        self.has_actions = actions

    async def notify(self, summary, body="", actions=(), timeout_ms=0, urgency=1):
        self.sent.append((summary, body, list(actions)))
        return len(self.sent)

    async def copy_to_clipboard(self, text):
        raise RuntimeError("no klipper")


def _fake_daemon(notifier):
    lines = []

    class Log:
        def write(self, event, **data):
            lines.append((event, data))
    d = type("D", (), {})()
    d.notifier, d.log, d._shown, d._prompt_ids = notifier, Log(), {}, {}
    d._update_kick = asyncio.Event()
    return d, lines


def test_consent_line_shown_when_no_clipboard_works(monkeypatch):
    import asyncio

    from wirenote.detector.daemon import Daemon
    from wirenote.notify import clipboard

    async def nothing(text, klipper=None):
        return None
    monkeypatch.setattr(clipboard, "copy", nothing)
    n = _FakeNotifier()
    d, lines = _fake_daemon(n)
    asyncio.run(Daemon._copy_consent(d, "OK if I record?"))
    (summary, body, _), = n.sent
    assert "Copy this into the call chat" in summary and body == "OK if I record?"


def test_prompt_without_buttons_says_how_to_record():
    import asyncio

    from wirenote.detector.daemon import Daemon
    from wirenote.detector.tracker import ShowPrompt

    n = _FakeNotifier(actions=False)
    d, _ = _fake_daemon(n)
    asyncio.run(Daemon._show_prompt(d, (ShowPrompt("chrome", "Chrome", "browser", None, None), None)))
    assert "wirenote record" in n.sent[0][1]


def test_reload_settings_control(tmp_path, monkeypatch):
    from dataclasses import replace

    from wirenote.config.settings import Settings
    from wirenote.detector.daemon import Daemon

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = tmp_path / "wirenote" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('consent_line = "new line"\n')
    d, lines = _fake_daemon(_FakeNotifier())
    d.settings = replace(Settings(), consent_line="old")
    d.detector = type("Det", (), {"tracker": None})()
    assert Daemon._control(d, "reload-settings")["ok"] is True
    assert d.settings.consent_line == "new line"
    cfg.write_text("nonsense = = =\n")
    assert Daemon._control(d, "reload-settings")["ok"] is False
    assert d.settings.consent_line == "new line"


def _record_and_stop(d, stop="stop"):
    prompt(d)
    assert d._control("record")["ok"]
    asyncio.run(drain(d))
    rec_id = d.recording.id
    assert d._control(stop)["ok"]
    asyncio.run(drain(d))
    return rec_id


def test_call_opens_in_the_ui_when_the_recording_stops(tmp_path, monkeypatch):
    import wirenote.ui.launch as launch

    opened = []
    monkeypatch.setattr(launch, "open_ui",
                        lambda port, route, fallback=None: opened.append((route, fallback)) or True)
    rec_id = _record_and_stop(make(tmp_path))
    assert opened == [(f"#/call/{rec_id}", None)]   # never the Markdown file instead
    _record_and_stop(make(tmp_path / "again"), stop="stop-in-app")
    assert len(opened) == 1                         # Stop in the app: it shows the call already


def test_open_call_after_recording_can_be_turned_off(tmp_path, monkeypatch):
    import wirenote.ui.launch as launch

    opened = []
    monkeypatch.setattr(launch, "open_ui", lambda *a, **k: opened.append(a) or True)
    d = make(tmp_path)
    d.settings = replace(d.settings, open_call_after_recording=False)
    _record_and_stop(d)
    assert opened == []


def test_new_version_is_offered_once_and_never_during_a_call(tmp_path, monkeypatch):
    import wirenote.ui.launch as launch
    from wirenote import updates

    (tmp_path / updates.CHECK).write_text(json.dumps({"checked": 1.0, "latest": "99.0.0"}))
    d = make(tmp_path)
    prompt(d)
    assert d._control("record")["ok"]
    asyncio.run(drain(d))
    d._offer_update()
    asyncio.run(drain(d))
    assert not [v for v in d.notifier.shown.values() if "99.0.0" in v[0]]     # recording
    assert d._control("stop")["ok"]
    asyncio.run(drain(d))
    d._offer_update()
    d._offer_update()
    asyncio.run(drain(d))
    offers = [(k, v) for k, v in d.notifier.shown.items() if v[0] == "Wirenote 99.0.0 is available"]
    assert len(offers) == 1 and offers[0][1][2] == ["open"]
    opened = []
    monkeypatch.setattr(launch, "open_ui", lambda port, route, fallback=None: opened.append(route) or True)
    d._on_action(offers[0][0], "open")
    assert opened == ["#/updates"]
    d2 = make(tmp_path)                    # a restart does not offer it again
    d2._offer_update()
    asyncio.run(drain(d2))
    assert not [v for v in d2.notifier.shown.values() if "99.0.0" in v[0]]


def test_update_loop_checks_only_when_turned_on(tmp_path, monkeypatch):
    from wirenote import updates

    checked = []
    monkeypatch.setattr(updates, "check", lambda state_dir: checked.append(state_dir) or
                        {"latest": None, "error": None})

    async def one_round(d):
        task = asyncio.create_task(d._update_loop())
        await asyncio.sleep(0.05)
        task.cancel()

    d = make(tmp_path)
    asyncio.run(one_round(d))
    assert checked == []
    d.settings = replace(d.settings, update_check=True)
    asyncio.run(one_round(d))
    assert checked == [tmp_path]


def test_flag_rules(tmp_path):
    from wirenote.capture.recorder import read_flags

    d = make(tmp_path)
    assert d._control("flag") == {"ok": False, "error": "not recording"}
    prompt(d)
    assert d._control("record")["ok"]
    asyncio.run(drain(d))
    reply = d._control("flag")
    assert reply["ok"] is True and reply["app"] == "discord" and reply["t"] >= 0
    assert read_flags(d.recording.dir) == [reply["t"]]
    assert d._control("flag")["ok"] is True                 # twice within 2 s: stored once
    assert len(read_flags(d.recording.dir)) == 1
    d._write_status(force=True)
    assert json.loads((tmp_path / "status.json").read_text())["flags"] == 1
    rec_dir = d.recording.dir
    assert d._control("stop")["ok"]
    asyncio.run(drain(d))
    assert read_flags(rec_dir) == [reply["t"]]              # kept after the stop
    assert d._control("flag") == {"ok": False, "error": "not recording"}
    assert json.loads((tmp_path / "status.json").read_text())["flags"] == 0


def test_toggle_starts_and_stops_a_manual_recording(tmp_path):
    d = make(tmp_path)
    assert d._control("toggle") == {"ok": True, "app": "manual"}
    asyncio.run(drain(d))
    assert d.recording is not None and d.recording.meta.app == "manual"
    assert d._control("toggle")["ok"] is True               # recording: stop
    asyncio.run(drain(d))
    assert d.recording is None and d.jobs.next_queued() is not None


def test_toggle_records_the_one_prompt_and_refuses_several(tmp_path):
    d = make(tmp_path)
    nid = prompt(d)
    assert d._control("toggle") == {"ok": True, "app": "discord"}
    asyncio.run(drain(d))
    assert d.recording is not None and nid in d.notifier.closed
    assert d._control("toggle")["ok"] is True
    asyncio.run(drain(d))

    other = tmp_path / "two"
    other.mkdir()
    d = make(other)
    two = {**DISCORD, "zoom": Observation("zoom", "Zoom", "meeting", frozenset({2}))}
    for now in (0.0, 2.0):
        d._handle(d.detector.tracker.update(now, two))
    asyncio.run(drain(d))
    reply = d._control("toggle")
    assert reply["ok"] is False and reply["error"].startswith("several prompts: ")
    assert d.recording is None
