"""Listen-only global keyboard hook.

WH_KEYBOARD_LL delivers every key event system-wide. The hook must be
installed on the thread that pumps messages, so the listener owns a thread
running its own GetMessage loop.

The callback does as little as possible: Windows silently unregisters a
low-level hook whose callback exceeds LowLevelHooksTimeout (300 ms by
default), so all it does is record a virtual key and return.
"""
import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# Must match voisr.inject.VOISR_SIGNATURE — that is how we tell our own
# synthetic Ctrl+V apart from a real keypress.
VOISR_SIGNATURE = 0x564F4953

_DOWN_MESSAGES = (WM_KEYDOWN, WM_SYSKEYDOWN)
_UP_MESSAGES = (WM_KEYUP, WM_SYSKEYUP)

HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# Without explicit argtypes/restype, ctypes marshals each argument as a
# 32-bit c_int by default. lparam is a pointer-sized value, so on 64-bit
# Windows that silently overflows and raises ArgumentError inside the hook
# callback — which Windows swallows, leaving the hook dead.
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


def _struct_from_lparam(lparam):
    return ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents


class WindowsKeyListener:
    """Calls handler(vk, kind) for every key event.

    kind: "down" | "up". Unlike the macOS Quartz tap there is no separate
    "flags" kind — Windows reports modifiers as ordinary key events.
    """

    def __init__(self, handler):
        self._handler = handler
        self._thread = None
        self._hook = None
        self._thread_id = None
        self.running = False
        # set once the hook install has been tried, whichever way it went
        self._settled = threading.Event()

    @property
    def state(self) -> str:
        """"starting" until the hook install is tried, then "running" while
        the hook is live and "failed" once it is gone or never came up."""
        if self.running:
            return "running"
        return "failed" if self._settled.is_set() else "starting"

    def wait_started(self, timeout: float) -> str:
        """Wait up to `timeout` for the install to settle; return state."""
        self._settled.wait(timeout)
        return self.state

    def _callback(self, ncode, wparam, lparam):
        if ncode < 0:
            return user32.CallNextHookEx(None, ncode, wparam, lparam)
        try:
            if wparam in _DOWN_MESSAGES:
                kind = "down"
            elif wparam in _UP_MESSAGES:
                kind = "up"
            else:
                kind = None
            if kind is not None:
                data = _struct_from_lparam(lparam)
                if data.dwExtraInfo != VOISR_SIGNATURE:
                    self._handler(int(data.vkCode), kind)
        except Exception:
            pass  # a callback error must never take down the hook
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._proc = HOOKPROC(self._callback)  # kept alive on self
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            self._settled.set()
            return  # caller retries; nothing usable without a hook
        self.running = True
        self._settled.set()
        try:
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self.running = False
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def stop(self) -> None:
        thread_id, self._thread_id = self._thread_id, None
        if thread_id is not None:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)


def capture_next_vk(timeout: float = 8.0):
    """Block until any key goes down; return its virtual-key code or None."""
    result = {}
    got = threading.Event()

    def handler(vk, kind):
        if kind == "down" and "vk" not in result:
            result["vk"] = vk
            got.set()

    listener = WindowsKeyListener(handler)
    listener.start()
    got.wait(timeout)
    listener.stop()
    return result.get("vk")
