"""The click watch against real input: a click anywhere is heard, a move is
not, and stopping it ends its thread."""
import ctypes
import sys
import threading
import time
from ctypes import wintypes

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Raw Input needs Windows")

if sys.platform == "win32":
    from voisr.click_watch import ClickWatch

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
        wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def send_mouse(flags):
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(0, 0, 0, flags, 0, 0)
    assert user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) == 1


def test_a_real_click_is_heard_and_a_move_is_not():
    clicks = []
    heard = threading.Event()
    watch = ClickWatch(lambda: (clicks.append(1), heard.set()))
    assert watch.start() is True
    # a window of the test's own under the pointer, so the click lands on nothing else
    target = user32.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, "STATIC", "click target",
                                    WS_POPUP | WS_VISIBLE, 200, 200, 240, 140,
                                    None, None, None, None)
    old = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    try:
        user32.SetCursorPos(320, 270)
        send_mouse(MOUSEEVENTF_MOVE)
        time.sleep(0.3)
        assert clicks == [], "a move is not a click"
        send_mouse(MOUSEEVENTF_LEFTDOWN)
        send_mouse(MOUSEEVENTF_LEFTUP)
        assert heard.wait(3), "the click never arrived"
        time.sleep(0.2)
        assert clicks == [1], "one press, one click"
    finally:
        user32.SetCursorPos(old.x, old.y)
        if target:
            user32.DestroyWindow(target)
        watch.stop()
    watch._thread.join(3)
    assert not watch._thread.is_alive() and watch.listening is False


def test_a_failing_callback_does_not_end_the_watch():
    calls = []

    def flaky():
        calls.append(1)
        raise RuntimeError("callback trouble")

    watch = ClickWatch(flaky)
    assert watch.start()
    old = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    target = user32.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, "STATIC", "click target",
                                    WS_POPUP | WS_VISIBLE, 200, 200, 240, 140,
                                    None, None, None, None)
    try:
        user32.SetCursorPos(320, 270)
        time.sleep(0.3)
        for count in (1, 2):
            send_mouse(MOUSEEVENTF_LEFTDOWN)
            send_mouse(MOUSEEVENTF_LEFTUP)
            deadline = time.monotonic() + 3
            while len(calls) < count and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(calls) == count, f"click {count} wasn't heard"
            time.sleep(0.6)  # past the double-click time
        assert watch.listening
    finally:
        user32.SetCursorPos(old.x, old.y)
        if target:
            user32.DestroyWindow(target)
        watch.stop()
