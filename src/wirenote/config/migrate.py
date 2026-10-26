# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""One-time move from meetnote's folders to Wirenote's.

Copies first and deletes the old folders only after everything worked, so a
failure leaves meetnote's data exactly as it was.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import tomllib
from pathlib import Path

from .perms import fix_modes
from .settings import config_dir, data_dir, state_dir

OLD = "meetnote"
LINK = re.compile(r"^(https?|webcal)://", re.IGNORECASE)


class MigrationError(Exception):
    pass


def _pairs() -> list[tuple[Path, Path]]:
    """(old, new) for the config, state and data folders."""
    return [(new.parent / OLD, new) for new in (config_dir(), state_dir(), data_dir())]


def _old_daemon_running() -> bool:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    path = runtime / f"{OLD}.sock"
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(str(path))
        return True
    except OSError:
        return False


def _replace_all(value, swaps: list[tuple[str, str]]):
    if isinstance(value, str):
        for old, new in swaps:
            if value == old or value.startswith(old + "/"):
                return new + value[len(old):]
        return value
    if isinstance(value, list):
        return [_replace_all(v, swaps) for v in value]
    if isinstance(value, dict):
        return {k: _replace_all(v, swaps) for k, v in value.items()}
    return value


def _rewrite_paths(new_data: Path, new_state: Path, swaps: list[tuple[str, str]]) -> None:
    """Absolute paths in manifests and the jobs database point at the new folders."""
    for manifest in (new_data / "recordings").glob("*/manifest.json"):
        data = json.loads(manifest.read_text())
        manifest.write_text(json.dumps(_replace_all(data, swaps), indent=2, ensure_ascii=False))
    db_path = new_state / "jobs.db"
    if db_path.exists():
        db = sqlite3.connect(db_path)
        try:
            for old, new in swaps:
                for col in ("recording", "note"):
                    db.execute(f"UPDATE jobs SET {col} = ? || substr({col}, ?) "
                               f"WHERE {col} = ? OR {col} LIKE ?",
                               (new, len(old) + 1, old, old + "/%"))
            db.commit()
        finally:
            db.close()


def _move_calendar_links(cfg_dir: Path) -> None:
    """Calendar links are secrets: they go from config.toml to secrets.toml (mode 600)."""
    from .secrets import load_secrets, save_secrets, set_config_value

    path = cfg_dir / "config.toml"
    if not path.exists():
        return
    urls = tomllib.loads(path.read_text()).get("calendar_urls", [])
    links = [u for u in urls if LINK.match(str(u))]
    if not links:
        return
    secrets = load_secrets()
    secrets["calendar_urls"] = list(dict.fromkeys([*secrets.get("calendar_urls", []), *links]))
    save_secrets(secrets)
    files = [u for u in urls if u not in links]
    set_config_value(path, "calendar_urls", json.dumps(files, ensure_ascii=False))
    tomllib.loads(path.read_text())   # a broken result rolls the migration back


def migrate_if_needed() -> str | None:
    """Move meetnote's data once; None when there is nothing to move."""
    pairs = _pairs()
    if not any(old.is_dir() for old, _ in pairs) or any(new.exists() for _, new in pairs):
        return None
    if _old_daemon_running():
        raise MigrationError("meetnote is still running; stop the old service first: "
                             "systemctl --user disable --now meetnote meetnote-ui")
    created: list[Path] = []
    try:
        for old, new in pairs:
            if not old.is_dir():
                continue
            tmp = new.with_name(f".{new.name}.migrating")
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(old, tmp, symlinks=True)
            created.append(tmp)
            tmp.rename(new)
            created[-1] = new
        swaps = [(str(old), str(new)) for old, new in pairs]
        _rewrite_paths(data_dir(), state_dir(), swaps)
        _move_calendar_links(config_dir())
        fix_modes([new for _, new in pairs])
    except Exception as e:  # noqa: BLE001 - undo the copies, keep the originals
        for path in created:
            shutil.rmtree(path, ignore_errors=True)
        raise MigrationError(f"could not move meetnote's data ({e}); nothing was changed") from e
    for old, _ in pairs:
        shutil.rmtree(old, ignore_errors=True)
    return "Wirenote: migrated your meetnote data (settings, recordings, jobs)."
