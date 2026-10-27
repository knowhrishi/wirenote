# macOS design note: platform split, Swift helper, permissions, packaging

Status: **draft, not approved.** The open decisions are under Questions at the end. Nothing here is built yet.

Machine facts were checked on 2026-10-26:
- macOS 27.0.1 on the build machine. Xcode is installed with the macOS 27 SDK, but its license is not accepted yet, so nothing was compiled.
- API availability was read from the SDK headers (`CoreAudio.framework/Headers`), not from memory:
  - `AudioHardwareCreateProcessTap`: `API_AVAILABLE(macos(14.2))`
  - process objects (`kAudioHardwarePropertyProcessObjectList`, `kAudioProcessPropertyPID`, `…BundleID`, `…Devices`, `…IsRunningInput`): present in the same header. Their availability is not annotated on the enum; Apple's documentation says Sonoma.
  - `CATapDescription.bundleIDs` and `processRestoreEnabled`: `API_AVAILABLE(macos(26.0))`

Points still marked **unverified** need checking on real hardware during Mac-0 (see Milestones).

## What you get at the end

The same flow as on Linux (docs/dev/m2-m4-design.md › What you get at the end), with Mac pieces underneath:

1. You install Wirenote with `brew install --cask` or from a signed download, and open it once. It asks for Microphone, System Audio Recording and Notifications, and adds itself to Login Items.
2. A call starts, and a notification asks **Record** or **Ignore**. The menu-bar icon shows the same choice, in case the banner has gone.
3. While recording, the menu-bar icon shows a recording state with a **Stop** item, and macOS shows its own orange mic indicator.
4. After the call, the same worker writes the same note to `~/Meetings`, and the same web app opens it.

**Minimum: macOS 14.2 on Apple Silicon** (see Questions 3 and 7).

## The platform split

About two thirds of the code has nothing to do with Linux and stays as it is. The rest moves behind a small `wirenote/platform/` package that is chosen once at startup from `sys.platform`. `cli.py:197` already refuses non-Linux; that check becomes "Linux or macOS ≥ 14.2".

**Unchanged on both:**
- `process/` (transcribe, speakers, merge, summarize, note, jobs, worker)
- `ui/data.py`, `ui/static/`, most of `ui/server.py`
- `calendar/`, `config/` (except paths, below)
- `detector/tracker.py`, and `detector/core.py` apart from where it gets its streams
- `capture/wav.py`, and the segment, manifest and heartbeat logic in `capture/recorder.py`

**Per platform:**

| Concern | Linux today | macOS | Interface |
|---|---|---|---|
| Who is on the mic | `pw-dump --monitor` → `Graph` → `mic_streams()` | `wirenote-audio monitor` → snapshot → `mic_streams()` | `StreamSource` |
| Which app it is | `/proc`, cgroups, Flatpak, Snap | bundle ID from Core Audio, parent walk with libproc | `ProcReader`, plus a `bundle_id` match field |
| Recording | `pw-record` children | `wirenote-audio record` children | the recorder's `Spawner` and `_argv` |
| Child dies with parent | `prctl(PR_SET_PDEATHSIG)` | the helper exits when stdin closes; Python children watch the parent with kqueue | `die_with_parent()` |
| Orphan check after a crash | `/proc/<pid>/cmdline` | `psutil.Process(pid).cmdline()` | `cmdline(pid)` |
| Notifications | D-Bus `org.freedesktop.Notifications` | the Wirenote app over a pipe (below) | the existing `Notifier` API |
| Sleep and wake | logind `PrepareForSleep` | `NSWorkspace` sleep and wake, relayed by the app | `watch_sleep()` |
| Clipboard | Klipper, wl-copy, xclip, xsel | `pbcopy` (text on stdin, as now) | `clipboard.TOOLS` |
| Open a URL | `xdg-open` | `open` | `open_url()` |
| Services | systemd user units, `.desktop` | the app is a login item and supervises `wirenote run` and `wirenote ui` | `plan_install()` / `uninstall()` |
| Socket owner | `/proc/net/tcp` | own processes' sockets through psutil | `tcp_owner()`, `listener_uid()` |
| Runtime folder | `$XDG_RUNTIME_DIR` | the per-user temp folder (`$TMPDIR`, 0700) | `runtime_dir()` |
| Doctor | PipeWire, pw-record, desktop | permissions, app present, login item | `checks()` |

### Detector

`Detector` owns a `Graph` and calls `mic_streams(self.graph, …)`. That changes to a `StreamSource` with three methods: `apply(batch)`, `streams() -> list[MicStream]` and `default_source()`.
- Linux: `GraphSource` wraps today's `Graph` and `mic_streams()`, with no change in behaviour. The replay tests prove it.
- macOS: `SnapshotSource` keeps the last snapshot from the helper. A snapshot replaces the previous one, much as pw-dump's first batch replaces the whole graph.

`MicStream` keeps its fields. On macOS they are filled like this:
- `serial`: the Core Audio process object ID plus the PID. Object IDs can be reused, so the pair stands in for `object.serial`.
- `client_serial`: the PID.
- `media_name`: always `None`, because Core Audio has no stream title (see Browsers).
- `source_names`: the UIDs of the input devices the process uses (`kAudioProcessPropertyDevices`, input scope). The recorder already treats these as the mic target.
- `node_props` and `client_props`: `application.process.id`, `application.process.binary` (the executable name) and `macos.bundle_id`, so `gather_evidence()` needs only a small branch.

**Own processes:** the recorder's own capture shows up as a process running input. `own_pids` therefore holds the daemon, the Wirenote app and every `wirenote-audio` child, which is the same rule as today with PIDs instead of node names.

### Identity and apps.toml

There is a new match field, `bundle_id`, with exact values only, as in docs/dev/PLAN.md › Matching rules.

The lookup order on macOS, where the first exact match wins:
1. the bundle ID of the process on the mic
2. the bundle ID of each parent, up to `PARENT_DEPTH`, through libproc's `proc_pidinfo` (Chrome and Brave start their audio helpers themselves, so the helper's parent is the browser)
3. the executable name (`exe`)

Default rules for macOS. All are **unverified** until a detector log confirms them, using the same comment convention as the Linux rules:

```toml
[[app]]
key = "chrome"
bundle_id = ["com.google.Chrome"]      # unverified: helper process, matched by parent
[[app]]
key = "brave"
bundle_id = ["com.brave.Browser"]      # unverified
[[app]]
key = "firefox"
bundle_id = ["org.mozilla.firefox"]    # unverified
[[app]]
key = "safari"
bundle_id = ["com.apple.Safari"]       # unverified: see below
[[app]]
key = "zoom"
bundle_id = ["us.zoom.xos"]            # unverified
[[app]]
key = "teams"
bundle_id = ["com.microsoft.teams2"]   # unverified
[[app]]
key = "discord"
bundle_id = ["com.hnc.Discord"]        # unverified
```

**Safari is the hard case.** WebKit's audio work runs in XPC services that launchd starts, so their parent is PID 1 and the parent walk finds nothing. Mac-0 has to show which process object Core Audio reports during a Safari call, and whether its bundle ID is enough. If not, Safari stays out of the defaults. The private "responsible PID" call would solve it, but private API is not used.

### Browsers

On Linux, Firefox puts the page title in `media.name`, which the `[[app.title]]` rules match (for example a Jitsi room). Core Audio has no such field.

On macOS, a web call in any browser therefore stays kind `browser`:
- It gets the browser dwell.
- It becomes a meeting only through the calendar hint (docs/dev/PLAN.md › Browsers), as Chrome already does on Linux.

Reading window titles would need the Accessibility permission. That is out of scope.

## The Swift helper: `wirenote-audio`

The helper is one command-line binary inside the app bundle, with three modes. It does what `pw-dump` and `pw-record` do today, so the Python side keeps its shape: one child that streams JSON, and one child per track that writes a WAV file.

Rules for every mode:
- stdout carries the protocol, one JSON object per line, UTF-8. stderr carries the logs.
- Every message has `"v": 1`. A newer daemon refuses a helper whose `v` it does not know, and the doctor reports it.
- The helper exits when its stdin reaches EOF, or when its parent exits (kqueue `NOTE_EXIT` on `getppid()`). This replaces `PR_SET_PDEATHSIG`.

### `wirenote-audio monitor`

```json
{"v":1,"type":"hello","helper":"0.2.0","macos":"14.5"}
{"v":1,"type":"snapshot","seq":1,
 "default_input":{"uid":"BuiltInMicrophoneDevice","name":"MacBook Pro Microphone"},
 "processes":[{"object":91,"pid":4242,"ppid":4100,"bundle_id":"com.google.Chrome.helper",
               "exe":"Google Chrome Helper","input":true,"output":true,
               "input_devices":["BuiltInMicrophoneDevice"]}]}
{"v":1,"type":"error","code":"coreaudio","message":"…"}
```

- **What a snapshot holds:** every process with audio I/O running (`kAudioProcessPropertyIsRunning`), not only the mic users. The system-audio tap needs the meeting app's object IDs, and the list stays small.
- **When a snapshot is sent:** the helper listens for changes to the process list, to each listed process's `IsRunningInput`, and to the default input device. It coalesces them to at most one snapshot per 250 ms, and sends one at least every 5 s, so the daemon can tell a stuck helper from a quiet machine.
- **Why full snapshots:** they are small, they cannot drift the way diffs can, and a restarted helper simply starts over. The daemon treats `seq` going back to 1 as a restart, like `reset_graph()`.
- **The PID** comes from `kAudioProcessPropertyPID`. The parent PID and executable name come from libproc, so Python does not walk processes itself.

### `wirenote-audio record`

```
wirenote-audio record --track mic    --out …/mic-000.wav    [--device UID]
wirenote-audio record --track system --out …/system-000.wav [--processes 91,93] [--exclude-pid 777]
```

**Output** matches `pw-record --rate 16000 --channels 1 --format s16`:
- a 44-byte PCM header, then samples
- the header sizes are written on a clean stop, so `wav.repair()`, the heartbeat (the file must grow), the segments and `recover()` all work unchanged
- **SIGINT** finishes the file, fixes the header and exits 0. **SIGKILL** leaves a file that `wav.repair()` already knows how to fix.

**Mic track:** a Core Audio input on the device UID (AUHAL), or the default input with no `--device`. The audio is converted to 16 kHz mono s16 with `AVAudioConverter`.

**System track:** a process tap (`CATapDescription`, `privateTap = true`, unmuted), attached to a private aggregate device, read with an IOProc, then converted the same way.
- With `--processes`, it records only those process objects (`monoMixdownOfProcesses`).
- Without it, it records all system audio except `--exclude-pid` (`monoGlobalTapButExcludeProcesses`), which is the same as the Linux sink monitor.
- Which one is the default is Question 2.

**When the default output device changes,** the aggregate device follows it. If it cannot, the helper exits with code 75 and the recorder starts a new segment, as it does for a dead pw-record.

**Fatal errors** produce one JSON line on stdout before exit, so the daemon can show something useful instead of "exited with 1":
```json
{"v":1,"type":"error","code":"permission_denied","track":"system"}
```

### `wirenote-audio permissions`

```json
{"v":1,"type":"permissions","microphone":"authorized","system_audio":"unknown","notifications":"authorized"}
```

`wirenote doctor` uses this. There is no public API to read the System Audio Recording state, so the answer is `unknown` until a test recording has run (see Permissions).

## The Wirenote app

`Wirenote.app/Contents/MacOS/Wirenote` is a small AppKit/SwiftUI menu-bar app (`LSUIElement`, so there is no Dock icon). It is not a second interface: the web app stays the interface. The app does four things:

1. **Holds the permissions.** macOS grants permissions to the *responsible* process, which for anything this app starts is the app itself. The daemon runs as the app's child and the helpers run as the daemon's children, so the prompts name "Wirenote" and the grants survive restarts. The claim that this responsibility passes down through the Python child is **unverified**; it is Mac-0's first test.
2. **Supervises** `wirenote run` and `wirenote ui`, with `UMask 077`. It restarts either one 5 s after a failure, as the systemd units do now.
3. **Shows notifications** for the daemon, and passes on sleep and wake.
4. **Starts at login**, through `SMAppService.mainApp`. There is no LaunchAgent plist to write or remove.

**Channel to the daemon:** the app starts `wirenote run` with fd 3 as one end of a socketpair. Each message is one JSON line.

| Direction | Message | Meaning |
|---|---|---|
| daemon → app | `{"type":"notify","id":7,"summary":"…","body":"…","actions":[["record","Record"],["ignore","Ignore"]],"timeout_ms":0,"urgency":1}` | show or replace notification 7 |
| daemon → app | `{"type":"close","id":7}` | withdraw it |
| daemon → app | `{"type":"state","recording":true,"prompt":{"key":"zoom","name":"Zoom"}}` | update the menu-bar icon and its items |
| app → daemon | `{"type":"hello","caps":["actions"],"app":"0.2.0"}` | the first line; becomes `Notifier.caps` |
| app → daemon | `{"type":"action","id":7,"key":"record"}` | → `Notifier.on_action` |
| app → daemon | `{"type":"closed","id":7,"reason":2}` | → `Notifier.on_closed`, with the same reason codes as the Desktop Notifications spec |
| app → daemon | `{"type":"sleep","going":true}` | → `watch_sleep` callback |
| app → daemon | `{"type":"menu","key":"stop"}` | a menu-bar click; handled like the control socket's `stop` or `record` |

On the Python side, `notify/macos.py` gives `MacNotifier` the same methods and callbacks as today's `Notifier`. `daemon.py` does not need to know which one it has. Notification IDs are assigned by the daemon, because there is no server to assign them.

**Banners disappear.** macOS shows a new app's notifications as banners, which go away after a few seconds. Only the user can switch them to Alerts, in System Settings › Notifications › Wirenote. Without that, the Record prompt can vanish while the call goes on. So:
- The menu-bar icon carries the prompt as long as the tracker does. **Record** and **Ignore** are always one click away.
- The first-run screen and `wirenote doctor` suggest Alerts, without requiring it.
- The consent rule "a notice for as long as it records" (README › Recording law) is met by the menu-bar recording state together with the system mic indicator. The README says so for macOS.

## Permissions

| Permission | Asked when | Info.plist key and entitlement | How we check it |
|---|---|---|---|
| Microphone | first run, `AVCaptureDevice.requestAccess(for: .audio)` | `NSMicrophoneUsageDescription`; hardened-runtime entitlement `com.apple.security.device.audio-input` | `authorizationStatus`, through `wirenote-audio permissions` |
| System Audio Recording | the first time a tap starts | `NSAudioCaptureUsageDescription` | no public API; a 2 s test recording from the first-run screen |
| Notifications | first run, `UNUserNotificationCenter.requestAuthorization` | none | `getNotificationSettings`, which also tells banner from alert |
| Login item | first run, `SMAppService.mainApp.register()` | none | `SMAppService.mainApp.status` |

**Not needed:** Screen Recording, Accessibility, Full Disk Access, Input Monitoring. Reading the Core Audio process list should need no permission at all; that is **unverified**.

**No App Sandbox.** The app runs a bundled Python, talks to Ollama on loopback and writes `~/Meetings`. It ships outside the Mac App Store, so the sandbox brings no gain here, only friction.

**If System Audio Recording is denied,** the tap may deliver silence rather than an error (**unverified**). The heartbeat only checks that the file grows, so a silent "Others" track would pass it. Two parts to the answer:
- The first-run test recording plays a short tone through the default output and checks that the tap hears it.
- The note writer flags a system track whose peak level never rises above zero. A meeting where nobody else spoke still has some room noise, and digital silence does not.

## Paths and peer checks

**Paths:** keep the XDG layout on macOS (`~/.config/wirenote`, `~/.local/share/wirenote`, `~/.local/state/wirenote`, and `~/.cache/huggingface` for the models). `config/settings.py` already falls back to these when the variables are unset, so docs, paths and support answers stay the same on both systems (Question 4).

**Runtime folder:** `runtime_dir()` becomes `$TMPDIR` on macOS. That is the per-user `/var/folders/…/T/` folder, mode 0700, and launchd sets it for the app's children. Unix socket paths are limited to 104 bytes on macOS, and `$TMPDIR/wirenote/wirenote.sock` is about 70, so the control socket fits. The daemon checks the length and refuses clearly if a custom `TMPDIR` makes it too long.

**Socket owner (`net/peer.py`):** macOS has no `/proc/net/tcp`. psutil's machine-wide connection list needs root on macOS, but a process may list the sockets of its own user's processes, and that is exactly the question being asked.
- `tcp_owner()`: walk this user's processes (`psutil.process_iter`, filtered by uid) and look for the client socket. Found means it is ours. Not found means it is refused, which keeps the current rule that unknown counts as someone else.
- `listener_uid()`: the same walk, looking for the listener. Ollama.app and `brew services` both run Ollama as the user, so the usual setup is found. A listener we cannot see is treated as another user's, which is stricter than Linux, where system accounts are trusted.
- **Cost:** walking processes takes tens to hundreds of milliseconds (**unverified**: measure it). So the verdict is cached per TCP connection, not checked per request, and browsers keep their connections alive.
- `psutil` becomes a macOS-only dependency.

**Dependencies in `pyproject.toml`:**
```toml
"dbus-fast>=2.0; sys_platform == 'linux'",
"psutil>=7; sys_platform == 'darwin'",
```
The classifiers gain `Operating System :: MacOS`.

## Processing on a Mac

faster-whisper (CTranslate2) and sherpa-onnx both ship macOS arm64 wheels. The worker stays on CPU with `int8`, as `transcribe.py` already sets it. Ollama has a native Mac app.

Metal backends (whisper.cpp, MLX) are out of scope for this note. Speed is measured in Mac-3 first, and a separate note follows only if it is too slow.

`_worker_setup()` keeps `os.nice(10)`. Its `prctl` is replaced by a thread that waits on kqueue `NOTE_EXIT` for the daemon's PID and exits. `select.kqueue` has this on macOS.

## Packaging

### The bundle

```
Wirenote.app/Contents/
  Info.plist              bundle ID (Question 1), LSMinimumSystemVersion 14.2, LSUIElement,
                          NSMicrophoneUsageDescription, NSAudioCaptureUsageDescription
  MacOS/Wirenote          the menu-bar app (Swift)
  MacOS/wirenote-audio    the helper (Swift)
  Resources/python/       python-build-standalone 3.13, arm64
  Resources/site-packages/  wirenote and its dependencies, from uv.lock
  Resources/bin/wirenote  shell wrapper: exec the bundled Python with -m wirenote.cli
```

- The site-packages are built with `uv export --frozen` and `uv pip install --target` against the bundled Python, so the app runs exactly the locked versions.
- Speech models are not bundled. They download on the first note, as on Linux.
- The bundle size is TBD; measure it in Mac-5. onnxruntime and CTranslate2 are most of it.

### Signing and notarization

- Every Mach-O file is signed from the inside out with a Developer ID Application certificate, `--options runtime` and `--timestamp`. That means every `.so` and `.dylib` from the wheels, the Python binary, the helper, then the app.
- Wheels arrive signed by their own authors or ad hoc. Re-signing them all with one identity keeps library validation happy without `disable-library-validation`.
- Entitlements start with `com.apple.security.device.audio-input` only. `allow-jit` or `allow-unsigned-executable-memory` get added only if a library fails without them under the hardened runtime (**unverified** for CTranslate2 and onnxruntime; Mac-5 tests it).
- Then `xcrun notarytool submit --wait`, `xcrun stapler staple`, and a `.zip` and a `.dmg`.

**Development builds:** macOS ties permission grants to the code signature. Ad-hoc signatures change on every build, so every rebuild asks for permissions again. Local builds should be signed with a stable Apple Development certificate. `scripts/macos/build_app.sh` takes the identity from an environment variable.

### Homebrew

- The cask lives in our own tap first, `knowhrishi/homebrew-wirenote`: `brew install --cask knowhrishi/wirenote/wirenote`. The official `homebrew/cask` has its own acceptance rules, so that comes later.
- The cask declares `depends_on macos: ">= :sonoma"` and `depends_on formula: "ffmpeg"` (for playback in the web app).
- A `binary` stanza links `Resources/bin/wirenote`, so `wirenote doctor`, `wirenote open` and the rest work from a terminal.
- The `zap` stanza removes `~/.config/wirenote`, `~/.local/share/wirenote` and `~/.local/state/wirenote`, and **never `~/Meetings`**. This matches `setup uninstall --purge`: notes stay.
- Ollama stays the user's choice (`brew install ollama`), as on Linux.

**`uv tool install wirenote` on a Mac** still installs. `wirenote ui`, `reprocess` and the processing commands work. `wirenote run` refuses with a message saying to install the app, and `wirenote doctor` says the same.

### Release workflow

There is a new `macos` job on `macos-14` (arm64) in `release.yml`. It builds, signs, notarizes and attaches the zip and dmg to the GitHub release, next to the wheel, with the same checksums and provenance step.
- Signing material lives in GitHub secrets: the certificate as a p12, and a notary API key.
- A cask bump PR goes to the tap.
- THIRD_PARTY.md gains python-build-standalone and its bundled libraries.

**Updates:** `brew upgrade --cask`, or a new download. There is no update check in the app: it would be a network call, and PRIVACY.md promises none beyond the model download (Question 5).

## Tests

- **Python, on both systems:**
  - `SnapshotSource` and the macOS identity path, fed from recorded helper output in `tests/replay/mac-*.json`. These are captured in Mac-0 from real calls, the same way the Linux replays were.
  - The recorder with a fake `wirenote-audio` (a Python script that writes a WAV and obeys SIGINT), as `test_capture.py` does for pw-record.
  - `MacNotifier` against a fake app on a socketpair.
  - `tcp_owner()` on macOS with a real loopback connection.
- **Linux-only and macOS-only tests** are marked `skipif`. CI gets a `macos-14` job next to the Ubuntu one.
- **Swift:** `swift test` covers the WAV writer (header, sizes after SIGINT), the protocol encoder, and snapshot coalescing. The audio paths are checked by hand with `docs/dev/macos-runbook.md`, written in Mac-0 in the style of the M0 runbook.

## Milestones

Each one ends with an exit test, as in docs/dev/PLAN.md › Milestones.

- **Mac-0: findings.** A bare `wirenote-audio monitor`, plus a 30 s tap recording, run during real calls in Chrome, Safari, Zoom and Teams.
  - Record which process objects appear, and what a denied tap gives.
  - Test whether permission responsibility passes from an app through a Python child to the helper.
  - *Exit:* `docs/dev/macos-findings.md` answers every **unverified** point in this note.
- **Mac-1: split, on Linux.** The `platform/` package, `StreamSource`, and the `Notifier` and peer interfaces. *Exit:* every existing test and replay passes unchanged on Linux.
- **Mac-2: detection.** `SnapshotSource`, `bundle_id` rules, and logging to `detector.jsonl`. *Exit:* a week of real calls gives prompts with no false ones, from the replays and the log.
- **Mac-3: recording and processing.** The `record` mode, the recorder backend, kqueue parent watch, orphan check. *Exit:* a 30 min call gives a note; `kill -9` of the daemon mid-call still gives a note; transcription time is measured.
- **Mac-4: the app.** Menu bar, notifications, the fd 3 channel, login item, supervision, the first-run screen. *Exit:* a fresh user account goes from install to a first note with no terminal.
- **Mac-5: packaging.** Bundle, signing, notarization, cask, release job. *Exit:* `brew install --cask` on a clean Mac passes `wirenote doctor`.

## Questions

1. **Bundle ID.** It is permanent: changing it later resets every permission and the login item. Proposal: `io.github.knowhrishi.wirenote`.
2. **System track default.** All system audio, as on Linux, or only the meeting app's processes?
   - Only the app is more private, and it leaves out other apps' sounds.
   - But a process that starts mid-call (a restarted Chrome helper) is missed before macOS 26, where `bundleIDs` and `processRestoreEnabled` fix that.
   - Proposal: all system audio by default, with per-app as a setting.
3. **Intel Macs.** Proposal: Apple Silicon only. It halves the build and test work, and CTranslate2's x86_64 macOS wheels need checking first.
4. **Paths.** Keep the XDG layout (proposal), or move to `~/Library/Application Support/Wirenote`?
5. **Updates.** Homebrew and downloads only (proposal), or an opt-in update check in the app?
6. **Apple Developer account.** Its name appears in the signature ("Developer ID Application: …"). It belongs to a person or to an organization, which matters for the later company edition mentioned in the README.
7. **Floor.** macOS 14.2 (proposal), or 26.0, which removes the mid-call process gap in Question 2 but leaves out older Macs?
