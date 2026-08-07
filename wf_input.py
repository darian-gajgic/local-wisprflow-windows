#!/usr/bin/env python3
"""Text injection for local-wisprflow on Windows (Win32 SendInput + clipboard).

This replaces the Linux original's `wf_layout.py` + ydotool pair, and it is the one place
where the Windows port is genuinely *simpler* than the source.

On Linux, `ydotool type` emits raw evdev keycodes that the focused app re-interprets under
its own XKB layout, so typing "?" on a German QWERTZ keyboard produced "_". The original had
to load libxkbcommon, compile the user's keymap, and map every character to a
(keycode, shift-level) pair — and characters not physically on the keyboard (—, “, …) had to
be normalized away.

Win32 has no such problem: `SendInput` with `KEYEVENTF_UNICODE` carries the **UTF-16 code
unit itself**, which the target window receives as a WM_CHAR regardless of the active
keyboard layout. So typing is layout-independent by construction and every character —
including em dashes, curly quotes and emoji (sent as surrogate pairs) — arrives intact.

Known limits, in order of how often they bite:
  * **UIPI**: a non-elevated process cannot send input to an elevated window. If you dictate
    into an app running as Administrator, run wisprflow as Administrator too.
  * Some apps (a few games, remote-desktop clients) read raw scan codes and ignore
    WM_CHAR; there is no keyboard layout that can express arbitrary text for them anyway.
    Use `"inject_method": "paste"` there.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# Tag stamped into every event we synthesize, so our own low-level keyboard hook
# (wf_hotkey.py) can tell "wisprflow is typing" apart from a real key press — otherwise
# a paste chord we send could re-trigger the double-tap hotkey detector.
WF_EXTRA_INFO = 0x57465F31   # 'WF_1'

# --- SendInput constants ---------------------------------------------------------------
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RETURN, VK_TAB, VK_INSERT, VK_BACK = 0x5B, 0x0D, 0x09, 0x2D, 0x08

# Keys that live on the "extended" half of the keyboard; without the flag the target app
# can mistake Insert for Numpad-0 and Right-Alt for Left-Alt.
_EXTENDED = {VK_INSERT, VK_LWIN, 0x5C, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E, 0x91}

# Characters SendInput cannot express as a WM_CHAR — they are *keys*, not text.
_AS_KEY = {"\n": VK_RETURN, "\r": VK_RETURN, "\t": VK_TAB}

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


# =======================================================================================
# ctypes plumbing
# =======================================================================================
if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # importable on Linux for tests / linting; every call raises
    _user32 = _kernel32 = None

ULONG_PTR = wintypes.WPARAM   # pointer-sized unsigned, correct on both 32- and 64-bit


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _init_signatures() -> None:
    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
    _user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
    _user32.MapVirtualKeyW.restype = wintypes.UINT
    _user32.OpenClipboard.argtypes = (wintypes.HWND,)
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.argtypes = ()
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.argtypes = ()
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.GetClipboardData.argtypes = (wintypes.UINT,)
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
    _user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    _kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
    _kernel32.GlobalSize.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalSize.restype = ctypes.c_size_t
    # Window handles are pointers: leaving restype at ctypes' default (C int) truncates
    # them on 64-bit Windows, so these would silently return garbage.
    _user32.GetForegroundWindow.argtypes = ()
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _user32.GetWindowTextW.restype = ctypes.c_int


if IS_WINDOWS:
    _init_signatures()


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise RuntimeError("wf_input requires Windows")


# =======================================================================================
# Keyboard
# =======================================================================================
def _key_input(vk: int, scan: int, flags: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0,
                        dwExtraInfo=WF_EXTRA_INFO)
    return inp


def _vk_events(vk: int, down: bool) -> list:
    """Press/release a virtual key, carrying its scan code so scan-code readers see it too."""
    scan = _user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = 0 if down else KEYEVENTF_KEYUP
    if vk in _EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    return [_key_input(vk, scan, flags)]


def _unicode_events(ch: str) -> list:
    """Down+up events carrying `ch` as UTF-16 code units (two pairs for a surrogate pair)."""
    out = []
    units = ch.encode("utf-16-le")
    for i in range(0, len(units), 2):
        cu = units[i] | (units[i + 1] << 8)
        out.append(_key_input(0, cu, KEYEVENTF_UNICODE))
        out.append(_key_input(0, cu, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return out


def _send(events: list) -> int:
    if not events:
        return 0
    arr = (INPUT * len(events))(*events)
    n = _user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if n != len(events):
        err = ctypes.get_last_error()
        raise OSError(f"SendInput sent {n}/{len(events)} events (WinError {err}); "
                      "an elevated window will refuse input from a normal-privilege process")
    return n


def type_text(text: str, key_delay_ms: int = 1, chunk: int = 40) -> None:
    """Type `text` into the foreground window, character by character.

    Sent in small batches with an optional delay: a single 5000-event SendInput is accepted
    by Win32 but some apps (Electron editors, terminals) drop characters when the queue
    arrives faster than they drain it. `key_delay_ms: 0` disables pacing entirely.
    """
    _require_windows()
    batch: list = []
    delay = max(0, int(key_delay_ms)) / 1000.0

    def flush() -> None:
        nonlocal batch
        if batch:
            _send(batch)
            batch = []
            if delay:
                time.sleep(delay)

    for ch in text:
        vk = _AS_KEY.get(ch)
        if vk is not None:
            flush()                      # keep Enter in its own batch: apps often act on it
            _send(_vk_events(vk, True) + _vk_events(vk, False))
            if delay:
                time.sleep(delay)
            continue
        batch += _unicode_events(ch)
        if len(batch) >= chunk * 2:
            flush()
    flush()


# Physical key combinations, as virtual-key sequences. These are PHYSICAL keys, so they mean
# the same thing on every keyboard layout — which is exactly why paste is layout-safe.
PASTE_CHORDS = {
    "ctrl+v": [VK_CONTROL, 0x56],
    "ctrl+shift+v": [VK_CONTROL, VK_SHIFT, 0x56],   # most terminals
    "shift+insert": [VK_SHIFT, VK_INSERT],
}


def send_chord(vks: list) -> None:
    """Press `vks` in order, then release them in reverse (modifiers first, key last)."""
    _require_windows()
    events = []
    for vk in vks:
        events += _vk_events(vk, True)
    for vk in reversed(vks):
        events += _vk_events(vk, False)
    _send(events)


# =======================================================================================
# Clipboard
# =======================================================================================
def _open_clipboard(retries: int = 12, wait: float = 0.03) -> bool:
    """OpenClipboard, retrying: the clipboard is a single global resource and another app
    (a clipboard manager, the browser) may hold it for a few milliseconds."""
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(wait)
    return False


def set_clipboard_text(text: str) -> bool:
    _require_windows()
    if not _open_clipboard():
        return False
    try:
        _user32.EmptyClipboard()
        buf = text.encode("utf-16-le") + b"\x00\x00"
        h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(buf))
        if not h:
            return False
        ptr = _kernel32.GlobalLock(h)
        if not ptr:
            _kernel32.GlobalFree(h)
            return False
        ctypes.memmove(ptr, buf, len(buf))
        _kernel32.GlobalUnlock(h)
        if not _user32.SetClipboardData(CF_UNICODETEXT, h):
            _kernel32.GlobalFree(h)   # ownership only transfers to the OS on success
            return False
        return True
    finally:
        _user32.CloseClipboard()


def get_clipboard_text() -> str | None:
    """Current clipboard text, or None if the clipboard holds something else / is locked."""
    _require_windows()
    if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _open_clipboard():
        return None
    try:
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        ptr = _kernel32.GlobalLock(h)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def paste_text(text: str, chord: str = "ctrl+v", restore: bool = True,
               settle: float = 0.06) -> None:
    """Put `text` on the clipboard and send a paste chord, then restore the old clipboard.

    The restore is delayed: the target app reads the clipboard asynchronously after the
    chord, so putting the previous contents back immediately would paste the wrong thing.
    """
    _require_windows()
    previous = get_clipboard_text() if restore else None
    set_clipboard_text(text)
    time.sleep(settle)   # let clipboard managers register the new contents
    send_chord(PASTE_CHORDS.get(chord, PASTE_CHORDS["ctrl+v"]))
    if restore and previous is not None:
        time.sleep(0.35)
        set_clipboard_text(previous)


def foreground_window_title() -> str:
    """Title of the focused window — used only for diagnostics in wf_doctor."""
    if not IS_WINDOWS:
        return ""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return ""
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def is_elevated() -> bool:
    """True if this process runs elevated. A non-elevated process cannot inject input into
    an elevated window (UIPI), which is the usual cause of "typing does nothing in app X"."""
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False
