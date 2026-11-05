"""Hear mouse clicks anywhere, without being in their way.

A click can move the caret, so the next dictation may not follow on from the
last: smart spacing must not join it with a space. The Mac hears clicks with
a passive global monitor. Here it is Raw Input with RIDEV_INPUTSINK, read on
a thread of its own: Windows hands this window a copy of each mouse report
after the fact. Unlike a WH_MOUSE_LL hook, nothing waits on Python, so a busy
interpreter can't slow the pointer, and no permission is involved.

Every mouse report arrives, moves too; a button press is all that counts.
"""
import ctypes
import threading
from ctypes import wintypes

WM_INPUT = 0x00FF
WM_QUIT = 0x0012
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
HWND_MESSAGE = wintypes.HWND(-3)
# any button going down: left, right, middle, X1, X2
BUTTONS_DOWN = 0x0001 | 0x0004 | 0x0010 | 0x0040 | 0x0100


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class _Buttons(ctypes.Structure):
    _fields_ = [("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT)]


class _ButtonsUnion(ctypes.Union):
    _anonymous_ = ("b",)
    _fields_ = [("ulButtons", wintypes.ULONG), ("b", _Buttons)]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("usFlags", wintypes.USHORT), ("u", _ButtonsUnion),
                ("ulRawButtons", wintypes.ULONG), ("lLastX", wintypes.LONG),
                ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


def clicked(report: RAWINPUT) -> bool:
    return (report.header.dwType == RIM_TYPEMOUSE
            and bool(report.mouse.usButtonFlags & BUTTONS_DOWN))


class ClickWatch:
    """Calls on_click, on its own thread, for each mouse button press in any
    app. start() returns once the watch is listening, or has failed."""

    def __init__(self, on_click):
        self._on_click = on_click
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self.listening = False

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, name="voisr-clicks",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self.listening

    def stop(self) -> None:
        if self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32")
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
            wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.UINT), wintypes.UINT]
        user32.GetRawInputData.restype = wintypes.UINT
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        hwnd = None
        try:
            self._thread_id = kernel32.GetCurrentThreadId()
            # a message-only window of a system class: no window procedure of ours
            hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0,
                                          HWND_MESSAGE, None, None, None)
            if not hwnd:
                raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
            device = RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd)  # generic mouse
            if not user32.RegisterRawInputDevices(ctypes.byref(device), 1,
                                                  ctypes.sizeof(device)):
                raise OSError(f"RegisterRawInputDevices failed: {ctypes.get_last_error()}")
            self.listening = True
        except Exception as error:
            from voisr.updater import _log
            _log(f"click watch not installed ({error})")
            if hwnd:
                user32.DestroyWindow(hwnd)
            return
        finally:
            self._ready.set()
        report = RAWINPUT()
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_INPUT:
                    size = wintypes.UINT(ctypes.sizeof(report))
                    got = user32.GetRawInputData(msg.lParam, RID_INPUT, ctypes.byref(report),
                                                 ctypes.byref(size), header_size)
                    if got != 0xFFFFFFFF and clicked(report):
                        try:
                            self._on_click()
                        except Exception:
                            pass  # nothing here may end the watch
                # the default handling frees the input's buffer
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self.listening = False
            device = RAWINPUTDEVICE(0x01, 0x02, RIDEV_REMOVE, None)
            user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(device))
            user32.DestroyWindow(hwnd)
