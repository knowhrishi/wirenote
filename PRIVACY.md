# Privacy

Wirenote records conversations, so it is built to keep them on your computer. This page lists everything it stores and every connection it makes.

## What stays on your computer

| What | Where |
| --- | --- |
| Audio (one file per track), deleted 30 days after the note by default | `~/.local/share/wirenote/recordings/<id>/` |
| Transcripts, summaries, your notes typed during the call, questions you asked | same folder |
| Markdown notes | `~/Meetings/YYYY/MM/` (kept until you delete the call) |
| Voice prints of people you chose to recognize | `~/.local/share/wirenote/voices.json` |
| Settings, vocabulary, app rules | `~/.config/wirenote/` |
| Claude API key and calendar links | `~/.config/wirenote/secrets.toml` |
| Event log, processing queue | `~/.local/state/wirenote/` |

Everything Wirenote writes is readable only by your user (files mode 600, folders 700); `wirenote doctor --fix` repairs files that lost that. The event log does not contain room names, note paths or speaker names.

The meetings app is a web page served on 127.0.0.1, so other computers cannot reach it. On your own computer, it only answers your user: other accounts and other web pages in your browser are refused.

## Connections Wirenote makes

- **Hugging Face, once per model.** On the first note, the speech model (Whisper large-v3-turbo) and the two speaker models are downloaded from huggingface.co at fixed revisions. After that they are read from `~/.cache/huggingface` and Hugging Face is not contacted again.
- **Ollama, on your computer.** Local summaries go to Ollama on 127.0.0.1. Wirenote checks that the Ollama there runs as your own user before sending anything.
- **Your calendars, if you add any.** The ICS links you add are fetched over https every 5 minutes.
- **Anthropic, only if you choose Claude.** See below.

There is no telemetry, no crash reporting and no update check.

## If you choose Claude

Claude is off unless you choose it, in Settings or with `wirenote setup claude`. Before switching, the app shows this list and asks you to confirm:

With Claude on, these go to Anthropic: the transcript of each call (in parts), the calendar title and invitees, your notes, your vocabulary, questions you ask with the transcript, and for Ask across meetings up to 120,000 characters from all your calls.

Anthropic's [commercial terms and privacy policy](https://www.anthropic.com/legal) apply to that data. Each call in the app is marked "Local" or "Sent to Claude", so you can see where its summary was made. Switching back to Ollama affects the next notes; it does not recall what was already sent.

## The people you record

Their voices, words and names end up in your files. Tell them you are recording and get their consent; in many countries (for example Germany, §201 StGB) recording without it is a crime. Delete a call in the app to remove its audio, transcript and note; forget a voice in Settings to remove its voice print.
