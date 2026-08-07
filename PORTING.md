# Porting notes: Linux/GNOME → Windows

What changed between [local-wisprflow](https://github.com/darian-gajgic/local-wisprflow) and
this port, and why. The pipeline (record → faster-whisper → Ollama cleanup → inject) and all of
its text-handling logic are unchanged; everything below is about the platform underneath it.

## Summary

| Concern | Linux original | Windows port |
|---|---|---|
| IPC | `AF_UNIX` socket in `$XDG_RUNTIME_DIR`, mode 0600 | `wf_ipc.py` — loopback TCP, ephemeral port + 128-bit token |
| Text injection | `ydotool` + `/dev/uinput` + libxkbcommon keymap compilation | `wf_input.py` — `SendInput` with `KEYEVENTF_UNICODE` |
| Clipboard | `wl-copy` | Win32 clipboard API via ctypes |
| Hotkey | `evdev` raw device reader, `input` group, udev rule | `wf_hotkey.py` — `RegisterHotKey` + `WH_KEYBOARD_LL` |
| Service management | 4 systemd user units | one process; a Startup-folder shortcut |
| Status / control | `systemctl --user status` | `wf_tray.py` — system-tray icon and menu |
| CUDA discovery | `LD_LIBRARY_PATH` in `wf-run` | `wf_cuda.py` — `os.add_dll_directory()` |
| Monitor geometry | `xrandr` parsing | `SPI_GETWORKAREA` |
| DPI | X server DPI | per-monitor DPI awareness v2 |
| System audio | PipeWire `<sink>.monitor` via `ffmpeg -f pulse` | WASAPI loopback / Stereo Mix via sounddevice |
| Config | `~/.config/wisprflow/config.json` | `%APPDATA%\wisprflow\config.json` |
| Install | `install-system.sh` (sudo) + `install-services.sh` | `install.ps1` + `wf_setup.py`, no admin rights |

## The changes worth explaining

### Text injection got simpler, not harder

This is the one place the port *removed* a whole subsystem. On Linux, `ydotool type` emits raw
evdev keycodes that the focused app re-interprets under its own XKB layout — so typing `?` on a
German QWERTZ keyboard produced `_`. The original fixed this by loading libxkbcommon through
ctypes, compiling the user's keymap, and mapping every character to a (keycode, shift-level)
pair; characters not physically on the keyboard had to be normalised away (`—` → `-`).

Win32 `SendInput` accepts the **UTF-16 code unit itself** via `KEYEVENTF_UNICODE`. The target
window receives a `WM_CHAR` regardless of the active layout. So `wf_layout.py` has no
counterpart here: typing is layout-independent by construction, and em dashes, curly quotes and
emoji (sent as surrogate pairs) survive intact.

What Windows adds in return is **UIPI**: a non-elevated process cannot send input to an
elevated window. `inject()` catches that, falls back to the clipboard so the dictation is never
lost, and `wf_doctor.py` reports the elevation state.

### IPC needed an actual security decision

CPython on Windows does not expose `socket.AF_UNIX`, so the Unix socket had to become TCP on
127.0.0.1 — and filesystem permissions no longer provide the access control that `chmod 0600`
used to. Any local process that guessed the port could otherwise trigger dictation and have
text typed into the foreground window.

So the daemon binds an **ephemeral** port and writes `{port, token, pid}` to
`%LOCALAPPDATA%\wisprflow\daemon.json` (a per-user directory), and every command must carry
that 128-bit token, compared with `secrets.compare_digest`. The runtime file is written
atomically via `os.replace`, so a client never reads a half-written one.

### One process instead of four units

The original relied on systemd user services for the daemon, the key listener, ydotoold and an
isolated Ollama — including dependency ordering, restart-on-failure and autostart. Windows has
no user-level service manager worth using here, so the daemon hosts the hotkey listener, the
tray icon and Ollama supervision in-process, and autostart is a shortcut in the Startup folder.

That makes the **tray icon load-bearing rather than decorative**: without `systemctl status`
there is otherwise no way to see whether the thing is alive, and no way to quit it short of
Task Manager. It is still built so that any failure in it is caught and logged, and dictation
continues without it.

### The GPU rationale inverted

The original hard-defaulted to `asr_device: "cpu"` with a long comment explaining why: that
machine's 12 GB GPU was permanently occupied by a resident 14B research harness, so putting
whisper on the GPU would evict it.

That reasoning is specific to that machine. A typical Windows box has its GPU free, so the
default here is `"auto"`. The adaptive machinery is kept verbatim — promote to GPU on dictation
activity, demote when another process takes more than `asr_auto_other_vram_mib`, demote after
`gpu_idle_timeout_s` so a laptop GPU can power down, always keep a warm CPU model so a swap
never blocks a dictation — because it is just as useful for a gaming GPU that a game might
claim at any moment.

One addition: after three consecutive failed GPU promotions the monitor stops retrying for the
session. On a machine with no NVIDIA GPU or without the CUDA wheels, the original would attempt
a full model load every poll interval, forever.

### The isolated Ollama became opt-in

The original ran a *second* Ollama on port 11435 with its own models directory and an f16 KV
cache, because the system one was configured with `OLLAMA_KV_CACHE_TYPE=q4_0` (needed to keep
that 14B resident) and a q4_0 cache garbles small models like gemma3:4b.

A stock Windows Ollama already uses an f16 KV cache, so the port talks to the normal instance
on 11434 and the isolation is available behind `"ollama_isolated": true` for anyone who has
tuned Ollama globally for another workload. `wf_ollama.ensure_running()` replaces the systemd
dependency ordering that used to guarantee the cleanup LLM was up first.

### Meeting mode had to get stricter

On Linux every PulseAudio sink exposes a `.monitor` source, so capturing the far side of a call
was free. Windows has no such guarantee, so `wf_meeting.py` looks for a PortAudio **WASAPI
loopback** device, then a legacy **Stereo Mix**, and if it finds neither it **refuses to start**
with an explanation.

That refusal is deliberate, and it is a lesson taken from a bug in the original: `pw-record
--target <sink>` silently fell back to the default microphone for Bluetooth sinks, so both
channels recorded the same voice and produced a transcript that looked fine while attributing
everything to one speaker. Failing loudly is better than that. For the same reason the port
also refuses to start if the resolved mic and loopback are the same device index.

The capture path is also simpler: the original shelled out to `ffmpeg` (because `sounddevice`
hung on PipeWire monitor sources); here both channels go through `sounddevice`, with WASAPI's
`auto_convert` doing the 48 kHz-stereo → 16 kHz-mono conversion, and a numpy fallback when a
driver rejects that.

### The overlay must never take focus

A detail with no Linux equivalent: on X11 the pill was an override-redirect window, which is
already non-focusable. On Windows an `overrideredirect` Tk window *will* take focus — and since
`SendInput` targets the **focused** window, that would send every dictation into the overlay
instead of the user's editor.

`wf_overlay.py` therefore stamps `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` on the toplevel (before
and after mapping, since Tk can recreate the frame). It also declares per-monitor DPI awareness
v2 before creating any window — without it Windows reports 96 DPI and bitmap-stretches the
result, which looks blurry on a scaled display — and uses `-transparentcolor` so the pill's
rounded corners show the desktop through them.

The orphan watchdog changed too. The original watched for its parent PID changing, which works
because Unix re-parents orphans to init. Windows does not re-parent and recycles PIDs, so the
overlay pings the daemon over the same IPC channel its buttons use, and closes after three
consecutive misses.

### Hotkeys: a different failure mode

The original's `wf-keylistener.py` had an elaborate duplicate-collapse scheme, because that
laptop reported `KEY_PRESENTATION` from several input devices at once and one physical press
produced two toggles that cancelled out. `RegisterHotKey` delivers exactly one `WM_HOTKEY` per
press, so that entire problem disappears.

The double-tap trigger (`"doubletap:rctrl"`) has the mirror-image concern and handles it:
auto-repeat is ignored (taps are counted on key-*up*, and a held key produces no ups), any other
key in between cancels the sequence, and keystrokes wisprflow itself synthesised are skipped —
they carry a magic `dwExtraInfo` tag, so a paste chord cannot trigger a dictation.

The hook callback also never does real work. Windows silently uninstalls a `WH_KEYBOARD_LL`
hook that exceeds `LowLevelHooksTimeout` (300 ms by default), so callbacks are dispatched to a
worker thread; otherwise the hotkey would just stop working with no error anywhere.

## Carried over unchanged

The text logic is identical, and the test suite pins it: the cleanup prompt and its per-language
variants, the pattern-completion framing (`Input:` / `Output:`) that stops the model replying to
a dictated question, `_sanitize`'s preamble/quote stripping, the off-script and expansion/
collapse guards, the `translated_away` word-containment backstop, `format_notes`'s deterministic
sentence splitting, the language cycle with its mid-transcription re-check, and the meeting-mode
speaker-bleed dedup.

One inherited trade-off is documented rather than fixed: the rule that keeps `J. R. R. Tolkien`
on one line cannot distinguish it from a sentence that genuinely ends in a single letter
(`The answer is B.`), so that sentence does not split. There is a test asserting this so it
stays a decision rather than a surprise.

## Not ported

- `set-hotkey.sh`, `grab-key-gui.py`, `grab-key-evdev.py` — GNOME/evdev key-capture helpers.
  `wf_setup.py` asks for a hotkey directly, and `wf_hotkey.parse_hotkey` validates it.
- `wf_layout.py` — obsolete, see above.
- `systemd/*.service`, `*.desktop` — replaced by shortcuts and in-process supervision.
- `install-system.sh` — no sudo, no udev rule and no `input` group are needed on Windows.
