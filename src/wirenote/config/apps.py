# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""App rules from apps.toml: exact matches only, never substrings.

Each rule has a key, a display name, a kind (meeting, browser or ignore) and
lists of exact values for flatpak, snap, exe, binary and app_name. Browser
rules may carry title patterns: regexes on the stream's media.name that turn
the stream into a known meeting (Firefox puts the page title there; see
docs/dev/m0-findings.md › Firefox details).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("meeting", "browser", "ignore")
MATCH_FIELDS = ("flatpak", "snap", "exe", "binary", "app_name")

DEFAULT_APPS_TOML = r'''# wirenote app rules. Exact matches only; see docs/dev/PLAN.md › Matching rules.
# Fields: key, name, kind (meeting | browser | ignore), and any of
# flatpak, snap, exe, binary, app_name (lists of exact values).
# [[app.title]] entries match the stream's media.name with a regex; a match
# makes the stream a meeting. A named group "room" is shown in the prompt.

[[app]]
key = "chrome"
name = "Chrome"
kind = "browser"
exe = ["chrome"]        # verified: jitsi-chrome (audio service process)

[[app]]
key = "brave"
name = "Brave"
kind = "browser"
exe = ["brave"]         # verified: live log 2026-10-26 (audio service process, pid ok)

[[app]]
key = "firefox"
name = "Firefox"
kind = "browser"
exe = ["firefox-bin"]   # verified: jitsi-firefox (main process)

  [[app.title]]
  service = "Jitsi"
  pattern = '^(?:(?P<room>.+) \| )?Jitsi Meet$'

[[app]]
key = "discord"
name = "Discord"
kind = "meeting"
flatpak = ["com.discordapp.Discord"]   # unverified: confirm from detector log

[[app]]
key = "rocketchat"
name = "Rocket.Chat"
kind = "meeting"
flatpak = ["chat.rocket.RocketChat"]   # unverified: confirm from detector log
'''


class AppsError(ValueError):
    pass


@dataclass(frozen=True)
class TitleRule:
    service: str
    pattern: re.Pattern


@dataclass(frozen=True)
class AppRule:
    key: str
    name: str
    kind: str
    match: dict[str, frozenset[str]] = field(default_factory=dict)
    titles: tuple[TitleRule, ...] = ()

    def title_match(self, media_name: str | None) -> tuple[str, str | None] | None:
        """(service, room) when media_name matches one of the title rules."""
        if not media_name:
            return None
        for t in self.titles:
            m = t.pattern.search(media_name)
            if m:
                room = m.groupdict().get("room")
                return t.service, room
        return None


class AppRules:
    def __init__(self, rules: list[AppRule]) -> None:
        self.rules = rules
        self._index: dict[tuple[str, str], AppRule] = {}
        for rule in rules:
            for fld, values in rule.match.items():
                for v in values:
                    other = self._index.setdefault((fld, v), rule)
                    if other is not rule:
                        raise AppsError(f"{fld}={v!r} is claimed by both "
                                        f"{other.key!r} and {rule.key!r}")

    def lookup(self, fld: str, value: str | None) -> AppRule | None:
        if not value:
            return None
        return self._index.get((fld, value))

    def get(self, key: str) -> AppRule | None:
        return next((r for r in self.rules if r.key == key), None)


def parse_apps(text: str) -> AppRules:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise AppsError(f"apps.toml: {e}") from e
    rules = []
    seen = set()
    for i, raw in enumerate(data.get("app", [])):
        where = f"apps.toml [[app]] #{i + 1}"
        unknown = set(raw) - {"key", "name", "kind", "title", *MATCH_FIELDS}
        if unknown:
            raise AppsError(f"{where}: unknown field(s) {sorted(unknown)}")
        key = raw.get("key")
        if not key or key in seen:
            raise AppsError(f"{where}: missing or duplicate key {key!r}")
        seen.add(key)
        kind = raw.get("kind")
        if kind not in KINDS:
            raise AppsError(f"{where} ({key}): kind must be one of {KINDS}, got {kind!r}")
        match = {}
        for fld in MATCH_FIELDS:
            values = raw.get(fld, [])
            if isinstance(values, str):
                values = [values]
            if values:
                match[fld] = frozenset(values)
        if not match:
            raise AppsError(f"{where} ({key}): needs at least one of {MATCH_FIELDS}")
        titles = []
        for t in raw.get("title", []):
            try:
                titles.append(TitleRule(t["service"], re.compile(t["pattern"])))
            except (KeyError, re.error) as e:
                raise AppsError(f"{where} ({key}): bad title rule: {e}") from e
        rules.append(AppRule(key, raw.get("name", key), kind, match, tuple(titles)))
    return AppRules(rules)


def load_apps(path: Path) -> AppRules:
    """Read apps.toml, writing the default file first if it does not exist."""
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(DEFAULT_APPS_TOML)
    return parse_apps(path.read_text())
