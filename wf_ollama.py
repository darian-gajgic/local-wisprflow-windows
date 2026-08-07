#!/usr/bin/env python3
"""Ollama discovery, lifecycle and model management for the Windows port.

The Linux original delegated all of this to systemd: a `wf-cleanup-llm.service` unit started a
second, isolated Ollama on port 11435 with its own models directory and an f16 KV cache,
because the *system* Ollama on that machine was configured with `OLLAMA_KV_CACHE_TYPE=q4_0`
(needed to keep an unrelated 14B model resident) and a q4_0 KV cache garbles small models like
gemma3:4b.

Windows has no systemd and, more importantly, no such conflict: a stock Ollama install uses an
f16 KV cache already, so the port talks to the normal instance on **11434** by default. Setting
``"ollama_isolated": true`` restores the original two-instance design — own port, own models
directory, own cache setting — for users who have tuned Ollama globally for some other workload.

Either way the daemon can *start* Ollama on demand (``"ollama_manage": true``), which replaces
the systemd dependency ordering that used to guarantee the cleanup LLM was up before the daemon.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import wf_paths

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
DETACHED_PROCESS = 0x00000008 if sys.platform == "win32" else 0

ISOLATED_PORT = 11435


def _flags() -> int:
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# Locating ollama.exe
# ---------------------------------------------------------------------------
def ollama_exe() -> str | None:
    """Absolute path to ollama.exe, or None. The Windows installer is per-user and does not
    always refresh PATH in already-running processes, so the well-known locations are
    checked too."""
    found = shutil.which("ollama")
    if found:
        return found
    candidates = []
    for var in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(var)
        if not base:
            continue
        candidates += [Path(base) / "Programs" / "Ollama" / "ollama.exe",
                       Path(base) / "Ollama" / "ollama.exe"]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def installed() -> bool:
    return ollama_exe() is not None


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------
def is_up(url: str, timeout: float = 2.0) -> bool:
    import requests
    try:
        r = requests.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def list_models(url: str, timeout: float = 5.0) -> list[str]:
    import requests
    try:
        r = requests.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def has_model(url: str, model: str) -> bool:
    """True if `model` is present. Ollama reports 'gemma3:4b' but a user may configure
    'gemma3' (which resolves to ':latest'), so compare both forms."""
    want = model if ":" in model else f"{model}:latest"
    names = list_models(url)
    return any(n == want or n == model or n.split(":")[0] == model for n in names)


def env_for(cfg: dict) -> dict:
    """Environment for a managed Ollama, mirroring the original's systemd unit."""
    env = os.environ.copy()
    url = cfg.get("ollama_url", "http://127.0.0.1:11434")
    p = urlparse(url)
    env["OLLAMA_HOST"] = f"{p.hostname or '127.0.0.1'}:{p.port or 11434}"
    env["OLLAMA_KEEP_ALIVE"] = str(cfg.get("llm_keep_alive", "5m"))
    if cfg.get("ollama_isolated"):
        # Fully isolated instance: its own models directory and an explicit f16 KV cache, so
        # nothing about the user's main Ollama configuration can affect cleanup quality.
        models = wf_paths.isolated_models_dir()
        models.mkdir(parents=True, exist_ok=True)
        env["OLLAMA_MODELS"] = str(models)
        env["OLLAMA_KV_CACHE_TYPE"] = "f16"
        env["OLLAMA_FLASH_ATTENTION"] = "0"
    return env


def start_server(cfg: dict, log=print, wait_s: int = 30) -> bool:
    """Start `ollama serve` and wait for it to answer. Returns True when it is up."""
    exe = ollama_exe()
    url = cfg.get("ollama_url", "http://127.0.0.1:11434")
    if not exe:
        log("ollama: not installed — run  install.ps1  or  wf_setup.py  to install it")
        return False
    log(f"ollama: starting {exe} serve -> {url}")
    try:
        subprocess.Popen([exe, "serve"], env=env_for(cfg),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=_flags() | DETACHED_PROCESS)
    except Exception as e:  # noqa: BLE001
        log(f"ollama: could not start: {e!r}")
        return False
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if is_up(url, timeout=1.5):
            log("ollama: ready")
            return True
        time.sleep(0.5)
    log(f"ollama: did not become ready within {wait_s}s")
    return False


def ensure_running(cfg: dict, log=print) -> bool:
    """Make sure the configured Ollama answers, starting it if allowed. Never raises."""
    url = cfg.get("ollama_url", "http://127.0.0.1:11434")
    if is_up(url):
        return True
    if not cfg.get("ollama_manage", True):
        log(f"ollama: {url} is not reachable and ollama_manage is off")
        return False
    return start_server(cfg, log=log, wait_s=int(cfg.get("autostart_ollama_wait_s", 30)))


# ---------------------------------------------------------------------------
# Pulling models
# ---------------------------------------------------------------------------
def pull(url: str, model: str, progress=None, timeout: int = 3600) -> bool:
    """Stream `POST /api/pull`, reporting progress as (status, completed, total).

    Ollama sends one JSON object per line; download lines carry byte counters, and the
    stream ends with {"status": "success"}. An error is reported in an "error" field with a
    200 status code, so the body has to be inspected rather than just the HTTP result.
    """
    import requests
    ok = False
    try:
        with requests.post(f"{url.rstrip('/')}/api/pull",
                           json={"model": model, "stream": True},
                           stream=True, timeout=timeout) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("error"):
                    if progress:
                        progress(f"error: {msg['error']}", 0, 0)
                    return False
                status = msg.get("status", "")
                if progress:
                    progress(status, int(msg.get("completed") or 0), int(msg.get("total") or 0))
                if status == "success":
                    ok = True
    except Exception as e:  # noqa: BLE001
        if progress:
            progress(f"error: {e!r}", 0, 0)
        return False
    return ok


def unload(url: str, model: str) -> None:
    """Ask Ollama to drop `model` from memory now (keep_alive=0)."""
    import requests
    try:
        requests.post(f"{url.rstrip('/')}/api/generate",
                      json={"model": model, "prompt": "", "keep_alive": 0}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def loaded_models(url: str, timeout: float = 2.0) -> list[dict]:
    """`/api/ps` — which models are resident, and how much VRAM each holds."""
    import requests
    try:
        r = requests.get(f"{url.rstrip('/')}/api/ps", timeout=timeout)
        return r.json().get("models", []) or []
    except Exception:  # noqa: BLE001
        return []
