# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""PID rule, pid check and lookup order (docs/dev/m0-findings.md §3, docs/dev/PLAN.md › Identifying the app)."""

from __future__ import annotations

from wirenote.config.apps import DEFAULT_APPS_TOML, parse_apps
from wirenote.detector.identity import cmdline0, gather_evidence, parse_cgroup, resolve

from conftest import FakeProc

RULES = parse_apps(DEFAULT_APPS_TOML)
PULSE_SERVER = {"exe_basename": "pipewire", "comm": "pipewire-pulse", "ppid": 1}


def test_pulse_client_uses_self_reported_pid_not_the_pulse_server():
    # Measured with parecord: sec.pid is pipewire-pulse, application.process.id is the app.
    client = {"client.api": "pipewire-pulse", "pipewire.sec.pid": 6148,
              "application.process.id": 1288290, "application.process.binary": "pacat"}
    proc = FakeProc({6148: PULSE_SERVER,
                     1288290: {"exe_basename": "pacat", "comm": "parecord", "ppid": 1}})
    ev = gather_evidence(client, {}, proc)
    assert (ev.pid, ev.pid_source, ev.pid_ok, ev.exe) == (1288290, "application.process.id", True, "pacat")


def test_native_client_uses_socket_pid():
    client = {"pipewire.sec.pid": 1279929, "application.process.id": 1279929,
              "application.process.binary": "pw-cat"}
    proc = FakeProc({1279929: {"exe_basename": "pw-cat", "comm": "pw-record", "ppid": 1}})
    ev = gather_evidence(client, {}, proc)
    assert (ev.pid_source, ev.pid_ok) == ("pipewire.sec.pid", True)


def test_pid_mismatch_skips_pid_steps_but_portal_app_id_still_resolves():
    # A Flatpak app reporting a sandbox PID that names an unrelated host process.
    client = {"client.api": "pipewire-pulse", "application.process.id": 2,
              "application.process.binary": "Discord",
              "pipewire.access.portal.app_id": "com.discordapp.Discord"}
    proc = FakeProc({2: {"exe_basename": "kthreadd", "comm": "kthreadd", "ppid": 0,
                         "flatpak_id": "org.example.Wrong"}})
    ev = gather_evidence(client, {}, proc)
    assert ev.pid_ok is False and ev.exe is None and ev.flatpak_id is None
    ident = resolve(ev, RULES)
    assert (ident.key, ident.matched_by) == ("discord", "portal_app_id")


def test_flatpak_cgroup_wins_over_exe():
    client = {"client.api": "pipewire-pulse", "application.process.id": 500,
              "application.process.binary": "Discord"}
    proc = FakeProc({500: {"exe_basename": "Discord", "comm": "Discord", "ppid": 1,
                           "flatpak_id": "com.discordapp.Discord"}})
    ident = resolve(gather_evidence(client, {}, proc), RULES)
    assert (ident.key, ident.matched_by) == ("discord", "cgroup.flatpak")


def test_parent_chain_resolves_helper_processes():
    # A helper whose own exe is unknown, under a known browser.
    client = {"client.api": "pipewire-pulse", "application.process.id": 30,
              "application.process.binary": "helper"}
    proc = FakeProc({30: {"exe_basename": "helper", "comm": "helper", "ppid": 20},
                     20: {"exe_basename": "chrome", "comm": "chrome", "ppid": 1}})
    ident = resolve(gather_evidence(client, {}, proc), RULES)
    assert (ident.key, ident.matched_by) == ("chrome", "parent1")


def test_unknown_app_has_no_rule():
    client = {"pipewire.sec.pid": 9, "application.process.binary": "obs"}
    proc = FakeProc({9: {"exe_basename": "obs", "comm": "obs", "ppid": 1}})
    ident = resolve(gather_evidence(client, {}, proc), RULES)
    assert ident.rule is None and ident.evidence.exe == "obs"


def test_dead_process_falls_back_to_reported_binary():
    client = {"client.api": "pipewire-pulse", "application.process.id": 77,
              "application.process.binary": "firefox-bin"}
    ident = resolve(gather_evidence(client, {}, FakeProc({})), RULES)
    assert (ident.key, ident.matched_by) == ("firefox", "binary")


def test_comm_truncated_to_15_chars_still_passes_pid_check():
    client = {"pipewire.sec.pid": 5, "application.process.binary": "teams-for-linux-bin"}
    proc = FakeProc({5: {"exe_basename": None, "comm": "teams-for-linux", "ppid": 1}})
    assert gather_evidence(client, {}, proc).pid_ok is True


def test_cmdline0_handles_rewritten_chrome_argv():
    assert cmdline0(["/opt/google/chrome/chrome --type=utility --x=/dev/dri/renderD128"]) == "chrome"
    assert cmdline0(["/usr/bin/pacat", "--record"]) == "pacat"
    assert cmdline0([]) is None


def test_parse_cgroup():
    fp = parse_cgroup("0::/user.slice/user@1000.service/app.slice/"
                      "app-flatpak-com.discordapp.Discord-12345.scope\n")
    assert fp["flatpak_id"] == "com.discordapp.Discord"
    assert fp["unit"] == "app-flatpak-com.discordapp.Discord-12345.scope"
    snap = parse_cgroup("0::/user.slice/user@1000.service/app.slice/snap.zoom-client.zoom-client-1a2b.scope")
    assert snap["snap_name"] == "zoom-client"
    plain = parse_cgroup("0::/user.slice/user@1000.service/app.slice/app-com.google.Chrome-154384.scope")
    assert plain["flatpak_id"] is None and plain["unit"] == "app-com.google.Chrome-154384.scope"


def test_brave_audio_service_resolves_by_exe():
    # Live evidence: like Chrome, the PID is the audio service; its scope says Chromium.
    client = {"client.api": "pipewire-pulse", "application.process.id": 40,
              "application.process.binary": "brave", "application.name": "Brave input"}
    proc = FakeProc({40: {"exe_basename": "brave", "comm": "brave", "ppid": 39},
                     39: {"exe_basename": "brave", "comm": "brave", "ppid": 1}})
    ident = resolve(gather_evidence(client, {}, proc), RULES)
    assert (ident.key, ident.matched_by) == ("brave", "exe")
