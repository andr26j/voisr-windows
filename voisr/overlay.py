"""Floating dictation pill: a frameless, transparent, click-through,
never-activating pywebview window hosting web/widget.html.

Proven by scripts/spike_overlay.py (deleted once this module landed): a
plain pywebview window on Windows can be made genuinely transparent and
click-through by applying four extended window styles to its HWND after
creation. This module wraps that into the app's real overlay.
"""
import ctypes
import re
import threading
from ctypes import wintypes
from pathlib import Path

import webview

from voisr.paths import REPO_ROOT

WEB_DIR = REPO_ROOT / "voisr" / "web"

WIDTH, HEIGHT = 232, 52
BOTTOM_MARGIN = 72
WINDOW_TITLE = "Voisr Recording"

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

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

SM_CXSCREEN = 0
SM_CYSCREEN = 1

user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]

# Themes must match web/widget.html's THEME_VALUES.
_PILL_THEME_VALUES = {
    "classic", "ember", "aurora", "ocean", "neon", "breathe", "scanner",
}
_HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def sanitize_pill_theme(name) -> str:
    """Whitelist a theme name from config before it reaches JS interpolation."""
    return name if isinstance(name, str) and name in _PILL_THEME_VALUES else "classic"


def sanitize_pill_color(value) -> str | None:
    """Whitelist a custom bar color from config before JS interpolation.
    Returns the hex string, or None (= use the theme's designed color)."""
    if isinstance(value, str) and _HEX_COLOR_RE.fullmatch(value):
        return value
    return None


def apply_styles(hwnd) -> int:
    """Make the window layered, click-through, non-activating, and a tool
    window (no taskbar entry), then pin it topmost. Returns the new exstyle."""
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    return ex


def find_hwnd(title: str):
    return user32.FindWindowW(None, title)


class RecordingWidget:
    """Floating Wispr-style pill shown while dictating.

    Built as a frameless pywebview window (rendering web/widget.html) whose
    HWND is patched with WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    | WS_EX_TOOLWINDOW right after creation, so it is genuinely transparent,
    click-through, and never steals focus — paste-at-cursor stays intact.
    """

    def __init__(self):
        self._window = None
        self._hwnd = None
        self._shown = False
        self._guard_started = False

    def _start_foreground_guard(self) -> None:
        """Continuously reclaim the foreground from the pill for as long as
        it exists. Measured on real Windows: the pill's animated waveform
        (a requestAnimationFrame loop in widget.html) causes WebView2 to
        repeatedly re-activate the host window even with WS_EX_NOACTIVATE
        set — a one-shot restore after each show_* call is not enough, the
        theft recurs continuously while content is animating. This guard
        polls at a tight interval and hands focus straight back to whatever
        last legitimately held it, so any theft window is a handful of
        milliseconds rather than persisting indefinitely."""
        if self._guard_started:
            return
        self._guard_started = True

        def guard():
            last_external = None
            while True:
                try:
                    fg = user32.GetForegroundWindow()
                    if self._hwnd and fg == self._hwnd:
                        if last_external and last_external != self._hwnd:
                            user32.SetForegroundWindow(last_external)
                    elif fg:
                        last_external = fg
                except Exception:
                    pass
                threading.Event().wait(0.03)

        threading.Thread(target=guard, daemon=True).start()

    def create(self) -> None:
        """Build the window and locate its HWND. Safe to call once; any
        failure is swallowed so a broken pill never blocks dictation."""
        try:
            screen_w = user32.GetSystemMetrics(SM_CXSCREEN)
            screen_h = user32.GetSystemMetrics(SM_CYSCREEN)
            x = int((screen_w - WIDTH) / 2)
            y = int(screen_h - BOTTOM_MARGIN - HEIGHT)

            self._window = webview.create_window(
                WINDOW_TITLE,
                url=str(WEB_DIR / "widget.html"),
                width=WIDTH, height=HEIGHT, x=x, y=y,
                frameless=True, on_top=True, transparent=True,
                resizable=False, hidden=True, focus=False,
            )

            self._start_foreground_guard()

            # The Edge Chromium backend reveals its window asynchronously
            # once the WebView2 control finishes initializing, and that
            # reveal briefly activates the window (stealing focus) and
            # resets the extended window styles we apply below (hidden=True
            # only delays the reveal, it doesn't prevent the backend's own
            # show/activate/style logic from running once). So we patch on
            # the native "shown" event: hide again and hand focus back to
            # whatever owned it before creation. Both the event and a
            # polling fallback (in case "shown" never fires) funnel through
            # finish_setup, which runs its body at most once — self._hwnd
            # must never be visible to callers until hide + focus-restore
            # have actually completed, or a caller could observe (or a test
            # could measure) the window mid-transition.
            prev_fg = user32.GetForegroundWindow()
            setup_lock = threading.Lock()
            setup_done = threading.Event()

            def finish_setup():
                with setup_lock:
                    if setup_done.is_set():
                        return
                    setup_done.set()
                try:
                    hwnd = find_hwnd(WINDOW_TITLE)
                    if not hwnd:
                        return
                    apply_styles(hwnd)
                    if not self._shown:
                        user32.ShowWindow(hwnd, SW_HIDE)
                        if prev_fg and user32.GetForegroundWindow() != prev_fg:
                            user32.SetForegroundWindow(prev_fg)
                    self._hwnd = hwnd
                except Exception:
                    pass

            self._window.events.shown += finish_setup

            def fallback_poll():
                for _ in range(50):
                    if setup_done.is_set():
                        return
                    threading.Event().wait(0.1)
                finish_setup()

            threading.Thread(target=fallback_poll, daemon=True).start()
            webview.start(gui="edgechromium")
        except Exception:
            pass

    def _apply(self, state: str, theme: str | None, color: str | None) -> None:
        if self._window is None:
            return
        try:
            if theme is not None:
                # sanitize_* whitelist both values — config.json content
                # can never be interpolated into JS verbatim
                self._window.evaluate_js(f'setTheme("{sanitize_pill_theme(theme)}")')
                safe = sanitize_pill_color(color)
                self._window.evaluate_js(f'setColor("{safe}")' if safe else "setColor(null)")
            self._window.evaluate_js(f'setState("{state}")')
        except Exception:
            pass  # never break the record path
        self._show_window()

    def _show_window(self) -> None:
        self._shown = True
        if self._hwnd:
            try:
                # Reapply defensively: the backend's own show handling can
                # reset the extended styles (see finish_setup in create()).
                apply_styles(self._hwnd)
                # Measured on real Windows: even with WS_EX_NOACTIVATE set,
                # ShowWindow(SW_SHOWNOACTIVATE) can still activate the pill
                # and steal the foreground -- see _start_foreground_guard,
                # which is running continuously by the time any show_* call
                # can reach here and reclaims it.
                user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            except Exception:
                pass

    def _set(self, state: str, theme: str | None = None, color: str | None = None,
              is_current=None, if_current=None) -> None:
        def apply():
            self._apply(state, theme, color)

        try:
            if if_current is not None:
                if_current(apply)
            elif is_current is None or is_current():
                apply()
        except Exception:
            pass  # never break the record path

    def show_recording(self, theme: str = "classic", color: str | None = None,
                        is_current=None, if_current=None):
        self._set("recording", theme=theme, color=color, is_current=is_current,
                   if_current=if_current)

    def show_processing(self, is_current=None, if_current=None):
        self._set("processing", is_current=is_current, if_current=if_current)

    def show_done(self, is_current=None, if_current=None):
        self._set("done", is_current=is_current, if_current=if_current)

    def show_error(self, is_current=None, if_current=None):
        self._set("error", is_current=is_current, if_current=if_current)

    def hide(self, is_current=None, if_current=None):
        def apply():
            self._shown = False
            if self._hwnd:
                try:
                    user32.ShowWindow(self._hwnd, SW_HIDE)
                except Exception:
                    pass

        try:
            if if_current is not None:
                if_current(apply)
            elif is_current is None or is_current():
                apply()
        except Exception:
            pass  # never break the record path
