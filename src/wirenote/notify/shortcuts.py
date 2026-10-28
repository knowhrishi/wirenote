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
import time
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
# Fixed, not random: it only has to be unique on our connection, and a portal without our app
# id (before 1.19.4) makes KDE name the shortcut component after it. A random one would be a
# new component, and a new dialog, at every start.
SESSION_TOKEN = "wirenote"
# docs/dev/PLAN.md › Defaults › Shortcut debounce: a second toggle this soon is the same press
TOGGLE_DEBOUNCE_S = 1.5

# What status.json says about the shortcuts, for `wirenote doctor`; None when they are off
BOUND = "bound"           # the desktop has them
DENIED = "denied"         # declined in the desktop's dialog, or refused by the portal
# BOUND says the desktop accepted the request; the keys it set, possibly none, go to on_keys
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


_now = time.monotonic     # the debounce's clock; tests replace it


def _token() -> str:
    return f"wirenote_{secrets.token_hex(6)}"


def _keys(shortcuts: list) -> dict[str, str]:
    """{id: the desktop's description of its key, "" for none} from a portal shortcut list."""
    keys = {sid: "" for sid in SHORTCUTS}
    for sid, props in shortcuts:
        if sid in keys and "trigger_description" in props:
            # The desktop's own text: one short line wherever it is shown
            keys[sid] = " ".join(str(props["trigger_description"].value).split())[:40]
    return keys


async def start_shortcuts(bus: Any, on_activated: Callable[[str], None],
                          on_error: Callable[[str], None] = log.warning,
                          on_status: Callable[[str], None] = lambda status: None,
                          on_keys: Callable[[dict[str, str]], None] = lambda keys: None,
                          clock: Callable[[], float] | None = None) -> str | None:
    """Register, create a session and bind SHORTCUTS on bus, a connection used for nothing
    else. None once bound, otherwise the reason: DENIED, NO_PORTAL or FAILED. The keys the
    desktop set go to on_keys, now and whenever they change. Later changes (the portal stops,
    or comes back and binds again) go to on_status."""
    return await _Shortcuts(bus, on_activated, on_error, on_status, on_keys, clock).start()


class _Shortcuts:
    def __init__(self, bus, on_activated, on_error, on_status, on_keys, clock) -> None:
        self._bus = bus
        self._on_activated = on_activated
        self._on_error = on_error
        self._on_status = on_status
        self._on_keys = on_keys
        self._clock = clock or (lambda: _now())
        self._last_toggle: float | None = None
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
                "CreateSession", "a{sv}", [], {"session_handle_token": Variant("s", SESSION_TOKEN)})
            if code != 0:
                return self._refused(step, code)
            session = results["session_handle"].value
            step = "bind"
            wanted = [[sid, {"description": Variant("s", d), "preferred_trigger": Variant("s", t)}]
                      for sid, (d, t) in SHORTCUTS.items()]
            code, results = await self._request("BindShortcuts", "oa(sa{sv})sa{sv}",
                                                [session, wanted, ""], {})
            if code != 0:
                return self._refused(step, code)
            keys = _keys(results["shortcuts"].value if "shortcuts" in results else [])
        except PortalError as e:
            if e.name in NO_METHOD | NO_SERVICE:
                return NO_PORTAL
            if e.name == NOT_ALLOWED:          # e.g. "An app id is required" (portal 1.21+)
                self._on_error(f"global shortcuts: {step} refused ({e.name})")
                return DENIED
            return self._fail(step, e)
        except Exception as e:  # noqa: BLE001
            return self._fail(step, e)
        self.session = session
        self._keys_changed(keys)
        return None

    def _refused(self, step: str, code: int) -> str:
        """A Response other than 0: 1 is the user's no, 2 is the backend ending it otherwise."""
        if code == 1:
            return DENIED
        self._on_error(f"global shortcuts: {step} failed (response {code})")
        return FAILED

    def _keys_changed(self, keys: dict[str, str]) -> None:
        try:
            self._on_keys(keys)
        except Exception as e:  # noqa: BLE001
            self._fail("keys", e)

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
            elif (msg.interface == GLOBAL_SHORTCUTS and msg.member == "ShortcutsChanged"
                  and self.session is not None and len(msg.body) == 2 and msg.body[0] == self.session):
                self._keys_changed(_keys(msg.body[1]))   # you changed them in the desktop's settings
            elif (msg.interface == DBUS and msg.member == "NameOwnerChanged"
                  and len(msg.body) == 3 and msg.body[0] == PORTAL):
                self._portal_changed(bool(msg.body[1]), bool(msg.body[2]))
        except Exception as e:  # noqa: BLE001 - never into dbus-fast's reader
            self._fail("signal", e)
        return None

    def _activate(self, shortcut_id: str) -> None:
        if shortcut_id == "toggle-recording":
            # Some desktops send one press twice; a second toggle would stop what the first started
            now = self._clock()
            if self._last_toggle is not None and now - self._last_toggle < TOGGLE_DEBOUNCE_S:
                return
            self._last_toggle = now
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
