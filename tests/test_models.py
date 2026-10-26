# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Model files: pinned revisions, offline after the first download."""

from __future__ import annotations

import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

from wirenote.process import models


class Calls(list):
    fake = None


@pytest.fixture
def calls(monkeypatch):
    seen = Calls()

    def fake(repo, filename=None, *, revision=None, local_files_only=False, **_k):
        seen.append({"repo": repo, "revision": revision, "local": local_files_only})
        if local_files_only and fake.missing:
            raise LocalEntryNotFoundError("not cached")
        return f"/cache/{repo}/{filename or ''}"
    fake.missing = False
    monkeypatch.setattr(models, "hf_hub_download", fake)
    monkeypatch.setattr(models, "_snapshot", lambda name, **k: fake(models.whisper_repo(name), **k))
    seen.fake = fake
    return seen


def test_hub_file_tries_offline_first(calls):
    models.hub_file(models.SEG_REPO, models.SEG_FILE)
    assert calls == [{"repo": models.SEG_REPO, "revision": models.PINS[models.SEG_REPO],
                      "local": True}]


def test_hub_file_downloads_once_when_missing(calls):
    calls.fake.missing = True
    models.hub_file(models.EMB_REPO, "nemo_en_titanet_small.onnx")
    assert [c["local"] for c in calls] == [True, False]
    assert all(c["revision"] == models.PINS[models.EMB_REPO] for c in calls)


def test_local_only_never_downloads(calls):
    calls.fake.missing = True
    with pytest.raises(LocalEntryNotFoundError):
        models.whisper_path("large-v3-turbo", local_only=True)
    assert [c["local"] for c in calls] == [True]
    assert calls[0]["revision"] == models.PINS["mobiuslabsgmbh/faster-whisper-large-v3-turbo"]


def test_whisper_path_accepts_a_local_folder(tmp_path, calls):
    assert models.whisper_path(str(tmp_path)) == str(tmp_path)
    assert calls == []
