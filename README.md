# Wirenote

Private meeting notes for Linux: detects calls, records, transcribes and summarizes locally.

When an app starts using your microphone for a call, Wirenote asks whether to record. After the call it writes a Markdown note with a transcript, who said what, a summary, decisions and action items. By default nothing leaves your computer: transcription runs with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), speakers are told apart with [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), and summaries come from a local model through [Ollama](https://ollama.com).

![Wirenote's meetings app](https://raw.githubusercontent.com/knowhrishi/wirenote/main/docs/screenshot.png)

- **Notices calls by itself.** It watches PipeWire for apps that open the mic: Jitsi or any web call in Chrome, Brave or Firefox, Discord, Rocket.Chat, and whatever you add to `apps.toml`. No bot joins your call.
- **Two clean tracks.** Your mic and the system audio are recorded separately, so "Me" and the others are never mixed up.
- **Notes you own.** Plain Markdown files in `~/Meetings`, plus a local web app to search calls, play back moments, name speakers, tick off action items and ask questions about past meetings.
- **Private by default.** Files are readable only by you, the web app only by your user, and the cloud is opt-in. See [PRIVACY.md](https://github.com/knowhrishi/wirenote/blob/main/PRIVACY.md).

## Requirements

- Linux on **x86_64 or ARM64** with **PipeWire** (most current distributions), and a desktop with notifications. Tested on **KDE Plasma 6 (Wayland)**; GNOME and others should work but are not tested yet. macOS and Windows are not supported.
- **Python 3.11 to 3.14**, and [uv](https://docs.astral.sh/uv/). uv downloads a matching Python if yours is newer or older.
- **ffmpeg** (for playback in the web app).
- About 2 GB of disk for the speech models, downloaded once on the first note.
- Optional: [Ollama](https://ollama.com) for local summaries, or an Anthropic API key for summaries by Claude.

## Install

```sh
uv tool install wirenote
wirenote setup install     # systemd user services and an app-menu entry; no root needed
wirenote doctor            # checks PipeWire, notifications, models, summaries
```

To install from source instead: `uv tool install git+https://github.com/knowhrishi/wirenote`.

For local summaries, install Ollama and pull the default model:

```sh
ollama pull qwen3:4b
```

## Your first note

1. Join a call. After a few seconds a notification asks **Record** or **Ignore**.
2. Click **Record** and tell the others. If you set a consent line in Settings, it is now on your clipboard, ready to paste into the call chat.
3. Leave the call. A few minutes later "Meeting note ready" appears.
4. Click **Open note**, or open **Wirenote** from the app menu.

Everything else, from calendars to commands to files, is in [docs/usage.md](https://github.com/knowhrishi/wirenote/blob/main/docs/usage.md).

## Recording law

**You are responsible for getting consent; in many countries (for example Germany, §201 StGB) recording without it is a crime.** Wirenote never records without your click, shows a notification for as long as it records, and can copy a consent line for the call chat. Set it in the app's Settings.

## Privacy in brief

- Audio, transcripts, notes, voice prints and settings stay on your computer, readable only by your user.
- The meetings app listens on 127.0.0.1 and only lets your own user in.
- Speech models are downloaded once from Hugging Face. There is no telemetry.
- **Claude is opt-in.** If you choose it, transcripts and notes are sent to Anthropic; the app shows exactly what before you switch, and marks each call "Local" or "Sent to Claude".

Details: [PRIVACY.md](https://github.com/knowhrishi/wirenote/blob/main/PRIVACY.md). Security reports: [SECURITY.md](https://github.com/knowhrishi/wirenote/blob/main/SECURITY.md).

## Uninstall

```sh
wirenote setup uninstall           # services and app-menu entry; your data stays
wirenote setup uninstall --purge   # also recordings, transcripts and settings (asks first); notes stay
uv tool uninstall wirenote
```

## Contributing

Bug reports and pull requests are welcome; see [CONTRIBUTING.md](https://github.com/knowhrishi/wirenote/blob/main/CONTRIBUTING.md). Pull requests need a one-time [Contributor License Agreement](https://github.com/knowhrishi/wirenote/blob/main/CLA.md), so that the project can later offer a supported edition for companies; your contributions stay available as open source. Please follow the [code of conduct](https://github.com/knowhrishi/wirenote/blob/main/CODE_OF_CONDUCT.md).

## License

Wirenote is free software under the [GNU Affero General Public License v3.0 or later](https://github.com/knowhrishi/wirenote/blob/main/LICENSE). Third-party components and models are listed in [THIRD_PARTY.md](https://github.com/knowhrishi/wirenote/blob/main/THIRD_PARTY.md).
