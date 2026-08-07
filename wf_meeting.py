#!/usr/bin/env python3
"""Meeting-mode dual-channel transcription for local-wisprflow on Windows.

Captures TWO streams and labels them by source (no ML diarization needed):
  * the microphone       -> "Me"
  * a system-audio loopback -> "Client"   (= whatever is playing, e.g. the Teams/Zoom call)

    Client: ...

    Me: ...

**How the port differs.** The Linux original captured the client side from PipeWire's
`<sink>.monitor` via `ffmpeg -f pulse`, because every PulseAudio sink exposes a monitor source
for free. Windows has no such guarantee: WASAPI can capture a render endpoint in *loopback*
mode, but whether that shows up as a recordable device depends on the audio stack. So this
port looks for, in order:

  1. a PortAudio **WASAPI loopback** device (named "<device> [Loopback]"), the modern path —
     always available, needs nothing enabled by the user;
  2. a legacy **"Stereo Mix" / "What U Hear"** device, which some Realtek drivers expose but
     ship disabled;
  3. nothing — in which case `start()` fails with an explanation the daemon shows on screen,
     instead of silently recording the microphone on both channels.

Point 3 is the one worth being strict about: the original had exactly this bug on Bluetooth
sinks (`pw-record --target` silently fell back to the mic, so both channels recorded the same
voice), so this port refuses to start rather than produce a transcript that looks fine and
attributes everything to one speaker.

Both streams are segmented on silence with a lightweight energy VAD, transcribed by the
daemon's shared WhisperModel behind `daemon.model_lock` (CTranslate2 is NOT safe for
concurrent transcribe() calls), and appended live to a speaker-labeled Markdown file.

Use headphones. With the call's audio in your earbuds the mic never hears it, so the two
streams stay cleanly separated; on speakers the mic re-captures the client (bleed) and the
dedup guard in `_append` has to clean it up.
"""
from __future__ import annotations

import datetime
import difflib
import os
import sys
import threading
import time
from collections import deque
from queue import Empty, Queue

import numpy as np

SR = 16000
BLOCK = 512  # 32 ms frames

_LOOPBACK_HINTS = ("stereo mix", "stereomix", "what u hear", "wave out mix",
                   "loopback", "was abgespielt wird", "mixage stéréo")


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _hostapi_name(dev) -> str:
    import sounddevice as sd
    try:
        return sd.query_hostapis(dev["hostapi"])["name"]
    except Exception:  # noqa: BLE001
        return ""


def find_loopback_device(preferred=None):
    """(index, kind) of a system-audio capture device, or (None, '').

    `preferred` may be an index or a name substring from the config, for machines with a
    virtual cable (VB-Audio, VoiceMeeter) the user would rather use.
    """
    import sounddevice as sd
    devs = sd.query_devices()

    if preferred not in (None, ""):
        try:
            return int(preferred), "configured"
        except (TypeError, ValueError):
            needle = str(preferred).lower()
            for i, d in enumerate(devs):
                if d["max_input_channels"] > 0 and needle in d["name"].lower():
                    return i, "configured"
    # 1) WASAPI loopback pseudo-device — the reliable modern path.
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0 and "[loopback]" in d["name"].lower():
            return i, "wasapi-loopback"
    # 2) legacy Stereo Mix, if the user enabled it in Sound settings.
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0 and any(k in d["name"].lower() for k in _LOOPBACK_HINTS):
            return i, "stereo-mix"
    return None, ""


NO_LOOPBACK_HELP = (
    "no system-audio capture device found. Meeting mode needs to hear the other side of the "
    "call. Fix it in one of these ways:\n"
    "  * update to a build of sounddevice/PortAudio that exposes WASAPI '[Loopback]' devices; or\n"
    "  * enable Stereo Mix: Settings > System > Sound > More sound settings > Recording tab >\n"
    "    right-click > Show Disabled Devices > enable 'Stereo Mix'; or\n"
    "  * install a virtual audio cable (e.g. VB-Audio) and set\n"
    "    \"meeting_loopback_device\": \"CABLE Output\" in your config.json."
)


class MeetingSession:
    """One meeting: two capture+VAD threads feed a single transcribe/writer worker."""

    def __init__(self, daemon, log):
        self.d = daemon
        self.cfg = daemon.cfg
        self.log = log
        self.stop_event = threading.Event()
        self.segq: Queue = Queue()
        self.threads = []
        self.turns = []            # [(speaker, text)] with consecutive same-speaker merged
        self.path = None
        self.header = ""
        self.error = ""

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> bool:
        if sys.platform != "win32":
            self.error = "meeting mode requires Windows"
            return False
        try:
            import sounddevice as sd  # noqa: F401
        except Exception as e:  # noqa: BLE001
            self.error = f"sounddevice unavailable: {e}"
            self.log(f"meeting: {self.error}")
            return False

        loop_idx, kind = find_loopback_device(self.cfg.get("meeting_loopback_device"))
        if loop_idx is None:
            self.error = "no system-audio device"
            self.log("meeting: " + NO_LOOPBACK_HELP)
            return False

        import sounddevice as sd
        mic_idx = self.cfg.get("input_device")
        try:
            from wf_daemon import resolve_input_device
            mic_idx = resolve_input_device(mic_idx)
        except Exception:  # noqa: BLE001
            mic_idx = None
        if mic_idx is None:
            mic_idx = sd.default.device[0]
        if mic_idx is None or (isinstance(mic_idx, int) and mic_idx < 0):
            self.error = "no microphone"
            self.log("meeting: no default input device")
            return False
        if int(mic_idx) == int(loop_idx):
            # Both channels reading the same device would attribute everything to one speaker.
            self.error = "mic and system audio are the same device"
            self.log(f"meeting: {self.error} (index {mic_idx}) — refusing to start")
            return False

        d = os.path.expanduser(self.cfg.get("meeting_dir", "~/Documents/wf-meetings"))
        os.makedirs(d, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.path = os.path.join(d, f"meeting-{ts}.md")
        self.header = f"# Meeting transcript — {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n"
        self.turns = []
        self._flush()

        try:
            mic_name = sd.query_devices(mic_idx)["name"]
            loop_name = sd.query_devices(loop_idx)["name"]
        except Exception:  # noqa: BLE001
            mic_name = loop_name = "?"
        self.log(f"meeting: -> {self.path}")
        self.log(f"meeting: Me={mic_name!r} (device {mic_idx})")
        self.log(f"meeting: Client={loop_name!r} (device {loop_idx}, {kind})")

        self.threads = [
            threading.Thread(target=self._worker, name="wf-meeting-asr", daemon=True),
            threading.Thread(target=self._channel, args=(mic_idx, "Me"),
                             name="wf-meeting-me", daemon=True),
            threading.Thread(target=self._channel, args=(loop_idx, "Client"),
                             name="wf-meeting-client", daemon=True),
        ]
        for t in self.threads:
            t.start()
        return True

    def stop(self) -> str:
        self.stop_event.set()
        for _ in range(60):          # let the worker drain queued segments (bounded)
            if self.segq.empty():
                break
            time.sleep(0.05)
        self.log(f"meeting: saved {self.path} ({len(self.turns)} turns)")
        return self.path

    # -- capture + energy VAD (per channel) -----------------------------------
    def _open_stream(self, device):
        """InputStream at 16 kHz mono, with the fallbacks Windows audio actually needs.

        Returns (stream, native_sr, channels). A loopback endpoint runs at the mixer's rate
        (usually 48 kHz) and is stereo; WASAPI's auto_convert can resample and downmix in the
        driver, but MME/DirectSound devices and some drivers reject the request outright — so
        we fall back to opening the device natively and converting in numpy.
        """
        import sounddevice as sd
        info = sd.query_devices(device)
        is_wasapi = _hostapi_name(info) == "Windows WASAPI"
        extra = None
        if is_wasapi:
            try:
                # auto_convert inserts WASAPI's own sample-rate converter and channel matrix,
                # which is what makes a 48 kHz stereo loopback readable as 16 kHz mono.
                extra = sd.WasapiSettings(auto_convert=True)
            except Exception:  # noqa: BLE001
                extra = None
        try:
            s = sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=BLOCK,
                               device=device, extra_settings=extra)
            s.start()
            return s, SR, 1
        except Exception as e:  # noqa: BLE001
            self.log(f"meeting: {device} not readable as 16 kHz mono ({e!r}); "
                     "falling back to the device's native format")
        native_sr = int(info.get("default_samplerate") or 48000)
        ch = min(2, max(1, int(info.get("max_input_channels") or 1)))
        block = max(1, int(BLOCK * native_sr / SR))
        s = sd.InputStream(samplerate=native_sr, channels=ch, dtype="float32",
                           blocksize=block, device=device, extra_settings=extra)
        s.start()
        return s, native_sr, ch

    @staticmethod
    def _to_mono_16k(data: np.ndarray, native_sr: int) -> np.ndarray:
        """Downmix to mono and resample to 16 kHz (linear — fine for speech VAD + Whisper)."""
        mono = data.mean(axis=1) if data.ndim > 1 and data.shape[1] > 1 else data.reshape(-1)
        if native_sr == SR or mono.size == 0:
            return mono.astype(np.float32, copy=False)
        n_out = max(1, int(round(mono.size * SR / native_sr)))
        return np.interp(np.linspace(0, mono.size - 1, n_out),
                         np.arange(mono.size), mono).astype(np.float32)

    def _channel(self, device, label):
        cfg = self.cfg
        floor = float(cfg.get("meeting_vad_floor", 0.02))
        sil_need = float(cfg.get("meeting_silence_ms", 700)) / 1000.0
        min_speech = float(cfg.get("meeting_min_speech_ms", 300)) / 1000.0
        maxseg = float(cfg.get("meeting_max_seg_s", 24))
        try:
            stream, native_sr, _ch = self._open_stream(device)
        except Exception as e:  # noqa: BLE001
            self.log(f"meeting: could not open {label} device {device}: {e!r}")
            return
        win = deque(maxlen=5)      # ~160 ms rolling window -> smooths transient noise spikes
        preroll = deque(maxlen=6)  # ~190 ms pre-roll so speech onsets aren't clipped
        buf, speaking, silence, seg_start, speech_dur = [], False, 0.0, 0.0, 0.0
        read_frames = max(1, int(BLOCK * native_sr / SR))
        try:
            while not self.stop_event.is_set():
                data, _overflowed = stream.read(read_frames)
                frame = self._to_mono_16k(np.asarray(data), native_sr)
                if frame.size == 0:
                    continue
                win.append(float(np.sqrt(np.mean(frame ** 2))))
                loud = (sum(win) / len(win)) >= floor      # windowed level, not a single frame
                if not speaking:
                    preroll.append(frame)
                    if loud:
                        speaking, seg_start = True, time.time()
                        buf, speech_dur, silence = list(preroll), 0.0, 0.0
                else:
                    buf.append(frame)                      # keep trailing silence for a clean cut
                    dur = frame.size / SR
                    if loud:
                        silence = 0.0
                        speech_dur += dur
                    else:
                        silence += dur
                    if (silence >= sil_need and speech_dur >= min_speech) \
                            or (time.time() - seg_start) >= maxseg:
                        seg, spoke = np.concatenate(buf), speech_dur
                        buf, speaking, silence = [], False, 0.0
                        preroll.clear()
                        if spoke >= min_speech:
                            self.segq.put((seg_start, label, seg))
        except Exception as e:  # noqa: BLE001
            self.log(f"meeting: {label} capture stopped: {e!r}")
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    # -- transcribe (serialized) + write --------------------------------------
    def _worker(self):
        cfg = self.cfg
        while not (self.stop_event.is_set() and self.segq.empty()):
            try:
                seg_start, label, audio = self.segq.get(timeout=0.3)
            except Empty:
                continue
            if audio.size < int(0.2 * SR):
                continue
            self.d.mark_activity()   # keep whisper on the GPU during the meeting (auto mode)
            t0 = time.time()
            try:
                with self.d.model_lock:
                    segments, _ = self.d.asr.transcribe(
                        audio, language=self.d._eff_language() or None,
                        beam_size=int(cfg.get("meeting_beam_size", 3)),
                        vad_filter=False, condition_on_previous_text=False)
                    text = " ".join(s.text.strip() for s in segments).strip()
            except Exception as e:  # noqa: BLE001
                self.log(f"meeting: transcribe error: {e!r}")
                continue
            self.log(f"meeting: [{label}] {audio.size / SR:.1f}s -> {time.time() - t0:.2f}s "
                     f"-> {text[:60]!r}")
            if text:
                self._append(label, text)

    def _append(self, label, text):
        speaker = "Me" if label == "Me" else "Client"
        # Speaker-bleed dedup (matters only WITHOUT headphones): the mic re-captures the
        # client's speaker audio, so the same utterance appears back-to-back from BOTH
        # channels. Keep the "Client" (clean loopback) copy and drop the mic duplicate —
        # works in either arrival order.
        if len(text) >= 5 and self.turns and self.turns[-1][0] != speaker \
                and _similar(text, self.turns[-1][1]) >= 0.82:
            if speaker == "Client":
                self.turns[-1] = ("Client", text)      # replace the bleed "Me" with clean Client
                if len(self.turns) >= 2 and self.turns[-2][0] == "Client":  # re-merge if it split
                    self.turns[-2] = ("Client", self.turns[-2][1] + " " + text)
                    self.turns.pop()
                self.log(f"meeting: bleed pair -> kept Client -> {text[:40]!r}")
            else:
                self.log(f"meeting: dropped mic-bleed duplicate -> {text[:40]!r}")
            self._flush()
            return
        if self.turns and self.turns[-1][0] == speaker:
            self.turns[-1] = (speaker, self.turns[-1][1] + " " + text)
        else:
            self.turns.append((speaker, text))
        self._flush()

    def _flush(self):
        body = self.header + "\n\n".join(f"{s}: {t}" for s, t in self.turns)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(body.rstrip() + "\n")
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            self.log(f"meeting: write failed: {e!r}")
