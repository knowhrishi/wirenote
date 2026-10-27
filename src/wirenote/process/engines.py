# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Speech engines (docs/dev/engines-design.md): each turns 16 kHz mono audio into words
with times, so sentences, speakers and the note work the same whichever one ran.

- "auto", the default: the fastest engine that fits (`choose`), Parakeet or Whisper.
- "whisper": faster-whisper on the CPU.
- "parakeet": NVIDIA Parakeet TDT 0.6B v3 through sherpa-onnx (already installed for
  speakers). Several times faster on a CPU; 25 European languages, detected by itself.
- "whisper.cpp": an external `whisper-cli`, for example one built with Vulkan to use an
  AMD or Intel GPU. Wirenote only runs it; nothing is compiled or installed here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config.settings import Settings
from .speakers import Word

SR = 16000
ENGINES = ("auto", "whisper", "parakeet", "whisper.cpp")
# Parakeet TDT 0.6B v3's languages, from its model card.
PARAKEET_LANGUAGES = frozenset("bg hr cs da nl en et fi fr de el hu it lv lt mt pl pt ro sk sl es "
                               "sv ru uk".split())
PARAKEET_REPO = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
PARAKEET_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
WHISPERCPP_REPO = "ggerganov/whisper.cpp"
# Speech is cut into pieces of at most this length for engines without their own
# windowing (Parakeet), and silence is left out so no engine hears it.
MAX_CHUNK_S = 30.0
PAD_S = 0.2


class EngineError(Exception):
    pass


@dataclass
class Heard:
    words: list[Word]
    language: str | None
    language_probability: float | None
    speech_s: float


def speech_spans(audio: np.ndarray) -> list[tuple[int, int]]:
    """Sample ranges with speech, from the Silero VAD that faster-whisper ships."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    spans = get_speech_timestamps(audio, VadOptions(max_speech_duration_s=MAX_CHUNK_S))
    pad = int(PAD_S * SR)
    return [(max(0, s["start"] - pad), min(len(audio), s["end"] + pad)) for s in spans]


def tokens_to_words(tokens: list[str], starts: list[float], ends: list[float],
                    offset_s: float = 0.0) -> list[Word]:
    """Subword tokens to words: a token starting with a space starts a word, and
    punctuation stays with the word before it."""
    words: list[Word] = []
    for tok, t0, t1 in zip(tokens, starts, ends):
        if not tok:
            continue
        if words and not tok.startswith(" "):
            w = words[-1]
            words[-1] = Word(w.start, max(w.end, offset_s + t1), w.text + tok)
        else:
            words.append(Word(offset_s + t0, offset_s + t1, tok if tok.startswith(" ") else " " + tok))
    return words


class WhisperEngine:
    name = "whisper"

    def __init__(self, settings: Settings, vocabulary: list[str], threads: int | None = None,
                 local_only: bool = False) -> None:
        from faster_whisper import WhisperModel

        from .models import whisper_path

        self.label = settings.whisper_model
        self.language = None if settings.whisper_language == "auto" else settings.whisper_language
        self.model = WhisperModel(whisper_path(settings.whisper_model, local_only=local_only),
                                  device="cpu", compute_type="int8", cpu_threads=threads or 0)
        # Each term as its own sentence: Whisper copies the prompt's style, and a bare
        # comma list made it drop sentence punctuation (docs/dev/features-2-design.md).
        self.hotwords = " ".join(f"{t.rstrip('.')}." for t in vocabulary)[:600] or None

    def hear(self, audio: np.ndarray) -> Heard:
        segments, info = self.model.transcribe(audio, language=self.language, vad_filter=True,
                                               word_timestamps=True, hotwords=self.hotwords)
        segments = [s for s in segments if s.text.strip()]
        words = [Word(w.start, w.end, w.word) for s in segments for w in (s.words or [])]
        return Heard(words, info.language, round(info.language_probability, 2),
                     round(info.duration_after_vad, 1))


class ParakeetEngine:
    name = "parakeet"
    label = "parakeet-tdt-0.6b-v3"

    def __init__(self, settings: Settings, vocabulary: list[str], threads: int | None = None,
                 local_only: bool = False) -> None:
        import sherpa_onnx

        from .models import hub_file

        f = {n: hub_file(PARAKEET_REPO, n, local_only=local_only) for n in PARAKEET_FILES}
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=f["encoder.int8.onnx"], decoder=f["decoder.int8.onnx"],
            joiner=f["joiner.int8.onnx"], tokens=f["tokens.txt"],
            num_threads=threads or max(1, (os.cpu_count() or 2) // 2),
            model_type="nemo_transducer")

    def hear(self, audio: np.ndarray) -> Heard:
        spans = speech_spans(audio)
        words: list[Word] = []
        for i in range(0, len(spans), 8):          # a few at a time: bounded memory
            batch = spans[i:i + 8]
            streams = []
            for s, e in batch:
                st = self.recognizer.create_stream()
                st.accept_waveform(SR, audio[s:e])
                streams.append(st)
            self.recognizer.decode_streams(streams)
            for (s, _e), st in zip(batch, streams):
                r = st.result
                starts = list(r.timestamps)
                durs = list(getattr(r, "durations", []) or [])
                ends = [t + d for t, d in zip(starts, durs)] if len(durs) == len(starts) \
                    else starts[1:] + [starts[-1] + 0.3 if starts else 0.0]
                words += tokens_to_words(list(r.tokens), starts, ends, s / SR)
        return Heard(words, None, None, round(sum(e - s for s, e in spans) / SR, 1))


class WhisperCppEngine:
    name = "whisper.cpp"

    def __init__(self, settings: Settings, vocabulary: list[str]) -> None:
        self.bin = shutil.which(settings.whispercpp_bin) or (
            settings.whispercpp_bin if os.access(settings.whispercpp_bin, os.X_OK) else None)
        if not self.bin:
            raise EngineError(f"{settings.whispercpp_bin} not found: install whisper.cpp "
                              "(docs/usage.md › Speech engines) or set whispercpp_bin")
        model = Path(settings.whispercpp_model).expanduser()
        if not model.is_file():
            from .models import hub_file
            model = Path(hub_file(WHISPERCPP_REPO, settings.whispercpp_model))
        self.model = str(model)
        self.label = f"whisper.cpp {model.name}"
        self.language = settings.whisper_language
        self.prompt = " ".join(f"{t.rstrip('.')}." for t in vocabulary)[:600]

    def hear(self, audio: np.ndarray) -> Heard:
        """Only the speech goes to whisper-cli, joined into one file; times are mapped back."""
        spans = speech_spans(audio)
        if not spans:
            return Heard([], None, None, 0.0)
        pieces, starts, pos = [], [], 0
        for s, e in spans:
            starts.append((pos / SR, s / SR, (e - s) / SR))
            pieces.append(audio[s:e])
            pos += e - s
        joined = np.concatenate(pieces)
        with tempfile.TemporaryDirectory(prefix="wirenote-") as tmp:
            wav_path = Path(tmp) / "speech.wav"
            with wave.open(str(wav_path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes((np.clip(joined, -1, 1) * 32767).astype("<i2").tobytes())
            argv = [self.bin, "-m", self.model, "-f", str(wav_path), "-l", self.language,
                    "-ojf", "-of", str(Path(tmp) / "out"), "-np"]
            if self.prompt:
                argv += ["--prompt", self.prompt]
            r = subprocess.run(argv, capture_output=True, text=True, check=False)
            if r.returncode != 0:
                tail = (r.stderr.strip().splitlines() or ["no output"])[-1]
                raise EngineError(f"whisper-cli failed ({r.returncode}): {tail[:200]}")
            data = json.loads((Path(tmp) / "out.json").read_text())
        words = parse_whispercpp(data)

        def back(t: float) -> float:
            for joined_start, orig_start, length in reversed(starts):
                if t >= joined_start:
                    return orig_start + min(t - joined_start, length)
            return t
        words = [Word(back(w.start), back(w.end), w.text) for w in words]
        lang = (data.get("result") or {}).get("language")
        return Heard(words, lang, None, round(len(joined) / SR, 1))


def parse_whispercpp(data: dict) -> list[Word]:
    """Words from `whisper-cli -ojf` output: per-token offsets in ms, special tokens dropped."""
    tokens, starts, ends = [], [], []
    for seg in data.get("transcription", []):
        for tok in seg.get("tokens", []):
            text = tok.get("text", "")
            off = tok.get("offsets")
            if not off or (text.startswith("[_") and text.endswith("]")) or not text.strip():
                continue
            tokens.append(text)
            starts.append(off["from"] / 1000)
            ends.append(off["to"] / 1000)
    return tokens_to_words(tokens, starts, ends)


def system_language() -> str | None:
    """The desktop's language code from the locale ("de" for de_DE.UTF-8), or None."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        code = os.environ.get(var, "").split(".")[0].split("_")[0].lower()
        if code:
            return None if code in ("c", "posix") else code
    return None


def choose(settings: Settings, vocabulary: list[str]) -> str:
    """What "auto" means: Parakeet, several times faster on a CPU, when it can do as well
    as Whisper here; otherwise Whisper. whisper.cpp is never chosen for you."""
    if settings.asr_engine != "auto":
        return settings.asr_engine
    language = (system_language() if settings.whisper_language == "auto"
                else settings.whisper_language.lower())
    if (language in PARAKEET_LANGUAGES and not vocabulary
            # A Whisper model picked on purpose means you want Whisper.
            and settings.whisper_model == Settings.whisper_model):
        return "parakeet"
    return "whisper"


def make_engine(settings: Settings, vocabulary: list[str]):
    from dataclasses import replace

    name = choose(settings, vocabulary)
    kind = {"whisper": WhisperEngine, "parakeet": ParakeetEngine,
            "whisper.cpp": WhisperCppEngine}.get(name)
    if kind is None:
        raise EngineError(f"asr_engine must be one of {', '.join(ENGINES)}, "
                          f"not {settings.asr_engine!r}")
    if settings.asr_engine != "auto" or name == "whisper":
        return kind(replace(settings, asr_engine=name), vocabulary)
    try:
        return kind(replace(settings, asr_engine=name), vocabulary)
    except Exception as e:  # noqa: BLE001 - offline, or a broken download: Whisper still works
        print(f"worker: {name} unavailable ({type(e).__name__}: {str(e)[:120]}); using Whisper",
              file=sys.stderr, flush=True)
        return WhisperEngine(replace(settings, asr_engine="whisper"), vocabulary)


# A recording's manifest may carry {"engine", "model", "language"} for its next
# transcription ("Transcribe again" with other choices); they beat the settings.
OVERRIDES = {"engine": "asr_engine", "model": "whisper_model", "language": "whisper_language"}


def check_overrides(choices: dict) -> dict:
    """The non-empty, valid choices; raises ValueError on anything else."""
    out = {}
    for key, value in (choices or {}).items():
        if key not in OVERRIDES:
            raise ValueError(f"unknown choice {key!r}")
        value = str(value or "").strip()
        if not value:
            continue
        if len(value) > 100 or not value.isprintable():
            raise ValueError(f"{key}: one short line of text")
        if key == "engine" and value not in ENGINES:
            raise ValueError(f"engine: one of {', '.join(ENGINES)}")
        out[key] = value
    return out


def with_overrides(settings: Settings, manifest: dict) -> Settings:
    from dataclasses import replace

    try:
        choices = check_overrides(manifest.get("transcribe") or {})
    except ValueError:
        return settings
    return replace(settings, **{OVERRIDES[k]: v for k, v in choices.items()})


def engine_label(settings: Settings, vocabulary: list[str] | None = None) -> str:
    """What the note and transcript.json name as the transcript model."""
    name = choose(settings, vocabulary or [])
    if name == "parakeet":
        return ParakeetEngine.label
    if name == "whisper.cpp":
        return f"whisper.cpp {Path(settings.whispercpp_model).name}"
    return settings.whisper_model
