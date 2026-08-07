# local-wisprflow — Windows

A fully-local, private dictation tool for Windows. Press a hotkey, speak, and cleaned,
punctuated text is typed into whatever app is focused. **Zero cloud, zero telemetry** — the
audio never leaves the machine.

This is the Windows port of [local-wisprflow](https://github.com/darian-gajgic/local-wisprflow)
(Linux/GNOME). Same two-stage "wait-then-polish" pipeline, rebuilt on Win32.

```
mic ─▶ record (hotkey toggle) ─▶ faster-whisper ASR ─▶ Ollama LLM cleanup ─▶ type into focused app
       Ctrl+Alt+Space            (large-v3, GPU/CPU)     (gemma3:4b)          (Win32 SendInput)
```

## Install

Download or clone this folder, then **double-click `install.bat`**.

That is the whole install. It needs no Administrator rights and touches nothing outside your
user profile. It will:

1. find Python (offering to install 3.12 via winget if you have none);
2. build a private virtual environment in this folder;
3. install the Python packages;
4. hand over to the setup wizard, which **asks before each download** — the CUDA libraries
   (only if you have an NVIDIA GPU), Ollama, the cleanup model and the speech model, each
   with its size stated up front;
5. create Start Menu and Desktop shortcuts, and offer to start it.

```powershell
.\install.ps1            # same thing, from a PowerShell prompt
.\install.ps1 -Yes       # accept every recommended default (downloads ~7 GB)
.\install.ps1 -NoModels  # environment + shortcuts only; run wf-setup.cmd later
```

**Disk space:** ~2 GB for the Python environment, plus ~3.1 GB (speech model) and ~3.3 GB
(cleanup model). The wizard offers smaller speech models if that is too much.

You can re-run `wf-setup.cmd` any time — it is idempotent, and reports what is already done
instead of redoing it.

## Use it

- Press **Ctrl + Alt + Space** → a "Listening" pill appears at the bottom of the screen → speak.
- Press it **again** to stop → it transcribes, cleans up, and types the result into the
  focused window.
- **Ctrl + Alt + X** cancels a recording without typing anything.
- The **system-tray icon** shows the current state and gives you everything by right-click:
  start/stop, NoteMode, language, meeting mode, config, logs, setup, diagnostics, quit.

Prefer a Wispr-Flow-style trigger? Set `"hotkey": "doubletap:rctrl"` in the config and tap
**Right Ctrl twice**. No combination to remember, and no key taken away from other apps.

### Something not working?

```
wf-doctor.cmd
```

It checks every common cause in one pass — daemon running, hotkey conflicts, Ollama up, models
downloaded, microphone present, elevation — and prints the exact command to fix each problem
it finds, plus the tail of the log.

## Components

| File | Role |
|---|---|
| `wf_daemon.py` | Resident daemon: keeps whisper warm, hosts the hotkey/tray/IPC, runs record→ASR→cleanup→inject. |
| `wf_input.py` | Text injection: Win32 `SendInput` (Unicode), clipboard, paste chords. |
| `wf_hotkey.py` | Global hotkeys: `RegisterHotKey` combos **and** double-tap detection via a low-level keyboard hook. |
| `wf_ipc.py` | Loopback TCP + token IPC (Windows CPython has no `AF_UNIX`). |
| `wf_overlay.py` | The on-screen pill — DPI-aware, non-focus-stealing, with MeetingMode / NoteMode / Language buttons. |
| `wf_tray.py` | System-tray icon and right-click menu (`Shell_NotifyIcon` via ctypes). |
| `wf_meeting.py` | Meeting mode: dual-channel (mic + system audio) speaker-labeled transcription. |
| `wf_ollama.py` | Finds, starts and supervises Ollama; pulls models with progress. |
| `wf_cuda.py` | Registers the CUDA DLL directories before ctranslate2 loads (the Windows `LD_LIBRARY_PATH`). |
| `wf_paths.py` | `%APPDATA%` / `%LOCALAPPDATA%` layout, config defaults, logging. |
| `wf_setup.py` | The guided setup wizard: packages, GPU, Ollama, both models, mic test, hotkey, autostart. |
| `wf_doctor.py` | Diagnostics. |
| `wf_toggle.py` | CLI client — for scripting, AutoHotkey or a Stream Deck. |
| `install.ps1` / `install.bat` | Installer. `uninstall.ps1` removes it again. |

## NoteMode (one sentence per line)

For longer notes, a single paragraph is hard to read. Turn on **NoteMode** and each dictation
is written **one sentence per line** instead:

1. Press your hotkey → click **📝 NoteMode** on the pill (or run `wf-toggle.cmd note`, or use
   the tray menu).
2. Speak and stop as usual — the text is broken at sentence boundaries, one per line, and ends
   on a fresh line so the next note starts cleanly.
3. It is a **persistent toggle** — it stays on until you turn it off (or set `"note_mode": true`
   in the config to default it on).

Sentence splitting is **deterministic** (no LLM), so it works even when cleanup is
unavailable — it relies on the punctuation Whisper already produces. Abbreviations (`Dr.`,
`e.g.`, `z.B.`), initials, decimals and standalone list markers (`1.`) don't trigger a break,
while a clause that merely ends in a number (`I scored 8.`) still splits.

> **NoteMode types real Enter keys.** That is perfect in a text editor or notes app, but in a
> **terminal or chat box** each newline submits the line — so use it where newlines mean "new
> line", not "send". The pill shows **"NoteMode •ON"** while it is active.

## Languages (EN → DE → RO)

Click the **🌐** button on the pill (or `wf-toggle.cmd lang`, or the tray menu) to cycle the
recognition language. It is session-only and resets to English on restart.

Cleanup uses a **prompt written in the target language**, including its few-shot examples.
That detail matters: with an all-English prompt a 4B model "completes the pattern" by
translating your German or Romanian dictation into English — usually only half of it. A
language-agnostic backstop also compares the cleaned output against the words actually spoken
and falls back to the raw transcript if they diverge, so a misbehaving model can never replace
what you said with a translation.

## Meeting mode (dual-channel transcription)

Transcribes a call with **speaker separation**, for meetings you are allowed to record:

1. Press your hotkey → click **👥 MeetingMode** on the pill.
2. It captures two streams and writes a live transcript to `Documents\wf-meetings\meeting-<timestamp>.md`:
   ```
   Client: <what the other side said>

   Me: <what you said>
   ```
3. Press your hotkey again to stop and finalize the file.

The **microphone** is "Me" and a **system-audio loopback** is "Client". Speaker labels come
from the source channel — no diarization model involved. Each stream is segmented on silence
(windowed energy VAD) and transcribed faithfully (no LLM rewrite).

> **Use headphones.** With the call's audio in your earbuds the mic never hears it, so the two
> streams are cleanly separated. On speakers the mic re-captures the client (bleed); a dedup
> guard keeps the clean copy, but headphones are the happy path.

**If meeting mode says "no system-audio device":** Windows does not always expose one. Either
enable **Stereo Mix** (Settings → System → Sound → More sound settings → Recording → right-click
→ Show Disabled Devices → enable "Stereo Mix"), or install a virtual audio cable such as
VB-Audio and set `"meeting_loopback_device": "CABLE Output"`. `wf-doctor.cmd` tells you which
one it found. It deliberately **refuses to start** rather than record your mic on both channels
and produce a transcript that silently attributes everything to one speaker.

## Configuration

`%APPDATA%\wisprflow\config.json` — the wizard writes it; see `config.example.json` for the
full set. Common knobs:

- **`hotkey`** — `"ctrl+alt+space"`, or `"doubletap:rctrl"` (also `lctrl`, `rshift`, `ralt`…).
- **`asr_device`** — `"auto"` (GPU while dictating, CPU when idle or when a big model is on the
  GPU), `"cpu"`, or `"cuda"`.
- **`asr_model`** — `large-v3` (default), `distil-large-v3` or `medium` for lower latency.
- **`inject_method`** — `type` (default), `paste` (clipboard + a paste chord), or `clipboard`.
- **`auto_stop: true`** — stop automatically after `silence_ms` of silence instead of a second
  keypress. Calibrate `vad_rms_threshold` to your mic (`wf-setup.cmd` measures it for you).
- **`initial_prompt`** — bias recognition toward names and jargon you dictate often.
- **`llm_enable: false`** — skip cleanup and insert the raw transcript (lowest latency).
- **`ollama_isolated: true`** — run a private Ollama on port 11435 with its own models
  directory, instead of sharing the one on 11434.

Logs: `%LOCALAPPDATA%\wisprflow\logs\daemon.log`.

## Performance

End-to-end latency after you stop talking is roughly **ASR + cleanup**:

| | ASR (6 s utterance) | Cleanup | Total |
|---|---|---|---|
| NVIDIA GPU (`large-v3`) | ~0.3 s | ~0.8–1.3 s | **~1.1–1.6 s** |
| CPU only (`large-v3`, int8) | ~3 s | ~0.8–1.3 s | ~4 s |
| CPU only (`distil-large-v3`) | ~1.5 s | ~0.8–1.3 s | ~2.5 s |

In `auto` mode a CPU model is always kept warm, so a dictation is never blocked while the GPU
model loads or unloads. The GPU model is released after 30 minutes of inactivity
(`gpu_idle_timeout_s`) so a laptop GPU can power down.

## Notes and gotchas

- **Typing is layout-independent.** `SendInput` with `KEYEVENTF_UNICODE` carries the character
  itself rather than a key position, so text lands correctly on any keyboard layout, in
  terminals and GUIs alike, with no paste chord and no clipboard use — and characters that
  aren't on your keyboard at all (em dashes, curly quotes, emoji) arrive intact. This is the
  one place the Windows port is genuinely simpler than the Linux original, which had to compile
  your XKB keymap and map every character to an evdev keycode.
- **Elevated windows.** Windows blocks input from a normal process into a window running as
  Administrator (UIPI). If you dictate into an elevated app, start wisprflow as Administrator
  too. When injection fails the text is copied to the clipboard instead, so a dictation is
  never lost — `wf-doctor.cmd` reports your elevation state.
- **Hotkey conflicts.** `RegisterHotKey` is exclusive: if another app already owns the
  combination, registration fails and the daemon logs it (and `wf-doctor.cmd` flags it). Pick
  another combination, or switch to `doubletap:`.
- **First start is slow.** The speech model has to load into memory (~10–30 s). The tray icon
  appears immediately; the hotkey works once the log says `ready`.
- **The overlay never takes focus** (`WS_EX_NOACTIVATE`), which is what keeps dictated text
  going to your editor rather than to the pill.

## Requirements

- Windows 10 1809 or newer (64-bit), or Windows 11
- Python 3.9–3.13 (the installer can fetch 3.12)
- ~12 GB free disk space for a full install
- Optional: an NVIDIA GPU for much faster transcription
- Optional: [Ollama](https://ollama.com/download/windows) for cleanup — without it you still
  get dictation, just the raw transcript

## Development

```powershell
python -m unittest discover -s tests -v     # 40 tests, no Windows needed
python scripts\make_icon.py                 # regenerate assets\wisprflow.ico
```

The test suite covers the platform-independent logic: NoteMode sentence splitting, the LLM
output sanitiser, the off-script and translation backstops, hotkey parsing and the double-tap
state machine. The Win32 layers need a real desktop and are exercised by `wf_doctor.py`.

See [PORTING.md](PORTING.md) for what changed relative to the Linux original and why.

## License

MIT — see [LICENSE](LICENSE).
