# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
from __future__ import annotations

import pytest

from wirenote.config.apps import DEFAULT_APPS_TOML, AppsError, parse_apps
from wirenote.config.settings import Settings, parse_settings
from wirenote.detector.graph import Graph, iter_batches
from wirenote.detector.streams import mic_streams

MONITOR_OUTPUT = """[
  {
    "id": 58,
    "type": "PipeWire:Interface:Node",
    "info": { "state": "idle", "props": { "media.class": "Audio/Source", "node.name": "mic" } }
  }
]
[
  {
    "id": 145,
    "type": "PipeWire:Interface:Node",
    "info": {
      "state": "running",
      "props": { "media.class": "Stream/Input/Audio", "object.serial": 8036, "client.id": 87 },
      "params": { "Format": [
]
      }
    }
  },
  {
    "id": 110,
    "type": "PipeWire:Interface:Link",
    "info": { "output-node-id": 58, "input-node-id": 145, "state": "active" }
  }
]
[
  {
    "id": 145,
    "info": null
  }
]
"""


def test_framing_handles_nested_bracket_at_column_0():
    batches = list(iter_batches(MONITOR_OUTPUT.splitlines(keepends=True)))
    assert [len(b) for b in batches] == [1, 2, 1]


def test_mirror_replace_remove_and_mic_detection():
    g = Graph()
    batches = list(iter_batches(MONITOR_OUTPUT.splitlines(keepends=True)))
    g.apply(batches[0])
    g.apply(batches[1])
    streams = mic_streams(g, own_pids=set(), own_prefix="wirenote-")
    assert [(s.serial, s.source_names) for s in streams] == [(8036, ("mic",))]
    g.apply(batches[2])                      # removal: {"id": 145, "info": null}
    assert 145 not in g.objects
    assert mic_streams(g, set(), "wirenote-") == []


def _graph(stream_props, source_class="Audio/Source", state="running", client_props=None):
    g = Graph()
    g.apply([
        {"id": 1, "type": "PipeWire:Interface:Node",
         "info": {"state": "running", "props": {"media.class": source_class, "node.name": "src"}}},
        {"id": 2, "type": "PipeWire:Interface:Node",
         "info": {"state": state, "props": {"media.class": "Stream/Input/Audio",
                                            "object.serial": 10, "client.id": 3, **stream_props}}},
        {"id": 3, "type": "PipeWire:Interface:Client", "info": {"props": client_props or {}}},
        {"id": 4, "type": "PipeWire:Interface:Link",
         "info": {"output-node-id": 1, "input-node-id": 2, "state": "active"}},
    ])
    return g


def test_stream_fed_from_sink_monitor_is_not_a_mic():
    assert mic_streams(_graph({}, source_class="Audio/Sink"), set(), "wirenote-") == []


def test_idle_stream_is_not_a_mic():
    assert mic_streams(_graph({}, state="idle"), set(), "wirenote-") == []


def test_own_streams_are_excluded_by_prefix_and_pid():
    assert mic_streams(_graph({"node.name": "wirenote-mic"}), set(), "wirenote-") == []
    g = _graph({}, client_props={"application.process.id": 4242})
    assert mic_streams(g, {4242}, "wirenote-") == []
    assert len(mic_streams(g, {1}, "wirenote-")) == 1


def test_virtual_source_counts_as_mic():
    assert len(mic_streams(_graph({}, source_class="Audio/Source/Virtual"), set(), "wirenote-")) == 1


def test_default_apps_parse_and_firefox_title_rule():
    rules = parse_apps(DEFAULT_APPS_TOML)
    ff = rules.get("firefox")
    assert ff.title_match("Room | Jitsi Meet") == ("Jitsi", "Room")
    assert ff.title_match("Jitsi Meet") == ("Jitsi", None)
    assert ff.title_match("RecordStream") is None
    assert ff.title_match("Not Jitsi Meet at all") is None
    assert rules.lookup("exe", "chrome").key == "chrome"
    assert rules.lookup("exe", "chromium") is None          # exact only, no substrings


@pytest.mark.parametrize("text, msg", [
    ('[[app]]\nkey="a"\nkind="meeting"\n', "needs at least one"),
    ('[[app]]\nkey="a"\nkind="party"\nexe=["a"]\n', "kind must be"),
    ('[[app]]\nkey="a"\nkind="meeting"\nexe=["x"]\n[[app]]\nkey="b"\nkind="meeting"\nexe=["x"]\n',
     "claimed by both"),
    ('[[app]]\nkey="a"\nkind="meeting"\nexe=["x"]\nexes=["y"]\n', "unknown field"),
])
def test_bad_apps_toml_is_rejected(text, msg):
    with pytest.raises(AppsError, match=msg):
        parse_apps(text)


def test_settings_defaults_and_overrides():
    assert Settings().grace_s == 4.0 and Settings().dwell_browser_s == 5.0
    s = parse_settings("grace_s = 6\ndwell_browser_s = 8.5\n")
    assert (s.grace_s, s.dwell_browser_s, s.dwell_meeting_s) == (6.0, 8.5, 2.0)
    with pytest.raises(ValueError, match="unknown setting"):
        parse_settings("gracee_s = 1\n")
