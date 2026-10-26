# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Turn the graph mirror into the set of running microphone streams.

A mic stream (docs/dev/PLAN.md › What counts as a mic stream): a Stream/Input/Audio
node in state "running", linked from at least one Audio/Source* node, and not
owned by this daemon.
"""

from __future__ import annotations

from dataclasses import dataclass

from .graph import Graph, info, props

CAPTURE_CLASS = "Stream/Input/Audio"


@dataclass(frozen=True)
class MicStream:
    serial: int                 # object.serial; node ids get reused
    node_id: int
    client_serial: int | None   # identity is cached per client
    media_name: str | None
    node_props: dict
    client_props: dict
    source_names: tuple[str, ...]


def mic_streams(graph: Graph, own_pids: set[int], own_prefix: str) -> list[MicStream]:
    sources_by_node: dict[int, list[str]] = {}
    for link in graph.links():
        li = info(link)
        src = graph.props(li.get("output-node-id"))
        if src.get("media.class", "").startswith("Audio/Source"):
            sources_by_node.setdefault(li.get("input-node-id"), []).append(
                src.get("node.name", "?"))

    out = []
    for node in graph.nodes():
        np = props(node)
        if np.get("media.class") != CAPTURE_CLASS or info(node).get("state") != "running":
            continue
        if node["id"] not in sources_by_node:
            continue  # fed only from a sink monitor, or not linked yet
        if str(np.get("node.name", "")).startswith(own_prefix):
            continue
        cp = graph.props(np.get("client.id"))
        claimed = {cp.get("application.process.id"), np.get("application.process.id"),
                   cp.get("pipewire.sec.pid")}
        if any(_as_int(p) in own_pids for p in claimed):
            continue
        out.append(MicStream(
            serial=np.get("object.serial"),
            node_id=node["id"],
            client_serial=cp.get("object.serial"),
            media_name=np.get("media.name"),
            node_props=np,
            client_props=cp,
            source_names=tuple(sorted(set(sources_by_node[node["id"]]))),
        ))
    return out


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
