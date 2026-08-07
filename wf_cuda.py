#!/usr/bin/env python3
"""Make the pip-installed CUDA libraries findable before faster-whisper is imported.

The Linux original solved this in `wf-run` with one line::

    export LD_LIBRARY_PATH="$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib:..."

Windows has no LD_LIBRARY_PATH, and since Python 3.8 it deliberately ignores PATH when
resolving dependencies of extension modules — the only supported mechanism is
``os.add_dll_directory()``. So this must run **before** ``import faster_whisper`` (which
loads ctranslate2, which links cuBLAS/cuDNN); otherwise CUDA silently fails to initialise and
the daemon quietly falls back to CPU.

Note the layout difference: the NVIDIA wheels put shared objects in ``nvidia/<pkg>/lib`` on
Linux but DLLs in ``nvidia/<pkg>/bin`` on Windows. Both are added, so this is harmless either way.
"""
from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

# CUDA packages faster-whisper/ctranslate2 needs at runtime, in load order.
_CUDA_PKGS = ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc")

_added: list[str] = []
_handles: list = []   # keep the DLL-directory cookies alive: dropping one un-registers it


def candidate_dirs() -> list[Path]:
    roots: list[Path] = []
    for key in ("purelib", "platlib"):
        p = sysconfig.get_paths().get(key)
        if p:
            roots.append(Path(p))
    # a venv's site-packages is not always what sysconfig reports (e.g. under an embedded
    # interpreter), so also walk back from this file: <venv>/Lib/site-packages/...
    here = Path(__file__).resolve().parent
    for extra in (here / ".venv" / "Lib" / "site-packages",
                  here / "venv" / "Lib" / "site-packages"):
        roots.append(extra)

    out: list[Path] = []
    seen = set()
    for root in roots:
        for pkg in _CUDA_PKGS:
            for sub in ("bin", "lib"):
                d = root / "nvidia" / pkg / sub
                if d.is_dir() and str(d) not in seen:
                    seen.add(str(d))
                    out.append(d)
    return out


def setup(log=None) -> list[str]:
    """Register the CUDA DLL directories. Returns the list actually added."""
    global _added
    if _added:
        return _added
    if sys.platform != "win32":
        return []
    for d in candidate_dirs():
        try:
            _handles.append(os.add_dll_directory(str(d)))
            _added.append(str(d))
        except OSError as e:
            if log:
                log(f"cuda: could not add DLL directory {d}: {e!r}")
    # Some ctranslate2 builds shell out to helpers that DO read PATH; harmless to also set it.
    if _added:
        os.environ["PATH"] = os.pathsep.join(_added + [os.environ.get("PATH", "")])
        if log:
            log(f"cuda: registered {len(_added)} DLL director{'y' if len(_added) == 1 else 'ies'}")
    elif log:
        log("cuda: no NVIDIA wheel directories found — GPU mode will fall back to CPU")
    return _added


def have_nvidia_gpu() -> bool:
    """True when nvidia-smi reports at least one GPU. Used only for setup/diagnostics."""
    import subprocess
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=_no_window())
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def gpu_names() -> list[str]:
    import subprocess
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                            "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=_no_window())
        return [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def _no_window() -> int:
    """CREATE_NO_WINDOW, so console helpers don't flash a black box on a GUI-less daemon."""
    return 0x08000000 if sys.platform == "win32" else 0
