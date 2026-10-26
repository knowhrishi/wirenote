# M0 capture runbook

One capture per run, about 2 minutes each. Run from the repo root. `tests/fixtures/` is git-ignored until a redaction pass exists.

## Captures, in order

| # | Fixture | What |
| --- | --- | --- |
| 1 | `jitsi-chrome` | Jitsi on **meet.jit.si** in Chrome (daily Jitsi browser). Done 2026-10-25, without markers. |
| 1b | `jitsi-chrome-marked2` | Repeat of #1. Done: mute does not close the stream. |
| 2 | `jitsi-firefox` | Jitsi on **meet.jit.si** in Firefox. Done: page title appears in `media.name`. |
| 3 | `discord` | Discord (Flatpak) voice channel on your own server |
| 4 | `rocketchat-call` | Rocket.Chat (Flatpak) call; needs a second person to call you, or start one in a room. Note in `NOTES.md` whether it opened in the app or in the browser. |
| 5 | `jitsi-chrome-selfhosted` | **One confirming run** on a self-hosted Jitsi server in Chrome, throwaway room, only you in it |

## Procedure for each capture

1. Close anything else that uses the mic (other calls, dictation, OBS).
2. Open the app or browser, but **do not join a call yet**.
3. Start the probe:
   ```sh
   python3 scripts/dump_streams.py --monitor --out tests/fixtures/<fixture>
   ```
4. Wait about 5 s. This gives the baseline with no mic stream.

   **Timing matters more than markers.** Keep roughly to the times below, so each step shows up as a recognisable gap in the timeline. *Optional:* if the probe runs in a real terminal window (not `!` in Claude Code), typing the word in **bold** + Enter logs a marker. Talking is not needed; the probe does not record audio.
5. Open the meeting page and type **prejoin** once the pre-join screen shows. Then **join the call with the mic unmuted** and type **join**. For Jitsi, use a throwaway room name such as `m0-probe-<random>`.
6. Stay in the call, unmuted, for **20 s**.
7. **Mute** (**mute**) and stay muted for **20 s**. **Unmute** (**unmute**) and wait **20 s**. Watch whether a `STATE` or `REMOVE` line appears.
8. *Optional:* change the input device in the app's settings, or plug in a headset (type **device**). This shows whether the app closes and reopens the stream, which the 4 s grace has to cover.
9. **Leave the call**, type **leave**, and keep the app or tab open. Wait **15 s**.
10. Press **Ctrl-C**. The probe writes `streams.json`, `monitor.jsonl` and `mirror-*.json` into the fixture folder.

Write down anything odd (a second stream, the stream never closing, a call opening in the browser) in `tests/fixtures/<fixture>/NOTES.md`, one line per item.

## Optional extra fixtures

Short non-call mic uses in Chrome, for tuning the browser dwell in M1:

- `chrome-voice-search`: google.com › microphone icon › say a few words
- `chrome-mic-test`: any site that asks for the mic for a few seconds

## What gets checked afterwards

For each capture: `identity.pid_check` (does the PID match the stream owner?), `media.name` on the capture stream, and the resolved identity. See [m0-findings.md](m0-findings.md).
