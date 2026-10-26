# Testing

Every test runs without PipeWire, a desktop, a GPU or the network, except the `models` tier, which downloads models on purpose. Keep it that way: fake what lies outside Wirenote, never Wirenote itself.

## The tiers

| Tier | Where | What it proves | Runs |
|---|---|---|---|
| Unit | `tests/test_*.py` | one module's rules: the tracker, merge, settings, the WAV repair | every push |
| Replay | `tests/test_replay.py`, `tests/replay/*.json` | the detector on PipeWire graphs recorded during real calls | every push |
| Glue | `tests/test_daemon.py`, `tests/test_ui.py` | the daemon and the web app in-process, with fake children and a fake notification server | every push |
| **End to end** | `tests/e2e/test_call_to_note.py` | a whole call: the real `wirenote run`, `record`, worker and `ui` as processes | every push |
| **Real models** | `tests/e2e/test_real_models.py` | the same, with real Whisper `tiny.en` and speaker separation | nightly, before a release |
| **Wheel** | `scripts/test_wheel.sh` | all of the above against the built wheel, not the checkout | every push (3.13); release (3.11 to 3.14) |
| **Stored data** | `tests/test_compat.py`, `tests/compat/<version>/` | every released version's config, app rules and recordings still read | every push |

## Running them

On Linux:

```sh
uv run pytest -q                      # everything CI runs (about 30 s)
uv run pytest -q -m e2e               # only end to end
uv run pytest -q -m models            # real models: ~100 MB download, needs espeak-ng
uv build && scripts/test_wheel.sh dist
```

On macOS, most tests need Linux. Run them in a container (Docker or a compatible runtime):

```sh
scripts/test_in_linux.sh                      # what CI runs
scripts/test_in_linux.sh tests/e2e -m e2e     # end to end, real models included
```

## How the end-to-end test works

`tests/e2e/harness.py` builds a private world for each run:
- HOME and every XDG folder live in a temporary directory.
- The D-Bus addresses point nowhere, so a run on your desktop cannot show notifications, use the clipboard or talk to your real daemon.
- `pw-dump` and `pw-record` on PATH are fakes (`tests/e2e/fakes/`):
  - `pw-dump` replays `tests/replay/jitsi-firefox.json` up to the moment the call is running, then waits for the test to "hang up".
  - `pw-record` writes WAV files in real time, exactly as pw-record does: header sizes 0 until SIGINT.
- Ollama is a small HTTP server in the test process that answers `/api/chat` and keeps every request.
- Whisper is swapped for a fake through `tests/e2e/fake_models/sitecustomize.py`, which Python loads in every process the test starts. The production code has no test hooks.

The flow runs once per module. Each test then checks one promise about the result: the note, the manifest, what went to the summary backend, that the log holds no private words, file modes, a clean stop, the web app. A failure names the promise that broke. A separate test kills the daemon with SIGKILL mid-call and checks that the note still appears.

### Rules for end-to-end tests

- **Wait for events, never sleep.** Use `world.wait_for(check, timeout, what)` on something the program says, usually an event in `detector.jsonl`.
  - Waiting on something earlier in the chain races. Example: the job turns "done" in the database before the daemon logs `job_end`, so stopping the daemon at "done" loses the event.
- **On a timeout, the harness prints the event list, the last lines of every process log and `wirenote jobs`.** Keep that output useful: it is all you get from CI.
- **Fake at the edge of the program:** a binary on PATH, a network service, a model. If a test needs to reach inside Wirenote, it belongs in a lower tier.
- **Keep the whole tier under 30 s.** The tones last seconds, not minutes; thresholds come from the test's own `config.toml`.
- **A new call app or desktop gets a replay trace first** (`scripts/make_replay.py` from a `wirenote dump-streams` capture), then a replay test. It only needs an end-to-end case if it takes a path the existing one does not.

## Flaky tests

A test that fails one time in fifty is a broken test, not bad luck. Before merging a new end-to-end test, run it 20 times in a row:

```sh
for i in $(seq 20); do uv run pytest -q -m e2e || break; done
```

If it fails, find the race; do not raise the timeout. Every timeout in the harness is at least 4 times what the step takes on a CI runner.
