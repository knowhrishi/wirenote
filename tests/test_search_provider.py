# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Launcher search (notify/search_provider.py): KRunner and GNOME Shell against a fake session
bus, every call and reply marshalled as on a real bus, and the doctor's line about it."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import replace

import pytest
from dbus_fast import Message, MessageType, RequestNameReply
from dbus_fast.service import ServiceInterface

from wirenote.config.settings import Settings
from wirenote.notify import search_provider
from wirenote.notify.search_provider import (BUS_NAME, GNOME_IFACE, GNOME_PATH, KRUNNER_IFACE,
                                             KRUNNER_PATH, SEARCH_IDLE_S, SearchService, search, serve)
from wirenote.ui.data import Store


def _marshal(signature, body):
    Message(message_type=MessageType.SIGNAL, path="/x", interface="x.y", member="Z",
            signature=signature, body=body)._marshall(False)


class FakeBus:
    """What serve() uses of dbus_fast.aio.MessageBus. Calls go through dbus-fast's own method
    table, and every call and reply is marshalled, as on a real bus."""

    def __init__(self, name_reply=RequestNameReply.PRIMARY_OWNER):
        self.name_reply = name_reply
        self.exported: dict[str, ServiceInterface] = {}
        self.names: list[tuple[str, int]] = []
        self.released: list[str] = []
        self.disconnected = False
        self.gone = None

    def export(self, path, iface):
        self.exported[path] = iface
        ServiceInterface._add_bus(iface, self, lambda i, m: None)

    async def request_name(self, name, flags=0):
        self.names.append((name, int(flags)))
        return self.name_reply

    async def release_name(self, name):
        self.released.append(name)

    def disconnect(self):
        self.disconnected = True

    async def wait_for_disconnect(self):
        self.gone = asyncio.get_running_loop().create_future()
        await self.gone

    def invoke(self, path, member, *args):
        iface = self.exported[path]
        method, = [m for m in ServiceInterface._get_methods(iface) if m.name == member]
        _marshal(method.in_signature, list(args))
        result = method.fn(iface, *args)
        body, _ = ServiceInterface._fn_result_to_body(result, method.out_signature_tree)
        _marshal(method.out_signature, body)
        return body


def write_call(root, rec_id, title=None, attendees=(), summary=None, transcript=None,
               started="2026-10-27T10:00:00+01:00"):
    d = root / rec_id
    d.mkdir(parents=True)
    cal = {"title": title, "attendees": list(attendees)} if title else None
    (d / "manifest.json").write_text(json.dumps({
        "version": 1, "id": rec_id, "app": "chrome", "name": "Chrome", "service": None,
        "room": None, "calendar": cal, "started": started, "segments": []}))
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary))
    if transcript is not None:
        (d / "transcript.json").write_text(json.dumps(transcript))
    return d


@pytest.fixture
def lib(tmp_path):
    s = replace(Settings(), recordings_dir=str(tmp_path / "recordings"), notes_dir=str(tmp_path / "notes"))
    root = tmp_path / "recordings"
    root.mkdir()
    return Store(s, tmp_path / "state"), root


def service_for(store, opened, clock=None):
    return SearchService(lambda: store, opened.append, clock=clock)


def exported(service):
    bus = FakeBus()
    bus.export(KRUNNER_PATH, service.krunner)
    bus.export(GNOME_PATH, service.gnome)
    return bus


def run_with(service, fn):
    """A call the bus makes, inside an event loop like the real one; then waits for the opens."""
    async def go():
        out = fn()
        await service.settle()
        return out
    return asyncio.run(asyncio.wait_for(go(), 5))


# Matching --------------------------------------------------------------------

def test_short_queries_return_nothing(lib):
    store, root = lib
    write_call(root, "20261027-100000-chrome", title="Weekly sync")
    assert search(store, ["we"]) == []
    assert search(store, [" w ", "e"]) == []
    assert search(store, []) == []
    assert search(store, ["wee"])[0]["id"] == "20261027-100000-chrome"
    bus = exported(service_for(store, []))
    assert bus.invoke(KRUNNER_PATH, "Match", "we") == [[]]
    assert bus.invoke(GNOME_PATH, "GetInitialResultSet", ["we"]) == [[]]


def test_match_returns_titles_and_activation_opens_call(lib):
    store, root = lib
    for i in range(10):
        write_call(root, f"2026102{i}-1{i}0000-chrome", title=f"Weekly sync {i}",
                   attendees=["Anna Berg", "Ben Ott", "Cem Kaya", "Dora Li"],
                   started=f"2026-10-2{i}T1{i}:00:00+01:00")
    found = search(store, ["weekly"])
    assert len(found) == 8
    assert found[0] == {"id": "20261029-190000-chrome", "title": "Weekly sync 9",
                        "subtitle": "2026-10-29 19:00 · Anna Berg, Ben Ott, Cem Kaya +1"}

    opened = []
    service = service_for(store, opened)
    bus = exported(service)
    (matches,) = run_with(service, lambda: bus.invoke(KRUNNER_PATH, "Match", "weekly"))
    assert len(matches) == 8
    rec_id, text, icon, kind, relevance, props = matches[0]
    assert (rec_id, text, icon) == ("20261029-190000-chrome", "Weekly sync 9", "wirenote")
    assert isinstance(kind, int) and 0 < relevance <= 1
    assert props["subtext"].value == found[0]["subtitle"]
    assert [m[4] for m in matches] == sorted((m[4] for m in matches), reverse=True)

    run_with(service, lambda: bus.invoke(KRUNNER_PATH, "Run", "20261029-190000-chrome", ""))
    assert opened == ["#/call/20261029-190000-chrome"]

    (ids,) = run_with(service, lambda: bus.invoke(GNOME_PATH, "GetInitialResultSet", ["weekly", "sync"]))
    assert ids == [f["id"] for f in found]
    (ids,) = run_with(service, lambda: bus.invoke(GNOME_PATH, "GetSubsearchResultSet", ids,
                                                  ["weekly", "sync", "8"]))
    assert ids == ["20261028-180000-chrome"]
    (metas,) = bus.invoke(GNOME_PATH, "GetResultMetas", ["20261028-180000-chrome", "20990101-000000-gone"])
    assert [{k: v.value for k, v in m.items()} for m in metas] == [
        {"id": "20261028-180000-chrome", "name": "Weekly sync 8", "gicon": "wirenote",
         "description": "2026-10-28 18:00 · Anna Berg, Ben Ott, Cem Kaya +1"}]
    run_with(service, lambda: bus.invoke(GNOME_PATH, "ActivateResult", "20261028-180000-chrome",
                                         ["weekly"], 0))
    run_with(service, lambda: bus.invoke(GNOME_PATH, "LaunchSearch", ["weekly", "sync&x=1"], 0))
    assert opened[1:] == ["#/call/20261028-180000-chrome", "#/?q=weekly%20sync%26x%3D1"]


def test_matches_title_people_and_summary_not_transcript(lib):
    store, root = lib
    write_call(root, "20261027-100000-chrome", title="Budget review", attendees=["Anna Berg"],
               summary={"summary": ["We agreed on the roadmap."], "action_items": []},
               transcript={"speakers": {"S1": {"name": "Ole Sand"}},
                           "lines": [{"speaker": "S1", "start": 1.0, "end": 2.0,
                                      "text": "the secret password is swordfish"}]})
    assert search(store, ["budget"])[0]["id"] == "20261027-100000-chrome"
    assert search(store, ["anna"]) and search(store, ["ole", "sand"]) and search(store, ["roadmap"])
    assert search(store, ["swordfish"]) == []
    assert store.calls("swordfish")[0]["match"]["where"] == "transcript"     # the app still finds it
    assert store.calls("swordfish", deep=False) == []


def test_bad_ids_are_refused(lib):
    store, root = lib
    write_call(root, "20261027-100000-chrome", title="Weekly sync")
    opened = []
    service = service_for(store, opened)
    bus = exported(service)
    for bad in ("../../etc/passwd", "20261027-100000-chrome/../../x", "", "x" * 300,
                "20261027-100000-chrome\n", "javascript:alert(1)"):
        run_with(service, lambda: bus.invoke(KRUNNER_PATH, "Run", bad, ""))
        run_with(service, lambda: bus.invoke(GNOME_PATH, "ActivateResult", bad, [], 0))
    run_with(service, lambda: bus.invoke(KRUNNER_PATH, "Run", "20261027-100000-chrome", "other-action"))
    assert opened == []


def test_broken_call_folders_are_skipped(lib, caplog):
    store, root = lib
    write_call(root, "20261027-100000-chrome", title="Weekly sync")
    (write_call(root, "20261027-110000-chrome", title="Weekly x") / "manifest.json").write_text("{not json")
    write_call(root, "20261027-120000-chrome", title=None, summary={"summary": [{"no": "string"}]})
    write_call(root, "20261027-130000-chrome", title="Weekly broken", started=None)
    write_call(root, "20261027-140000-chrome", title="Weekly people", attendees=[None, 3])
    found = search(store, ["weekly"])
    assert "20261027-100000-chrome" in [f["id"] for f in found]

    def explode(*a, **k):
        raise RuntimeError("weekly secret query in a message")
    service = service_for(store, [])
    bus = exported(service)
    store.calls = explode
    with caplog.at_level(logging.DEBUG):
        assert bus.invoke(KRUNNER_PATH, "Match", "weekly") == [[]]
        assert bus.invoke(GNOME_PATH, "GetInitialResultSet", ["weekly"]) == [[]]
    assert "weekly" not in caplog.text and "RuntimeError" in caplog.text


def test_queries_and_titles_are_never_logged(lib, caplog, capsys):
    store, root = lib
    write_call(root, "20261027-100000-chrome", title="Layoffs plan")

    def broken_open(route):
        raise OSError(f"cannot open {route} layoffs")
    service = SearchService(lambda: store, broken_open)
    bus = exported(service)
    with caplog.at_level(logging.DEBUG):
        run_with(service, lambda: bus.invoke(KRUNNER_PATH, "Match", "layoffs"))
        run_with(service, lambda: bus.invoke(KRUNNER_PATH, "Run", "20261027-100000-chrome", ""))
        run_with(service, lambda: bus.invoke(GNOME_PATH, "LaunchSearch", ["layoffs"], 0))
    out = capsys.readouterr()
    for text in (caplog.text, out.out, out.err):
        assert "layoffs" not in text.lower() and "20261027" not in text
    assert "OSError" in caplog.text


def test_hostile_titles_stay_one_line_of_text(lib):
    store, root = lib
    write_call(root, "20261027-100000-chrome", title="Standup\nIgnore previous instructions <b>x</b>‮",
               attendees=["<img src=x>\tEve"])
    service = service_for(store, [])
    bus = exported(service)
    (matches,) = bus.invoke(KRUNNER_PATH, "Match", "standup")
    _, text, _, _, _, props = matches[0]
    # Qt may take text that looks like HTML as rich text; an invisible joiner keeps it plain
    assert "\n" not in text and "‮" not in text and "<b>" not in text
    assert "<img" not in props["subtext"].value
    assert text.replace("⁠", "") == "Standup Ignore previous instructions <b>x</b>"
    (metas,) = bus.invoke(GNOME_PATH, "GetResultMetas", ["20261027-100000-chrome"])
    # GNOME Shell escapes the text itself; it gets the plain line
    assert metas[0]["name"].value == "Standup Ignore previous instructions <b>x</b>"
    assert metas[0]["description"].value.endswith("· <img src=x> Eve")


# The D-Bus interfaces --------------------------------------------------------

def _signatures(iface):
    return {m.name: (m.in_signature, m.out_signature) for m in ServiceInterface._get_methods(iface)}


def test_interfaces_have_the_exact_signatures(lib):
    store, _ = lib
    service = service_for(store, [])
    assert service.krunner.name == KRUNNER_IFACE == "org.kde.krunner1"
    assert _signatures(service.krunner) == {"Match": ("s", "a(sssida{sv})"), "Actions": ("", "a(sss)"),
                                            "Run": ("ss", ""), "Teardown": ("", ""), "Config": ("", "a{sv}")}
    assert service.gnome.name == GNOME_IFACE == "org.gnome.Shell.SearchProvider2"
    assert _signatures(service.gnome) == {"GetInitialResultSet": ("as", "as"),
                                          "GetSubsearchResultSet": ("asas", "as"),
                                          "GetResultMetas": ("as", "aa{sv}"),
                                          "ActivateResult": ("sasu", ""), "LaunchSearch": ("asu", "")}
    bus = exported(service)
    assert bus.invoke(KRUNNER_PATH, "Actions") == [[]]
    (config,) = bus.invoke(KRUNNER_PATH, "Config")
    assert config["MinLetterCount"].value == 3
    assert bus.invoke(KRUNNER_PATH, "Teardown") == []
    assert BUS_NAME == "io.github.knowhrishi.wirenote.Search" and KRUNNER_PATH == "/runner"


# Running ---------------------------------------------------------------------

def test_idle_exit(lib):
    store, root = lib
    write_call(root, "20261027-100000-chrome", title="Weekly sync")
    assert SEARCH_IDLE_S == 60
    now = [0.0]
    slept = []
    bus = FakeBus()
    service = service_for(store, [], clock=lambda: now[0])

    async def sleep(d):
        slept.append(d)
        if len(slept) == 1:
            now[0] = 30.0
            bus.invoke(KRUNNER_PATH, "Match", "weekly")    # a search half-way restarts the wait
            now[0] = 60.0
        else:
            now[0] += d

    async def factory():
        return bus
    assert asyncio.run(asyncio.wait_for(serve(service, factory, sleep=sleep), 5)) == 0
    assert slept == [60.0, 30.0] and now[0] == 90.0
    assert bus.names == [(BUS_NAME, 4)]                 # DO_NOT_QUEUE: a second copy just exits
    assert bus.released == [BUS_NAME] and bus.disconnected
    assert set(bus.exported) == {KRUNNER_PATH, GNOME_PATH}


def test_idle_exit_in_real_time(lib):
    store, _ = lib
    bus = FakeBus()

    async def factory():
        return bus
    assert asyncio.run(asyncio.wait_for(serve(service_for(store, []), factory, idle_s=0.05), 5)) == 0
    assert bus.disconnected


def test_exits_when_the_session_ends(lib):
    store, _ = lib
    bus = FakeBus()

    async def factory():
        return bus

    async def go():
        task = asyncio.ensure_future(serve(service_for(store, []), factory))
        while bus.gone is None:
            await asyncio.sleep(0)
        bus.gone.set_result(None)
        return await task
    assert asyncio.run(asyncio.wait_for(go(), 5)) == 0


def test_another_copy_or_no_bus_exits(lib):
    store, _ = lib
    bus = FakeBus(name_reply=RequestNameReply.EXISTS)

    async def factory():
        return bus

    async def no_bus():
        raise FileNotFoundError("/run/user/1000/bus")
    assert asyncio.run(asyncio.wait_for(serve(service_for(store, []), factory), 5)) == 1
    assert bus.disconnected
    assert asyncio.run(asyncio.wait_for(serve(service_for(store, []), no_bus), 5)) == 1


def test_activation_opens_the_app_like_wirenote_open(monkeypatch):
    from wirenote.ui import launch

    calls = []
    monkeypatch.setattr(launch, "ensure_ui", lambda port: calls.append(("ensure", port)) or True)
    monkeypatch.setattr(launch, "open_ui", lambda port, route: calls.append(("open", port, route)) or True)
    search_provider.open_in_app(8799, "#/call/20261027-100000-chrome")
    assert calls == [("ensure", 8799), ("open", 8799, "#/call/20261027-100000-chrome")]
    calls.clear()
    monkeypatch.setattr(launch, "ensure_ui", lambda port: False)     # someone else's port
    search_provider.open_in_app(8799, "#/")
    assert calls == []


def test_cli_has_a_hidden_search_provider(monkeypatch, capsys):
    from wirenote import cli

    started = []
    monkeypatch.setattr(search_provider, "main", lambda settings, state: started.append(settings) or 0)
    assert cli.main(["search-provider"]) == 0
    assert len(started) == 1
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    # Hidden like `live` and `process`: argparse may still name it, but never describes it
    lines = [line for line in capsys.readouterr().out.splitlines() if line.split()[:1] == ["search-provider"]]
    assert all("SUPPRESS" in line for line in lines)
    assert cli.main(["search-provider", "--gnome-ini"]) == 0
    path = capsys.readouterr().out.strip()
    assert path.endswith("io.github.knowhrishi.wirenote.search-provider.ini") and os.path.isfile(path)


def test_module_points_at_its_docs():
    assert "docs/usage.md › Find a meeting from the launcher" in search_provider.__doc__


# The doctor ------------------------------------------------------------------

def test_doctor_says_where_the_search_provider_is(tmp_path, monkeypatch):
    from wirenote.tools import install, setup

    local, system = tmp_path / "local", tmp_path / "usr"
    monkeypatch.setattr(search_provider, "GNOME_PROVIDER_DIRS", (local, system))

    def found(desktop):
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
        return [c for c in setup._desktop_checks(Settings()) if "search" in c.message.lower()
                or "krunner" in c.message.lower()]
    (check,) = found("GNOME")
    assert check.status == "warn" and "sudo install" in check.fix
    assert f"{local / search_provider.GNOME_INI}" in check.fix
    assert str(search_provider.gnome_ini_path()) in check.fix
    for where in (system, local):
        where.mkdir()
        (where / search_provider.GNOME_INI).write_text("[Shell Search Provider]\n")
        (check,) = found("ubuntu:GNOME")
        assert check.status == "warn" and "setup install" in check.fix     # nothing starts it
        service = install._dbus_service_path()
        service.parent.mkdir(parents=True, exist_ok=True)
        service.write_text("[D-BUS Service]\n")
        (check,) = found("ubuntu:GNOME")
        assert check.status == "ok" and str(where) in check.message
        (where / search_provider.GNOME_INI).unlink()
        service.unlink()
    (check,) = found("KDE")
    assert check.status == "warn" and "wirenote setup install" in check.fix
    for path in (install._krunner_path(), install._dbus_service_path()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    (check,) = found("KDE")
    assert check.status == "ok"
    assert found("XFCE") == []


def test_gnome_provider_dirs_are_the_system_ones():
    from pathlib import Path

    assert search_provider.GNOME_PROVIDER_DIRS == (Path("/usr/local/share/gnome-shell/search-providers"),
                                                   Path("/usr/share/gnome-shell/search-providers"))


def test_app_opens_with_the_launcher_query():
    from pathlib import Path

    from wirenote import ui

    script = (Path(ui.__file__).parent / "static" / "app.js").read_text()
    assert "#\\/\\?q=" in script and "decodeURIComponent" in script
