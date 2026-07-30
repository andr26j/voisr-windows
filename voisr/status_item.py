"""System tray presence for Voisr.

A hotkey-driven app should be reachable without its window open, and closing
the dashboard should not quit the app -- the tray icon is what keeps it
reachable and is how the user quits.

UI only: this touches no input APIs and cannot interfere with the
WH_KEYBOARD_LL hook that drives push-to-talk.

pystray owns its own thread and marshals icon updates internally, so there is
no equivalent of the macOS AppKit main-thread dispatch dance. Every
try/except is kept: a tray failure must never take down the app.
"""
import threading

from PIL import Image, ImageDraw


def _dark_taskbar_from_registry() -> bool:
    """Read the Windows taskbar theme. Defaults to light-on-dark (the
    Windows default taskbar) if the value can't be read."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return value == 0
    except Exception:
        return True


def glyph_image(recording: bool, size: int = 32, dark_taskbar=None) -> Image.Image:
    """Three-bar waveform glyph, drawn programmatically so there is no asset
    to lose. macOS template images auto-invert to match the menu bar;
    Windows tray icons do not, so the bar colour is picked from the taskbar
    theme instead.

    `dark_taskbar=None` means "detect it"; the explicit argument exists so
    tests are deterministic."""
    if dark_taskbar is None:
        dark_taskbar = _dark_taskbar_from_registry()
    # A dark taskbar needs a light glyph to read against it, and vice versa.
    color = (255, 255, 255, 255) if dark_taskbar else (0, 0, 0, 255)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bar_w = max(1, round(size * 4 / 22))
    gap = max(1, round(size * 7 / 22)) - bar_w
    heights = (size * 10 / 14, size * 6 / 14, size * 10 / 14) if recording \
        else (size * 4 / 14,) * 3
    total_w = bar_w * 3 + gap * 2
    x = (size - total_w) / 2
    for bh in heights:
        y0 = (size - bh) / 2
        y1 = y0 + bh
        radius = min(bar_w, bh) / 2
        draw.rounded_rectangle([x, y0, x + bar_w, y1], radius=radius, fill=color)
        x += bar_w + gap
    return img


class StatusItem:
    """Tray icon and menu. `_dispatch(action)` routes every menu action
    through one testable entry point -- the pystray menu items are thin glue
    over it, so menu routing can be tested without a real tray."""

    def __init__(self, on_dictate, on_open, on_quit, hotkey_label_fn):
        self._on_dictate = on_dictate
        self._on_open = on_open
        self._on_quit = on_quit
        self._hotkey_label_fn = hotkey_label_fn
        self._icon = None
        self._thread = None
        self._recording = False

    def _hotkey_menu_text(self) -> str:
        try:
            label = self._hotkey_label_fn()
        except Exception:
            label = "unset"
        return f"Push-to-talk: {label}"

    def _dispatch(self, action: str) -> None:
        handler = {"dictate": self._on_dictate,
                   "open": self._on_open,
                   "quit": self._on_quit}.get(action)
        if handler is None:
            return
        try:
            handler()
        except Exception as e:
            print(f"Voisr status item: {action} failed: {e}", flush=True)

    def _build_menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem("Dictate now", lambda: self._dispatch("dictate")),
            pystray.MenuItem("Open Dashboard", lambda: self._dispatch("open")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: self._hotkey_menu_text(), lambda: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Voisr", lambda: self._dispatch("quit")),
        )

    def create(self) -> bool:
        """Create and show the tray icon. Must not block -- pystray's
        Icon.run() blocks forever pumping its own message loop, so it runs
        on a daemon thread here rather than the caller's thread. A daemon
        thread means a stuck tray loop can never keep the process alive
        after everything else has shut down.

        Returns False (and logs) if it cannot be created -- a missing tray
        icon must never stop the app from running."""
        if self._icon is not None:
            return True
        try:
            import pystray

            icon = pystray.Icon(
                "voisr",
                icon=glyph_image(recording=False),
                title="Voisr",
                menu=self._build_menu(),
            )
            self._icon = icon
            self._thread = threading.Thread(
                target=icon.run, name="voisr-tray", daemon=True)
            self._thread.start()
        except Exception as e:
            print(f"Voisr status item: create failed: {e}", flush=True)
            self._icon = None
            self._thread = None
            return False
        return True

    def set_recording(self, is_recording: bool) -> None:
        if self._icon is None:
            return
        self._recording = bool(is_recording)
        try:
            self._icon.icon = glyph_image(recording=self._recording)
        except Exception as e:
            print(f"Voisr status item: set_recording failed: {e}", flush=True)

    def release(self) -> None:
        icon, self._icon = self._icon, None
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:
            pass
