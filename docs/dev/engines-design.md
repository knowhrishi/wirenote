# Design note: speech engines

Status: **chosen 2026-10-27**. Transcription used only faster-whisper on the CPU. This note adds two engines, picks the faster one by default where it fits, and lets you transcribe one call again with other choices.

## What changes

1. **Setting `asr_engine`**, default `"auto"`:
   - `"auto"`: Parakeet when it fits, otherwise Whisper (`choose()` in `process/engines.py`). Parakeet fits when the language is one it knows (`whisper_language`, or the desktop's locale when that is `auto`), no vocabulary is set (only Whisper uses it), and `whisper_model` is still the default (a model picked on purpose means you want Whisper). If Parakeet's model cannot be downloaded or loaded, the job uses Whisper and logs why, so a note is still written. whisper.cpp is never picked for you: Wirenote cannot tell whether a `whisper-cli` was built for a GPU.
   - `"whisper"`: faster-whisper, as before.
   - `"parakeet"`: NVIDIA Parakeet TDT 0.6B v3 (int8 ONNX) through sherpa-onnx, which Wirenote already uses for speakers. No new Python package. Several times faster than Whisper on a CPU. It knows 25 European languages and detects the language by itself, so `whisper_language` and the vocabulary do not apply.
   - `"whisper.cpp"`: an external `whisper-cli` that you build, for example with Vulkan for an AMD or Intel GPU. Wirenote runs it as a subprocess and reads its JSON (`-ojf`); it never compiles or installs anything. Settings `whispercpp_bin` (a name on `PATH` or a full path) and `whispercpp_model` (a file, or a file name in the `ggerganov/whisper.cpp` Hugging Face repo, downloaded once at a pinned revision).
2. **One interface.** Each engine is a class with `hear(audio) -> Heard` (words with times, language, seconds of speech) in `process/engines.py`. Sentences, speakers, echo removal and the note are the same whichever engine ran.
3. **Only speech goes in.** Parakeet and whisper.cpp get the speech found by the Silero VAD that faster-whisper ships, in pieces of at most 30 s with 0.2 s padding. Parakeet decodes them one at a time (see Measured). For whisper.cpp the pieces are joined into one temporary WAV, so the model loads once, and word times are mapped back to the recording.
4. **Transcribe again with other choices.** `wirenote reprocess ID --engine … --model … --language …`, or **Transcribe again…** on a call page. The choices are stored in the call's `manifest.json` under `"transcribe"` and apply to that call from then on; the settings stay as they are. An empty choice means "use the setting". A broken value in a hand-edited manifest is ignored rather than failing the job.
5. **The note says which engine ran.** `transcript.json` `"model"` and the note's footer name it (`parakeet-tdt-0.6b-v3`, `whisper.cpp ggml-large-v3-turbo-q5_0.bin`, or the Whisper model as before). Rewriting a note keeps the name from `transcript.json`.
6. **`doctor`** checks the chosen engine: Parakeet's model downloaded, `whisper-cli` found, the whisper.cpp model present.

## What stays

- Every data format. `"transcribe"` in the manifest is optional and older versions ignore it.
- No new dependency and nothing sent out beyond the model download from Hugging Face, which PRIVACY.md already lists.
- Everything still runs on the CPU unless you build a GPU `whisper-cli` yourself.

## Measured

A real 30-minute call on the developer laptop (16 threads, 14 GB RAM, no GPU): 278 s of speech on the mic, 1485 s on the system track.

| Step | Time | Peak memory |
| --- | --- | --- |
| Parakeet, mic, batches of 8 | 24.6 s | 3.0 GB |
| Parakeet, mic, one piece at a time | 14.6 s | 1.39 GB |
| Parakeet, system track, one at a time | 53 s | 1.41 GB |
| Speaker separation, system track | 180 s | 0.91 GB |

Batches of 8 ran out of memory in a real job while the browser was in a call (swap full), and padding made them slower too. The worker also drops the speech models before the summary, so they are not held while Ollama loads its own model.

## Upgrading

On an existing install that kept the defaults, the next note uses Parakeet: a 670 MB download once, then faster notes. A vocabulary, a language Parakeet does not know, or a Whisper model you chose keeps Whisper. `asr_engine = "whisper"` brings back the old behaviour.

## Not chosen

- **Building whisper.cpp into the wheel** (pywhispercpp and similar): a native build per platform and GPU, for a feature most people do not need.
- **onnxruntime GPU for Parakeet:** the ROCm and CUDA packages are large and fragile on desktop Linux; the CPU is already fast enough.

## Defaults (docs/dev/PLAN.md › Defaults)

| Parameter | Value |
| --- | --- |
| Speech engine | `auto`: Parakeet when it fits the language and no vocabulary is set, else Whisper |
| Speech pieces for Parakeet and whisper.cpp | at most 30 s, 0.2 s padding |
| Parakeet batch | 1 piece |
| whisper.cpp model | `ggml-large-v3-turbo-q5_0.bin` |
