# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Who spoke when on the Others track, and who they are (docs/dev/features-2-design.md › Speakers).

- Diarization: sherpa-onnx, pyannote segmentation 3.0 plus a speaker-embedding
  model, CPU only. Speakers are labelled S1, S2, ... in order of first speech.
- Whisper's word timestamps are split at speaker changes, so one line never
  mixes two people.
- Names, in this order: a remembered voice (voices.json, taught by renaming a
  speaker in the app), then the summary backend's reading of the transcript
  ("Thanks, Anna") checked against the calendar attendees, else "Speaker N".
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config.settings import Settings, data_dir
from .models import EMB_REPO, SEG_FILE, SEG_REPO, hub_file  # noqa: F401 - re-exported

EMBED_SECONDS = 30.0      # speech per speaker used for its voice print
MIN_TURN_S = 0.8          # shorter turns are too short for a reliable voice print


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    label: int


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


def label_name(label: str) -> str:
    """S3 -> Speaker 3; Me and legacy Others stay."""
    m = re.fullmatch(r"S(\d+)", label)
    return f"Speaker {m.group(1)}" if m else label


def display_name(label: str, speakers: dict) -> str:
    info = speakers.get(label) or {}
    return info.get("name") or label_name(label)


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


class Diarizer:
    def __init__(self, settings: Settings) -> None:
        import sherpa_onnx

        seg = hub_file(SEG_REPO, SEG_FILE)
        emb = hub_file(EMB_REPO, settings.diarize_model)
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg)),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1,
                                                        threshold=settings.diarize_threshold),
            min_duration_on=0.3, min_duration_off=0.5)
        if not cfg.validate():
            raise RuntimeError("speaker separation models are incomplete")
        self.sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb))

    def run(self, audio: np.ndarray, rate: int = 16000) -> tuple[list[Turn], dict[int, np.ndarray]]:
        """Speaker turns of one file, plus one voice print per speaker."""
        turns = [Turn(s.start, s.end, s.speaker) for s in self.sd.process(audio).sort_by_start_time()]
        prints = {}
        for label in {t.label for t in turns}:
            mine = sorted((t for t in turns if t.label == label), key=lambda t: t.start - t.end)
            chunks, total = [], 0.0
            for t in mine:
                if t.end - t.start < MIN_TURN_S and chunks:
                    continue
                chunks.append(audio[int(t.start * rate):int(t.end * rate)])
                total += t.end - t.start
                if total >= EMBED_SECONDS:
                    break
            stream = self.extractor.create_stream()
            stream.accept_waveform(rate, np.concatenate(chunks))
            stream.input_finished()
            prints[label] = _unit(self.extractor.compute(stream))
        return turns, prints


def split_by_speaker(words: list[Word], turns: list[Turn]) -> list[tuple[int | None, list[Word]]]:
    """Group consecutive words by the speaker whose turn holds each word's middle
    (the nearest turn when none does)."""
    runs: list[tuple[int | None, list[Word]]] = []
    for w in words:
        mid = (w.start + w.end) / 2
        inside = [t for t in turns if t.start <= mid <= t.end]
        if inside:
            label = min(inside, key=lambda t: t.end - t.start).label
        elif turns:
            label = min(turns, key=lambda t: min(abs(t.start - mid), abs(t.end - mid))).label
        else:
            label = None
        if runs and runs[-1][0] == label:
            runs[-1][1].append(w)
        else:
            runs.append((label, [w]))
    return runs


SENTENCE_END = (".", "?", "!", "…", "。", "？", "！")
MAX_PAUSE_S = 1.0          # a longer pause ends a sentence even without punctuation
SPLIT_BELOW_SHARE = 0.6    # a sentence is split only if no speaker has this share of it
MIN_SPLIT_WORDS = 6        # shorter sentences are never split


def sentences(words: list[Word]) -> list[list[Word]]:
    """Words grouped into sentences: at end punctuation or a pause. Whisper's own
    segments are not used, because a vocabulary prompt turns them into one block."""
    out: list[list[Word]] = []
    for w in words:
        if out and not out[-1][-1].text.strip().endswith(SENTENCE_END) \
                and w.start - out[-1][-1].end <= MAX_PAUSE_S:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def _overlap(w: Word, t: Turn) -> float:
    return max(0.0, min(w.end, t.end) - max(w.start, t.start))


def assign_sentence(words: list[Word], turns: list[Turn]) -> list[tuple[int | None, list[Word]]]:
    """A sentence goes to the speaker who covers most of it. Only a long sentence
    that really spans two people is split; 1-word runs then rejoin a neighbour."""
    share: dict[int, float] = {}
    for w in words:
        for t in turns:
            o = _overlap(w, t)
            if o:
                share[t.label] = share.get(t.label, 0.0) + o
    if not share:   # between turns: the nearest speaker takes the whole sentence
        runs = split_by_speaker(words, turns)
        return [(runs[0][0] if runs else None, words)]
    best = max(share, key=share.get)
    if len(words) < MIN_SPLIT_WORDS or share[best] / sum(share.values()) >= SPLIT_BELOW_SHARE:
        return [(best, words)]
    runs = split_by_speaker(words, turns)
    merged: list[tuple[int | None, list[Word]]] = []
    for label, ws in runs:
        if merged and (len(ws) < 2 or merged[-1][0] == label):
            merged[-1] = (merged[-1][0], merged[-1][1] + ws)
        else:
            merged.append((label, list(ws)))
    if len(merged) > 1 and len(merged[0][1]) < 2:
        merged[1] = (merged[1][0], merged[0][1] + merged[1][1])
        merged.pop(0)
    return merged


class SpeakerBook:
    """Speakers of one recording across its segment files, labelled S1, S2, ..."""

    def __init__(self, same_voice: float) -> None:
        self.same_voice = same_voice
        self.prints: dict[str, np.ndarray] = {}
        self.seconds: dict[str, float] = {}

    def label_for(self, voice: np.ndarray) -> str:
        best, score = None, -1.0
        for label, v in self.prints.items():
            s = float(v @ voice)
            if s > score:
                best, score = label, s
        if best is not None and score >= self.same_voice:
            return best
        label = f"S{len(self.prints) + 1}"
        self.prints[label] = voice
        return label

    def add_time(self, label: str, seconds: float) -> None:
        self.seconds[label] = self.seconds.get(label, 0.0) + seconds


# Remembered voices ---------------------------------------------------------

def voices_path() -> Path:
    return data_dir() / "voices.json"


class Voices:
    """Voice prints the user named in the app. Stay on this machine."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or voices_path()
        try:
            self.data: dict = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def names(self) -> list[dict]:
        return [{"name": n, "samples": v.get("count", 1), "updated": v.get("updated")}
                for n, v in sorted(self.data.items())]

    def match(self, voice, threshold: float) -> tuple[str | None, float]:
        best, score = None, -1.0
        for name, v in self.data.items():
            s = float(_unit(v["embedding"]) @ _unit(voice))
            if s > score:
                best, score = name, s
        return (best, score) if score >= threshold else (None, score)

    def remember(self, name: str, voice) -> None:
        old = self.data.get(name)
        if old:
            n = old.get("count", 1)
            merged = _unit((_unit(old["embedding"]) * n + _unit(voice)) / (n + 1))
            count = n + 1
        else:
            merged, count = _unit(voice), 1
        self.data[name] = {"embedding": [round(float(x), 6) for x in merged], "count": count,
                           "updated": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        self._save()

    def forget(self, name: str) -> bool:
        if self.data.pop(name, None) is None:
            return False
        self._save()
        return True

    def _save(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f)
        tmp.replace(self.path)


# Names from the transcript ---------------------------------------------------

NAME_SYSTEM = """You identify the speakers of a call transcript.
Lines are labelled "Me" (the person who recorded) or S1, S2, ... (other people).
For each S label, give the person's name only if the transcript makes it clear: \
they introduce themselves, someone addresses them by name right before they answer, \
or they are greeted by name. Otherwise give "".
{attendees}evidence: the short quote that shows it, or "".
Never give the same name to two labels. Never guess from voice or topic."""

NAME_SCHEMA = {"type": "object", "properties": {"speakers": {"type": "array", "items": {
    "type": "object",
    "properties": {"label": {"type": "string"}, "name": {"type": "string"},
                   "evidence": {"type": "string"}},
    "required": ["label", "name", "evidence"], "additionalProperties": False}}},
    "required": ["speakers"], "additionalProperties": False}


def guess_names(text: str, labels: list[str], attendees: list[str], backend) -> dict[str, dict]:
    """{label: {"name", "evidence"}} for labels the transcript names, checked:
    the name must occur in the transcript (or be an invited attendee whose first
    name occurs there)."""
    if not labels:
        return {}
    who = (f"People invited to the call: {', '.join(attendees)}. Prefer these spellings.\n"
           if attendees else "")
    data = backend.complete_json(NAME_SYSTEM.format(attendees=who),
                                 f"Labels to name: {', '.join(labels)}\n\nTranscript:\n{text}",
                                 NAME_SCHEMA)
    low = text.lower()
    out: dict[str, dict] = {}
    used: set[str] = set()
    for item in data.get("speakers", []):
        label, name = str(item.get("label", "")), str(item.get("name", "")).strip()
        if label not in labels or not name or name.lower() in used:
            continue
        first = name.split()[0].lower()
        if not re.search(rf"\b{re.escape(first)}\b", low):
            continue        # not supported by the transcript
        full = next((a for a in attendees if a.lower().split()[0] == first), name)
        out[label] = {"name": full, "evidence": str(item.get("evidence", ""))[:200]}
        used.add(full.lower())
    return out
