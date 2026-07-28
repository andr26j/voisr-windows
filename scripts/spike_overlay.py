"""Phase 0 spike: can a pywebview window be transparent, click-through,
always-on-top, and never take focus?

Run it, then follow the checklist printed to the console. This file is
deleted once voisr/overlay.py exists.
"""
import ctypes
from ctypes import wintypes
import threading
import time

import webview

user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]

HTML = """
<body style="margin:0;background:transparent;">
  <div style="width:232px;height:52px;border-radius:26px;
              background:rgba(20,20,22,0.85);color:#fff;
              font:600 14px system-ui;display:flex;
              align-items:center;justify-content:center;">
    spike pill
  </div>
</body>
"""


def find_hwnd(title: str):
    return user32.FindWindowW(None, title)


def apply_styles(hwnd) -> int:
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    return ex


def patch_later(title: str):
    for _ in range(50):
        hwnd = find_hwnd(title)
        if hwnd:
            ex = apply_styles(hwnd)
            print(f"hwnd={hwnd:#x} new exstyle={ex:#x}")
            print("\n--- CHECK ALL FOUR ---")
            print("1. Pill visible with NO white/black box around it")
            print("2. Open a fullscreen app - pill still visible on top")
            print("3. Click where the pill is - the app underneath receives it")
            print("4. Type in Notepad while pill shows - keystrokes land in Notepad")
            return
        time.sleep(0.1)
    print("FAILED: never found the window")


title = "voisr-spike"
threading.Thread(target=patch_later, args=(title,), daemon=True).start()
window = webview.create_window(
    title, html=HTML, width=232, height=52,
    frameless=True, on_top=True, transparent=True, resizable=False,
)
webview.start(gui="edgechromium")
