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

from voisr import logo

# Recording badge, drawn over the mark's lower-right corner. A colour
# logo cannot signal state by changing its own bars -- at 16px that
# difference is invisible -- so the state is carried by a badge instead,
# the same convention Teams and Zoom use in the notification area.
BADGE_FILL = (0xE5, 0x1C, 0x23, 255)
BADGE_RING = (255, 255, 255, 255)


def glyph_image(recording: bool, size: int = 32, dark_taskbar=None) -> Image.Image:
    """The Voisr mark for the notification area, with a red badge while a
    dictation is in flight.

    Shares voisr.logo's geometry with the taskbar and Start Menu icon, so
    the app presents one mark everywhere rather than a bespoke tray glyph.

    `dark_taskbar` is accepted and ignored: it exists because macOS template
    images auto-invert against the menu bar, and the previous monochrome
    glyph had to emulate that. A full-colour mark carries its own rounded
    background and reads on a light or dark taskbar alike -- inverting it
    would corrupt the brand colour. The parameter is kept so callers and
    tests that pass it keep working.
    """
    img = logo.render(size)

    if not recording:
        return img

    draw = ImageDraw.Draw(img)
    # Sized off the frame so it stays visible at 16px without swamping 48px.
    d = max(5, round(size * 0.42))
    x1, y1 = size - 1, size - 1
    x0, y0 = x1 - d, y1 - d
    ring = max(1, round(size * 0.055))
    draw.ellipse((x0 - ring, y0 - ring, x1, y1), fill=BADGE_RING)
    draw.ellipse((x0, y0, x1 - ring, y1 - ring), fill=BADGE_FILL)
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
