# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""secrets.toml (mode 600): the Claude API key and calendar links.

Calendar links are secret too: anyone with an ICS link can read the calendar.
Kept out of config.toml so that file can be shared or committed.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

from .settings import config_dir


def secrets_path() -> Path:
    return config_dir() / "secrets.toml"


def load_secrets() -> dict:
    try:
        return tomllib.loads(secrets_path().read_text())
    except FileNotFoundError:
        return {}


def save_secrets(data: dict) -> None:
    """Write strings and string lists; JSON strings are valid TOML basic strings."""
    lines = ["# wirenote secrets: keep private (mode 600)."]
    for key, value in data.items():
        if isinstance(value, str):
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
        elif isinstance(value, list):
            lines.append(f"{key} = [")
            lines += [f"  {json.dumps(str(v), ensure_ascii=False)}," for v in value]
            lines.append("]")
    path = secrets_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    tmp.replace(path)


def calendar_sources(configured: tuple[str, ...]) -> tuple[str, ...]:
    """Calendar links from config.toml plus those added with `wirenote calendar add`."""
    extra = tuple(load_secrets().get("calendar_urls", []))
    return tuple(dict.fromkeys(configured + extra))


def with_config_value(text: str, key: str, toml_value: str) -> str:
    """config.toml text with one top-level key set. Replaces its live line, with
    every line of a value that spans several (a list, a multi-line string), else
    its commented-out line, else appends it."""
    line = f"{key} = {toml_value}\n"
    lines = text.splitlines(keepends=True)
    live = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=")
    commented = re.compile(rf"^[ \t]*#[ \t]*{re.escape(key)}[ \t]*=")
    for i, current in enumerate(lines):
        if live.match(current):
            return "".join(lines[:i]) + line + "".join(lines[_value_end(lines, i):])
    for i, current in enumerate(lines):
        if commented.match(current):
            return "".join(lines[:i]) + line + "".join(lines[i + 1:])
    return text + f"\n{line}"


def _value_end(lines: list[str], i: int) -> int:
    """Index after the last line of the key = value that starts on line i."""
    for k in range(i + 1, len(lines) + 1):
        try:
            tomllib.loads("".join(lines[i:k]))
            return k
        except tomllib.TOMLDecodeError:
            continue
    return i + 1


def write_config_atomic(path: Path, text: str) -> None:
    """Replace path with text in one step, private to this user (mode 600)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def set_config_value(path: Path, key: str, toml_value: str) -> None:
    """Set one top-level key in config.toml, replacing its (commented) line if present."""
    text = path.read_text() if path.exists() else ""
    write_config_atomic(path, with_config_value(text, key, toml_value))
