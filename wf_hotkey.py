#!/usr/bin/env python3
"""Global hotkeys for local-wisprflow on Windows.

Replaces the Linux original's `wf-keylistener.py`, which read raw evdev devices (and needed
membership in the `input` group plus a udev rule) to catch a vendor key GNOME could not bind.
Windows gives us two better mechanisms, and this module supports both in a single thread:

* ``"ctrl+alt+space"``  — a normal system-wide hotkey via **RegisterHotKey**. Cheap, reliable,
  and the OS enforces exclusivity: if another app already owns the combination, registration
  fails loudly instead of silently doing nothing.
* ``"doubletap:rctrl"`` — tap Right Ctrl twice quickly, via a **WH_KEYBOARD_LL** hook. This is
  the Wispr-Flow-style trigger: no combination to remember and no key taken away from other
  apps, because the hook observes and never swallows the key.

Both need a message loop on the thread that created them, so everything lives on one
dedicated thread here.

The evdev listener's *duplicate-collapse* problem does not exist on Windows — RegisterHotKey
delivers exactly one WM_HOTKEY per press (with MOD_NOREPEAT, none at all while held) — but the
double-tap detector has the mirror-image concern and handles it: key auto-repeat is ignored,
any other key in between cancels the sequence, and keystrokes wisprflow itself synthesized
(tagged with WF_EXTRA_INFO) are skipped so a paste chord can't trigger a dictation.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes

from wf_input import WF_EXTRA_INFO

IS_WINDOWS = sys.platform == "win32"

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WH_KEYBOARD_LL = 13
HC_ACTION = 0

MODIFIER_NAMES = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN,
}

# Keys that can be double-tapped. Modifiers are the useful ones: they are already "spare"
# (a lone Ctrl press does nothing in most apps), so double-tapping one steals nothing.
DOUBLETAP_KEYS = {
    "rctrl": 0xA3, "lctrl": 0xA2,
    "rshift": 0xA1, "lshift": 0xA0,
    "ralt": 0xA5, "lalt": 0xA4,
    "rwin": 0x5C, "lwin": 0x5B,
    "capslock": 0x14, "scrolllock": 0x91, "pause": 0x13,
}

VK_NAMES = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "scrolllock": 0x91, "pause": 0x13, "printscreen": 0x2C,
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA,
    "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
}
VK_NAMES.update({f"f{i}": 0x6F + i for i in range(1, 25)})          # F1..F24
VK_NAMES.update({f"num{i}": 0x60 + i for i in range(0, 10)})        # numpad 0..9


class HotkeyError(ValueError):
    pass


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+space' -> (modifier mask, virtual-key code). Raises HotkeyError."""
    mods, vk = 0, None
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    if not parts:
        raise HotkeyError("empty hotkey")
    for p in parts:
        if p in MODIFIER_NAMES:
            mods |= MODIFIER_NAMES[p]
        elif vk is not None:
            raise HotkeyError(f"{spec!r}: more than one non-modifier key")
        elif p in VK_NAMES:
            vk = VK_NAMES[p]
        elif len(p) == 1 and (p.isalpha() or p.isdigit()):
            vk = ord(p.upper())
        else:
            raise HotkeyError(f"{spec!r}: unknown key {p!r}")
    if vk is None:
        raise HotkeyError(f"{spec!r}: no non-modifier key (a hotkey cannot be modifiers alone)")
    if not mods:
        raise HotkeyError(f"{spec!r}: needs at least one modifier, or use 'doubletap:<key>'")
    return mods, vk


def parse_doubletap(spec: str) -> int | None:
    """'doubletap:rctrl' -> virtual-key code; None if `spec` is not a doubletap spec."""
    s = str(spec).strip().lower()
    if not s.startswith("doubletap:"):
        return None
    key = s.split(":", 1)[1].strip()
    if key not in DOUBLETAP_KEYS:
        raise HotkeyError(f"{spec!r}: cannot double-tap {key!r}; "
                          f"choose one of {sorted(DOUBLETAP_KEYS)}")
    return DOUBLETAP_KEYS[key]


# =======================================================================================
# ctypes plumbing
# =======================================================================================
if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = wintypes.LPARAM
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    # Declaring these matters on 64-bit: ctypes defaults an undeclared restype to C `int`,
    # which TRUNCATES the returned HHOOK/HMODULE pointer — the hook would then appear to
    # install and never fire, or fail to uninstall.
    _user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HMODULE, wintypes.DWORD)
    _user32.SetWindowsHookExW.restype = ctypes.c_void_p
    _user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
    _user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    _user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                       wintypes.WPARAM, wintypes.LPARAM)
    _user32.CallNextHookEx.restype = LRESULT
    _user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int,
                                       wintypes.UINT, wintypes.UINT)
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.UnregisterHotKey.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT)
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM)
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
else:
    _user32 = _kernel32 = None
    HOOKPROC = None


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", wintypes.WPARAM)]


class _DoubleTap:
    """State machine for 'tap a key twice quickly'.

    Counts on key-UP: a tap is a complete down→up, so auto-repeat (which produces a stream of
    downs with no up) can never look like a second tap. Any *other* key going down resets the
    sequence, so Ctrl+C followed by Ctrl+V does not read as a double-tapped Ctrl.
    """

    def __init__(self, vk: int, window_s: float, fire) -> None:
        self.vk, self.window, self.fire = vk, window_s, fire
        self.last_up = 0.0
        self.down = False

    def on_event(self, vk: int, is_down: bool) -> None:
        now = time.monotonic()
        if vk != self.vk:
            if is_down:
                self.last_up = 0.0      # a different key interrupts the sequence
            return
        if is_down:
            self.down = True
            return
        if not self.down:               # an up we never saw the down for — ignore
            return
        self.down = False
        if self.last_up and (now - self.last_up) <= self.window:
            self.last_up = 0.0          # consume both taps; a third tap starts fresh
            self.fire()
        else:
            self.last_up = now


class HotkeyService:
    """Runs RegisterHotKey hotkeys and/or double-tap detection on one message-loop thread.

    Bindings are ``[(spec, callback), ...]``. Callbacks run on a short-lived worker thread,
    never on the message-loop thread: a WH_KEYBOARD_LL hook that takes longer than the
    system's LowLevelHooksTimeout (300 ms by default) is silently uninstalled by Windows,
    which would kill the hotkey with no error anywhere.
    """

    def __init__(self, bindings, log=print, doubletap_ms: int = 400) -> None:
        self.bindings = list(bindings)
        self.log = log
        self.doubletap_window_s = max(0.12, min(1.5, doubletap_ms / 1000.0))
        self.thread: threading.Thread | None = None
        self.thread_id = 0
        self._hook = None
        self._hookproc = None          # must outlive the hook: Windows holds a raw pointer
        self._taps: list[_DoubleTap] = []
        self._byid: dict[int, object] = {}
        self._ready = threading.Event()
        self.errors: list[str] = []

    # -- public ---------------------------------------------------------------
    def start(self) -> bool:
        if not IS_WINDOWS:
            raise RuntimeError("wf_hotkey requires Windows")
        self.thread = threading.Thread(target=self._run, name="wf-hotkey", daemon=True)
        self.thread.start()
        self._ready.wait(timeout=5)
        return not self.errors

    def stop(self) -> None:
        if self.thread_id:
            _user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    # -- internals ------------------------------------------------------------
    def _dispatch(self, cb) -> None:
        threading.Thread(target=self._safe, args=(cb,), daemon=True).start()

    def _safe(self, cb) -> None:
        try:
            cb()
        except Exception as e:  # noqa: BLE001
            self.log(f"hotkey callback error: {e!r}")

    def _run(self) -> None:
        self.thread_id = _kernel32.GetCurrentThreadId()
        registered = []
        for i, (spec, cb) in enumerate(self.bindings, start=1):
            if not spec:
                continue
            try:
                tap_vk = parse_doubletap(spec)
                if tap_vk is not None:
                    self._taps.append(_DoubleTap(tap_vk, self.doubletap_window_s,
                                                 lambda cb=cb: self._dispatch(cb)))
                    self.log(f"hotkey: double-tap {spec.split(':', 1)[1]} -> armed")
                    continue
                mods, vk = parse_hotkey(spec)
                if not _user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                    err = ctypes.get_last_error()
                    msg = (f"hotkey {spec!r} could not be registered (WinError {err}"
                           f"{'; another application already owns it' if err == 1409 else ''})")
                    self.errors.append(msg)
                    self.log(msg)
                    continue
                registered.append(i)
                self._byid[i] = cb
                self.log(f"hotkey: {spec} -> registered")
            except HotkeyError as e:
                self.errors.append(str(e))
                self.log(f"hotkey: {e}")

        if self._taps and not self._install_hook():
            self.errors.append("could not install the low-level keyboard hook")

        self._ready.set()
        try:
            self._pump()
        finally:
            for i in registered:
                _user32.UnregisterHotKey(None, i)
            if self._hook:
                _user32.UnhookWindowsHookEx(self._hook)

    def _install_hook(self) -> bool:
        def proc(ncode, wparam, lparam):
            # Fast path first: this runs on every keystroke system-wide.
            if ncode == HC_ACTION:
                try:
                    kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if kb.dwExtraInfo != WF_EXTRA_INFO:   # skip keys wisprflow itself sent
                        is_down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                        if is_down or wparam in (WM_KEYUP, WM_SYSKEYUP):
                            for t in self._taps:
                                t.on_event(kb.vkCode, is_down)
                except Exception:  # noqa: BLE001
                    pass          # never let an exception escape into the Windows hook chain
            return _user32.CallNextHookEx(None, ncode, wparam, lparam)

        self._hookproc = HOOKPROC(proc)      # keep a strong reference for the hook's lifetime
        self._hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hookproc,
                                               _kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            self.log(f"hotkey: SetWindowsHookEx failed (WinError {ctypes.get_last_error()})")
            return False
        return True

    def _pump(self) -> None:
        msg = wintypes.MSG()
        while True:
            r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):        # WM_QUIT, or an error
                break
            if msg.message == WM_HOTKEY:
                cb = self._byid.get(msg.wParam)
                if cb:
                    self._dispatch(cb)
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))


def describe(spec: str) -> str:
    """Human-readable form of a hotkey spec, for the tray tooltip and setup output."""
    s = str(spec or "").strip()
    tap = s.lower().startswith("doubletap:")
    if tap:
        return f"double-tap {s.split(':', 1)[1].replace('rctrl', 'Right Ctrl').replace('lctrl', 'Left Ctrl')}"
    return " + ".join(p.strip().capitalize() for p in s.split("+") if p.strip())
