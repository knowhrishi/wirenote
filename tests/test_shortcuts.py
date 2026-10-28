# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Global shortcuts (notify/shortcuts.py) against a fake xdg-desktop-portal, and the keys
reaching the daemon, the doctor, `wirenote setup shortcuts` and the app's Settings."""

from __future__ import annotations

import asyncio
import io
import json
import os
from dataclasses import replace

import pytest
from dbus_fast import Message, MessageType, Variant
from dbus_fast._private.unmarshaller import Unmarshaller

from wirenote import APP_ID
from wirenote.notify import shortcuts
from wirenote.notify.shortcuts import (BOUND, DENIED, FAILED, GLOBAL_SHORTCUTS, NO_PORTAL, PORTAL,
                                       PORTAL_PATH, REGISTRY, SHORTCUTS, start_shortcuts)

from test_daemon import drain, make

UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"


def _roundtrip(msg: Message) -> Message:
    """What the other side reads: marshalled and unmarshalled by dbus-fast, as on a real bus."""
    u = Unmarshaller(io.BytesIO(msg._marshall(False)))
    u.unmarshall()
    return u.message


class FakePortal:
    """What notify/shortcuts.py uses of dbus_fast.aio.MessageBus, with xdg-desktop-portal 1.22
    behind it: Register, then CreateSession and BindShortcuts, each answered by a Response
    signal on the request path the caller predicted."""

    unique_name = ":1.42"

    def __init__(self, portal=True, interface=True, registry=True, refuse_register=None,
                 session_answer=0, bind_answer=0, refuse_session=None, hang=None,
                 respond_first=False):
        self.portal = portal                    # org.freedesktop.portal.Desktop on the bus
        self.interface = interface              # GNOME 46/47: a portal without GlobalShortcuts
        self.registry = registry                # portal 1.19.4+
        self.refuse_register = refuse_register  # an error name
        self.session_answer = session_answer    # Response codes: 0 ok, 1 cancelled, 2 other
        self.bind_answer = bind_answer
        self.refuse_session = refuse_session    # an error name
        self.hang = hang                        # a method the portal never answers
        self.respond_first = respond_first      # the Response read in one batch with the reply
        self.calls: list[tuple[str, str, list]] = []
        self.handlers = []
        self.disconnected = False
        self.session = None
        self.registered = None
        self._serial = 0

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    def remove_message_handler(self, handler):
        self.handlers.remove(handler)

    def disconnect(self):
        self.disconnected = True

    def portal_calls(self, member=None):
        return [c for c in self.calls if c[0] != "AddMatch" and (member is None or c[0] == member)]

    async def call(self, msg):
        self._serial += 1
        msg.serial = self._serial
        msg = _roundtrip(msg)
        self.calls.append((msg.member, msg.interface, list(msg.body)))
        if msg.destination == "org.freedesktop.DBus":
            return self._reply(msg)
        if not self.portal:
            return self._error(msg, "org.freedesktop.DBus.Error.ServiceUnknown")
        if msg.member == self.hang:
            await asyncio.Event().wait()
        if msg.interface == REGISTRY:
            if not self.registry:
                return self._error(msg, UNKNOWN_METHOD)
            if self.refuse_register:
                return self._error(msg, self.refuse_register)
            self.registered = msg.body[0]
            return self._reply(msg)
        if msg.interface != GLOBAL_SHORTCUTS or not self.interface:
            return self._error(msg, UNKNOWN_METHOD)
        options = msg.body[-1]
        request = f"{PORTAL_PATH}/request/1_42/{options['handle_token'].value}"
        if msg.member == "CreateSession":
            if self.refuse_session:
                return self._error(msg, self.refuse_session)
            self.session = f"{PORTAL_PATH}/session/1_42/{options['session_handle_token'].value}"
            results = {"session_handle": Variant("s", self.session)} if self.session_answer == 0 else {}
            self._respond(request, self.session_answer, results)
        elif msg.member == "BindShortcuts":
            assert msg.body[0] == self.session and msg.body[2] == ""
            bound = [[sid, {"description": props["description"],
                            "trigger_description": Variant("s", props["preferred_trigger"].value)}]
                     for sid, props in msg.body[1]]
            self._respond(request, self.bind_answer,
                          {"shortcuts": Variant("a(sa{sv})", bound)} if self.bind_answer == 0 else {})
        return self._reply(msg, "o", [request])

    def _reply(self, msg, signature="", body=()):
        return _roundtrip(Message.new_method_return(msg, signature, list(body)))

    def _error(self, msg, name):
        return _roundtrip(Message.new_error(msg, name, "refused"))

    def _respond(self, path, code, results):
        def emit():
            self.emit(path, "org.freedesktop.portal.Request", "Response", "ua{sv}", [code, results])
        if self.respond_first:
            emit()
        else:
            asyncio.get_running_loop().call_soon(emit)

    def emit(self, path, interface, member, signature, body):
        msg = _roundtrip(Message(message_type=MessageType.SIGNAL, sender=":1.7", path=path,
                                 interface=interface, member=member, signature=signature,
                                 body=body, serial=1))
        for h in list(self.handlers):
            assert not h(msg)              # never swallows a message meant for someone else

    # The desktop's side ------------------------------------------------------

    def press(self, shortcut_id, session=None):
        self.emit(PORTAL_PATH, GLOBAL_SHORTCUTS, "Activated", "osta{sv}",
                  [session or self.session, shortcut_id, 1234, {}])

    def restart(self, gone):
        self.emit("/org/freedesktop/DBus", "org.freedesktop.DBus", "NameOwnerChanged", "sss",
                  [PORTAL, ":1.7" if gone else "", "" if gone else ":1.8"])


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def bind(bus, **kwargs):
    activated = []

    async def go():
        reason = await start_shortcuts(bus, activated.append, **kwargs)
        await settle()
        return reason
    return asyncio.run(go()), activated


# The portal conversation ----------------------------------------------------

@pytest.mark.parametrize("respond_first", [False, True])
def test_session_bind_and_activation(respond_first):
    activated = []

    async def go():
        bus = FakePortal(respond_first=respond_first)
        reason = await start_shortcuts(bus, activated.append)
        bus.press("flag-moment")
        bus.press("toggle-recording")
        bus.press("no-such-shortcut")
        bus.press("flag-moment", session=f"{PORTAL_PATH}/session/1_99/other")   # not ours
        bus.emit(PORTAL_PATH, GLOBAL_SHORTCUTS, "Deactivated", "osta{sv}",
                 [bus.session, "flag-moment", 1235, {}])
        return reason, bus
    reason, bus = asyncio.run(go())
    assert reason is None
    assert activated == ["flag-moment", "toggle-recording"]
    # Register comes before any other portal call on the connection
    assert [c[0] for c in bus.portal_calls()] == ["Register", "CreateSession", "BindShortcuts"]
    assert bus.registered == APP_ID == "io.github.knowhrishi.wirenote"
    (_, _, (session, wanted, parent, _options)), = bus.portal_calls("BindShortcuts")
    assert session == bus.session and parent == ""
    assert {sid: (p["description"].value, p["preferred_trigger"].value) for sid, p in wanted} == SHORTCUTS
    assert SHORTCUTS == {"toggle-recording": ("Start or stop recording", "CTRL+ALT+R"),
                         "flag-moment": ("Flag this moment", "CTRL+ALT+F")}


def test_portal_older_than_the_registry_still_binds():
    """Register arrived in xdg-desktop-portal 1.19.4; older ones answer UnknownMethod."""
    bus = FakePortal(registry=False)
    reason, activated = bind(bus)
    assert reason is None and bus.registered is None
    bus.press("toggle-recording")
    assert activated == ["toggle-recording"]


def test_portal_missing_is_reported():
    errors = []
    for bus in (FakePortal(portal=False),       # no xdg-desktop-portal at all
                FakePortal(interface=False),    # GNOME 46 and 47: no GlobalShortcuts backend
                FakePortal(registry=False, interface=False)):
        reason, _ = bind(bus, on_error=errors.append)
        assert reason == NO_PORTAL
        assert bus.portal_calls("BindShortcuts") == []
    assert errors == []                         # not an error: the doctor explains it


def test_denied_binding_is_reported_not_retried():
    for answer in (1, 2):                       # cancelled in the dialog, or ended otherwise
        bus = FakePortal(bind_answer=answer)
        reason, activated = bind(bus)
        assert reason == DENIED
        assert len(bus.portal_calls("BindShortcuts")) == 1
        bus.press("toggle-recording")
        assert activated == []
    bus = FakePortal(session_answer=1)
    assert bind(bus)[0] == DENIED and bus.portal_calls("BindShortcuts") == []
    bus = FakePortal(refuse_session="org.freedesktop.portal.Error.NotAllowed")
    assert bind(bus)[0] == DENIED


def test_register_refused_is_a_failure_with_its_reason():
    """For example "App info not found" when the desktop entry is not installed."""
    errors = []
    bus = FakePortal(refuse_register="org.freedesktop.portal.Error.Failed")
    reason, _ = bind(bus, on_error=errors.append)
    assert reason == FAILED and bus.portal_calls("CreateSession") == []
    assert errors == ["global shortcuts: register failed (org.freedesktop.portal.Error.Failed)"]


def test_shortcut_errors_are_contained():
    errors = []

    def boom(shortcut_id):
        raise RuntimeError("daemon bug")

    async def go():
        bus = FakePortal()
        assert await start_shortcuts(bus, boom, on_error=errors.append) is None
        bus.press("flag-moment")                # never raises into dbus-fast
        bus.emit(PORTAL_PATH, GLOBAL_SHORTCUTS, "Activated", "s", ["short"])   # a malformed one
        return bus
    asyncio.run(go())
    assert errors == ["global shortcuts: flag-moment failed (RuntimeError)"]

    class Broken(FakePortal):
        async def call(self, msg):
            raise ConnectionError("bus gone")
    errors.clear()
    assert bind(Broken(), on_error=errors.append)[0] == FAILED
    assert errors == ["global shortcuts: start failed (ConnectionError)"]


def test_rebinds_when_the_portal_restarts():
    statuses = []

    async def go():
        bus = FakePortal()
        assert await start_shortcuts(bus, lambda sid: None, on_status=statuses.append) is None
        bus.restart(gone=True)
        bus.restart(gone=False)
        await settle()
        return bus
    bus = asyncio.run(go())
    assert statuses == [FAILED, BOUND]
    assert [c[0] for c in bus.portal_calls()] == ["Register", "CreateSession", "BindShortcuts"] * 2


# In the daemon ---------------------------------------------------------------

def with_portal(d, *buses):
    """The daemon opens a connection of its own for each attempt."""
    made = list(buses)

    async def factory():
        return made.pop(0)
    d.make_shortcuts_bus = factory
    return made


def start(d):
    async def go():
        task = d._start_shortcuts()
        if task is not None:
            await task
        await settle()
    asyncio.run(go())


def status(tmp_path):
    return json.loads((tmp_path / "status.json").read_text())["shortcuts"]


def on(d):
    d.settings = replace(d.settings, global_shortcuts=True)
    return d


def test_off_by_default_and_no_portal_is_asked(tmp_path):
    d = make(tmp_path)
    assert d.settings.global_shortcuts is False
    d.make_shortcuts_bus = lambda: pytest.fail("off by default: binding can show a dialog")
    assert d._start_shortcuts() is None
    d._write_status(force=True)
    assert status(tmp_path) is None


def test_status_values(tmp_path):
    cases = [(FakePortal(), "bound"), (FakePortal(bind_answer=1), "denied"),
             (FakePortal(portal=False), "no portal"),
             (FakePortal(refuse_register="org.freedesktop.portal.Error.Failed"), "failed")]
    for n, (bus, want) in enumerate(cases):
        state = tmp_path / str(n)
        state.mkdir()
        d = on(make(state))
        with_portal(d, bus)
        start(d)
        assert status(state) == want
        assert bus.disconnected is (want != "bound")       # a refusal leaves no session open

    async def no_bus():
        raise FileNotFoundError("no session bus")
    state = tmp_path / "nobus"
    state.mkdir()
    d = on(make(state))
    d.make_shortcuts_bus = no_bus
    start(d)
    assert status(state) == "failed"


def test_shortcuts_reach_control(tmp_path):
    d = on(make(tmp_path))
    bus = FakePortal()
    with_portal(d, bus)
    received = []
    control = d._control
    d._control = lambda cmd: received.append(cmd) or control(cmd)

    async def go():
        await d._start_shortcuts()
        bus.press("flag-moment")                    # nothing records yet: noted, not raised
        bus.press("toggle-recording")
        await drain(d)
        assert d.recording is not None
        bus.press("flag-moment")
        assert len(d.recording.flags) == 1
        bus.press("toggle-recording")
        await drain(d)
    asyncio.run(go())
    # toggle answers through record-now and stop
    assert received == ["flag", "toggle", "record-now", "flag", "toggle", "stop"]
    assert d.recording is None and d.jobs.next_queued() is not None
    log = [json.loads(line) for line in (tmp_path / "detector.jsonl").open()]
    assert any(e.get("event") == "shortcuts" and e.get("status") == "bound" for e in log)
    assert any(e.get("message") == "shortcut flag-moment: not recording" for e in log)


def test_setting_switched_in_the_app_binds_and_unbinds(tmp_path):
    from wirenote.config.secrets import set_config_value
    from wirenote.config.settings import config_dir, load_settings

    d = make(tmp_path)
    first, second = FakePortal(bind_answer=1), FakePortal()
    with_portal(d, first, second)
    cfg = config_dir() / "config.toml"
    load_settings(cfg)                                      # writes the default file

    async def go():
        set_config_value(cfg, "global_shortcuts", "true")
        assert d._control("reload-settings")["ok"]
        await settle()
        assert status(tmp_path) == "denied" and first.disconnected
        assert d._control("reload-settings")["ok"]          # a denial is not asked again...
        await settle()
        assert len(first.portal_calls("BindShortcuts")) == 1 and second.calls == []
        assert d._control("bind-shortcuts")["ok"]           # ...unless you ask for it
        await settle()
        assert status(tmp_path) == "bound"
        assert d._control("bind-shortcuts")["ok"]           # bound: nothing to do
        await settle()
        set_config_value(cfg, "global_shortcuts", "false")
        assert d._control("reload-settings")["ok"]
        await settle()
    asyncio.run(go())
    assert second.disconnected and status(tmp_path) is None
    assert len(second.portal_calls("CreateSession")) == 1


def test_hung_portal_never_delays_detection(tmp_path, monkeypatch):
    import wirenote.detector.daemon as daemon_mod

    monkeypatch.setenv("WIRENOTE_SOCKET", str(tmp_path / "control.sock"))

    async def no_sleep_signals(on_sleep):
        raise ConnectionError("no system bus")
    monkeypatch.setattr(daemon_mod, "watch_sleep", no_sleep_signals)
    d = on(make(tmp_path))
    d.settings = replace(d.settings, tray_icon=False)
    bus = FakePortal(hang="CreateSession")
    with_portal(d, bus)
    reached = []

    async def first_loop():
        await settle()                              # let the background start reach the portal
        reached.append("detecting")
        d._stopping.set()

    async def idle():
        await asyncio.Event().wait()
    d._pw_loop = first_loop
    for name in ("_tick_loop", "_op_loop", "_rec_loop", "_worker_loop", "_housekeeping_loop",
                 "_control_loop", "_calendar_loop", "_update_loop"):
        setattr(d, name, idle)
    asyncio.run(asyncio.wait_for(d.run(), 5))
    assert reached == ["detecting"]
    assert len(bus.portal_calls("CreateSession")) == 1      # it was still waiting
    assert status(tmp_path) is None and bus.disconnected


# The doctor and `wirenote setup shortcuts` -----------------------------------

def test_doctor_explains_each_status(tmp_path, monkeypatch):
    from wirenote.config.settings import Settings
    from wirenote.tools import setup

    monkeypatch.setattr(setup, "state_dir", lambda: tmp_path)
    base = {"running": True, "pid": os.getpid(), "notifications": True, "jobs": {}}
    s_on = replace(Settings(), global_shortcuts=True)
    for value, s, want in (("bound", s_on, "ok"), ("denied", s_on, "warn"), ("no portal", s_on, "warn"),
                           ("failed", s_on, "warn"), (None, s_on, "warn"), (None, Settings(), None)):
        (tmp_path / "status.json").write_text(json.dumps({**base, "shortcuts": value}))
        found = [c for c in setup._daemon_checks(s) if "shortcut" in c.message]
        assert [c.status for c in found] == ([want] if want else [])
        if value in ("no portal", "denied"):
            assert "wirenote toggle" in found[0].fix and "wirenote flag" in found[0].fix
        if value == "failed":
            assert "journalctl" in found[0].fix and "setup install" in found[0].fix


def test_setup_shortcuts_turns_it_on_and_reports(tmp_path, monkeypatch, capsys):
    from wirenote.config.settings import config_dir, load_settings
    from wirenote.tools import setup

    monkeypatch.setattr(setup, "state_dir", lambda: tmp_path)
    sent = []

    def daemon(command):
        sent.append(command)
        (tmp_path / "status.json").write_text(json.dumps({"shortcuts": None}))
        return {"ok": True, "app": None}
    monkeypatch.setattr(setup, "control", daemon)
    answers = iter([None, None, "bound"])

    def tick(seconds):          # the desktop's dialog is answered after a while
        (tmp_path / "status.json").write_text(json.dumps({"shortcuts": next(answers)}))
    assert setup.setup_shortcuts(sleep=tick) == 0
    assert sent == ["bind-shortcuts"]
    assert load_settings(config_dir() / "config.toml").global_shortcuts is True
    out = capsys.readouterr().out
    assert "allow" in out and "Ctrl+Alt+R" in out

    monkeypatch.setattr(setup, "control", lambda c: {"ok": False, "error": "daemon not running"})
    assert setup.setup_shortcuts(sleep=lambda s: pytest.fail("nothing to wait for")) == 1
    assert "systemctl --user start wirenote" in capsys.readouterr().out

    monkeypatch.setattr(setup, "control", daemon)
    answers = iter(["no portal"])
    assert setup.setup_shortcuts(sleep=tick) == 1
    assert "wirenote toggle" in capsys.readouterr().out

    clock = iter(range(0, 100000, 60))          # nobody answers the dialog
    assert setup.setup_shortcuts(sleep=lambda s: None, clock=lambda: next(clock)) == 1
    assert "doctor" in capsys.readouterr().out


def test_cli_has_setup_shortcuts(monkeypatch):
    from wirenote import cli
    from wirenote.tools import setup

    monkeypatch.setattr(setup, "setup_shortcuts", lambda: 7)
    assert cli.main(["setup", "shortcuts"]) == 7


# The app's Settings › Desktop -------------------------------------------------

def test_app_switches_global_shortcuts(tmp_path, monkeypatch):
    from wirenote.config.settings import Settings, config_dir, load_settings
    from wirenote.ui import data
    from wirenote.ui.data import Invalid, Store

    pokes = []
    monkeypatch.setattr(data, "poke_daemon", pokes.append)
    st = Store(Settings(), tmp_path)
    assert st.settings_info()["editable"]["global_shortcuts"] is False
    assert st.update_settings({"global_shortcuts": True}, False)["editable"]["global_shortcuts"] is True
    assert load_settings(config_dir() / "config.toml").global_shortcuts is True
    assert pokes == ["reload-settings"]
    with pytest.raises(Invalid):
        st.update_settings({"global_shortcuts": "yes"}, False)


def test_app_script_has_the_desktop_switch():
    from pathlib import Path

    from wirenote import ui

    script = (Path(ui.__file__).parent / "static" / "app.js").read_text()
    assert '"Desktop"' in script and "global_shortcuts" in script


def test_module_points_at_its_docs():
    assert "docs/usage.md › Keyboard shortcuts" in shortcuts.__doc__
