# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Tray icon (notify/tray.py) against a fake session bus, and its menu reaching the daemon."""

from __future__ import annotations

import asyncio
import json
import os

from dbus_fast import Message, MessageType, Variant
from dbus_fast.service import ServiceInterface

from wirenote.notify.indicator import IndicatorState, NullIndicator
from wirenote.notify.tray import ITEM_PATH, MENU_PATH, WATCHER, TrayIndicator

from test_daemon import drain, make, prompt


class Reply:
    def __init__(self, body, error=None):
        self.body = body
        self.error_name = error
        self.message_type = MessageType.ERROR if error else MessageType.METHOD_RETURN


class FakeBus:
    """What the tray uses of dbus_fast.aio.MessageBus. Calls into exported objects go through
    dbus-fast's own method table, and every reply and signal is marshalled, as on a real bus."""

    def __init__(self, watcher=True, fail_calls=False, fail_signals=False, fail_register=False,
                 hang_register=False, appear_during_lookup=False, refuse_name=False):
        self.watcher = watcher
        self.fail_calls = fail_calls
        self.fail_register = fail_register
        self.hang_register = hang_register          # a hung kded never answers
        self.appear_during_lookup = appear_during_lookup
        self.refuse_name = refuse_name
        self.fail_signals = fail_signals
        self.exported: dict[str, ServiceInterface] = {}
        self.names: list[str] = []
        self.calls: list[tuple[str, list]] = []
        self.signals: list[tuple[str, list]] = []
        self.handlers = []
        self.disconnected = False

    def export(self, path, iface):
        self.exported[path] = iface
        ServiceInterface._add_bus(iface, self, lambda i, m: None)

    async def request_name(self, name):
        if self.refuse_name:
            raise PermissionError("not allowed to own the name")
        self.names.append(name)

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    def disconnect(self):
        self.disconnected = True

    async def call(self, msg):
        if self.fail_calls:
            raise ConnectionError("bus gone")
        self.calls.append((msg.member, list(msg.body)))
        if msg.member == "NameHasOwner":
            answer = self.watcher and msg.body[0] == WATCHER
            if self.appear_during_lookup:
                # dbus-fast reads the watcher's NameOwnerChanged in the same batch as this
                # reply and hands it to the handlers before the caller resumes
                self.set_watcher(True)
            return Reply([answer])
        if msg.member == "RegisterStatusNotifierItem" and self.hang_register:
            await asyncio.Event().wait()
        if msg.member == "RegisterStatusNotifierItem" and (self.fail_register or not self.watcher):
            return Reply([], "org.freedesktop.DBus.Error.ServiceUnknown")
        return Reply([])

    def _interface_signal_notify(self, iface, interface, member, signature, body, fds=()):
        if self.fail_signals:
            raise ConnectionError("bus gone")
        _marshal(signature, body)
        self.signals.append((member, body))

    # The host's side ---------------------------------------------------------

    def invoke(self, path, member, *args):
        iface = self.exported[path]
        method, = [m for m in ServiceInterface._get_methods(iface) if m.name == member]
        result = method.fn(iface, *args)
        body, _ = ServiceInterface._fn_result_to_body(result, method.out_signature_tree)
        _marshal(method.out_signature, body)
        return body

    def props(self, path):
        iface = self.exported[path]
        out = {}
        for p in ServiceInterface._get_properties(iface):
            value = getattr(iface, p.prop_getter.__name__)
            out[p.name] = Variant(p.signature, value).value   # checks the type, like GetAll
        return out

    def set_watcher(self, present):
        """The panel (or GNOME's extension) starts, stops or restarts."""
        self.watcher = present
        msg = Message(message_type=MessageType.SIGNAL, path="/org/freedesktop/DBus",
                      interface="org.freedesktop.DBus", member="NameOwnerChanged", signature="sss",
                      body=[WATCHER, "" if present else ":1.98", ":1.99" if present else ""])
        for h in self.handlers:
            assert not h(msg)          # never swallows a message meant for someone else

    def registrations(self):
        return len([c for c in self.calls if c[0] == "RegisterStatusNotifierItem"])


async def settle():
    for _ in range(3):
        await asyncio.sleep(0)


def start_tray(d):
    """The daemon starts its tray in the background; wait for that here."""
    async def go():
        task = d._start_indicator()
        if task is not None:
            await task
    asyncio.run(go())


def with_bus(bus):
    async def factory():
        return bus
    return lambda: TrayIndicator(factory)


def _marshal(signature, body):
    Message(message_type=MessageType.SIGNAL, path="/x", interface="x.y", member="Z",
            signature=signature, body=body)._marshall(False)


def started(bus=None):
    bus = bus or FakeBus()

    async def factory():
        return bus

    tray = TrayIndicator(factory)
    assert asyncio.run(tray.start()) is True
    return tray, bus


def menu(bus):
    revision, (root_id, root, children) = bus.invoke(MENU_PATH, "GetLayout", 0, -1, [])
    assert root_id == 0 and root["children-display"].value == "submenu"
    items = []
    for child in children:
        item_id, props, _ = child.value
        items.append((props["label"].value, props["enabled"].value, item_id))
    return revision, items


def labels(bus):
    return [(label, enabled) for label, enabled, _ in menu(bus)[1]]


def item_id(bus, label):
    (found,) = [i for text, _, i in menu(bus)[1] if text == label]
    return found


IDLE = IndicatorState(None, None, None, 0)
PROMPT = IndicatorState(None, None, ("discord", "Discord"), 0)
RECORDING = IndicatorState("Discord", "2026-10-27T14:05:00+01:00", None, 0)


def test_tray_registers_with_the_watcher():
    tray, bus = started()
    name = f"org.freedesktop.StatusNotifierItem-{os.getpid()}-1"
    assert bus.names == [name]
    assert set(bus.exported) == {ITEM_PATH, MENU_PATH}
    assert ("RegisterStatusNotifierItem", [name]) in bus.calls
    item = bus.props(ITEM_PATH)
    assert item["Id"] and item["Menu"] == MENU_PATH and item["ItemIsMenu"] is True
    assert item["Status"] == "Active"          # Passive would hide the icon
    assert bus.props(MENU_PATH)["Version"] == 3
    assert bus.invoke(MENU_PATH, "AboutToShow", 0) == [False]
    # What hosts introspect for: GNOME's extension clicks Activate only if it is there
    item_intro = bus.exported[ITEM_PATH].introspect()
    assert {"Activate", "SecondaryActivate", "ContextMenu", "Scroll"} <= {m.name for m in item_intro.methods}
    menu_intro = bus.exported[MENU_PATH].introspect()
    assert {"GetLayout", "GetGroupProperties", "GetProperty", "Event", "EventGroup", "AboutToShow",
            "AboutToShowGroup"} <= {m.name for m in menu_intro.methods}
    assert "LayoutUpdated" in {s.name for s in menu_intro.signals}
    assert item_intro.to_xml() is not None and menu_intro.to_xml() is not None


def test_registers_again_when_the_watcher_returns():
    hosts = []

    async def run():
        bus = FakeBus()

        async def factory():
            return bus
        tray = TrayIndicator(factory)
        tray.on_host = hosts.append
        assert await tray.start()
        bus.set_watcher(False)                   # the panel restarts: gone...
        assert tray.registered is False
        bus.set_watcher(True)                    # ...and back
        await settle()
        assert tray.registered is True
        return bus
    bus = asyncio.run(run())
    assert bus.registrations() == 2 and hosts == [True, False, True]


def test_icon_appears_when_the_panel_starts_after_wirenote():
    """At login the daemon usually starts before plasmashell or GNOME's extension."""
    hosts = []

    async def run():
        bus = FakeBus(watcher=False)

        async def factory():
            return bus
        tray = TrayIndicator(factory)
        tray.on_host = hosts.append
        assert await tray.start() is False       # no tray host yet...
        assert not bus.disconnected and bus.registrations() == 0
        tray.update(RECORDING)                   # ...the state is kept for when it comes
        assert bus.signals == []
        bus.set_watcher(True)
        await settle()
        return tray, bus
    tray, bus = asyncio.run(run())
    assert tray.registered and bus.registrations() == 1 and hosts == [True]
    assert bus.props(ITEM_PATH)["IconName"] == "media-record"
    assert labels(bus)[2] == ("Stop", True)
    tray.update(IDLE)                            # and it follows the state from then on
    assert ("NewIcon", []) in bus.signals


def test_watcher_appearing_while_start_looks_for_it_still_registers():
    async def run():
        bus = FakeBus(watcher=False, appear_during_lookup=True)

        async def factory():
            return bus
        tray = TrayIndicator(factory)
        await tray.start()                       # the lookup's answer is "no watcher"...
        await settle()
        return tray, bus
    tray, bus = asyncio.run(run())
    assert tray.registered and bus.registrations() >= 1   # ...but its arrival was not lost


def test_menu_layout_per_state():
    tray, bus = started()
    tray.update(IDLE)
    rev0, _ = menu(bus)
    assert labels(bus) == [("Record a meeting", True), ("Open Wirenote", True)]
    assert bus.props(ITEM_PATH)["IconName"] == "audio-input-microphone"

    tray.update(PROMPT)
    rev1, _ = menu(bus)
    assert rev1 > rev0 and ("LayoutUpdated", [rev1, 0]) in bus.signals
    assert labels(bus) == [("Record Discord", True), ("Ignore", True)]
    assert bus.props(ITEM_PATH)["Status"] == "NeedsAttention"
    assert ("NewStatus", ["NeedsAttention"]) in bus.signals

    # Two prompts: Record and Ignore could not tell which call is meant
    tray.update(IndicatorState(None, None, ("discord", "Discord"), 0, other_prompts=1))
    assert labels(bus) == [("2 calls are using your mic", False), ("Open Wirenote", True)]
    assert bus.props(ITEM_PATH)["Status"] == "NeedsAttention"

    tray.update(RECORDING)
    assert labels(bus) == [("Recording Discord since 14:05", False), ("Flag this moment", True),
                           ("Stop", True), ("Open Wirenote", True)]
    item = bus.props(ITEM_PATH)
    assert item["IconName"] == "media-record" and item["Status"] == "Active"
    assert item["ToolTip"][3] == "Recording Discord since 14:05"

    tray.update(IndicatorState("Discord", "2026-10-27T14:05:00+01:00", None, 2))
    assert labels(bus)[0] == ("Recording Discord since 14:05, 2 flags", False)

    # Group and single-item reads agree with the layout
    stop = item_id(bus, "Stop")
    ((got,),) = bus.invoke(MENU_PATH, "GetGroupProperties", [stop], ["label"])
    assert got[0] == stop and got[1]["label"].value == "Stop"
    assert bus.invoke(MENU_PATH, "GetProperty", stop, "label")[0].value == "Stop"


def test_untrusted_names_are_shown_as_text():
    tray, bus = started()
    tray.update(IndicatorState(None, None, ("x", "my_app\nIgnore <b>all</b>"), 0))
    # `_` marks a keyboard mnemonic in dbusmenu labels; a line break would split the menu line
    assert labels(bus)[0] == ("Record my__app Ignore <b>all</b>", True)
    tray.update(IndicatorState("<b>Board</b> & co", "2026-10-27T14:05:00+01:00", None, 0))
    assert bus.props(ITEM_PATH)["ToolTip"][3] == "Recording &lt;b&gt;Board&lt;/b&gt; &amp; co since 14:05"


def test_control_and_direction_characters_never_reach_the_panel():
    tray, bus = started()
    hostile = "Disc‮ord\x00\x1b[31m⁦x‏"           # bidi override, NUL, escape, isolates
    tray.update(IndicatorState(None, None, ("x", hostile), 0))
    assert labels(bus)[0] == ("Record Discord[31mx", True)
    tray.update(IndicatorState(hostile, "2026-10-27T14:05:00+01:00", None, 0))
    assert labels(bus)[0][0] == "Recording Discord[31mx since 14:05"
    assert bus.props(ITEM_PATH)["ToolTip"][3] == "Recording Discord[31mx since 14:05"


def test_launcher_and_tray_share_one_cleaner():
    from wirenote.notify import search_provider, text, tray

    assert search_provider.plain is text.plain and tray.plain is text.plain
    assert text.plain("a‮b\n c", limit=3) == "ab…" and text.plain("a‮b\n c") == "ab c"


def test_menu_clicks_call_on_menu_and_activate_opens():
    tray, bus = started()
    clicks = []
    tray.on_menu = clicks.append
    tray.update(RECORDING)
    bus.invoke(MENU_PATH, "Event", item_id(bus, "Stop"), "clicked", Variant("s", ""), 0)
    bus.invoke(MENU_PATH, "Event", item_id(bus, "Stop"), "opened", Variant("s", ""), 0)
    bus.invoke(MENU_PATH, "Event", item_id(bus, "Recording Discord since 14:05"), "clicked",
               Variant("s", ""), 0)
    flag = item_id(bus, "Flag this moment")
    assert bus.invoke(MENU_PATH, "EventGroup", [[flag, "clicked", Variant("s", ""), 0],
                                                [999, "clicked", Variant("s", ""), 0]]) == [[999]]
    bus.invoke(ITEM_PATH, "Activate", 0, 0)
    assert clicks == ["stop", "flag", "open"]
    tray.update(IDLE)                               # a click from a menu opened before the stop
    bus.invoke(MENU_PATH, "Event", flag, "clicked", Variant("s", ""), 0)
    assert clicks == ["stop", "flag", "open"]


def test_menu_click_reaches_control(tmp_path):
    d = make(tmp_path)
    bus = FakeBus()

    async def factory():
        return bus
    d.make_tray = lambda: TrayIndicator(factory)
    start_tray(d)
    assert isinstance(d.indicator, TrayIndicator)
    received = []
    control = d._control
    d._control = lambda cmd: received.append(cmd) or control(cmd)

    prompt(d)
    assert labels(bus) == [("Record Discord", True), ("Ignore", True)]
    bus.invoke(MENU_PATH, "Event", item_id(bus, "Record Discord"), "clicked", Variant("s", ""), 0)
    asyncio.run(drain(d))
    assert d.recording is not None
    assert labels(bus)[0][0].startswith("Recording Discord since ")
    bus.invoke(MENU_PATH, "Event", item_id(bus, "Flag this moment"), "clicked", Variant("s", ""), 0)
    assert labels(bus)[0][0].endswith(", 1 flag")
    bus.invoke(MENU_PATH, "Event", item_id(bus, "Stop"), "clicked", Variant("s", ""), 0)
    asyncio.run(drain(d))
    assert received == ["record", "flag", "stop"]
    assert d.recording is None and d.jobs.next_queued() is not None
    assert labels(bus) == [("Record a meeting", True), ("Open Wirenote", True)]
    assert json.loads((tmp_path / "status.json").read_text())["tray"] == "shown"


def test_several_prompts_offer_no_button_that_fails(tmp_path):
    from wirenote.detector.tracker import Observation

    from test_daemon import DISCORD

    d = make(tmp_path)
    bus = FakeBus()
    d.make_tray = with_bus(bus)
    start_tray(d)
    two = {**DISCORD, "zoom": Observation("zoom", "Zoom", "meeting", frozenset({2}))}
    for now in (0.0, 2.0):
        d._handle(d.detector.tracker.update(now, two))
    asyncio.run(drain(d))
    assert labels(bus) == [("2 calls are using your mic", False), ("Open Wirenote", True)]


def test_detection_never_waits_for_the_tray(tmp_path, monkeypatch):
    """A watcher that never answers RegisterStatusNotifierItem (a hung kded)."""
    import wirenote.detector.daemon as daemon_mod

    monkeypatch.setenv("WIRENOTE_SOCKET", str(tmp_path / "control.sock"))

    async def no_sleep_signals(on_sleep):
        raise ConnectionError("no system bus")
    monkeypatch.setattr(daemon_mod, "watch_sleep", no_sleep_signals)
    d = make(tmp_path)
    bus = FakeBus(hang_register=True)
    d.make_tray = with_bus(bus)
    reached = []

    async def first_loop():
        reached.append("detecting")
        d._stopping.set()

    async def idle():
        await asyncio.Event().wait()
    d._pw_loop = first_loop
    for name in ("_tick_loop", "_op_loop", "_rec_loop", "_worker_loop", "_housekeeping_loop",
                 "_control_loop", "_calendar_loop", "_update_loop"):
        setattr(d, name, idle)
    asyncio.run(asyncio.wait_for(d.run(), 5))
    assert reached == ["detecting"] and bus.registrations() == 1   # it was still waiting


def test_ignore_from_the_tray_counts_like_the_notification(tmp_path):
    d = make(tmp_path)
    assert d._control("ignore") == {"ok": False, "error": "no prompt showing"}
    nid = prompt(d)
    assert d._control("ignore") == {"ok": True, "app": "discord"}
    asyncio.run(drain(d))
    assert nid in d.notifier.closed
    d._on_closed(nid, 2)                        # the server confirms; not a second dismiss
    assert json.loads((tmp_path / "state.json").read_text())["dismisses"] == {"discord": 1}


def test_open_from_the_tray_opens_the_app(tmp_path, monkeypatch):
    import wirenote.ui.launch as launch

    opened = []
    monkeypatch.setattr(launch, "open_ui", lambda port, route, fallback=None: opened.append(route) or True)
    d = make(tmp_path)
    d._on_menu("open")
    d._on_menu("nonsense")
    assert opened == ["#/"]


def test_tray_errors_are_contained(tmp_path):
    errors = []

    async def no_bus():
        raise FileNotFoundError("no session bus")
    tray = TrayIndicator(no_bus)
    tray.on_error = errors.append
    assert asyncio.run(tray.start()) is False

    bus = FakeBus(fail_calls=True)

    async def factory():
        return bus
    tray = TrayIndicator(factory)
    tray.on_error = errors.append
    assert asyncio.run(tray.start()) is False and bus.disconnected

    async def late_refusal():                   # the watcher appears, then refuses the item
        bus = FakeBus(watcher=False, fail_register=True)

        async def factory():
            return bus
        tray = TrayIndicator(factory)
        tray.on_error = errors.append
        assert await tray.start() is False
        bus.set_watcher(True)
        await settle()
        bus.set_watcher(True)
        await settle()
        return tray
    errors.clear()
    assert asyncio.run(late_refusal()).registered is False
    assert errors == ["tray icon: register failed (TrayCallError: "
                      "org.freedesktop.DBus.Error.ServiceUnknown)"]

    tray, bus = started()
    tray.on_error = errors.append
    errors.clear()
    bus.fail_signals = True
    for s in (PROMPT, RECORDING, IDLE, PROMPT):
        tray.update(s)                               # never raises
    assert len(errors) == 1 and "ConnectionError" in errors[0]
    assert labels(bus)[0] == ("Record Discord", True)   # the menu still follows the state

    def boom(key):
        raise RuntimeError("daemon bug")
    tray.on_menu = boom
    bus.invoke(MENU_PATH, "Event", item_id(bus, "Ignore"), "clicked", Variant("s", ""), 0)
    bus.invoke(ITEM_PATH, "Activate", 0, 0)
    assert len(errors) == 2 and "RuntimeError" in errors[1]

    # And in the daemon: a broken indicator never stops the status file or detection
    class Broken(NullIndicator):
        def update(self, s):
            raise RuntimeError("broken")
    d = make(tmp_path)
    d.indicator = Broken()
    prompt(d)
    assert d._control("record")["ok"]
    asyncio.run(drain(d))
    assert json.loads((tmp_path / "status.json").read_text())["recording"] == "discord"
    log = [json.loads(line) for line in (tmp_path / "detector.jsonl").open()]
    assert len([e for e in log if "tray update failed" in e.get("message", "")]) == 1
    assert json.loads((tmp_path / "status.json").read_text())["tray"] == "failed"
    d._set_tray("shown")                         # a late registration does not hide that
    assert json.loads((tmp_path / "status.json").read_text())["tray"] == "failed"


def test_tray_setup_failure_is_not_called_a_missing_tray(tmp_path):
    async def no_bus():
        raise FileNotFoundError("no session bus")
    for n, make_tray in enumerate((lambda: TrayIndicator(no_bus),
                                   with_bus(FakeBus(refuse_name=True)))):
        state = tmp_path / str(n)
        state.mkdir()
        d = make(state)
        d.make_tray = make_tray
        start_tray(d)
        assert json.loads((state / "status.json").read_text())["tray"] == "failed"


def test_a_refused_registration_is_a_failure_not_a_missing_tray(tmp_path):
    """A tray host exists but refuses the icon: the doctor must not give the GNOME hint."""
    bus = FakeBus(fail_register=True)               # the watcher is there and says no
    d = make(tmp_path)
    d.make_tray = with_bus(bus)
    start_tray(d)
    assert d.indicator.registered is False and d.indicator.register_failed is True
    assert json.loads((tmp_path / "status.json").read_text())["tray"] == "failed"

    later = tmp_path / "later"                      # the panel restarts and then refuses
    later.mkdir()
    bus = FakeBus()
    d = make(later)
    d.make_tray = with_bus(bus)
    start_tray(d)
    assert json.loads((later / "status.json").read_text())["tray"] == "shown"

    async def panel_restarts_and_refuses():
        bus.set_watcher(False)
        await settle()
        bus.fail_register = True
        bus.set_watcher(True)
        await settle()
    asyncio.run(panel_restarts_and_refuses())
    assert json.loads((later / "status.json").read_text())["tray"] == "failed"

    async def panel_accepts_again():
        bus.fail_register = False
        bus.set_watcher(True)
        await settle()
    asyncio.run(panel_accepts_again())
    assert d.indicator.register_failed is False
    assert json.loads((later / "status.json").read_text())["tray"] == "shown"


def test_tray_stop_cancels_its_tasks_and_leaves_the_bus():
    async def go():
        bus = FakeBus(watcher=False, hang_register=True)    # a hung host never answers
        tray = with_bus(bus)()
        await tray.start()
        bus.set_watcher(True)
        await settle()
        tasks = list(tray._tasks)
        assert tasks and not any(t.done() for t in tasks)
        tray.stop()
        await settle()
        assert all(t.cancelled() for t in tasks) and bus.disconnected
        assert not tray.connected and not tray.registered
        tray.update(RECORDING)                              # after stop: stored, nothing sent
        tray.stop()                                         # twice is fine
        return bus
    bus = asyncio.run(asyncio.wait_for(go(), 5))
    assert not [s for s in bus.signals if s[0] == "NewIcon"]
    NullIndicator().stop()


def test_daemon_closes_the_tray_at_shutdown(tmp_path, monkeypatch):
    import signal

    from wirenote.detector import daemon as daemon_mod

    bus = FakeBus()
    d = make(tmp_path)
    d.make_tray = with_bus(bus)
    monkeypatch.setattr(daemon_mod, "control_socket_path", lambda: tmp_path / "ctl.sock")
    monkeypatch.setattr(daemon_mod, "watch_sleep", lambda cb: asyncio.sleep(0))

    async def idle(self=None):
        await asyncio.Event().wait()
    for name in ("_pw_loop", "_tick_loop", "_worker_loop", "_housekeeping_loop", "_calendar_loop",
                 "_update_loop", "_control_loop"):
        monkeypatch.setattr(d, name, idle)

    async def go():
        run = asyncio.create_task(d.run())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if d.indicator.registered:
                break
        assert d.indicator.registered
        os.kill(os.getpid(), signal.SIGTERM)
        await run
    asyncio.run(asyncio.wait_for(go(), 10))
    assert bus.disconnected and not d.indicator.connected


def test_tray_creation_error_leaves_the_daemon_running(tmp_path):
    def broken():
        raise RuntimeError("dbus-fast missing a piece")
    d = make(tmp_path)
    d.make_tray = broken
    assert d._start_indicator() is None             # no exception reaches run()
    assert isinstance(d.indicator, NullIndicator)
    d._write_status(force=True)
    assert json.loads((tmp_path / "status.json").read_text())["tray"] == "failed"
    log = [json.loads(line) for line in (tmp_path / "detector.jsonl").open()]
    assert any(e["event"] == "warning" and "RuntimeError" in e["message"] for e in log)
    prompt(d)
    assert d._control("record")["ok"]               # detection and recording carry on
