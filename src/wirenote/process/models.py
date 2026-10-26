# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Model files from Hugging Face: pinned revisions, and offline once
they are cached, so processing a meeting does not contact huggingface.co."""

from __future__ import annotations

from pathlib import Path

from huggingface_hub import hf_hub_download
from huggingface_hub.errors import LocalEntryNotFoundError

SEG_REPO, SEG_FILE = "csukuangfj/sherpa-onnx-pyannote-segmentation-3-0", "model.onnx"
EMB_REPO = "csukuangfj/speaker-embedding-models"
# Commit hashes checked on 2026-10-26. A model not listed (another whisper_model)
# uses the repo's current revision, still offline after its first download.
PINS = {
    "mobiuslabsgmbh/faster-whisper-large-v3-turbo": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    SEG_REPO: "9403a6902bb58e3d5ae8c7e77c3422de279db2e0",
    EMB_REPO: "0743f301363dec56491a490f6d6cbc9d67f9a3bf",
}


def whisper_repo(name: str) -> str:
    from faster_whisper.utils import _MODELS

    return name if "/" in name else _MODELS.get(name, name)


def _snapshot(name: str, *, revision: str | None, local_files_only: bool) -> str:
    from faster_whisper.utils import download_model

    return download_model(name, revision=revision, local_files_only=local_files_only)


def hub_file(repo: str, filename: str, local_only: bool = False) -> str:
    """Path of one file of a model repo; downloads it only if it is not cached."""
    try:
        return hf_hub_download(repo, filename, revision=PINS.get(repo), local_files_only=True)
    except LocalEntryNotFoundError:
        if local_only:
            raise
    return hf_hub_download(repo, filename, revision=PINS.get(repo))


def whisper_path(name: str, local_only: bool = False) -> str:
    """Folder of a faster-whisper model (a size like "large-v3-turbo", a repo id, or a
    local folder); downloads it only if it is not cached."""
    if Path(name).expanduser().is_dir():
        return name
    revision = PINS.get(whisper_repo(name))
    try:
        return _snapshot(name, revision=revision, local_files_only=True)
    except LocalEntryNotFoundError:
        if local_only:
            raise
    return _snapshot(name, revision=revision, local_files_only=False)
