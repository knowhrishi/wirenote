# Design note: live transcript

Status: **chosen 2026-10-27**. The transcript came only after the call. This note adds an optional preview during the call, without making the call or the note worse.

## What changes

1. **Setting `live_transcript`**, off by default: it uses CPU during the call, so you turn it on (Settings › Setup, or `config.toml`).
2. **`wirenote live REC_DIR`**, started by the daemon when a recording starts, and stopped (SIGTERM, then kill after 5 s) when it ends. Like the worker it runs at `nice 10` and dies with the daemon.
   - Every 1.5 s it reads what pw-record added to each track's WAV since last time. A closed file is read to its end, then the next segment of that track is followed with its own offset.
   - It cuts at pauses: a piece is heard once there is 0.5 s of silence after speech and at least 2 s of audio, or after 15 s without a pause. Silence is dropped. The Silero VAD from faster-whisper finds the speech.
   - It hears each piece with Parakeet where it knows the language, else Whisper `small`, on 2 threads, and appends sentences to `live.jsonl` in the recording folder: `{"speaker": "Me" | "Others", "start", "end", "text"}`, plus status lines (`listening` with the engine, or `unavailable` with a reason).
   - The far side's voice can leak into the mic. A mic line waits until the system track was heard past its end (at most 10 s) and is dropped if `merge.dedupe_echo` says it is an echo.
   - Others are not told apart live; that needs the whole call.
3. **No downloads during a call.** The live engine uses only models already downloaded. If there is none, it writes `unavailable` and exits; after the next note the worker downloads the live model (`prefetch`), so the following call has a live transcript.
4. **The call page** polls `GET /api/calls/ID/live?after=BYTES` every 2 s while recording. The server returns only whole lines after that byte, at most 256 KB, so a long call costs little per poll. Lines are shown in time order, since each track is cut at its own pauses.
5. **After the call** the worker makes the transcript from the whole recording as before and deletes `live.jsonl`.

## What stays

- Recording, the note and its transcript are unchanged; the live transcript only reads the WAVs.
- Nothing leaves the computer. The live model download is the same Hugging Face download as for notes.
- With the setting off, no process is started and nothing is written.

## Measured

On the developer laptop (16 threads, no GPU), two LibriSpeech tracks over 30 s: each line appeared 3 to 4 s after it was spoken, with the same words as the final transcript.

## Defaults (docs/dev/PLAN.md › Defaults)

| Parameter | Value |
| --- | --- |
| Live transcript | off |
| Live engine | Parakeet where it knows the language, else Whisper `small`; downloaded models only; 2 threads |
| Live pieces | read every 1.5 s; cut after 0.5 s of silence (at least 2 s) or at 15 s |
| Live echo check | a mic line waits at most 10 s for the system track |
