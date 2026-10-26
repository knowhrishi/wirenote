# M2–M4 design note: capture, transcript, summary

Status: **approved 2026-10-26.** Answers: summary backends Ollama and Claude API, default Ollama; Whisper `large-v3-turbo`; calendar built but unconfigured; M2 → M3 → M4 in one go. This note covers the rest of the pipeline in one go, because you asked for a full end-to-end build. It still goes in milestone order, and each milestone keeps its exit test from docs/dev/PLAN.md › Milestones.

Every threshold cites docs/dev/PLAN.md › Defaults. Machine facts were checked on 2026-10-26:
- 16 threads, 14 GB RAM (about 4 GB free while working), AMD iGPU (so no CUDA)
- ffmpeg present; no Ollama, no API key
- an EPOS USB headset as the default sink and source

**M1 is not finished.** The trial week has not run. The detector keeps running during this build, so the M1 notes (`docs/dev/m1-week.md`) can continue in parallel. Any detector fix found there still comes first.

## What you get at the end

1. A call starts and the prompt appears (M1, already working).
2. You click **Record**. Two audio tracks are recorded: your mic ("Me") and system audio ("Others"). A persistent "Recording" notification has a Stop button.
3. The call ends. The recording stops within about 5 s and a processing job is queued.
4. A worker transcribes both tracks, merges them into one transcript labelled Me / Others, and summarizes it.
5. A note lands in `~/Meetings/YYYY/MM/`. A notification says "Meeting note ready" and has an **Open** button.
   - The note holds a title, summary, decisions, action items with owners, open questions, and the full timestamped transcript.
   - The title comes from the calendar event if one matches, otherwise from the summary.

## M2: Capture

**Layout:** one directory per recording, `~/.local/share/wirenote/recordings/<id>/`, where `<id>` is `YYYYMMDD-HHMMSS-<app>`.

```
manifest.json          app, service, room, calendar event, start/end (wall + monotonic), end reason,
                       segments: [{track, file, target, start_offset_s, end_offset_s}]
mic-000.wav            16 kHz mono s16  (PLAN › Defaults: audio format)
system-000.wav         system-001.wav … one per sink change
```

**Recorder (`capture/recorder.py`):**
- It runs two `pw-record` children with `--rate 16000 --channels 1 --format s16`.
  - Each gets a `node.name` of `wirenote-mic` or `wirenote-system`, so the detector already ignores them (`own_node_prefix`).
- **Mic track:** targets the `Audio/Source` node that the app's stream is linked to, read from the graph mirror. It is not the default source. If the app relinks to another source, a new mic segment starts.
- **System track:** targets the default sink with `stream.capture.sink=true` (its monitor).
  - The default sink is read from the PipeWire `default` Metadata object, which pw-dump already streams.
  - When it changes, the old segment closes and a new one starts on the new sink.
- **Stopping:** SIGINT to each child, wait 3 s, then SIGKILL. The manifest is written atomically on every segment change and at the end.
- **Heartbeat:** every 5 s (PLAN › Defaults: capture heartbeat) each track's current file must have grown.
  - If not, one notification "Mic track stopped receiving audio" appears and is logged; the other track keeps running.
  - If a child dies, the recorder restarts it once as a new segment.
- **Crash recovery:** on daemon start, any recording without an end in its manifest is checked.
  - WAV headers whose RIFF/data sizes are 0 or wrong are rewritten from the file length.
  - The end reason is set to `crash`, and a job is queued.
  - This is the `kill -9` exit test. I will check what pw-record actually leaves on disk and write the repair for that.
- **Cap:** the 4 h cap (already in the tracker) ends the recording. Disk use is about 115 MB per track per hour.
- **Dry run is removed.** Record really records from now on.

**Control socket:** `$XDG_RUNTIME_DIR/wirenote.sock`, used by `wirenote stop` and `wirenote record`.
- This closes the M1 gap where a dismissed "Recording" notice left no way to stop.
- `record` starts recording the app currently prompted or on the mic, for notification servers without buttons (PLAN › Prompt).

**Consent (Germany, §201 StGB: recording a non-public conversation needs everyone's consent):**
- The "Recording" notification stays up for the whole recording.
- Optional `consent_line` in config.toml: when set, it is copied to the clipboard (`wl-copy`) on Record, so you can paste it into the call chat.

## M3: Transcript

**Job queue:** SQLite at `~/.local/state/wirenote/jobs.db` (PLAN › Processing).
- Table `jobs(id, recording, state, attempts, error, created, updated)`.
- States: `queued → transcribing → summarizing → done | failed`.
- The daemon runs **one worker at a time as a child process** (`wirenote process <id>`, `nice 10`), so a heavy or crashing transcription never stalls detection.
- Queued and interrupted jobs resume when the daemon starts.
- Failures retry twice, then notify.

**Transcription (`process/transcribe.py`):** faster-whisper on CPU, `int8`.
- `vad_filter=True`, `word_timestamps=True`, and language auto-detect per track (or `language = "de"` etc. in config).
- Each track's segments are shifted by their `start_offset_s`, so stitched sink segments line up.
- **Model:** see Question 2. Models download from Hugging Face on first use, into `~/.cache/huggingface`.

**Merge:**
- Segments from both tracks are sorted by start time and labelled **Me** (mic) and **Others** (system).
- Consecutive segments from the same speaker, less than 2 s apart, join into one paragraph.
- Splitting Others into individual people is out of scope (PLAN › Processing).

**Echo dedupe:** a Me segment is dropped when it overlaps an Others segment by at least 50% of its length and the normalized texts are at least 80% similar (difflib ratio). With your headset this should rarely fire. Every drop is logged, so the thresholds can be tuned.

**Note:** `~/Meetings/YYYY/MM/YYYY-MM-DD HHMM <title>.md`.

```markdown
---
title: Weekly sync
date: 2026-10-27T10:00:00+01:00
duration_min: 32
app: chrome
service: Jitsi           # when known
calendar: Weekly sync    # when matched
attendees: [...]         # from the calendar event
audio: [~/.local/share/wirenote/recordings/<id>/...]
transcript_model: large-v3-turbo
summary_backend: ollama:qwen3:4b
---
## Summary
## Decisions
## Action items
- [ ] **Me**: send the draft (by Friday)
## Open questions
## Transcript
**Me** [00:00:04] ...
**Others** [00:00:11] ...
```

In M3 the note has the transcript only; M4 fills in the four sections above it.

**Audio retention:** audio is deleted 30 days after the note is written (PLAN › Processing). Notes are never deleted.

## M4: Summary and calendar

**Summary backend:** a small interface, `summarize(transcript, meta) -> Summary`.
- `Summary` holds `title`, `summary`, `decisions`, `action_items[{owner, task, due}]`, and `open_questions`.
- The backend is asked for JSON, the output is validated, and it retries once on bad JSON.
- **Chunking:** for transcripts over about 15 min (PLAN › Processing), each chunk is summarized, then the chunk results are combined in one final call.
- **Backends** (see Question 1):
  - `ollama`: HTTP to localhost, with a configurable model
  - `anthropic`: Claude API, with the key in `~/.config/wirenote/secrets.toml` (mode 600) and never logged
  - `none`: transcript only
- If the backend fails, the note is still written with the transcript, and the summary can be redone with `wirenote reprocess <id> --summary`.

**Calendar (`calendar/ics.py`):** a list of ICS URLs or files in config.toml, refreshed every 5 min (PLAN › Defaults: calendar refresh).
- Recurring events are expanded with `recurring-ical-events`.
- **Window:** an event starts in ≤10 min, started ≤5 min ago, or is in progress (PLAN › Defaults: calendar window).
- **Tie-break:** prefer the event whose location or description holds a link for the detected service (a Jitsi URL for Jitsi). Otherwise, if exactly one event is in the window, use it. Otherwise fall back to the generic prompt.
- **Effects:**
  - The prompt title becomes the event name.
  - Chrome and Brave get the meeting dwell (2 s) instead of the browser dwell when an event with a meeting link is in the window (PLAN › Browsers).
  - The note gets the title and attendees.
- With no calendar configured, everything works as now.

## New dependencies

- `faster-whisper` (CTranslate2 CPU wheels, about 300 MB with onnxruntime)
- `icalendar` and `recurring-ical-events`
- `anthropic`, only if that backend is chosen
- Ollama would be a separate system install, only if that backend is chosen

## Tests

- **Recorder:** spawns fake `pw-record` children. Covers segment rotation on sink change, heartbeat on a stalled file, SIGINT then SIGKILL, the manifest, and WAV header repair on a truncated file.
- **Live smoke test:** record 20 s of a tone played to the sink, plus the mic, through the real daemon, then check both files have audio.
- **Jobs:** queue, crash resume, retry limit.
- **Merge and dedupe:** fixed segment lists, with no Whisper run.
- **Transcription:** one real run on a short generated clip, marked slow and skipped when the model is missing.
- **Summary:** a fake backend, chunking, invalid JSON then retry, and a backend failure that still writes the note.
- **Calendar:** ICS fixtures covering the window, recurrence, and the tie-break.

## Findings during the build (2026-10-26)

1. **pw-record headers.** On SIGINT, pw-record writes a correct WAV header and exits 1. After a `kill -9`, the header still says RIFF size 8 and data size 0, but the samples are intact, so `capture/wav.py` rewrites the two sizes from the file length.
2. **System audio follows the default sink by itself.** A monitor capture with no `--target` was moved to a new default sink by WirePlumber (`linking.follow-default-target = true`), with the tone recorded at -21 dB. So the system track never needs a segment change on a sink switch. Segments are still used when a child is restarted and when the mic target changes.
3. **Monitor capture keeps producing silence.** With nothing playing, the file still grows, so the 5 s heartbeat does not fire during quiet stretches.
4. **Default devices** live in the `default` Metadata object (`default.audio.source`, `default.audio.sink`). Its props are top-level (`props`), not under `info.props`.
5. **faster-whisper 1.2.1 cannot decode files with the installed PyAV** (`TypeError: open() got an unexpected keyword argument 'metadata_errors'`). `capture/wav.py` decodes the WAV itself and passes samples, so PyAV is not used.
6. **Speed:** `large-v3-turbo`, int8 on CPU, ran at **2.1× real time** on 5 minutes of English speech, with 2.2 GB peak RSS, 3 s to load, and a correct transcript. A 30-minute call should take roughly 15–30 minutes after the call, depending on how much each track contains speech.
7. **The daemon dying takes the children with it cleanly.** The children run with `PR_SET_PDEATHSIG = SIGINT`. After `kill -9` of the daemon, both pw-record children closed their files with correct headers and none were left running. On restart, `recover()` closed the manifest and queued the job, and the note was written.
8. **Live end-to-end run on PipeWire:** a test app on a virtual mic, then prompt, `wirenote record`, the two tracks (speech on mic, silence on system), auto-stop 4 s after the app left, a large-v3-turbo transcript labelled Me, and a note in the notes directory.
   - **Not tested live:** the summary. Neither Ollama nor Claude credentials are on the machine, so it is covered by tests with a fake backend only.
9. **Consent line through Klipper.** `wl-copy` is not installed and needs root, but Plasma's Klipper takes `setClipboardContents` on the session bus (`org.kde.klipper.klipper`). wirenote uses that and falls back to wl-copy.
10. **Ollama without root.** The official install script needs sudo. The standalone `ollama-linux-amd64.tar.zst` (1.5 GB) is 2.4 GB unpacked, mostly CUDA libraries; only the CPU and Vulkan parts (111 MB) were unpacked into `~/.local/opt/ollama`. It runs as a user service on 127.0.0.1 (`contrib/ollama.service`).
11. **Local summary quality (qwen3:4b, CPU), on a synthetic 4-minute German team call:**
    - 22–33 s per summary; 3.9 GB while loaded, then unloaded 60 s later (`keep_alive`).
    - First prompt: it invented due dates for two tasks and wrote an English title. After the prompt fix ("every field, including the title, in {language}"; "do not reuse another task's date"), both were gone in 3 of 3 runs.
    - Every run kept the decision, the open question, and 3 of 4 action items. **It always missed one of the 4 action items** (a task the recorder took on, phrased "ich würde das bis Donnerstag umbauen").
    - Thinking mode took more than 10 minutes on the same sample, so it is off.
    - Conclusion: local summaries are useful but can miss items. The transcript in the note is complete, and `wirenote setup claude` is the higher-quality option where policy allows.

## Exit tests (docs/dev/PLAN.md › Milestones, unchanged)

- **M2:** 10 real calls with both tracks present; recording stops within 10 s of leaving; `kill -9` mid-call leaves recoverable files.
- **M3:** a 30-minute call's transcript reads without wincing; duplicate Me lines are rare without headphones.
- **M4:** calendar-backed calls get the right title; summaries list the action items you actually agreed to.

I would build M2, M3 and M4 in that order in this session, with the automated tests and a live smoke test for each. The real-call exit tests then run during your normal week.

## Questions

1. **Summary backend.** Local Ollama keeps work meetings on the machine, but this laptop can only fit a small model, so summaries are weaker and slower. The Claude API gives better summaries, but transcripts leave the machine. Check your employer's policy for work calls.
2. **Transcription model.** `large-v3-turbo` is much better for German and mixed German/English, and runs on CPU after the call; I will measure its speed in M3. `small` is faster but weak in German.
3. **Calendar.** Do you have an ICS URL to use, or should calendar matching be built but left unconfigured for now?
4. **Build in one go?** Build M2 → M3 → M4 now, with the real-call exit tests afterwards, or stop for you after each milestone?
