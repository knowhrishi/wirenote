# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""In-memory mirror of the PipeWire object graph, fed by `pw-dump --monitor`.

Output format (docs/dev/m0-findings.md §2): pretty-printed JSON arrays written
back to back, each ending with "]" alone at column 0. The first array is the
full graph; later arrays are update batches. Updates carry the whole object,
so they replace; a removal is {"id": N, "info": null} with no "type".
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator


class BatchFramer:
    """Collects pw-dump output lines; feed() returns a batch once one is complete."""

    def __init__(self) -> None:
        self._buf: list[str] = []

    def feed(self, line: str) -> list[dict] | None:
        self._buf.append(line)
        if line.rstrip("\n") != "]":
            return None
        try:
            batch = json.loads("".join(self._buf))
        except json.JSONDecodeError:
            return None  # a nested "]" at column 0; keep reading
        self._buf.clear()
        return batch


def iter_batches(lines: Iterable[str]) -> Iterator[list[dict]]:
    """Yield each top-level JSON array from pw-dump output, line by line."""
    framer = BatchFramer()
    for line in lines:
        batch = framer.feed(line)
        if batch is not None:
            yield batch


def is_removal(obj: dict) -> bool:
    return obj.get("info") is None and "type" not in obj


class Graph:
    """Objects keyed by id. Ids are reused by PipeWire; object.serial is not."""

    def __init__(self) -> None:
        self.objects: dict[int, dict] = {}

    def apply(self, batch: list[dict]) -> None:
        for obj in batch:
            if is_removal(obj):
                self.objects.pop(obj["id"], None)
            else:
                self.objects[obj["id"]] = obj

    def clear(self) -> None:
        self.objects.clear()

    def props(self, oid: int | None) -> dict:
        obj = self.objects.get(oid) if oid is not None else None
        return ((obj or {}).get("info") or {}).get("props") or {}

    def of_type(self, suffix: str) -> Iterator[dict]:
        for obj in self.objects.values():
            if obj.get("type", "").endswith(suffix):
                yield obj

    def nodes(self) -> Iterator[dict]:
        return self.of_type(":Node")

    def links(self) -> Iterator[dict]:
        return self.of_type(":Link")

    def default_node(self, key: str = "default.audio.source") -> str | None:
        """node.name from the "default" Metadata object. Metadata keeps its props
        at the top level, not under info."""
        for obj in self.of_type(":Metadata"):
            if (obj.get("props") or {}).get("metadata.name") != "default":
                continue
            for entry in obj.get("metadata") or []:
                if entry.get("key") == key:
                    value = entry.get("value")
                    if isinstance(value, str):
                        try:
                            value = json.loads(value)
                        except json.JSONDecodeError:
                            return value or None
                    return (value or {}).get("name") if isinstance(value, dict) else None
        return None


def info(obj: dict | None) -> dict:
    return (obj or {}).get("info") or {}


def props(obj: dict | None) -> dict:
    return info(obj).get("props") or {}
