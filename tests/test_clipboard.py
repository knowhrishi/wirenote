# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Consent line to the clipboard on any desktop."""

from __future__ import annotations

import asyncio

import pytest

from wirenote.notify import clipboard


@pytest.fixture
def tools(monkeypatch):
    runs = []

    def run(argv, **kw):
        runs.append((argv, kw))
        if run.fail:
            raise clipboard.subprocess.CalledProcessError(1, argv)
    run.fail = False
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    return runs, run, monkeypatch


async def _broken_klipper(text):
    raise RuntimeError("no klipper")


def test_klipper_first(tools):
    runs, _, _ = tools
    got = []

    async def klipper(text):
        got.append(text)
    assert asyncio.run(clipboard.copy("OK to record?", klipper)) == "klipper"
    assert got == ["OK to record?"] and runs == []


def test_falls_back_in_order(tools):
    runs, _, mp = tools
    mp.setenv("DISPLAY", ":0")
    mp.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "xclip" else None)
    assert asyncio.run(clipboard.copy("OK to record?", _broken_klipper)) == "xclip"
    (argv, kw), = runs
    assert argv == ["xclip", "-selection", "clipboard"] and kw["input"] == b"OK to record?"


def test_wayland_prefers_wl_copy(tools):
    runs, _, mp = tools
    mp.setenv("WAYLAND_DISPLAY", "wayland-0")
    mp.setenv("DISPLAY", ":0")
    mp.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert asyncio.run(clipboard.copy("hi", None)) == "wl-copy"
    assert runs[0][0] == ["wl-copy"]


def test_none_when_nothing_works(tools):
    runs, run, mp = tools
    mp.setenv("DISPLAY", ":0")
    mp.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    run.fail = True
    assert asyncio.run(clipboard.copy("hi", _broken_klipper)) is None
    assert [r[0][0] for r in runs] == ["xclip", "xsel"]


def test_text_never_in_argv(tools):
    runs, _, mp = tools
    mp.setenv("WAYLAND_DISPLAY", "wayland-0")
    mp.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    asyncio.run(clipboard.copy("--secret-looking text", None))
    assert all("--secret-looking text" not in argv for argv, _ in runs)


def test_available_tools(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: "/x" if name in ("wl-copy", "xsel") else None)
    assert clipboard.available_tools() == ["wl-copy", "xsel"]
