"""Native window chrome for the dashboard, via DWM.

The redesign is flat: the sidebar is the window's own background and the
panel sits inset on it. On the Mac that means running the page up under a
transparent titlebar; Windows keeps its own titlebar, so the same look comes
from painting the caption the sidebar's colour (DWMWA_CAPTION_COLOR) with the
title in the ink colour, and no Mica backdrop, which would tint the band with
the wallpaper. The window also gets the sidebar's colour before the page's
first paint, so nothing flashes white in dark mode.

DWMWA_USE_IMMERSIVE_DARK_MODE works on Windows 10. The caption and text
colours need Windows 11 (build 22000+); on Windows 10 they fail, the failure
is reported, never raised, and the titlebar keeps the system's colours.
"""

import ctypes
from ctypes import wintypes

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36

# The window behind the page, and the titlebar's colour. Must match --bg (the
# sidebar) and --ink in web/css/tokens.css; tests/test_window_chrome.py checks
# that they do.
WINDOW_BACKGROUND = {"light": "#F5F4F1", "dark": "#141413"}
TITLE_TEXT = {"light": "#191918", "dark": "#F2F1EC"}


def colorref(hex_colour: str) -> int:
    """'#RRGGBB' as a Win32 COLORREF, 0x00BBGGRR."""
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


def launch_background(appearance, prefers_dark=None) -> str:
    """The window colour for a saved appearance ("system", "light" or
    "dark"). prefers_dark() answers "system"; it defaults to light. Never
    raises: a cosmetic failure must not stop the app."""
    if isinstance(appearance, str) and appearance in WINDOW_BACKGROUND:
        return WINDOW_BACKGROUND[appearance]
    try:
        dark = bool(prefers_dark()) if prefers_dark is not None else False
    except Exception:
        dark = False
    return WINDOW_BACKGROUND["dark" if dark else "light"]


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
    """Dark or light titlebar, painted the sidebar's colour. Never raises."""
    theme = "dark" if dark else "light"
    applied = {"dark_titlebar": False, "caption": False}
    try:
        if dwm is None:
            dwm = _Dwm()
        applied["dark_titlebar"] = dwm.set_attribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0) == 0
        caption = dwm.set_attribute(
            hwnd, DWMWA_CAPTION_COLOR, colorref(WINDOW_BACKGROUND[theme])) == 0
        text = dwm.set_attribute(
            hwnd, DWMWA_TEXT_COLOR, colorref(TITLE_TEXT[theme])) == 0
        applied["caption"] = caption and text
    except Exception:
        pass  # chrome is cosmetic: whatever was applied stays
    return applied
