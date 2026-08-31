#!/usr/bin/env python3
"""wisprflow on-screen overlay for Windows — DPI-aware, multi-monitor-correct indicators.

Modes (argv):
  listening        animated waveform + "Listening", plus three clickable buttons
                   (MeetingMode / NoteMode / Language); runs until terminated
  processing       spinner + "Processing…" (shown while transcribing)
  meeting          "Meeting — recording" pill (pulsing red dot); runs until terminated
  done  [TEXT]     green check + short text, auto-closes after ~1s

Environment:
  WF_NOTE_MODE=1        render the NoteMode button as active
  WF_LANG=en|de|ro      render the Language button with the active language
  WF_OVERLAY_SCALE=<f>  override the auto DPI scale (float)

Ported from the X11/GNOME original. Three things had to change:

* **Geometry.** The original parsed `xrandr` to find the primary monitor, because tkinter's
  winfo_screenwidth() returns the whole virtual desktop. On Windows, SPI_GETWORKAREA gives
  the primary monitor's work area directly — and it excludes the taskbar, so the pill sits
  above it instead of under it.
* **DPI.** The original derived a scale from the X server's reported DPI. Here the process
  declares itself per-monitor DPI aware and asks Windows for the real value; without that
  declaration Windows lies to the process (reporting 96) and then bitmap-stretches the
  window, which looks blurry on a scaled display.
* **Focus.** This is the important one. A window that takes focus would make dictated text go
  to the *overlay* instead of the app the user was typing into. The window is therefore
  stamped WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW: it can never be activated, never appears in
  Alt-Tab, and never becomes the SendInput target.
"""
from __future__ import annotations

import ctypes
import math
import os
import signal
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wf_input  # noqa: E402
import wf_ipc  # noqa: E402

# The window the user was typing in, captured BEFORE tkinter exists. Tk grabs the
# foreground the moment it realizes its toplevel (inside update_idletasks, before any
# deiconify/show call and even while the window is withdrawn), and no combination of
# WS_EX_NOACTIVATE or SWP_NOACTIVATE prevents that — the styles can only be applied to a
# window that already exists. So the pill takes the foreground for a few milliseconds and
# hands it straight back.
PREV_FOREGROUND = wf_input.foreground_window()


def give_focus_back() -> None:
    """Return the foreground to whatever had it before this overlay started."""
    try:
        wf_input.restore_foreground(PREV_FOREGROUND)
    except Exception:  # noqa: BLE001
        pass


def _focus_guard() -> None:
    """Undo our own focus theft within a few ms, for as long as the window is settling.

    Tk grabs the foreground at whatever moment it realizes the toplevel — sometimes while
    measuring fonts, sometimes at update_idletasks — so a fixed set of give_focus_back()
    calls leaves a race. This polls instead, and bounds the theft to the poll interval.

    It only ever acts when the thief is a window of THIS process: a deliberate app switch
    by the user during recording must be left alone, or the pill would fight the user for
    the foreground. It also stops after a short settling period for the same reason.
    """
    end = time.time() + 1.5
    while time.time() < end:
        try:
            fg = wf_input.foreground_window()
            if fg and fg != PREV_FOREGROUND and wf_input.window_pid(fg) == os.getpid():
                wf_input.restore_foreground(PREV_FOREGROUND)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.003)


if PREV_FOREGROUND:
    threading.Thread(target=_focus_guard, name="wf-focus-guard", daemon=True).start()

MODE = sys.argv[1] if len(sys.argv) > 1 else "listening"
TEXT = sys.argv[2] if len(sys.argv) > 2 else ""
NOTE_ON = os.environ.get("WF_NOTE_MODE") == "1"
LANG = os.environ.get("WF_LANG", "en")
LANG_LABEL = {"en": "🌐 EN", "de": "🌐 DE", "ro": "🌐 RO"}

IS_WINDOWS = sys.platform == "win32"

# ---- palette (dark, glassy) -------------------------------------------------------------
BG       = "#010203"   # window backdrop — keyed out via -transparentcolor, so only the pill shows
CARD     = "#191c24"   # pill body
CARD_HI  = "#262a35"   # top bevel highlight
BORDER   = "#39415a"
ACCENT   = "#5b93ff"   # brand blue
ACCENT2  = "#93b8ff"   # waveform highlight
OKC      = "#3ddc84"   # done check
REC      = "#ff5c5c"   # meeting record dot
FG       = "#f2f4f9"
SUBTLE   = "#96a0b4"
BTN      = "#242835"
BTN_HOV  = "#2e3342"
BTN_BRD  = "#404862"
NOTE_BG  = "#264a86"   # active-toggle button fill
NOTE_HOV = "#2d569b"
NOTE_BRD = "#5b93ff"

FPS = 30


# =========================================================================================
# Win32 geometry / DPI / window styles
# =========================================================================================
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _declare_dpi_aware() -> None:
    """Must run before Tk creates any window, or Windows bitmap-stretches (blurs) the overlay."""
    if not IS_WINDOWS:
        return
    try:   # Windows 10 1703+: per-monitor v2, the only mode that rescales correctly on the fly
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:  # noqa: BLE001
        pass
    try:   # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:  # noqa: BLE001
        pass
    try:   # Vista+
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def work_area(fallback):
    """(w, h, x, y) of the PRIMARY monitor's work area — the desktop minus the taskbar."""
    if not IS_WINDOWS:
        return fallback
    try:
        r = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            return (r.right - r.left, r.bottom - r.top, r.left, r.top)
    except Exception:  # noqa: BLE001
        pass
    return fallback


def system_dpi_scale() -> float:
    if not IS_WINDOWS:
        return 1.0
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:  # noqa: BLE001
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(None)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(None, hdc)
        return (dpi or 96) / 96.0
    except Exception:  # noqa: BLE001
        return 1.0


def _toplevel_hwnd(root):
    """The real top-level HWND behind a Tk root (Tk nests its window inside a wrapper)."""
    user32 = ctypes.windll.user32
    hwnd = root.winfo_id()
    return user32.GetParent(hwnd) or hwnd


def show_without_activating(root) -> None:
    """Map the pill WITHOUT handing it the foreground.

    WS_EX_NOACTIVATE is necessary but NOT sufficient, and that is the whole bug: the style
    stops the *user* activating the window by clicking it, but says nothing about the
    program doing it. Tk's deiconify() maps the window via ShowWindow(SW_RESTORE), which
    activates it — so every time the pill appeared, the user's editor was deactivated and
    the caret went away. SendInput then delivered the dictated text to whatever had focus
    instead, i.e. nowhere useful.

    SetWindowPos with SWP_NOACTIVATE|SWP_SHOWWINDOW shows and stacks the window while
    leaving the foreground exactly where it was. Tk still paints it: the canvas redraws on
    WM_PAINT and the animation timers are ordinary root.after() callbacks, neither of which
    cares what `wm state` thinks.
    """
    if not IS_WINDOWS:
        root.deiconify()
        return
    try:
        user32 = ctypes.windll.user32
        # ctypes.* only: this module does not import ctypes.wintypes, and a NameError here
        # would be swallowed by the except below and fall back to the activating path.
        user32.SetWindowPos.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_uint)
        user32.SetWindowPos.restype = ctypes.c_int
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0010, 0x0040
        HWND_TOPMOST = ctypes.c_void_p(-1)
        user32.SetWindowPos(ctypes.c_void_p(_toplevel_hwnd(root)), HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
    except Exception:  # noqa: BLE001
        root.deiconify()   # visible-but-stealing beats invisible


def make_non_activating(root) -> None:
    """Stamp WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW on the toplevel.

    Keeps the pill out of Alt-Tab and stops a click on it activating it. Showing it without
    activation is a separate problem — see show_without_activating().
    """
    if not IS_WINDOWS:
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = _toplevel_hwnd(root)
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x00000080
        # The *Ptr variants only exist on 64-bit Windows; on 32-bit the plain ones are
        # correct and return a 32-bit LONG. Picking the wrong width here silently corrupts
        # the style word.
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            getf, setf, LONG_T = (user32.GetWindowLongPtrW, user32.SetWindowLongPtrW,
                                  ctypes.c_longlong)
        else:
            getf, setf, LONG_T = (user32.GetWindowLongW, user32.SetWindowLongW,
                                  ctypes.c_long)
        getf.restype, getf.argtypes = LONG_T, (ctypes.c_void_p, ctypes.c_int)
        setf.restype, setf.argtypes = LONG_T, (ctypes.c_void_p, ctypes.c_int, LONG_T)
        style = getf(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
        setf(ctypes.c_void_p(hwnd), GWL_EXSTYLE,
             style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    except Exception:  # noqa: BLE001
        pass


_declare_dpi_aware()

root = tk.Tk()
root.withdraw()   # stay hidden until fully configured -> never flash at the wrong size/spot

_sw, _sh = root.winfo_screenwidth(), root.winfo_screenheight()
MW, MH, OX, OY = work_area((min(_sw, int(_sh * 16 / 10)), _sh, 0, 0))

scale = system_dpi_scale()
if scale < 1.05:                 # DPI not reporting HiDPI -> resolution heuristic
    scale = max(1.0, MH / 1080.0)
try:
    scale = float(os.environ.get("WF_OVERLAY_SCALE", scale))   # dev override
except ValueError:
    pass
scale = max(0.75, min(4.0, scale))   # clamp AFTER the override so a bad value can't zero the UI


def S(v):
    return int(round(v * scale))


# ---- fonts: negative size == pixels, so text tracks our pixel-space layout exactly -------
def pick_family():
    try:
        fams = set(tkfont.families(root))
    except Exception:  # noqa: BLE001
        return "Segoe UI"
    for f in ("Segoe UI Variable Display", "Segoe UI", "Inter", "Noto Sans",
              "Calibri", "Tahoma", "Arial"):
        if f in fams:
            return f
    return "Segoe UI"


FAM = pick_family()
F_TITLE = tkfont.Font(root=root, family=FAM, size=-S(14), weight="bold")
F_SUB   = tkfont.Font(root=root, family=FAM, size=-S(9))
F_BTN   = tkfont.Font(root=root, family=FAM, size=-S(11), weight="bold")
F_DONE  = tkfont.Font(root=root, family=FAM, size=-S(12))
# Enumerating font families and measuring text forces Tk to realize the toplevel, which is
# the moment it takes the foreground — earlier than the explicit update_idletasks() below.
give_focus_back()


def round_rect(c, x1, y1, x2, y2, r, **kw):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


# ---- per-mode base sizes (unscaled units) -----------------------------------------------
if MODE == "listening":
    BASE_W, BASE_H = 372, 123
elif MODE == "meeting":
    BASE_W, BASE_H = 264, 62
elif MODE == "processing":
    BASE_W, BASE_H = 244, 62
else:  # done
    BASE_W, BASE_H = 300, 58

W, H = S(BASE_W), S(BASE_H)
MARGIN = S(24)                         # gap above the taskbar (work_area already excludes it)
x = OX + (MW - W) // 2
y = OY + MH - H - MARGIN

root.overrideredirect(True)
root.attributes("-topmost", True)
try:
    root.attributes("-alpha", 0.98)
except tk.TclError:
    pass
if IS_WINDOWS:
    try:
        # Key out the backdrop so the pill's rounded corners show the desktop through them —
        # and so clicks in the corners pass through to the window underneath.
        root.attributes("-transparentcolor", BG)
    except tk.TclError:
        pass
root.geometry(f"{W}x{H}+{x}+{y}")
root.configure(bg=BG)
cv = tk.Canvas(root, width=W, height=H, bg=BG, highlightthickness=0, bd=0)
cv.pack(fill="both", expand=True)


def draw_card():
    """The rounded pill body: soft border + a thin top bevel for a glassy, non-cheap feel."""
    pad = S(2)
    r = S(16)
    round_rect(cv, pad, pad, W - pad, H - pad, r, fill=CARD, outline=BORDER, width=max(1, S(1)))
    cv.create_line(pad + r, pad + max(1, S(1)), W - pad - r, pad + max(1, S(1)),
                   fill=CARD_HI, width=max(1, S(1)))


draw_card()


def close(*_):
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass
    os._exit(0)


signal.signal(signal.SIGTERM, close)
signal.signal(signal.SIGINT, close)

_misses = [0]


def watchdog():
    """Close if the daemon went away, so no orphan pill is left floating on the desktop.

    The Linux original watched for its parent PID changing (Unix re-parents orphans to init).
    Windows does not re-parent, and PIDs are recycled, so liveness is checked over the same
    IPC channel the buttons use — three consecutive misses and the pill closes itself.
    """
    if wf_ipc.try_send("ping", timeout=1.0).startswith("pong"):
        _misses[0] = 0
    else:
        _misses[0] += 1
        if _misses[0] >= 3:
            close()
            return
    root.after(3000, watchdog)


def send_cmd(cmd: str) -> str:
    return wf_ipc.try_send(cmd, timeout=2.0)


# =========================================================================================
# DONE — green check + short text, auto-closes
# =========================================================================================
if MODE == "done":
    cx = S(30)
    cv.create_oval(cx - S(12), H / 2 - S(12), cx + S(12), H / 2 + S(12),
                   fill="", outline=OKC, width=max(1, S(2)))
    cv.create_text(cx, H / 2, text="✓", fill=OKC,
                   font=tkfont.Font(root=root, family=FAM, size=-S(14), weight="bold"))
    cv.create_text(S(54), H / 2, text=(TEXT or "Inserted"), anchor="w", fill=FG,
                   font=F_DONE, width=W - S(66))
    root.after(1000, close)


# =========================================================================================
# MEETING — pulsing red dot + status text
# =========================================================================================
elif MODE == "meeting":
    dotx, cy = S(32), H // 2
    cv.create_text(S(56), int(H * 0.36), text="Meeting", anchor="w", fill=FG, font=F_TITLE)
    cv.create_text(S(56), int(H * 0.68), text="recording · press your key to stop",
                   anchor="w", fill=SUBTLE, font=F_SUB)
    t0 = time.time()

    def pulse():
        cv.delete("dot")
        a = 0.5 + 0.5 * math.sin((time.time() - t0) * 4.0)
        rr = int(S(7) + a * S(4))
        cv.create_oval(dotx - rr - S(3), cy - rr - S(3), dotx + rr + S(3), cy + rr + S(3),
                       fill="", outline="#5a2323", width=max(1, S(1)), tags="dot")
        cv.create_oval(dotx - rr, cy - rr, dotx + rr, cy + rr, fill=REC, outline="", tags="dot")
        root.after(int(1000 / FPS), pulse)

    pulse()
    watchdog()


# =========================================================================================
# PROCESSING — spinner + "Processing…"
# =========================================================================================
elif MODE == "processing":
    cx, cy = S(30), H // 2
    rr = S(11)
    cv.create_text(S(52), H / 2, text="Processing…", anchor="w", fill=FG, font=F_TITLE)
    t0 = time.time()

    def spin():
        cv.delete("spin")
        ang = (time.time() - t0) * 320.0 % 360      # rotating 3/4 arc
        cv.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=ang, extent=270,
                      style="arc", outline=ACCENT, width=max(2, S(2)), tags="spin")
        root.after(int(1000 / FPS), spin)

    spin()
    watchdog()


# =========================================================================================
# LISTENING — waveform + label + MeetingMode / NoteMode / Language buttons
# =========================================================================================
else:
    note_on = [NOTE_ON]   # mutable so the click handler can flip it
    lang = [LANG if LANG in LANG_LABEL else "en"]

    PADX = S(14)
    wave_w = S(46)
    wave_cx = PADX + wave_w // 2
    wave_cy = H // 2
    lbl_x = PADX + wave_w + S(16)

    btn_w = S(156)
    btn_h = S(28)
    bgap = S(9)
    bx2 = W - PADX
    bx1 = bx2 - btn_w
    stack_h = btn_h * 3 + bgap * 2
    top = (H - stack_h) // 2
    meet_y1, meet_y2 = top, top + btn_h
    note_y1, note_y2 = top + btn_h + bgap, top + btn_h + bgap + btn_h
    lang_y1, lang_y2 = top + 2 * (btn_h + bgap), top + 2 * (btn_h + bgap) + btn_h

    # IMPORTANT: the button/label items are created ONCE here and only ever RECONFIGURED
    # (itemconfigure) on hover/toggle — never deleted+recreated. Recreating an item that the
    # cursor is over, from inside its own <Enter> handler, makes tkinter re-fire <Leave>+<Enter>
    # forever (a redraw storm that pegs the CPU and freezes clicks). Reconfiguring in place has
    # no such feedback loop.

    cv.create_text(lbl_x, int(H * 0.40), text="Listening", anchor="w", fill=FG, font=F_TITLE)
    sub_id = cv.create_text(lbl_x, int(H * 0.68), text="", anchor="w", font=F_SUB)

    def set_label():
        on = note_on[0]
        cv.itemconfigure(sub_id, text=("NoteMode · one line per sentence" if on
                                       else "press your key to stop"),
                         fill=(ACCENT2 if on else SUBTLE))

    round_rect(cv, bx1, meet_y1, bx2, meet_y2, S(9), fill=BTN, outline=BTN_BRD,
               width=max(1, S(1)), tags=("meet", "meet_bg"))
    cv.create_text((bx1 + bx2) // 2, (meet_y1 + meet_y2) // 2, text="👥  MeetingMode",
                   fill=FG, font=F_BTN, tags=("meet", "meet_tx"))
    round_rect(cv, bx1, note_y1, bx2, note_y2, S(9), fill=BTN, outline=BTN_BRD,
               width=max(1, S(1)), tags=("note", "note_bg"))
    cv.create_text((bx1 + bx2) // 2, (note_y1 + note_y2) // 2, text="", font=F_BTN,
                   tags=("note", "note_tx"))
    round_rect(cv, bx1, lang_y1, bx2, lang_y2, S(9), fill=BTN, outline=BTN_BRD,
               width=max(1, S(1)), tags=("lang", "lang_bg"))
    cv.create_text((bx1 + bx2) // 2, (lang_y1 + lang_y2) // 2, text="", font=F_BTN,
                   tags=("lang", "lang_tx"))

    def set_meet(hover=False):
        cv.itemconfigure("meet_bg", fill=(BTN_HOV if hover else BTN))

    def set_note(hover=False):
        on = note_on[0]
        fill = (NOTE_HOV if hover else NOTE_BG) if on else (BTN_HOV if hover else BTN)
        cv.itemconfigure("note_bg", fill=fill, outline=(NOTE_BRD if on else BTN_BRD))
        cv.itemconfigure("note_tx", text=("📝  NoteMode  •ON" if on else "📝  NoteMode"),
                         fill=(FG if on else SUBTLE))

    def set_lang(hover=False):
        cur = lang[0]
        active = cur != "en"   # EN is the default — only DE/RO get accent treatment
        fill = (NOTE_HOV if hover else NOTE_BG) if active else (BTN_HOV if hover else BTN)
        cv.itemconfigure("lang_bg", fill=fill, outline=(NOTE_BRD if active else BTN_BRD))
        cv.itemconfigure("lang_tx", text=LANG_LABEL.get(cur, "🌐 EN"),
                         fill=(FG if active else SUBTLE))

    def on_meeting(*_):
        send_cmd("meeting")
        close()   # the daemon relaunches this overlay in meeting mode

    def on_note(*_):
        reply = send_cmd("note")
        if reply.startswith("note on"):
            note_on[0] = True
        elif reply.startswith("note off"):
            note_on[0] = False
        else:
            note_on[0] = not note_on[0]   # optimistic fallback if the reply was lost
        set_note(hover=True)
        set_label()

    def on_lang(*_):
        reply = send_cmd("lang")
        new = reply.split()[-1] if reply.startswith("lang ") else None
        if new in LANG_LABEL:
            lang[0] = new
        else:
            order = ["en", "de", "ro"]
            lang[0] = order[(order.index(lang[0]) + 1) % len(order)]
        set_lang(hover=True)

    set_label()
    set_meet()
    set_note()
    set_lang()

    cv.tag_bind("meet", "<Button-1>", on_meeting)
    cv.tag_bind("meet", "<Enter>", lambda e: (set_meet(True), cv.config(cursor="hand2")))
    cv.tag_bind("meet", "<Leave>", lambda e: (set_meet(False), cv.config(cursor="")))
    cv.tag_bind("note", "<Button-1>", on_note)
    cv.tag_bind("note", "<Enter>", lambda e: (set_note(True), cv.config(cursor="hand2")))
    cv.tag_bind("note", "<Leave>", lambda e: (set_note(False), cv.config(cursor="")))
    cv.tag_bind("lang", "<Button-1>", on_lang)
    cv.tag_bind("lang", "<Enter>", lambda e: (set_lang(True), cv.config(cursor="hand2")))
    cv.tag_bind("lang", "<Leave>", lambda e: (set_lang(False), cv.config(cursor="")))

    # ---- animated waveform ----
    nb = 7
    bw = S(5)
    gap = S(4)
    total = nb * bw + (nb - 1) * gap
    x0 = wave_cx - total // 2
    phases = [i * 0.8 for i in range(nb)]
    t0 = time.time()

    def tick():
        cv.delete("bar")
        t = time.time() - t0
        for i in range(nb):
            amp = 0.22 + 0.78 * (0.5 + 0.5 * math.sin(t * 6.0 + phases[i]))
            bh = int(S(6) + amp * (H * 0.44))
            bx = x0 + i * (bw + gap)
            col = ACCENT if i % 2 == 0 else ACCENT2
            round_rect(cv, bx, wave_cy - bh // 2, bx + bw, wave_cy + bh // 2, bw // 2,
                       fill=col, outline="", tags="bar")
        root.after(int(1000 / FPS), tick)

    tick()
    watchdog()


root.update_idletasks()
give_focus_back()               # update_idletasks() is where Tk takes the foreground
make_non_activating(root)
show_without_activating(root)   # NOT deiconify(): that activates too
make_non_activating(root)   # re-apply: Tk can recreate the frame when the window is mapped
give_focus_back()
# Once more from inside the event loop: Tk can still touch the window as it settles
# (attribute re-application, the first WM_PAINT), and a late steal would be the one the
# user actually notices.
root.after(60, give_focus_back)
root.after(250, give_focus_back)
root.mainloop()
