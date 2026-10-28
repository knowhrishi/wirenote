# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Linux tray icon: a StatusNotifierItem with a com.canonical.dbusmenu menu, over dbus-fast
(docs/usage.md › Tray icon).

Plasma and GNOME's AppIndicator extension both host it. The item asks for the bus name
org.freedesktop.StatusNotifierItem-<pid>-1, lives at /StatusNotifierItem on the interface
org.kde.StatusNotifierItem, and registers with org.kde.StatusNotifierWatcher under that
name as soon as the watcher exists, which at login is often after Wirenote starts, and
again whenever it comes back (a panel restart). Status is Active, or
NeedsAttention while a prompt shows; Passive would hide the icon.

Every D-Bus call is guarded: a failure is reported once through on_error and the daemon
carries on without a working icon.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from dbus_fast import DBusError, Message, MessageType, PropertyAccess, Variant
from dbus_fast.annotations import (DBusBool, DBusInt32, DBusObjectPath, DBusSignature, DBusStr,
                                   DBusUInt32, DBusVariant)
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_method, dbus_property, dbus_signal

from .dbus import escape_markup
from .indicator import IndicatorState, MenuItem, describe, menu_for

WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_IFACE = "org.kde.StatusNotifierItem"
ITEM_PATH = "/StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
MENU_PATH = "/MenuBar"
DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
WATCHER_MATCH = (f"type='signal',sender='{DBUS}',interface='{DBUS}',member='NameOwnerChanged',"
                 f"arg0='{WATCHER}'")

ICON_IDLE = "audio-input-microphone"
ICON_RECORDING = "media-record"

# One id per command, whatever the state, so a click in a menu opened before a change
# still means what its label said.
ITEM_IDS = {"record": 1, "ignore": 2, "stop": 3, "flag": 4, "record-now": 5, "open": 6}
STATE_LINE_ID = 7
KEYS_BY_ID = {i: k for k, i in ITEM_IDS.items()}

Pixmaps = Annotated[list, DBusSignature("a(iiay)")]
ToolTipType = Annotated[list, DBusSignature("(sa(iiay)ss)")]
Layout = Annotated[list, DBusSignature("u(ia{sv}av)")]
Ids = Annotated[list, DBusSignature("ai")]
Strings = Annotated[list, DBusSignature("as")]
GroupProps = Annotated[list, DBusSignature("a(ia{sv})")]
Events = Annotated[list, DBusSignature("a(isvu)")]
TwoIdLists = Annotated[list, DBusSignature("aiai")]
RevisionParent = Annotated[list, DBusSignature("ui")]
IdTimestamp = Annotated[list, DBusSignature("iu")]
PropsUpdates = Annotated[list, DBusSignature("a(ia{sv})a(ias)")]

log = logging.getLogger(__name__)


class TrayCallError(Exception):
    """An error reply; its text is the D-Bus error name, which carries no user data."""


async def session_bus() -> MessageBus:
    return await MessageBus().connect()


def _label(text: str) -> str:
    """App names and calendar titles are untrusted: one line, and `_` is a mnemonic
    marker in dbusmenu labels, so a literal one is doubled."""
    return " ".join(text.split()).replace("_", "__")


def _icon(s: IndicatorState) -> str:
    return ICON_RECORDING if s.recording is not None else ICON_IDLE


def _status(s: IndicatorState) -> str:
    return "NeedsAttention" if s.prompt is not None and s.recording is None else "Active"


class TrayIndicator:
    """notify.indicator.Indicator for Plasma and GNOME with the AppIndicator extension."""

    def __init__(self, bus_factory: Callable[[], Awaitable[Any]] = session_bus) -> None:
        self.on_menu: Callable[[str], None] = lambda key: None
        self.on_error: Callable[[str], None] = log.warning
        # Called with True when a watcher lists the icon, False when that watcher goes away
        self.on_host: Callable[[bool], None] = lambda shown: None
        self.registered = False
        # A tray host exists but refused the icon: a failure, not a desktop without a tray
        self.register_failed = False
        self.name =f"org.freedesktop.StatusNotifierItem-{os.getpid()}-1"
        self.state = IndicatorState()
        self.revision = 1
        self._bus_factory = bus_factory
        self._bus = None
        self._failed: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._item = _Item(self)
        self._menu = _Menu(self)

    async def start(self) -> bool:
        """True once a watcher lists the icon. False means no tray host right now: at login
        Wirenote often starts before the panel, so the icon registers when a watcher appears.
        Without a session bus nothing is left behind and the icon never shows."""
        try:
            bus = await self._bus_factory()
        except Exception as e:  # noqa: BLE001 - no session bus: no icon, nothing else changes
            self._fail("start", e)
            return False
        try:
            bus.export(ITEM_PATH, self._item)
            bus.export(MENU_PATH, self._menu)
            await bus.request_name(self.name)
            # Watch first, then look, so a watcher that appears in between is not missed. The
            # bus is set before the watch: a signal read in one batch with a reply must count.
            self._bus = bus
            bus.add_message_handler(self._on_message)
            await self._call(bus, DBUS, DBUS_PATH, DBUS, "AddMatch", "s", [WATCHER_MATCH])
            (present,) = await self._call(bus, DBUS, DBUS_PATH, DBUS, "NameHasOwner", "s", [WATCHER])
        except Exception as e:  # noqa: BLE001
            self._bus = None
            self._fail("start", e)
            try:
                bus.disconnect()
            except Exception:  # noqa: BLE001 - already gone
                pass
            return False
        if present and not self.registered:
            await self._register()
        return self.registered

    @property
    def connected(self) -> bool:
        """On the session bus and watching for a tray host, listed or not."""
        return self._bus is not None

    def update(self, s: IndicatorState) -> None:
        """Before registration this only stores s; the host reads it when it lists the icon."""
        if s == self.state:
            return
        old, self.state = self.state, s
        menu_changed = menu_for(old) != menu_for(s)
        if menu_changed:
            self.revision += 1
        if not self.registered:
            return
        try:
            if _icon(old) != _icon(s):
                self._item.NewIcon()
            if _status(old) != _status(s):
                self._item.NewStatus(_status(s))
            self._item.NewToolTip()
            if menu_changed:
                self._menu.LayoutUpdated(self.revision, 0)
        except Exception as e:  # noqa: BLE001 - the panel is not worth stopping the daemon for
            self._fail("update", e)

    # Inside --------------------------------------------------------------------

    def _menu_items(self) -> list[tuple[int, MenuItem]]:
        return [(ITEM_IDS[m.key] if m.key else STATE_LINE_ID, m) for m in menu_for(self.state)]

    def _click(self, key: str) -> None:
        try:
            self.on_menu(key)
        except Exception as e:  # noqa: BLE001
            self._fail("menu", e)

    def _fail(self, what: str, e: Exception) -> None:
        """Once per kind of failure. Only the exception's type: its text may hold a name."""
        if what in self._failed:
            return
        self._failed.add(what)
        detail = f"{type(e).__name__}: {e}" if isinstance(e, TrayCallError) else type(e).__name__
        self.on_error(f"tray icon: {what} failed ({detail})")

    async def _call(self, bus, dest: str, path: str, iface: str, member: str, sig: str,
                    body: list) -> list:
        reply = await bus.call(Message(destination=dest, path=path, interface=iface,
                                       member=member, signature=sig, body=body))
        if reply.message_type == MessageType.ERROR:
            raise TrayCallError(reply.error_name)
        return reply.body

    async def _register(self) -> None:
        try:
            await self._call(self._bus, WATCHER, WATCHER_PATH, WATCHER,
                             "RegisterStatusNotifierItem", "s", [self.name])
        except Exception as e:  # noqa: BLE001
            self.register_failed = True
            self._fail("register", e)
            return
        self.register_failed = False
        self._set_registered(True)

    async def _register_again(self) -> None:
        """A watcher appeared. A refusal is reported through on_host(False), which reads
        register_failed; at start, start()'s False reports it."""
        await self._register()
        if self.register_failed:
            try:
                self.on_host(False)
            except Exception as e:  # noqa: BLE001
                self._fail("host", e)

    def _set_registered(self, on: bool) -> None:
        if on == self.registered:
            return
        self.registered = on
        try:
            self.on_host(on)
        except Exception as e:  # noqa: BLE001
            self._fail("host", e)

    def _on_message(self, msg: Message) -> None:
        """Sees every message on the bus; returns None so the rest are handled as usual."""
        if (msg.message_type == MessageType.SIGNAL and msg.interface == DBUS
                and msg.member == "NameOwnerChanged" and len(msg.body) == 3
                and msg.body[0] == WATCHER and self._bus is not None):
            if msg.body[2]:            # a watcher appeared: the panel started or restarted
                task = asyncio.ensure_future(self._register_again())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:                      # it went away, and with it our listing
                self._set_registered(False)
        return None


class _Item(ServiceInterface):
    def __init__(self, tray: TrayIndicator) -> None:
        super().__init__(ITEM_IFACE)
        self._tray = tray

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> DBusStr:
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> DBusStr:
        return "wirenote"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> DBusStr:
        return "Wirenote"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> DBusStr:
        return _status(self._tray.state)

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> DBusInt32:
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> DBusStr:
        return _icon(self._tray.state)

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> Pixmaps:
        return []

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> DBusStr:
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> DBusStr:
        return ICON_IDLE

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> DBusStr:
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> ToolTipType:
        # Plasma shows tooltip text as rich text
        return [_icon(self._tray.state), [], "Wirenote", escape_markup(describe(self._tray.state))]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> DBusBool:
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> DBusObjectPath:
        return MENU_PATH

    @dbus_method()
    def Activate(self, x: DBusInt32, y: DBusInt32):
        # Plasma opens the menu itself (ItemIsMenu); GNOME's extension calls this on a click
        self._tray._click("open")

    @dbus_method()
    def SecondaryActivate(self, x: DBusInt32, y: DBusInt32):
        pass

    @dbus_method()
    def ContextMenu(self, x: DBusInt32, y: DBusInt32):
        pass

    @dbus_method()
    def Scroll(self, delta: DBusInt32, orientation: DBusStr):
        pass

    @dbus_signal()
    def NewTitle(self):
        pass

    @dbus_signal()
    def NewIcon(self):
        pass

    @dbus_signal()
    def NewAttentionIcon(self):
        pass

    @dbus_signal()
    def NewOverlayIcon(self):
        pass

    @dbus_signal()
    def NewToolTip(self):
        pass

    @dbus_signal()
    def NewStatus(self, status: str) -> DBusStr:
        return status


class _Menu(ServiceInterface):
    def __init__(self, tray: TrayIndicator) -> None:
        super().__init__(MENU_IFACE)
        self._tray = tray

    def _props(self, m: MenuItem, names: list[str]) -> dict[str, Variant]:
        props = {"type": Variant("s", "standard"), "label": Variant("s", _label(m.label)),
                 "enabled": Variant("b", m.key is not None), "visible": Variant("b", True)}
        return {k: v for k, v in props.items() if not names or k in names}

    def _find(self, item_id: int) -> MenuItem | None:
        return next((m for i, m in self._tray._menu_items() if i == item_id), None)

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> DBusUInt32:
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> DBusStr:
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> DBusStr:
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> Strings:
        return []

    @dbus_method()
    def GetLayout(self, parent_id: DBusInt32, recursion_depth: DBusInt32,
                  property_names: Strings) -> Layout:
        revision = self._tray.revision
        if parent_id == 0:
            children = [] if recursion_depth == 0 else [
                Variant("(ia{sv}av)", [i, self._props(m, property_names), []])
                for i, m in self._tray._menu_items()]
            return [revision, [0, {"children-display": Variant("s", "submenu")}, children]]
        m = self._find(parent_id)
        if m is None:
            raise DBusError("org.freedesktop.DBus.Error.InvalidArgs", "no such menu item")
        return [revision, [parent_id, self._props(m, property_names), []]]

    @dbus_method()
    def GetGroupProperties(self, ids: Ids, property_names: Strings) -> GroupProps:
        items = self._tray._menu_items()
        return [[i, self._props(m, property_names)] for i, m in items if not ids or i in ids]

    @dbus_method()
    def GetProperty(self, item_id: DBusInt32, name: DBusStr) -> DBusVariant:
        m = self._find(item_id)
        props = self._props(m, [name]) if m else {}
        if name not in props:
            raise DBusError("org.freedesktop.DBus.Error.InvalidArgs", "no such menu property")
        return props[name]

    @dbus_method()
    def Event(self, item_id: DBusInt32, event_id: DBusStr, data: DBusVariant,
              timestamp: DBusUInt32):
        self._event(item_id, event_id)

    @dbus_method()
    def EventGroup(self, events: Events) -> Ids:
        missing = []
        for item_id, event_id, _data, _timestamp in events:
            if not self._event(item_id, event_id):
                missing.append(item_id)
        return missing

    def _event(self, item_id: int, event_id: str) -> bool:
        m = self._find(item_id)
        if m is None:
            return False      # not in the menu as it is now: a click from an outdated one
        if event_id == "clicked" and m.key is not None:
            self._tray._click(m.key)
        return True

    @dbus_method()
    def AboutToShow(self, item_id: DBusInt32) -> DBusBool:
        return False

    @dbus_method()
    def AboutToShowGroup(self, ids: Ids) -> TwoIdLists:
        return [[], []]

    @dbus_signal()
    def LayoutUpdated(self, revision: int, parent: int) -> RevisionParent:
        return [revision, parent]

    @dbus_signal()
    def ItemsPropertiesUpdated(self, updated: list, removed: list) -> PropsUpdates:
        return [updated, removed]

    @dbus_signal()
    def ItemActivationRequested(self, item_id: int, timestamp: int) -> IdTimestamp:
        return [item_id, timestamp]
