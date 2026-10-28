# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Global shortcuts through xdg-desktop-portal's GlobalShortcuts portal, over dbus-fast
(docs/usage.md › Keyboard shortcuts).

On a connection of its own, Wirenote first registers its app id with
org.freedesktop.host.portal.Registry (portal 1.19.4+; older portals lack it and are used
without). A systemd service has no app id the portal can find, and portal 1.21+ refuses
GlobalShortcuts without one. Then it creates a session and binds SHORTCUTS, every start,
with the same ids: KDE Plasma and GNOME 48+ ask only about ids they have not seen. Each
request is answered by a Response signal on a path the caller can predict, so the signal
is awaited before the call goes out. An Activated signal for our session calls
on_activated with the shortcut id.

Nothing here raises: a failure becomes one of the reasons below, and the daemon carries on.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from .. import APP_ID

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REGISTRY = "org.freedesktop.host.portal.Registry"
GLOBAL_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
REQUEST = "org.freedesktop.portal.Request"
DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
SIGNALS_MATCH = f"type='signal',sender='{PORTAL}',path_namespace='{PORTAL_PATH}'"
PORTAL_MATCH = (f"type='signal',sender='{DBUS}',interface='{DBUS}',member='NameOwnerChanged',"
                f"arg0='{PORTAL}'")

# id: (description, preferred trigger). Keep the ids: a new id asks the user again.
SHORTCUTS = {"toggle-recording": ("Start or stop recording", "CTRL+ALT+R"),
             "flag-moment": ("Flag this moment", "CTRL+ALT+F")}
COMMANDS = {"toggle-recording": "toggle", "flag-moment": "flag"}   # the daemon's _control

# What status.json says about the shortcuts, for `wirenote doctor`; None when they are off
BOUND = "bound"           # the desktop has them
DENIED = "denied"         # declined in the desktop's dialog, or refused by the portal
NO_PORTAL = "no portal"   # no GlobalShortcuts portal on this desktop
FAILED = "failed"         # something broke; the log says what

NO_METHOD = {"org.freedesktop.DBus.Error.UnknownMethod", "org.freedesktop.DBus.Error.UnknownInterface",
             "org.freedesktop.DBus.Error.UnknownObject"}
NO_SERVICE = {"org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner"}
NOT_ALLOWED = "org.freedesktop.portal.Error.NotAllowed"

log = logging.getLogger(__name__)


class PortalError(Exception):
    """An error reply; its text is the D-Bus error name, which carries no user data."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


async def session_bus() -> MessageBus:
    return await MessageBus().connect()


def _token() -> str:
    return f"wirenote_{secrets.token_hex(6)}"


async def start_shortcuts(bus: Any, on_activated: Callable[[str], None],
                          on_error: Callable[[str], None] = log.warning,
                          on_status: Callable[[str], None] = lambda status: None) -> str | None:
    """Register, create a session and bind SHORTCUTS on bus, a connection used for nothing
    else. None once bound, otherwise the reason: DENIED, NO_PORTAL or FAILED. Later changes
    (the portal stops, or comes back and binds again) go to on_status."""
    return await _Shortcuts(bus, on_activated, on_error, on_status).start()


class _Shortcuts:
    def __init__(self, bus, on_activated, on_error, on_status) -> None:
        self._bus = bus
        self._on_activated = on_activated
        self._on_error = on_error
        self._on_status = on_status
        self.session: str | None = None
        self._lost = False                  # bound, then the portal went away
        self._waiting: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> str | None:
        try:
            self._bus.add_message_handler(self._on_message)
            await self._call(DBUS, DBUS_PATH, DBUS, "AddMatch", "s", [SIGNALS_MATCH])
            await self._call(DBUS, DBUS_PATH, DBUS, "AddMatch", "s", [PORTAL_MATCH])
        except Exception as e:  # noqa: BLE001 - no bus to speak of: no shortcuts
            return self._fail("start", e)
        return await self._bind()

    async def _bind(self) -> str | None:
        step = "register"
        try:
            try:
                await self._call(PORTAL, PORTAL_PATH, REGISTRY, "Register", "sa{sv}", [APP_ID, {}])
            except PortalError as e:
                if e.name not in NO_METHOD:
                    raise
                # A portal before 1.19.4: it may still find an id, or refuse the session below
            step = "session"
            code, results = await self._request(
                "CreateSession", "a{sv}", [], {"session_handle_token": Variant("s", _token())})
            if code != 0:
                return DENIED
            session = results["session_handle"].value
            step = "bind"
            wanted = [[sid, {"description": Variant("s", d), "preferred_trigger": Variant("s", t)}]
                      for sid, (d, t) in SHORTCUTS.items()]
            code, _ = await self._request("BindShortcuts", "oa(sa{sv})sa{sv}", [session, wanted, ""], {})
            if code != 0:
                return DENIED
        except PortalError as e:
            if e.name in NO_METHOD | NO_SERVICE:
                return NO_PORTAL
            if e.name == NOT_ALLOWED:
                return DENIED
            return self._fail(step, e)
        except Exception as e:  # noqa: BLE001
            return self._fail(step, e)
        self.session = session
        return None

    def _fail(self, what: str, e: Exception) -> str:
        """Only the error name or the exception's type: its text may hold a path."""
        detail = e.name if isinstance(e, PortalError) else type(e).__name__
        self._on_error(f"global shortcuts: {what} failed ({detail})")
        return FAILED

    async def _call(self, dest: str, path: str, iface: str, member: str, sig: str, body: list) -> list:
        reply = await self._bus.call(Message(destination=dest, path=path, interface=iface,
                                             member=member, signature=sig, body=body))
        if reply.message_type == MessageType.ERROR:
            raise PortalError(reply.error_name)
        return reply.body

    async def _request(self, member: str, sig: str, args: list, options: dict) -> tuple[int, dict]:
        """A portal method answered later by a Response signal. Its path is known before the
        call, so a Response read in the same batch as the reply is not lost."""
        token = _token()
        sender = self._bus.unique_name.lstrip(":").replace(".", "_")
        path = f"{PORTAL_PATH}/request/{sender}/{token}"
        future = asyncio.get_running_loop().create_future()
        self._waiting[path] = future
        try:
            (handle,) = await self._call(PORTAL, PORTAL_PATH, GLOBAL_SHORTCUTS, member, sig,
                                         [*args, {**options, "handle_token": Variant("s", token)}])
            self._waiting[handle] = future      # a portal that picked its own path
            return await future
        finally:
            for key in [k for k, f in self._waiting.items() if f is future]:
                del self._waiting[key]

    def _on_message(self, msg: Message) -> None:
        """Sees every message on the connection; returns None so the rest are handled as usual."""
        try:
            if msg.message_type != MessageType.SIGNAL:
                return None
            if msg.interface == REQUEST and msg.member == "Response":
                future = self._waiting.get(msg.path)
                if future is not None and not future.done():
                    code, results = msg.body
                    future.set_result((code, results))
            elif (msg.interface == GLOBAL_SHORTCUTS and msg.member == "Activated"
                  and self.session is not None and len(msg.body) >= 2
                  and msg.body[0] == self.session and msg.body[1] in SHORTCUTS):
                self._activate(msg.body[1])
            elif (msg.interface == DBUS and msg.member == "NameOwnerChanged"
                  and len(msg.body) == 3 and msg.body[0] == PORTAL):
                self._portal_changed(bool(msg.body[1]), bool(msg.body[2]))
        except Exception as e:  # noqa: BLE001 - never into dbus-fast's reader
            self._fail("signal", e)
        return None

    def _activate(self, shortcut_id: str) -> None:
        try:
            self._on_activated(shortcut_id)
        except Exception as e:  # noqa: BLE001 - a key press is not worth stopping the daemon for
            self._fail(shortcut_id, e)

    def _portal_changed(self, old: bool, new: bool) -> None:
        """The portal went away (its session with it), or came back: bind again then, unless
        the shortcuts were never bound, which would ask a question already answered."""
        if old and self.session is not None:
            self.session = None
            self._lost = True
            self._on_error("global shortcuts: the portal stopped")
            self._on_status(FAILED)
        if new and self._lost:
            self._lost = False
            task = asyncio.ensure_future(self._rebind())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _rebind(self) -> None:
        reason = await self._bind()
        try:
            self._on_status(reason or BOUND)
        except Exception as e:  # noqa: BLE001
            self._fail("status", e)
