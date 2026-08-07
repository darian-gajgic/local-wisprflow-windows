#!/usr/bin/env python3
"""Diagnostics for local-wisprflow on Windows.

When dictation "does nothing", the cause is almost always one of a short list: the daemon is
not running, the hotkey is owned by another app, Ollama is down, the model was never
downloaded, the microphone is muted, or the target window is elevated. This prints the state
of every one of those in a single pass, so nobody has to guess.

    python wf_doctor.py
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wf_cuda    # noqa: E402
import wf_ipc     # noqa: E402
import wf_ollama  # noqa: E402
import wf_paths   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

OK, WARN, BAD = "[ok]", "[!]", "[X]"
_problems: list[str] = []


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


def report(ok, label: str, detail: str = "", fix: str = "") -> None:
    mark = OK if ok else (WARN if ok is None else BAD)
    print(f"  {mark} {label}" + (f": {detail}" if detail else ""))
    if not ok and fix:
        print(f"       -> {fix}")
        _problems.append(f"{label}: {fix}")


def main() -> int:  # noqa: PLR0912, PLR0915
    print("=" * 72)
    print("local-wisprflow — diagnostics")
    print("=" * 72)

    # --- environment ------------------------------------------------------
    section("Environment")
    print(f"  {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"  Python {sys.version.split()[0]} — {sys.executable}")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    report(True if in_venv else None, "virtual environment",
           "yes" if in_venv else "no (using the system Python)")
    if sys.platform != "win32":
        report(False, "platform", f"{sys.platform} — this port targets Windows",
               "run this on Windows")

    # --- packages ---------------------------------------------------------
    section("Python packages")
    for mod, pkg in (("faster_whisper", "faster-whisper"), ("sounddevice", "sounddevice"),
                     ("numpy", "numpy"), ("requests", "requests")):
        try:
            m = __import__(mod)
            report(True, pkg, getattr(m, "__version__", "installed"))
        except Exception as e:  # noqa: BLE001
            report(False, pkg, repr(e), f"pip install {pkg}")

    # --- GPU --------------------------------------------------------------
    section("GPU / CUDA")
    gpus = wf_cuda.gpu_names()
    if gpus:
        for g in gpus:
            report(True, "GPU", g)
        dirs = wf_cuda.candidate_dirs()
        report(bool(dirs), "CUDA libraries",
               f"{len(dirs)} director{'y' if len(dirs) == 1 else 'ies'}" if dirs else "not found",
               "pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
    else:
        report(None, "NVIDIA GPU", "not detected — transcription will run on the CPU")

    # --- configuration ----------------------------------------------------
    section("Configuration")
    cfg_path = wf_paths.config_path()
    report(cfg_path.exists(), "config file", str(cfg_path),
           "run  python wf_setup.py  to create it")
    cfg = wf_paths.load_config()
    print(f"      asr_model={cfg['asr_model']}  asr_device={cfg['asr_device']}  "
          f"inject={cfg['inject_method']}")
    print(f"      hotkey={cfg['hotkey']}  llm={cfg['llm_model']} @ {cfg['ollama_url']}")

    try:
        import wf_hotkey
        spec = cfg.get("hotkey", "")
        if wf_hotkey.parse_doubletap(spec) is None:
            wf_hotkey.parse_hotkey(spec)
        report(True, "hotkey syntax", wf_hotkey.describe(spec))
    except Exception as e:  # noqa: BLE001
        report(False, "hotkey syntax", str(e), "fix 'hotkey' in the config file")

    # --- speech model -----------------------------------------------------
    section("Speech model")
    try:
        from faster_whisper.utils import download_model
        try:
            p = download_model(cfg["asr_model"], local_files_only=True)
            report(True, cfg["asr_model"], str(p))
        except Exception:  # noqa: BLE001
            report(False, cfg["asr_model"], "not downloaded",
                   "run  python wf_setup.py  to download it")
    except Exception as e:  # noqa: BLE001
        report(False, "faster-whisper", repr(e), "pip install faster-whisper")

    # --- audio ------------------------------------------------------------
    section("Audio")
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        ins = [(i, d["name"]) for i, d in enumerate(devs) if d["max_input_channels"] > 0]
        report(bool(ins), "input devices", f"{len(ins)} found",
               "connect a microphone")
        try:
            di = sd.default.device[0]
            if isinstance(di, int) and 0 <= di < len(devs):
                print(f"      default: {devs[di]['name']}")
        except Exception:  # noqa: BLE001
            pass
        try:
            import wf_meeting
            idx, kind = wf_meeting.find_loopback_device(cfg.get("meeting_loopback_device"))
            if idx is None:
                report(None, "system-audio loopback",
                       "not available — meeting mode will not start")
                print("       -> see 'Meeting mode' in the README to enable Stereo Mix")
            else:
                report(True, "system-audio loopback", f"{devs[idx]['name']} ({kind})")
        except Exception as e:  # noqa: BLE001
            report(None, "system-audio loopback", repr(e))
    except Exception as e:  # noqa: BLE001
        report(False, "sounddevice", repr(e), "pip install sounddevice")

    # --- Ollama -----------------------------------------------------------
    section("Cleanup LLM (Ollama)")
    exe = wf_ollama.ollama_exe()
    report(bool(exe), "ollama binary", exe or "not found",
           "install from https://ollama.com/download/windows")
    url = cfg["ollama_url"]
    up = wf_ollama.is_up(url)
    report(up, "server", url, "start Ollama, or run  python wf_setup.py")
    if up:
        models = wf_ollama.list_models(url)
        have = wf_ollama.has_model(url, cfg["llm_model"])
        report(have, f"model {cfg['llm_model']}",
               f"{len(models)} model(s) installed",
               f"ollama pull {cfg['llm_model']}")
        loaded = wf_ollama.loaded_models(url)
        if loaded:
            print("      resident: " + ", ".join(
                f"{m.get('name')} ({(m.get('size_vram') or 0) / 2**30:.1f} GB VRAM)"
                for m in loaded))

    # --- daemon -----------------------------------------------------------
    section("Daemon")
    rt = wf_ipc.read_runtime()
    if rt:
        print(f"      runtime file: port {rt.get('port')}, pid {rt.get('pid')}")
    try:
        info = wf_ipc.send("info", timeout=3)
        report(True, "running", info)
    except wf_ipc.NotRunning as e:
        report(False, "running", str(e), "start it with  wf-start.cmd")

    # --- injection --------------------------------------------------------
    section("Text injection")
    try:
        import wf_input
        elevated = wf_input.is_elevated()
        report(True if elevated else None, "elevation",
               "running as Administrator" if elevated else
               "normal user — cannot type into windows running as Administrator")
        if sys.platform == "win32":
            title = wf_input.foreground_window_title()
            if title:
                print(f"      focused window: {title!r}")
    except Exception as e:  # noqa: BLE001
        report(False, "wf_input", repr(e))

    # --- logs -------------------------------------------------------------
    section("Logs")
    logf = wf_paths.log_dir() / "daemon.log"
    if logf.exists():
        print(f"  {logf}  ({logf.stat().st_size / 1024:.0f} KB)")
        try:
            tail = logf.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
            for line in tail:
                print(f"      {line}")
        except Exception:  # noqa: BLE001
            pass
    else:
        print(f"  {WARN} no log yet at {logf}")

    # --- summary ----------------------------------------------------------
    print("\n" + "=" * 72)
    if _problems:
        print(f"{len(_problems)} problem(s) found:")
        for p in _problems:
            print(f"  - {p}")
    else:
        print("No problems found.")
    print("=" * 72)
    return 1 if _problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
