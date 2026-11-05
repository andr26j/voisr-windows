"""A real window of native text boxes, for tests/test_uia_real.py.

Run as a script, in its own process, so UI Automation reads it the way it
reads any other app. Prints one JSON line of window handles, then pumps
messages until its stdin closes. --focus brings it to the foreground with the
caret in the main box.

Posting WM_APP_HANG with wParam=N to the main window makes its thread sleep N
ms, which is what a hung app looks like from outside.
"""
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_BORDER = 0x00800000
WS_VSCROLL = 0x00200000
ES_MULTILINE = 0x0004
ES_AUTOHSCROLL = 0x0080
ES_PASSWORD = 0x0020
ES_READONLY = 0x0800
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_APP_HANG = 0x8000 + 1
VK_SHIFT = 0x10
KEYEVENTF_KEYUP = 0x0002

TITLE = "Voisr UIA test window"
LABEL = "Invoice for Tareq Haddad"


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
    wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                wintypes.LPARAM]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetFocus.restype = wintypes.HWND
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def _wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_APP_HANG:
        time.sleep(wparam / 1000)
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_PROC = WNDPROC(_wndproc)  # kept alive for the window's life


def _child(parent, cls, text, style, y, width=420, height=26):
    hwnd = user32.CreateWindowExW(0, cls, text, WS_CHILD | WS_VISIBLE | WS_BORDER | style,
                                  10, y, width, height, parent, None, None, None)
    if not hwnd:
        raise OSError(f"CreateWindowExW({cls}) failed: {ctypes.get_last_error()}")
    return hwnd


def main() -> None:
    focus = "--focus" in sys.argv
    instance = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=_PROC,
                     hInstance=instance, lpszClassName="VoisrUIATestWindow",
                     hbrBackground=wintypes.HBRUSH(6))  # COLOR_WINDOW + 1
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        raise OSError(f"RegisterClassExW failed: {ctypes.get_last_error()}")
    main_hwnd = user32.CreateWindowExW(0, "VoisrUIATestWindow", TITLE,
                                       WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                       120, 120, 480, 360, None, None, instance, None)
    ctypes.WinDLL("Msftedit.dll")  # registers RICHEDIT50W
    hwnds = {
        "main": main_hwnd,
        "label": _child(main_hwnd, "STATIC", LABEL, 0, 10),
        "edit": _child(main_hwnd, "EDIT", "", ES_AUTOHSCROLL, 44),
        "password": _child(main_hwnd, "EDIT", "hunter2-secret", ES_PASSWORD | ES_AUTOHSCROLL, 78),
        "readonly": _child(main_hwnd, "EDIT", "fixed text", ES_READONLY | ES_AUTOHSCROLL, 112),
        "multiline": _child(main_hwnd, "EDIT", "", ES_MULTILINE | WS_VSCROLL, 146, height=60),
        "rich": _child(main_hwnd, "RICHEDIT50W", "", ES_MULTILINE, 214, height=60),
    }
    foreground = False
    if focus:
        # A process may only take the foreground right after input of its
        # own; a synthetic Shift press counts and does nothing else. (Alt, the
        # usual one, opens the window's menu and takes the focus with it.)
        for _ in range(5):
            user32.keybd_event(VK_SHIFT, 0, 0, 0)
            user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
            user32.SetForegroundWindow(main_hwnd)
            user32.SetFocus(hwnds["edit"])
            if (user32.GetForegroundWindow() == main_hwnd
                    and user32.GetFocus() == hwnds["edit"]):
                foreground = True
                break
            time.sleep(0.1)
    print(json.dumps({**hwnds, "pid": os.getpid(), "foreground": foreground}), flush=True)

    def close_on_eof():
        sys.stdin.read()
        user32.PostMessageW(main_hwnd, WM_CLOSE, 0, 0)

    threading.Thread(target=close_on_eof, daemon=True).start()
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
