"""Applies dark titlebar / Mica backdrop window chrome via DWM.

Mica (DWMWA_SYSTEMBACKDROP_TYPE) is Windows-11-only and is expected to
fail on Windows 10 (build 19045); the failure is caught and reported,
never raised. DWMWA_USE_IMMERSIVE_DARK_MODE works on Windows 10.
"""

import ctypes
from ctypes import wintypes

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_MAINWINDOW = 2


class _Dwm:
    """Adapter around dwmapi.DwmSetWindowAttribute with explicit ctypes types."""

    def __init__(self):
        dwmapi = ctypes.WinDLL("dwmapi")
        self._DwmSetWindowAttribute = dwmapi.DwmSetWindowAttribute
        self._DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._DwmSetWindowAttribute.restype = ctypes.c_long

    def set_attribute(self, hwnd, attribute, value):
        dword_value = wintypes.DWORD(value)
        return self._DwmSetWindowAttribute(
            hwnd,
            attribute,
            ctypes.byref(dword_value),
            ctypes.sizeof(dword_value),
        )


def apply_chrome(hwnd, dark=True, dwm=None):
    """Apply dark titlebar and attempt a Mica backdrop. Never raises."""
    if dwm is None:
        dwm = _Dwm()

    exception_occurred = False

    try:
        rc = dwm.set_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
        dark_titlebar = rc == 0
    except Exception:
        dark_titlebar = False
        exception_occurred = True

    try:
        rc = dwm.set_attribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_MAINWINDOW)
        vibrancy = rc == 0
    except Exception:
        vibrancy = False
        exception_occurred = True

    if exception_occurred:
        return {"dark_titlebar": False, "vibrancy": False}

    return {"dark_titlebar": dark_titlebar, "vibrancy": vibrancy}
