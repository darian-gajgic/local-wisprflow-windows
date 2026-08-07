#!/usr/bin/env python3
"""System-tray icon for local-wisprflow on Windows.

There is no Windows equivalent of `systemctl --user status wf-daemon`, so without this the
daemon is an invisible background process with no way to see whether it is alive, no way to
change NoteMode or language without the overlay, and no way to quit it short of Task Manager.
The tray icon is that missing control surface.

It is deliberately built on raw Shell_NotifyIcon via ctypes rather than pystray/Pillow: the
tray must never be a reason the installer fails, and every failure path here is caught by the
daemon, which logs it and carries on — dictation does not depend on the tray existing.

Implementation notes:
* The icon needs a window to receive its callback message, so a message-only window
  (HWND_MESSAGE) is created and pumped on this module's own thread.
* Explorer re-creates the notification area when it restarts (or crashes), silently dropping
  every icon. The documented fix is to listen for the "TaskbarCreated" broadcast and re-add —
  otherwise the tray icon just vanishes one day and never comes back.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

import wf_paths

IS_WINDOWS = sys.platform == "win32"

# --- Win32 constants ---------------------------------------------------------------------
WM_DESTROY, WM_COMMAND, WM_APP, WM_NULL, WM_QUIT = 0x0002, 0x0111, 0x8000, 0x0000, 0x0012
WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
WM_TRAY = WM_APP + 1

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x01, 0x02, 0x03

MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_GRAYED = 0x0000, 0x0800, 0x0008, 0x0001
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

IDI_APPLICATION = 32512
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
HWND_MESSAGE = -3
CW_USEDEFAULT = -0x80000000

# menu command ids
ID_TOGGLE, ID_CANCEL, ID_NOTE, ID_LANG, ID_MEETING = 1001, 1002, 1003, 1004, 1005
ID_CONFIG, ID_LOG, ID_SETUP, ID_DOCTOR, ID_QUIT = 1006, 1007, 1008, 1009, 1010

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = wintypes.LPARAM
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
else:
    _user32 = _shell32 = _kernel32 = None
    WNDPROC = None


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


def _declare() -> None:
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                        wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    _user32.DefWindowProcW.restype = LRESULT
    _user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM)
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.LoadIconW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR)
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadImageW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT)
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = (wintypes.HMENU, wintypes.UINT,
                                    ctypes.c_void_p, wintypes.LPCWSTR)
    _user32.TrackPopupMenu.restype = ctypes.c_int
    _user32.TrackPopupMenu.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       ctypes.c_void_p)
    _user32.DestroyMenu.argtypes = (wintypes.HMENU,)
    _user32.RegisterWindowMessageW.restype = wintypes.UINT
    _user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))


if IS_WINDOWS:
    _declare()

STATE_LABEL = {"idle": "idle", "recording": "recording…", "processing": "processing…",
               "meeting": "meeting"}


class Tray:
    def __init__(self, daemon, log=print) -> None:
        if not IS_WINDOWS:
            raise RuntimeError("wf_tray requires Windows")
        self.d = daemon
        self.log = log
        self.hwnd = None
        self.thread: threading.Thread | None = None
        self._nid = None
        self._wndproc = None      # strong reference: Windows keeps a raw pointer to it
        self._taskbar_created = 0
        self._ready = threading.Event()

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> bool:
        self.thread = threading.Thread(target=self._run, name="wf-tray", daemon=True)
        self.thread.start()
        self._ready.wait(timeout=5)
        return self.hwnd is not None

    def stop(self) -> None:
        if self.hwnd:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            _user32.PostMessageW(self.hwnd, WM_QUIT, 0, 0)

    # -- icon -----------------------------------------------------------------
    def _icon(self):
        ico = wf_paths.app_dir() / "assets" / "wisprflow.ico"
        if ico.is_file():
            h = _user32.LoadImageW(None, str(ico), IMAGE_ICON, 0, 0,
                                   LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        return _user32.LoadIconW(None, wintypes.LPCWSTR(IDI_APPLICATION))

    def _tooltip(self) -> str:
        d = self.d
        bits = [f"wisprflow — {STATE_LABEL.get(d.state, d.state)}",
                f"ASR: {d.asr_device or 'loading'} · {d.session_lang.upper()}"]
        if d.note_mode:
            bits.append("NoteMode ON")
        try:
            import wf_hotkey
            bits.append(wf_hotkey.describe(d.cfg.get("hotkey", "")))
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(bits)[:127]     # szTip is 128 wchars including the terminator

    def _make_nid(self, flags: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon()
        nid.szTip = self._tooltip()
        return nid

    def refresh(self) -> None:
        """Update the tooltip after a state change. Safe to call from any thread."""
        if not self.hwnd or self._nid is None:
            return
        try:
            self._nid.szTip = self._tooltip()
            self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:  # noqa: BLE001
            pass

    def notify(self, title: str, body: str, level: str = "info") -> None:
        """Balloon notification — used for failures the user would otherwise never see."""
        if not self.hwnd:
            return
        try:
            nid = self._make_nid(NIF_INFO | NIF_ICON)
            nid.szInfoTitle = title[:63]
            nid.szInfo = body[:255]
            nid.dwInfoFlags = {"info": NIIF_INFO, "warning": NIIF_WARNING,
                               "error": NIIF_ERROR}.get(level, NIIF_INFO)
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:  # noqa: BLE001
            pass

    # -- window + message loop ------------------------------------------------
    def _run(self) -> None:
        try:
            self._create_window()
            self._nid = self._make_nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
            _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))
            self._taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")
        except Exception as e:  # noqa: BLE001
            self.log(f"tray: setup failed: {e!r}")
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _create_window(self) -> None:
        hinst = _kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._proc)
        cls = WNDCLASSW()
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "WisprflowTrayWindow"
        if not _user32.RegisterClassW(ctypes.byref(cls)):
            err = ctypes.get_last_error()
            if err != 1410:      # ERROR_CLASS_ALREADY_EXISTS is fine (a restarted tray)
                raise OSError(f"RegisterClassW failed (WinError {err})")
        self.hwnd = _user32.CreateWindowExW(
            0, "WisprflowTrayWindow", "wisprflow", 0, CW_USEDEFAULT, CW_USEDEFAULT,
            0, 0, wintypes.HWND(HWND_MESSAGE), None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW failed (WinError {ctypes.get_last_error()})")

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._cmd(ID_TOGGLE)
                elif lparam == WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == WM_COMMAND:
                self._cmd(wparam & 0xFFFF)
                return 0
            if self._taskbar_created and msg == self._taskbar_created:
                # Explorer restarted and dropped every tray icon — put ours back.
                self._nid = self._make_nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
                _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))
                return 0
            if msg == WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception as e:  # noqa: BLE001
            self.log(f"tray: message handler error: {e!r}")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- menu -----------------------------------------------------------------
    def _menu(self) -> None:
        d = self.d
        m = _user32.CreatePopupMenu()
        add = _user32.AppendMenuW

        add(m, MF_STRING | MF_GRAYED, 0, f"wisprflow — {STATE_LABEL.get(d.state, d.state)}")
        add(m, MF_SEPARATOR, 0, None)
        add(m, MF_STRING, ID_TOGGLE,
            "Stop dictation" if d.state == "recording" else "Start dictation")
        add(m, MF_STRING | (MF_GRAYED if d.state != "recording" else 0), ID_CANCEL,
            "Cancel recording")
        add(m, MF_SEPARATOR, 0, None)
        add(m, MF_STRING | (MF_CHECKED if d.note_mode else 0), ID_NOTE,
            "NoteMode (one line per sentence)")
        add(m, MF_STRING, ID_LANG, f"Language: {d.session_lang.upper()}  (click to cycle)")
        add(m, MF_STRING, ID_MEETING,
            "Stop meeting" if d.state == "meeting" else "Start meeting mode")
        add(m, MF_SEPARATOR, 0, None)
        add(m, MF_STRING, ID_CONFIG, "Open config file")
        add(m, MF_STRING, ID_LOG, "Open log folder")
        add(m, MF_STRING, ID_SETUP, "Setup / install models…")
        add(m, MF_STRING, ID_DOCTOR, "Run diagnostics…")
        add(m, MF_SEPARATOR, 0, None)
        add(m, MF_STRING, ID_QUIT, "Quit wisprflow")

        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        # Documented dance: without the foreground/WM_NULL pair the menu refuses to close
        # when the user clicks elsewhere and stays stuck on screen.
        _user32.SetForegroundWindow(self.hwnd)
        chosen = _user32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                        pt.x, pt.y, 0, self.hwnd, None)
        _user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        _user32.DestroyMenu(m)
        if chosen:
            self._cmd(chosen)

    def _cmd(self, cid: int) -> None:
        d = self.d
        try:
            if cid == ID_TOGGLE:
                d.handle("toggle")
            elif cid == ID_CANCEL:
                d.handle("cancel")
            elif cid == ID_NOTE:
                d.handle("note")
            elif cid == ID_LANG:
                d.handle("lang")
            elif cid == ID_MEETING:
                d.handle("toggle" if d.state == "meeting" else "meeting")
            elif cid == ID_CONFIG:
                self._open(wf_paths.config_path(), create_default=True)
            elif cid == ID_LOG:
                self._open(wf_paths.log_dir())
            elif cid == ID_SETUP:
                self._console_script("wf_setup.py")
            elif cid == ID_DOCTOR:
                self._console_script("wf_doctor.py")
            elif cid == ID_QUIT:
                d.handle("shutdown")
        except Exception as e:  # noqa: BLE001
            self.log(f"tray: command {cid} failed: {e!r}")
        self.refresh()

    def _open(self, path: Path, create_default: bool = False) -> None:
        if create_default and not path.exists():
            wf_paths.save_config(dict(wf_paths.DEFAULTS))
        os.startfile(str(path))   # noqa: SIM115  (Windows-only shell open)

    def _console_script(self, script: str) -> None:
        """Launch a helper WITH a console — these are interactive and print progress."""
        exe = sys.executable or "python"
        if exe.lower().endswith("pythonw.exe"):
            cand = Path(exe).with_name("python.exe")
            if cand.is_file():
                exe = str(cand)
        subprocess.Popen([exe, str(wf_paths.app_dir() / script)],
                         cwd=str(wf_paths.app_dir()),
                         creationflags=0x00000010)   # CREATE_NEW_CONSOLE
