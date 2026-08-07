#!/usr/bin/env python3
"""Guided setup for local-wisprflow on Windows — installs and verifies everything needed.

This is the piece the Linux original never needed: there, models and the Python environment
were already in place and two shell scripts wired up systemd. On Windows the user starts from
nothing, so this wizard walks through every dependency in order, explains what each one costs
in disk space and why it is needed, and asks before downloading anything:

    1. Python packages        (faster-whisper, sounddevice, requests, numpy)
    2. GPU acceleration       (CUDA wheels — offered only when an NVIDIA GPU is present)
    3. Ollama                 (the local LLM runtime used for cleanup)
    4. The cleanup model      (gemma3:4b, ~3.3 GB)
    5. The speech model       (faster-whisper large-v3, ~3.1 GB — with smaller alternatives)
    6. Microphone check
    7. Hotkey + autostart

Run it any time — it is idempotent and re-runnable, and reports what is already done rather
than redoing it.

    python wf_setup.py                 interactive
    python wf_setup.py --yes           accept every recommended default (used by install.ps1)
    python wf_setup.py --check         report status only, change nothing
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wf_cuda      # noqa: E402
import wf_ollama    # noqa: E402
import wf_paths     # noqa: E402

try:   # emoji/box-drawing must never crash setup on a legacy code-page console
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Whisper models, with the disk cost the user is being asked to accept.
ASR_MODELS = [
    ("large-v3",         3100, "best accuracy; the default. ~0.5x realtime on a modern CPU, "
                               "much faster on an NVIDIA GPU"),
    ("distil-large-v3",  1500, "about 2x faster than large-v3, very close accuracy (English)"),
    ("medium",           1500, "good accuracy, lower memory"),
    ("small",             480, "fast on any machine; noticeably weaker punctuation"),
    ("base",              145, "for low-end machines / testing only"),
]
LLM_MODEL_MB = 3300     # gemma3:4b

PIP_CORE = ["faster-whisper", "sounddevice", "numpy", "requests"]
PIP_CUDA = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]

OK, WARN, BAD = "[ok]", "[!]", "[X]"


# ---------------------------------------------------------------------------
# console helpers
# ---------------------------------------------------------------------------
def hr(title: str = "") -> None:
    print("\n" + "=" * 72)
    if title:
        print(title)
        print("=" * 72)


def step(n: int, total: int, title: str) -> None:
    print(f"\n--- Step {n}/{total}: {title} " + "-" * max(0, 52 - len(title)))


def ask(question: str, default: bool = True, assume_yes: bool = False) -> bool:
    if assume_yes:
        print(f"{question} [{'Y/n' if default else 'y/N'}] -> {'yes' if default else 'no'} (auto)")
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            a = input(f"{question} {suffix} ").strip().lower()
        except EOFError:
            return default
        if not a:
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False


def choose(question: str, options: list, default_idx: int = 0, assume_yes: bool = False) -> int:
    """options = [(label, detail), ...]; returns the chosen index."""
    print(f"\n{question}")
    for i, (label, detail) in enumerate(options, start=1):
        mark = " (recommended)" if i - 1 == default_idx else ""
        print(f"  {i}. {label}{mark}")
        if detail:
            print(f"     {detail}")
    if assume_yes:
        print(f"-> {options[default_idx][0]} (auto)")
        return default_idx
    while True:
        try:
            a = input(f"Choice [1-{len(options)}, default {default_idx + 1}]: ").strip()
        except EOFError:
            return default_idx
        if not a:
            return default_idx
        if a.isdigit() and 1 <= int(a) <= len(options):
            return int(a) - 1


def human_mb(mb: int) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"


def progress_bar(done: int, total: int, label: str, width: int = 34) -> None:
    if total <= 0:
        sys.stdout.write(f"\r    {label[:60]:<60}")
    else:
        frac = min(1.0, done / total)
        filled = int(width * frac)
        sys.stdout.write(f"\r    [{'#' * filled}{'.' * (width - filled)}] "
                         f"{frac * 100:5.1f}%  {done / 2**30:.2f}/{total / 2**30:.2f} GB  "
                         f"{label[:22]:<22}")
    sys.stdout.flush()


def run(cmd: list, **kw) -> int:
    print(f"    $ {' '.join(str(c) for c in cmd)}")
    try:
        return subprocess.call(cmd, **kw)
    except FileNotFoundError:
        print(f"    {BAD} command not found: {cmd[0]}")
        return 127


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def check_python() -> bool:
    v = sys.version_info
    print(f"  Python {v.major}.{v.minor}.{v.micro} — {sys.executable}")
    if v < (3, 9):
        print(f"  {BAD} Python 3.9 or newer is required.")
        return False
    if v >= (3, 14):
        print(f"  {WARN} Python {v.major}.{v.minor} is very new; some wheels "
              "(faster-whisper, sounddevice) may not have binaries for it yet. "
              "Python 3.11 or 3.12 is the safest choice.")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    print(f"  {OK if in_venv else WARN} virtual environment: "
          f"{'yes' if in_venv else 'no (installing into the system Python)'}")
    return True


def missing_packages() -> list:
    missing = []
    for mod, pkg in (("faster_whisper", "faster-whisper"), ("sounddevice", "sounddevice"),
                     ("numpy", "numpy"), ("requests", "requests")):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            missing.append(pkg)
    return missing


def install_packages(pkgs: list) -> bool:
    if not pkgs:
        return True
    print(f"  installing: {', '.join(pkgs)}")
    rc = run([sys.executable, "-m", "pip", "install", "--upgrade"] + pkgs)
    if rc != 0:
        print(f"  {BAD} pip failed (exit {rc}).")
        return False
    return True


def step_packages(args) -> bool:
    missing = missing_packages()
    if not missing:
        print(f"  {OK} all Python packages present")
        return True
    print(f"  {WARN} missing: {', '.join(missing)}")
    if args.check:
        return False
    if not ask("  Install them now with pip?", True, args.yes):
        return False
    return install_packages(missing)


def step_gpu(args, cfg: dict) -> None:
    gpus = wf_cuda.gpu_names()
    if not gpus:
        print(f"  {WARN} no NVIDIA GPU detected (nvidia-smi not available).")
        print("      Dictation will run on the CPU — that works fine, it is just slower")
        print("      (roughly half of real time for large-v3 on a modern CPU).")
        cfg["asr_device"] = "cpu"
        return
    for g in gpus:
        print(f"  {OK} GPU: {g}")
    have = bool(wf_cuda.candidate_dirs())
    if have:
        print(f"  {OK} CUDA runtime libraries already installed")
        cfg["asr_device"] = "auto"
        return
    print(f"  {WARN} the CUDA libraries faster-whisper needs (cuBLAS, cuDNN) are not installed.")
    print(f"      They add about 1.5 GB and make transcription several times faster.")
    if args.check:
        return
    if ask("  Install the CUDA libraries now?", True, args.yes):
        if install_packages(PIP_CUDA):
            wf_cuda.setup()
            cfg["asr_device"] = "auto"
            print(f"  {OK} GPU acceleration enabled (asr_device = auto)")
            return
    print("      Skipping — running on CPU. Re-run this setup later to enable the GPU.")
    cfg["asr_device"] = "cpu"


def winget_available() -> bool:
    return shutil.which("winget") is not None


def step_ollama(args, cfg: dict) -> bool:
    exe = wf_ollama.ollama_exe()
    if exe:
        print(f"  {OK} Ollama found: {exe}")
    else:
        print(f"  {WARN} Ollama is not installed. It runs the small language model that turns")
        print("      raw speech into clean, punctuated text (about 4.5 GB with its model).")
        print("      Without it dictation still works — you just get the raw transcript.")
        if args.check:
            return False
        if winget_available():
            if ask("  Install Ollama now via winget?", True, args.yes):
                run(["winget", "install", "--id", "Ollama.Ollama", "-e",
                     "--accept-source-agreements", "--accept-package-agreements"])
                exe = wf_ollama.ollama_exe()
        else:
            print(f"  {WARN} winget is not available on this system.")
        if not exe:
            print("      Install it by hand from  https://ollama.com/download/windows")
            print("      then re-run:  python wf_setup.py")
            return False
        print(f"  {OK} Ollama installed: {exe}")

    url = cfg["ollama_url"]
    if wf_ollama.is_up(url):
        print(f"  {OK} Ollama is running at {url}")
        return True
    print(f"  ... {url} is not answering; starting Ollama")
    if wf_ollama.ensure_running(cfg, log=lambda m: print(f"      {m}")):
        print(f"  {OK} Ollama is running at {url}")
        return True
    print(f"  {BAD} could not start Ollama. Start it from the Start menu and re-run setup.")
    return False


def step_llm_model(args, cfg: dict) -> bool:
    url, model = cfg["ollama_url"], cfg["llm_model"]
    if not wf_ollama.is_up(url):
        print(f"  {BAD} Ollama is not running — skipping the cleanup model.")
        return False
    if wf_ollama.has_model(url, model):
        print(f"  {OK} cleanup model {model} is installed")
        return True
    print(f"  {WARN} cleanup model {model} is not installed (~{human_mb(LLM_MODEL_MB)}).")
    print("      This is the model that fixes punctuation, capitalisation and removes")
    print("      filler words. It runs locally; nothing is sent anywhere.")
    if args.check:
        return False
    if not ask(f"  Download {model} now?", True, args.yes):
        print("      Skipped — dictation will insert the raw transcript until it is installed.")
        return False

    print(f"  downloading {model} ...")
    state = {"last": 0.0}

    def prog(status, done, total):
        # Ollama emits a status line per layer; throttle redraws so a slow console
        # doesn't become the bottleneck for the download itself.
        now = time.time()
        if now - state["last"] > 0.2 or status in ("success",) or status.startswith("error"):
            state["last"] = now
            progress_bar(done, total, status)

    ok = wf_ollama.pull(url, model, progress=prog)
    print()
    if ok:
        print(f"  {OK} {model} installed")
    else:
        print(f"  {BAD} download failed. Try manually:  ollama pull {model}")
    return ok


def step_asr_model(args, cfg: dict) -> bool:
    try:
        from faster_whisper.utils import download_model
    except Exception as e:  # noqa: BLE001
        print(f"  {BAD} faster-whisper is not importable ({e!r}); finish step 1 first.")
        return False

    if args.check:
        name = cfg["asr_model"]
        if _asr_cached(name):
            print(f"  {OK} speech model {name} is downloaded")
            return True
        print(f"  {WARN} speech model {name} is not downloaded yet")
        return False

    gpus = bool(wf_cuda.gpu_names())
    default_idx = 0 if gpus else 1
    idx = choose("  Which speech-recognition model should wisprflow use?",
                 [(f"{n}  (~{human_mb(mb)})", d) for n, mb, d in ASR_MODELS],
                 default_idx=default_idx, assume_yes=args.yes)
    name, mb, _ = ASR_MODELS[idx]
    cfg["asr_model"] = name

    if _asr_cached(name):
        print(f"  {OK} {name} is already downloaded")
        return True
    print(f"  {name} will be downloaded (~{human_mb(mb)}) to your Hugging Face cache")
    print(f"      ({os.environ.get('HF_HOME') or Path.home() / '.cache' / 'huggingface'})")
    if not ask(f"  Download {name} now?", True, args.yes):
        print("      Skipped — the daemon will download it on first use instead.")
        return False
    print(f"  downloading {name} — this can take several minutes ...")
    try:
        path = download_model(name)
        print(f"  {OK} {name} downloaded to {path}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  {BAD} download failed: {e!r}")
        print("      Check your internet connection and re-run:  python wf_setup.py")
        return False


def _asr_cached(name: str) -> bool:
    """True if the model is already in the local Hugging Face cache (no network call)."""
    try:
        from faster_whisper.utils import download_model
        download_model(name, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def step_microphone(args, cfg: dict) -> bool:
    try:
        import sounddevice as sd
    except Exception as e:  # noqa: BLE001
        print(f"  {BAD} sounddevice not importable ({e!r})")
        return False
    try:
        devices = sd.query_devices()
        default_in = sd.default.device[0]
    except Exception as e:  # noqa: BLE001
        print(f"  {BAD} could not query audio devices: {e!r}")
        return False
    inputs = [(i, d["name"]) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    if not inputs:
        print(f"  {BAD} no microphone found. Plug one in and re-run setup.")
        return False
    print(f"  {OK} {len(inputs)} input device(s) found")
    if isinstance(default_in, int) and 0 <= default_in < len(devices):
        print(f"      default microphone: {devices[default_in]['name']}")
    if args.check or args.yes:
        return True
    if ask("  Record 3 seconds to check the microphone level?", True, False):
        _mic_test(cfg)
    return True


def _mic_test(cfg: dict) -> None:
    import numpy as np
    import sounddevice as sd
    sr = int(cfg["sample_rate"])
    print("      speak now ...")
    try:
        rec = sd.rec(int(3 * sr), samplerate=sr, channels=1, dtype="float32")
        sd.wait()
    except Exception as e:  # noqa: BLE001
        print(f"      {BAD} recording failed: {e!r}")
        return
    rms = float(np.sqrt(np.mean(rec ** 2)))
    peak = float(np.max(np.abs(rec)))
    print(f"      level: RMS {rms:.4f}, peak {peak:.3f}   "
          f"(speech threshold is {cfg['vad_rms_threshold']})")
    if peak < 0.01:
        print(f"      {BAD} almost silent — check that the right microphone is the Windows "
              "default and that its level is up.")
    elif rms < cfg["vad_rms_threshold"]:
        print(f"      {WARN} quieter than the auto-stop threshold. If you enable auto_stop, "
              f"lower vad_rms_threshold to about {max(0.003, rms * 0.6):.3f}.")
    else:
        print(f"      {OK} microphone level looks good")


def step_hotkey(args, cfg: dict) -> None:
    import wf_hotkey
    current = cfg.get("hotkey", "ctrl+alt+space")
    print(f"  current hotkey: {wf_hotkey.describe(current)}")
    if args.check or args.yes:
        return
    options = [
        ("Ctrl + Alt + Space", "a normal global hotkey — reliable everywhere"),
        ("Double-tap Right Ctrl", "Wispr-Flow style: no combination to remember"),
        ("Ctrl + Shift + D", "if Ctrl+Alt+Space is taken by another app"),
        ("Keep the current setting", ""),
    ]
    idx = choose("  How do you want to start and stop dictation?", options, 0, args.yes)
    cfg["hotkey"] = ["ctrl+alt+space", "doubletap:rctrl", "ctrl+shift+d",
                     current][idx]
    print(f"  {OK} hotkey: {wf_hotkey.describe(cfg['hotkey'])}")


def startup_shortcut() -> Path:
    return (Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
            / "Microsoft/Windows/Start Menu/Programs/Startup/wisprflow.lnk")


def step_autostart(args) -> None:
    lnk = startup_shortcut()
    if lnk.exists():
        print(f"  {OK} starts automatically at sign-in")
        return
    print(f"  {WARN} wisprflow does not start automatically at sign-in.")
    if args.check:
        return
    if not ask("  Start wisprflow automatically when you sign in?", True, args.yes):
        return
    if create_startup_shortcut():
        print(f"  {OK} added to your Startup folder")
    else:
        print(f"  {WARN} could not create the shortcut; run install.ps1 instead")


def create_startup_shortcut() -> bool:
    """Create the Startup .lnk via PowerShell's WScript.Shell COM object."""
    if sys.platform != "win32":
        return False
    app = wf_paths.app_dir()
    pyw = Path(sys.executable).with_name("pythonw.exe")
    target = str(pyw if pyw.is_file() else sys.executable)
    lnk = startup_shortcut()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    icon = app / "assets" / "wisprflow.ico"

    def q(p) -> str:
        """PowerShell single-quoted literal: an apostrophe is escaped by doubling it.
        Without this, a user folder like C:\\Users\\O'Brien breaks the whole command."""
        return "'" + str(p).replace("'", "''") + "'"

    # Built outside the f-string: nested same-type quotes and backslashes inside an
    # f-string expression are a SyntaxError before Python 3.12, and this must run on 3.9.
    daemon_arg = '"' + str(app / "wf_daemon.py") + '"'

    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$l = $s.CreateShortcut({q(lnk)}); "
        f"$l.TargetPath = {q(target)}; "
        f"$l.Arguments = {q(daemon_arg)}; "
        f"$l.WorkingDirectory = {q(app)}; "
        "$l.Description = 'local-wisprflow dictation daemon'; "
        + (f"$l.IconLocation = {q(icon)}; " if icon.is_file() else "")
        + "$l.Save()"
    )
    rc = subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-Command", ps], creationflags=CREATE_NO_WINDOW)
    return rc == 0 and lnk.exists()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Guided setup for local-wisprflow (Windows)")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="accept every recommended default without asking")
    ap.add_argument("--check", action="store_true",
                    help="report status only; install and change nothing")
    args = ap.parse_args()

    hr("local-wisprflow — setup")
    print("Fully local dictation: speak, and clean punctuated text is typed into whatever")
    print("app you are using. Nothing leaves this machine.")
    if args.check:
        print("\n(check mode — nothing will be installed or changed)")

    wf_paths.ensure_dirs()
    cfg = wf_paths.load_config()
    total = 8

    step(1, total, "Python and packages")
    if not check_python():
        return 1
    packages_ok = step_packages(args)

    step(2, total, "GPU acceleration")
    step_gpu(args, cfg)

    step(3, total, "Ollama (local LLM runtime)")
    ollama_ok = step_ollama(args, cfg)

    step(4, total, "Cleanup model")
    llm_ok = step_llm_model(args, cfg) if ollama_ok else False

    step(5, total, "Speech-recognition model")
    asr_ok = step_asr_model(args, cfg) if packages_ok else False

    step(6, total, "Microphone")
    mic_ok = step_microphone(args, cfg) if packages_ok else False

    step(7, total, "Hotkey")
    step_hotkey(args, cfg)

    step(8, total, "Start at sign-in")
    step_autostart(args)

    if not args.check:
        path = wf_paths.save_config(cfg)
        print(f"\n  {OK} configuration saved to {path}")

    hr("Summary")
    rows = [("Python packages", packages_ok), ("Speech model", asr_ok),
            ("Ollama", ollama_ok), ("Cleanup model", llm_ok), ("Microphone", mic_ok)]
    for label, ok in rows:
        print(f"  {OK if ok else WARN} {label}")

    ready = packages_ok and asr_ok and mic_ok
    if ready and llm_ok:
        print("\nEverything is ready.")
    elif ready:
        print("\nDictation will work, but WITHOUT cleanup — you will get the raw transcript")
        print("(no punctuation polish). Re-run this setup once Ollama and its model are in place.")
    else:
        print("\nSetup is incomplete — see the [!] items above and re-run:  python wf_setup.py")

    if not args.check:
        import wf_hotkey
        print("\nNext steps:")
        print("  1. Start it:            wf-start.cmd     (or double-click 'Start wisprflow')")
        print(f"  2. Press {wf_hotkey.describe(cfg.get('hotkey', ''))} and speak; press it again to stop.")
        print("  3. The text is typed into whatever window is focused.")
        print("\n  Diagnostics:  python wf_doctor.py")
        print(f"  Config file:  {wf_paths.config_path()}")
        print(f"  Log file:     {wf_paths.log_dir() / 'daemon.log'}")
    return 0 if ready else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130)
