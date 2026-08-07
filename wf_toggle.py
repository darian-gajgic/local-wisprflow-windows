#!/usr/bin/env python3
"""Tiny client for the wisprflow daemon — the Windows counterpart of the original `wf-toggle`.

Usage:  python wf_toggle.py [toggle|start|stop|cancel|status|info|ping|note|lang|meeting|shutdown]

The default command is `toggle`. The original had to stay standard-library-only so a desktop
keyboard shortcut could run it without the venv; here the daemon registers its own global
hotkey (wf_hotkey), so this exists for scripting, Stream Deck / AutoHotkey bindings, and
debugging. It still uses nothing but the standard library.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wf_ipc  # noqa: E402

COMMANDS = ("toggle", "start", "stop", "cancel", "status", "info", "ping",
            "note", "lang", "meeting", "shutdown")


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "toggle").lower()
    if cmd in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if cmd not in COMMANDS:
        sys.stderr.write(f"wf-toggle: unknown command {cmd!r}; expected one of "
                         f"{', '.join(COMMANDS)}\n")
        return 2
    try:
        print(wf_ipc.send(cmd))
        return 0
    except wf_ipc.NotRunning as e:
        sys.stderr.write(f"wf-toggle: {e}\n"
                         f"           start it with  wf-start.cmd\n")
        return 1
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"wf-toggle: {e!r}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
