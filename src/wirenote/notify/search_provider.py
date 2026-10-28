# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Find a meeting from the desktop's launcher: KRunner (org.kde.krunner1) and GNOME Shell
search (org.gnome.Shell.SearchProvider2) on one service object, over dbus-fast
(docs/usage.md › Find a meeting from the launcher).

It is off until you turn it on (launcher_search; `wirenote setup search`), and while it is off
it answers every search with nothing, so a plugin file left behind lists no meetings. The
session bus starts `wirenote search-provider` on the first search, through the D-Bus service
file that `wirenote setup search` (or `setup install`, while it is on) writes, and the process
exits after SEARCH_IDLE_S without a call. It owns io.github.knowhrishi.wirenote.Search on the session bus, which only
the user who started it can reach. It matches titles, people and summaries, never
transcripts, and opens a result in the meetings app with a one-time login link, as
`wirenote open` does.

Queries, titles and ids are never logged: a failure is logged as the exception's type only.
A broken call folder hides that call; nothing here stops the service.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

from dbus_fast import NameFlag, RequestNameReply, Variant
from dbus_fast.annotations import DBusSignature, DBusStr, DBusUInt32
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_method

from .. import APP_ID
from .text import plain

if TYPE_CHECKING:
    from ..config.settings import Settings
    from ..ui.data import Store

BUS_NAME = f"{APP_ID}.Search"
KRUNNER_IFACE = "org.kde.krunner1"
KRUNNER_PATH = "/runner"
GNOME_IFACE = "org.gnome.Shell.SearchProvider2"
GNOME_PATH = "/io/github/knowhrishi/wirenote/SearchProvider"
GNOME_INI = f"{APP_ID}.search-provider.ini"
# GNOME Shell reads search providers from the system data folders only, never from yours
GNOME_PROVIDER_DIRS = (Path("/usr/local/share/gnome-shell/search-providers"),
                       Path("/usr/share/gnome-shell/search-providers"))
ICON = "wirenote"

# docs/dev/PLAN.md › Defaults › Launcher search process: it ends after this long without a call
SEARCH_IDLE_S = 60
MIN_CHARS = 3            # › Launcher search: shorter queries match nearly everything
MAX_RESULTS = 8          # › Launcher search
MAX_PEOPLE = 3           # names in a result's second line
MAX_TEXT = 120           # characters of a title or a line of people
# KRunner's QueryMatch type (KF5 Type::PossibleMatch, KF6 CategoryRelevance::Low)
POSSIBLE_MATCH = 30
METAS_KEPT = 64          # results GNOME may ask GetResultMetas about
NOT_FOUND = "Meeting not found"

Matches = Annotated[list, DBusSignature("a(sssida{sv})")]
ActionList = Annotated[list, DBusSignature("a(sss)")]
ConfigMap = Annotated[dict, DBusSignature("a{sv}")]
Strings = Annotated[list, DBusSignature("as")]
Metas = Annotated[list, DBusSignature("aa{sv}")]

log = logging.getLogger(__name__)


def gnome_ini_path() -> Path:
    """The GNOME search provider file shipped in the package, for the one sudo step."""
    return Path(str(files("wirenote") / "data" / GNOME_INI))


def _plain(text: Any, limit: int = MAX_TEXT) -> str:
    """Calendar titles and names are untrusted: one line, no control or direction characters."""
    return plain(text, limit)


def _krunner_text(text: str) -> str:
    """Qt may render text that looks like HTML (a tag, or an entity like `&lt;`) as rich text;
    a word joiner after each `<` and `&` keeps it plain and looks the same."""
    return text.replace("<", "<⁠").replace("&", "&⁠")


def _date(started: Any) -> str:
    """The start as recorded, in the time zone of the call."""
    try:
        return dt.datetime.fromisoformat(str(started)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def _subtitle(brief: dict) -> str:
    names = [_plain(n, 40) for n in (brief.get("attendees") or brief.get("speakers") or [])
             if isinstance(n, str) and n.strip()]
    people = ", ".join(names[:MAX_PEOPLE]) + (f" +{len(names) - MAX_PEOPLE}" if len(names) > MAX_PEOPLE else "")
    return " · ".join(part for part in (_date(brief.get("started")), _plain(people)) if part)


def search(store: Store, terms: list[str]) -> list[dict]:
    """At most MAX_RESULTS calls whose title, people or summary contain the terms, newest
    first, as {id, title, subtitle}; nothing for a query under MIN_CHARS characters."""
    query = " ".join(" ".join(str(t) for t in terms).split())
    if len(query) < MIN_CHARS:
        return []
    out = []
    for brief in store.calls(query, deep=False, limit=MAX_RESULTS):
        r = _result(brief)
        if r is not None:
            out.append(r)
    return out


def _result(brief: dict) -> dict | None:
    """None for a call whose id is not a recording id: it could not go on the bus as a string,
    or be opened."""
    from ..ui.data import REC_ID

    rec_id = brief.get("id")
    if not isinstance(rec_id, str) or not REC_ID.fullmatch(rec_id):
        return None
    try:
        return {"id": rec_id, "title": _plain(brief["title"]), "subtitle": _subtitle(brief)}
    except (KeyError, TypeError, ValueError):
        return None


def open_in_app(port: int, route: str) -> None:
    """Like `wirenote open`: start this user's web app if needed, then open route there with
    a one-time login link. Never someone else's server."""
    from ..ui import launch

    if launch.ensure_ui(port):
        launch.open_ui(port, route)


class SearchService:
    """Both launchers' interfaces over one search. Opening a result runs in a thread: starting
    the web app can take seconds, and the bus must keep answering."""

    def __init__(self, store_factory: Callable[[], Store], open_route: Callable[[str], None],
                 clock: Callable[[], float] | None = None,
                 enabled: Callable[[], bool] = lambda: True) -> None:
        # launcher_search, asked at every call: a plugin file left behind finds nothing
        self.enabled = enabled
        self._store_factory = store_factory
        self._store: Store | None = None
        self._open_route = open_route
        self._clock = clock or time.monotonic
        self.last_active = self._clock()
        self._metas: OrderedDict[str, dict] = OrderedDict()
        self._opening: set[asyncio.Future] = set()
        self.krunner = _Runner(self)
        self.gnome = _GnomeProvider(self)

    def touch(self) -> None:
        self.last_active = self._clock()

    def find(self, terms: list[str]) -> list[dict]:
        self.touch()
        try:
            if not self._on():
                return []
            if self._store is None:
                self._store = self._store_factory()
            found = search(self._store, terms)
        except Exception as e:  # noqa: BLE001 - a bad query or library is no reason to stop
            self._fail("search", e)
            return []
        for r in found:
            self._metas[r["id"]] = r
            self._metas.move_to_end(r["id"])
        while len(self._metas) > METAS_KEPT:
            self._metas.popitem(last=False)
        return found

    def meta(self, rec_id: str) -> dict:
        """GNOME drops every result when it gets fewer metas than ids, so there is one for
        each: from the last searches, else from the call's folder (after a restart), else a
        placeholder."""
        from ..ui.data import REC_ID

        self.touch()
        if not self._on():
            return {"id": str(rec_id), "title": NOT_FOUND, "subtitle": ""}
        found = self._metas.get(rec_id)
        if found is None and isinstance(rec_id, str) and REC_ID.fullmatch(rec_id):
            try:
                if self._store is None:
                    self._store = self._store_factory()
                found = _result(self._store.quick_brief(rec_id))
            except Exception as e:  # noqa: BLE001 - NotFound, or a broken folder
                if type(e).__name__ != "NotFound":
                    self._fail("meta", e)
        return found or {"id": str(rec_id), "title": NOT_FOUND, "subtitle": ""}

    def activate(self, rec_id: str) -> None:
        from ..ui.data import REC_ID

        self.touch()
        if isinstance(rec_id, str) and REC_ID.fullmatch(rec_id) and self._on():
            self._open(f"#/call/{rec_id}")

    def launch_search(self, terms: list[str]) -> None:
        self.touch()
        if not self._on():
            return
        query = " ".join(" ".join(str(t) for t in terms).split())
        # `~` too: the web app's login accepts only the characters quote() makes plus `%`
        self._open(f"#/?q={quote(query, safe='').replace('~', '%7E')}" if query else "#/")

    def _open(self, route: str) -> None:
        try:
            future = asyncio.get_running_loop().run_in_executor(None, self._open_safely, route)
        except RuntimeError:              # no event loop: only outside the service
            self._open_safely(route)
            return
        self._opening.add(future)
        future.add_done_callback(self._opening.discard)

    def _open_safely(self, route: str) -> None:
        try:
            self._open_route(route)
        except Exception as e:  # noqa: BLE001
            self._fail("open", e)

    async def settle(self) -> None:
        """Wait for the results being opened (tests, and before the process exits)."""
        while self._opening:
            # Taken out here: gather can wake before the futures' own discard callbacks run
            batch = list(self._opening)
            self._opening.difference_update(batch)
            await asyncio.gather(*batch, return_exceptions=True)

    async def wait_idle(self, idle_s: float, sleep: Callable[[float], Awaitable[Any]]) -> None:
        while (left := self.last_active + idle_s - self._clock()) > 0:
            await sleep(left)

    def _on(self) -> bool:
        try:
            return bool(self.enabled())
        except Exception as e:  # noqa: BLE001 - unreadable settings: off
            self._fail("settings", e)
            return False

    def _fail(self, what: str, e: Exception) -> None:
        """Only the exception's type: its text may hold the query, a title or a path."""
        log.warning("search provider: %s failed (%s)", what, type(e).__name__)


class _Runner(ServiceInterface):
    """org.kde.krunner1, as KRunner calls a DBus2 runner."""

    def __init__(self, service: SearchService) -> None:
        super().__init__(KRUNNER_IFACE)
        self._s = service

    @dbus_method()
    def Match(self, query: DBusStr) -> Matches:
        found = self._s.find([query])
        return [[r["id"], _krunner_text(r["title"]), ICON, POSSIBLE_MATCH, round(1.0 - i * 0.05, 2),
                 {"subtext": Variant("s", _krunner_text(r["subtitle"]))}]
                for i, r in enumerate(found)]

    @dbus_method()
    def Actions(self) -> ActionList:
        return []

    @dbus_method()
    def Run(self, match_id: DBusStr, action_id: DBusStr):
        if action_id == "":           # the default action; this runner has no others
            self._s.activate(match_id)
        else:
            self._s.touch()

    @dbus_method()
    def Teardown(self):
        self._s.touch()

    @dbus_method()
    def Config(self) -> ConfigMap:
        return {"MinLetterCount": Variant("i", MIN_CHARS)}


class _GnomeProvider(ServiceInterface):
    """org.gnome.Shell.SearchProvider2, as GNOME Shell calls a Version=2 provider."""

    def __init__(self, service: SearchService) -> None:
        super().__init__(GNOME_IFACE)
        self._s = service

    @dbus_method()
    def GetInitialResultSet(self, terms: Strings) -> Strings:
        return [r["id"] for r in self._s.find(terms)]

    @dbus_method()
    def GetSubsearchResultSet(self, previous_results: Strings, terms: Strings) -> Strings:
        return [r["id"] for r in self._s.find(terms)]

    @dbus_method()
    def GetResultMetas(self, identifiers: Strings) -> Metas:
        out = []
        for rec_id in identifiers:          # exactly one meta per id, in order
            r = self._s.meta(rec_id)
            # GNOME Shell escapes these itself
            out.append({"id": Variant("s", rec_id), "name": Variant("s", r["title"]),
                        "description": Variant("s", r["subtitle"]), "gicon": Variant("s", ICON)})
        return out

    @dbus_method()
    def ActivateResult(self, identifier: DBusStr, terms: Strings, timestamp: DBusUInt32):
        self._s.activate(identifier)

    @dbus_method()
    def LaunchSearch(self, terms: Strings, timestamp: DBusUInt32):
        self._s.launch_search(terms)


async def session_bus() -> MessageBus:
    return await MessageBus().connect()


async def serve(service: SearchService, bus_factory: Callable[[], Awaitable[Any]] = session_bus,
                idle_s: float = SEARCH_IDLE_S,
                sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> int:
    """Answer searches until idle_s pass without one, or the session ends. 1 when another copy
    already answers, or there is no session bus."""
    try:
        bus = await bus_factory()
    except Exception as e:  # noqa: BLE001
        service._fail("start", e)
        return 1
    try:
        bus.export(KRUNNER_PATH, service.krunner)
        bus.export(GNOME_PATH, service.gnome)
        reply = await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
    except Exception as e:  # noqa: BLE001
        service._fail("start", e)
        bus.disconnect()
        return 1
    if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
        bus.disconnect()           # the other copy answers
        return 1
    service.touch()
    idle = asyncio.ensure_future(service.wait_idle(idle_s, sleep))
    gone = asyncio.ensure_future(bus.wait_for_disconnect())
    await asyncio.wait({idle, gone}, return_when=asyncio.FIRST_COMPLETED)
    for task in (idle, gone):
        task.cancel()
    await asyncio.gather(idle, gone, return_exceptions=True)
    await service.settle()
    try:
        await bus.release_name(BUS_NAME)     # the next search starts a fresh copy
    except Exception:  # noqa: BLE001 - the session already ended
        pass
    bus.disconnect()
    return 0


def launcher_search_on() -> bool:
    """config.toml's launcher_search as it is now; off if the file cannot be read."""
    from ..config.settings import config_dir, load_settings

    try:
        return load_settings(config_dir() / "config.toml").launcher_search
    except (OSError, ValueError):
        return False


def main(settings: Settings, state_dir: Path) -> int:
    from ..ui.data import Store

    service = SearchService(lambda: Store(settings, state_dir),
                            lambda route: open_in_app(settings.ui_port, route),
                            enabled=launcher_search_on)
    return asyncio.run(serve(service))
