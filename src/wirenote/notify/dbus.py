# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Desktop notifications (org.freedesktop.Notifications) and logind sleep signals."""

from __future__ import annotations

import logging
from collections.abc import Callable

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

NOTIFY_NAME = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"
KLIPPER_NAME = "org.kde.klipper"   # Plasma's clipboard, used for the consent line
KLIPPER_PATH = "/klipper"
KLIPPER_IFACE = "org.kde.klipper.klipper"
LOGIND_NAME = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"

# NotificationClosed reasons (Desktop Notifications spec)
CLOSED_EXPIRED, CLOSED_DISMISSED, CLOSED_BY_CALL, CLOSED_UNDEFINED = 1, 2, 3, 4

log = logging.getLogger(__name__)


def escape_markup(text: str) -> str:
    """Page titles and calendar titles reach the notification; servers that render
    markup (Plasma: body-markup, body-hyperlinks) must show them as text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Notifier:
    """Thin wrapper over the notification server.

    on_action(nid, action_key) and on_closed(nid, reason) are called from the
    event loop when the server reports a click or a close.
    """

    def __init__(self, app_name: str = "Wirenote") -> None:
        self.app_name = app_name
        self.caps: set[str] = set()
        self.server: str = "?"
        self.on_action: Callable[[int, str], None] = lambda nid, key: None
        self.on_closed: Callable[[int, int], None] = lambda nid, reason: None
        self._iface = None
        self._bus = None

    async def connect(self) -> None:
        bus = self._bus = await MessageBus().connect()
        intro = await bus.introspect(NOTIFY_NAME, NOTIFY_PATH)
        self._iface = bus.get_proxy_object(NOTIFY_NAME, NOTIFY_PATH, intro).get_interface(NOTIFY_NAME)
        self.caps = set(await self._iface.call_get_capabilities())
        name, vendor, version, _spec = await self._iface.call_get_server_information()
        self.server = f"{vendor} {name} {version}"
        self._iface.on_action_invoked(lambda nid, key: self.on_action(nid, key))
        self._iface.on_notification_closed(lambda nid, reason: self.on_closed(nid, reason))

    @property
    def has_actions(self) -> bool:
        return "actions" in self.caps

    async def notify(self, summary: str, body: str = "", actions: list[tuple[str, str]] = (),
                     timeout_ms: int = 0, urgency: int = 1) -> int:
        """Show a notification; timeout 0 means it never expires on its own."""
        flat = [x for key, label in actions for x in (key, label)] if self.has_actions else []
        hints = {"urgency": Variant("y", urgency)}
        return await self._iface.call_notify(self.app_name, 0, "audio-input-microphone",
                                             escape_markup(summary), escape_markup(body), flat,
                                             hints, timeout_ms)

    async def close(self, nid: int) -> None:
        await self._iface.call_close_notification(nid)

    async def copy_to_clipboard(self, text: str) -> None:
        """Through Klipper on the session bus; raises if Klipper is not running."""
        intro = await self._bus.introspect(KLIPPER_NAME, KLIPPER_PATH)
        klipper = self._bus.get_proxy_object(KLIPPER_NAME, KLIPPER_PATH, intro).get_interface(
            KLIPPER_IFACE)
        await klipper.call_set_clipboard_contents(text)


async def watch_sleep(on_sleep: Callable[[bool], None]) -> None:
    """Call on_sleep(True) before suspend and on_sleep(False) after resume."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    intro = await bus.introspect(LOGIND_NAME, LOGIND_PATH)
    mgr = bus.get_proxy_object(LOGIND_NAME, LOGIND_PATH, intro).get_interface(
        "org.freedesktop.login1.Manager")
    mgr.on_prepare_for_sleep(on_sleep)
