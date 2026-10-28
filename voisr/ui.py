"""Dashboard window: the js_api surface (`Api`), the push-to-talk
`HotkeyService`, and `main()`.

Ported from the macOS `voisr/ui.py`. Everything that already lives in a
dedicated Windows module is imported rather than re-implemented:
`voisr.keys` (hotkey table), `voisr.keytap` (the WH_KEYBOARD_LL listener),
`voisr.overlay` (the floating pill), `voisr.permissions` (microphone probe),
`voisr.window_chrome` (DWM dark titlebar), `voisr.launch_agent` (per-user Run
key), and `voisr.status_item` (the tray icon).
"""
import functools
import json
import os
import threading
import time
from pathlib import Path

import voisr
from voisr import launch_agent, permissions, speech_gate
from voisr.app import config_errors, load_config
from voisr.app import config_path as default_config_path
from voisr.cleanup import clean
from voisr.history import HistoryStore
from voisr.inject import paste_at_cursor, set_clipboard
from voisr.keys import DEFAULT_HOTKEY, hotkey_options, key_label, resolve_key
from voisr.keytap import WindowsKeyListener, capture_next_vk
from voisr.overlay import RecordingWidget, sanitize_pill_color, sanitize_pill_theme
from voisr.pasteboard import PasteDeliveryError
from voisr.paths import app_data_dir, app_icon
from voisr.paths import web_dir as _web_dir
from voisr.recording_queue import (
    RecordingQueue,
    RecordingQueueError,
    RecordingSession,
    SessionResult,
)
from voisr.status_item import StatusItem
from voisr.stream_recorder import StreamRecorder
from voisr.updater import UpdateChecker
# _log, not print: the shipped build is windowed (console=False), where
# sys.stdout is None and print() is a silent no-op. Shared with updater.py
# so both halves of an update failure land in the same file.
from voisr.updater import _log as _update_log
from voisr.window_chrome import apply_chrome

WEB_DIR = _web_dir()
WINDOW_TITLE = "Voisr"

# Taskbar identity. Without an explicit AppUserModelID, Windows attributes
# our window to the host process (pythonw.exe) -- so it groups with any
# other Python app and carries their icon and name rather than ours.
APP_USER_MODEL_ID = "Voisr.Voisr"


def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Claim a distinct taskbar identity. Cosmetic: never raises."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception:
        return False

# Adding a theme? Also update: widget.html (theme CSS + THEME_VALUES),
# style.css (mini-pill rule + keyframes), api.js's dev-mock get_pill_themes,
# and voisr.overlay._PILL_THEME_VALUES. test_widget_html_covers_all_pill_themes
# guards widget.html.
PILL_THEMES = [
    {"value": "classic", "label": "Classic"},
    {"value": "ember", "label": "Ember"},
    {"value": "aurora", "label": "Aurora"},
    {"value": "ocean", "label": "Ocean"},
    {"value": "neon", "label": "Neon EQ"},
    {"value": "breathe", "label": "Breathe"},
    {"value": "scanner", "label": "Scanner"},
]


def wav_seconds(path) -> float:
    """Length of a WAV in seconds, -1 if unparseable (skip the check)."""
    import wave
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return -1.0


# Shorter than any spoken word: a tap, or a hotkey used as a modifier. The
# WAV is measured, not the key hold, because the hold includes mic start-up.
MIN_SPEECH_S = 0.3
# Exact zeros from start to finish: Windows gives an app that microphone
# access is switched off for digital silence, and so does a wedged device.
MIC_SILENT_MESSAGE = (
    "the microphone sent only silence — check that microphone access is on "
    "in Settings → Privacy & security → Microphone, and the input device in "
    "Settings → System → Sound")
NO_SPEECH_MESSAGE = "didn't catch that — hold the key and speak"

# whisper.cpp truncates --prompt at ~224 tokens with no warning, so a large
# dictionary would silently lose its newest terms. Keep the most recently
# added ones (dict insertion order) and bound the text.
GLOSSARY_MAX_TERMS = 40
GLOSSARY_MAX_CHARS = 600


def glossary_prompt(dictionary) -> str | None:
    """Bias whisper toward the user's spellings: 'Glossary: A, B, C.'"""
    if not dictionary:
        return None
    seen = []
    for written in reversed(list(dictionary.values())):
        if written not in seen:
            seen.append(written)
        if len(seen) >= GLOSSARY_MAX_TERMS:
            break
    seen.reverse()
    while seen and len("Glossary: " + ", ".join(seen) + ".") > GLOSSARY_MAX_CHARS:
        seen.pop(0)
    return ("Glossary: " + ", ".join(seen) + ".") if seen else None


def capture_next_key(timeout: float = 8.0):
    """Block until the user presses any key; return (vk, label) or None."""
    vk = capture_next_vk(timeout)
    if vk is None:
        return None
    return vk, key_label(vk)


def _find_window_hwnd():
    """HWND of the main dashboard window, found by title -- the same
    FindWindowW-by-title pattern voisr.overlay uses for the pill. Never
    raises; chrome is cosmetic."""
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        return hwnd or None
    except Exception:
        return None


SW_RESTORE = 9


class _User32:
    """Adapter around the user32 calls that un-minimize a window, with
    explicit ctypes types. Mirrors voisr.window_chrome._Dwm so tests can
    inject a fake instead of touching the real Win32 API."""

    def __init__(self):
        import ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = ctypes.c_bool
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._user32 = user32

    def is_minimized(self, hwnd) -> bool:
        return bool(self._user32.IsIconic(hwnd))

    def restore(self, hwnd) -> None:
        self._user32.ShowWindow(hwnd, SW_RESTORE)


def present_window(window, user32=None) -> None:
    """Bring the dashboard window back for the tray's Open Dashboard.

    `window.show()` alone is not enough. pywebview's winforms backend maps
    it to Form.Show() + Form.Activate(), which un-hides a window that was
    closed to the tray but leaves a *minimized* one minimized -- hidden and
    minimized are different window states, and Show() only touches the
    first. ShowWindow(SW_RESTORE) is what un-minimizes (and activates) it.

    The SW_RESTORE is guarded by IsIconic because it would shrink an
    already-visible maximized window back to its restored size. That guard
    is also why this doesn't use pywebview's own `window.restore()`, which
    forces FormWindowState.Normal unconditionally and so loses the
    maximized state a window was minimized from.

    Never raises: a failing tray action must not take down the app."""
    try:
        window.show()
    except Exception:
        pass
    try:
        hwnd = _find_window_hwnd()
        if not hwnd:
            return
        if user32 is None:
            user32 = _User32()
        if user32.is_minimized(hwnd):
            user32.restore(hwnd)
    except Exception:
        pass


def destroy_windows(window, widget) -> None:
    """Destroy every pywebview window the app registered, so the WinForms
    message loop actually ends and the process exits.

    Destroying the dashboard alone was not enough. pywebview's winforms
    backend only reaches WinForms.Application.Exit() once
    BrowserView.instances is empty (see BrowserForm.on_close), and main()
    calls widget.create() before create_window(dashboard), so the pill holds
    the 'master' uid and kept that dict non-empty. The dashboard vanished and
    the process stayed alive -- still holding the global right-Ctrl hotkey, so
    the user could not dictate anywhere until they killed it in Task Manager,
    and a relaunch produced a second instance.

    Dashboard first, pill last. Both forms live on the same STA GUI thread
    (the backend creates non-master windows via Invoke onto master's thread),
    so order does not change correctness -- but it closes the window the user
    is looking at immediately, and leaves 'master', the form that owns
    Application.Run(), destroyed last as in the ordinary single-window case.

    Each destroy is guarded independently: a pill that never created, or a
    dashboard whose backend has already gone, must not strand the other window
    and leave the loop running. Never raises -- this is the tray's Quit
    handler and the route a finished install exits by."""
    for target in (window, widget):
        if target is None:
            continue
        try:
            target.destroy()
        except Exception:
            pass


def _system_prefers_dark() -> bool:
    """Read the Windows "app mode" theme preference. Defaults to dark (this
    app's own default appearance) if the value can't be read."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except Exception:
        return True


def _run_save_dialog(suggested_name: str):
    """Native Win32 "Save As" dialog. Returns the chosen path, or None if
    the user cancelled."""
    import win32gui
    import pywintypes
    try:
        path, _, _ = win32gui.GetSaveFileNameW(
            File=suggested_name, Filter="All Files\x00*.*\x00\x00")
        return path or None
    except pywintypes.error as error:
        if error.winerror == 0:
            return None  # user cancelled
        raise


def _log(message: str) -> None:
    """Hotkey diagnostics. Never raises: a broken log must not swallow the
    dictation path."""
    try:
        _update_log(f"hotkey: {message}")
    except Exception:
        pass


def _hook_listener_factory(handler):
    """Create and start the WH_KEYBOARD_LL listener (see voisr.keytap)."""
    listener = WindowsKeyListener(handler)
    listener.start()
    return listener


# Hotkey timing (Wispr's observed values)
TAP_GATE_S = 0.5  # let go sooner: a tap, unless a second press follows within this
CHORD_WINDOW_S = 1.0  # another key this soon after the press: a shortcut
ESC_VK = 0x1B
STALE_CHECK_S = 5.0  # how often a long hold is checked against the keyboard
STALE_AFTER_S = 10.0  # a hold this long has its key state confirmed


def hotkey_key_down(vk):
    """The keyboard's own reading of whether `vk` is down: True or False, or
    None when it can't be read. Never raises."""
    try:
        import ctypes
        state = ctypes.WinDLL("user32").GetAsyncKeyState(int(vk))
        return bool(state & 0x8000)
    except Exception:
        return None


def _daemon_timer(delay, fn):
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    return timer


class HotkeyService:
    """Global push-to-talk listener driving the shared Api record path."""

    PANE_REOPEN_S = 60  # a refused permission's pane opens at most this often

    def __init__(self, api, widget=None, hide_delay=1.2,
                 permissions_check=lambda: True, poll_interval=3.0,
                 listener_factory=None, tap_gate_s=TAP_GATE_S,
                 timer_factory=None, key_state=None,
                 stale_interval=STALE_CHECK_S):
        self._api = api
        self._widget = widget
        self._hide_delay = hide_delay
        self._permissions_check = permissions_check
        self._poll_interval = poll_interval
        self._listener_factory = listener_factory or _hook_listener_factory
        self._pane_opener = permissions.open_pane
        self._clock = time.monotonic
        self._pane_opened_at = None
        self._listener = None
        self._watcher = None
        self._wake = threading.Event()  # set by stop() to end a sleeping poll
        self._stopped = True  # until start(); see running
        self._pressed = False
        self._latched = False  # hands-free: the tray toggle, or a double-tap
        self._latch_armed = False  # hands-free and the key is up: the next press stops it
        self._pressed_at = None  # event time of the press that owns the recording
        # event time of the second press that latched a double-tap, until its
        # release is seen: the latch is provisional (held >= the gate, that
        # press was a dictation). None for toggles and once released.
        self._latch_press_at = None
        self._tap_gate_s = tap_gate_s
        self._timer_factory = timer_factory or _daemon_timer
        self._pending = None  # token while a quick tap waits for a second press
        self._pending_from = None  # event time that tap was let go
        self._pending_timer = None
        # Opt-in (main() passes hotkey_key_down): None means no stuck-key
        # check, so tests never read the real keyboard.
        self._key_state = key_state
        self._key_trusted = False  # the probe has read the held key as down
        self._stale_interval = stale_interval
        self._ticker = None
        self._tick_wake = threading.Event()  # set by stop() to end the ticker
        # This press already reported it couldn't start. Holding the key
        # auto-repeats WM_KEYDOWN, and without this every repeat would
        # hammer the queue and re-report: one report per physical hold.
        self._rejected = False
        # press() calls start_recording() with _state_lock released (it can
        # take seconds, and the pill's callbacks take the lock). While it
        # runs _pressed is already True, so auto-repeat stays out; a release
        # or chord in the gap is noted here and settled after.
        self._starting = False
        self._start_released = False
        self._start_released_at = None
        self._start_cancelled = False
        self._key_name = None
        self._target_vk = None
        self._state_lock = threading.RLock()
        self._epoch = 0  # bumped per recording; stale _finish threads must not touch the widget
        # Key events are handed to this serial worker instead of being acted
        # on inside the hook callback: press() can wait on the previous
        # recorder's stop(), and Windows silently unregisters a
        # WH_KEYBOARD_LL hook whose callback exceeds LowLevelHooksTimeout --
        # with every event in the gap lost, including the release that ends
        # the dictation. One worker keeps press/release ordering exactly as
        # the keyboard sent it.
        self._ops = None
        self._ops_thread = None
        self.status = "not started"
        # active | starting | failed | off -- see _refresh_status
        self.health = "off"

    _OPS_STOP = object()

    @property
    def running(self) -> bool:
        """False while stopped, e.g. for capture_hotkey's 8 s key capture,
        when status still reads "active on ..."."""
        return not self._stopped

    @property
    def holding(self) -> bool:
        """True while the key itself owns the current recording."""
        return self._pressed

    def _submit(self, fn) -> None:
        """Run fn on the serial worker; inline if the worker is not running
        (direct callers such as tests and the CLI)."""
        ops = self._ops
        if ops is None:
            fn()
            return
        ops.put(fn)

    def _start_worker(self) -> None:
        import queue as queue_module
        if self._ops is not None:
            return
        ops = queue_module.Queue()

        def run():
            while True:
                fn = ops.get()
                if fn is self._OPS_STOP:
                    return
                try:
                    fn()
                except Exception:
                    pass  # a failed op must never kill the worker

        self._ops = ops
        self._ops_thread = threading.Thread(target=run, name="voisr-hotkey-ops",
                                            daemon=True)
        self._ops_thread.start()

    def _stop_worker(self) -> None:
        ops, self._ops = self._ops, None
        if ops is not None:
            ops.put(self._OPS_STOP)

    def _widget_enabled(self) -> bool:
        return self._widget is not None and bool(self._api.get_config().get("show_widget", True))

    def _run_widget_action(self, epoch: int, action) -> None:
        with self._state_lock:
            if epoch == self._epoch:
                action()

    def _widget_action(self, name: str, epoch: int, *args, **kwargs) -> None:
        method = getattr(self._widget, name)
        if getattr(self._widget, "_supports_ownership_action", False):
            method(*args, **kwargs,
                   if_current=lambda action: self._run_widget_action(epoch, action))
        else:
            method(*args, **kwargs)

    def press(self, latched=False, at=None) -> None:
        at = self._clock() if at is None else at
        if self._settle_pending(at):
            return
        finish = None
        with self._state_lock:
            if self._pressed:
                # auto-repeat, or the key already held: only a fresh press
                # of a hands-free dictation stops it
                if latched or not (self._latched and self._latch_armed):
                    return
                finish = self._end_recording()
            else:
                if self._rejected and not latched:
                    return  # auto-repeat of a press that couldn't start
                self._pressed = True
                self._pressed_at = at
                self._latched = latched
                self._latch_press_at = None
                # a toggle isn't held: its next press (any key) stops it
                self._latch_armed = latched
                self._starting = True
                self._start_released = False
                self._start_released_at = None
                self._start_cancelled = False
        if finish is not None:
            self._spawn_finish(*finish)
            return
        try:
            result = self._api.start_recording()
        except Exception as error:
            result = {"error": str(error)}
        except BaseException:
            # nothing will settle this press: un-mark it or the key is dead
            with self._state_lock:
                self._starting = False
                self._pressed = False
                self._latched = False
            raise
        pane = None
        refused = False
        learn = False
        with self._state_lock:
            self._starting = False
            released, cancelled = self._start_released, self._start_cancelled
            if "error" in result:
                self._pressed = False
                self._latched = False
                # e.g. the dashboard owns the mic, the microphone is
                # unplugged, or the queue is full. Say so once per press.
                if not self._rejected:
                    # The log is the only durable trace that separates a
                    # missing device from a broken release.
                    _log(f"press could not start recording: {result['error']}")
                    self._flash_error()
                # a key already let go needs no guard against its repeats
                self._rejected = not released
                pane = self._claim_pane_open(result.get("permission"))
                refused = True
            else:
                self._rejected = False
                self._epoch += 1
                epoch = self._epoch
                if cancelled:
                    # still hide: the new epoch orphans an older pill's own hide
                    self._drop_chord(epoch)
                else:
                    cfg = self._api.get_config()
                    if self._widget is not None and bool(cfg.get("show_widget", True)):
                        self._widget_action(
                            "show_recording", epoch,
                            theme=sanitize_pill_theme(cfg.get("pill_theme")),
                            color=sanitize_pill_color(cfg.get("pill_color")))
                    if released:
                        # a toggle ends at once; a held key goes through the gate
                        finish = (self._end_recording() if self._latched
                                  else self._release_held(self._start_released_at))
                    else:
                        learn = not self._latched
        if refused:
            if pane is not None:
                try:
                    self._pane_opener(pane)
                except Exception:
                    pass
            return
        if cancelled:
            self._api._cancel_recording()
        if finish is not None:
            self._spawn_finish(*finish)
        elif learn:
            self._learn_key_state()

    def _settle_pending(self, at) -> bool:
        """A press while a quick tap waits. Within the tap gate it makes the
        dictation hands-free (True: nothing else to do). Later, the tap is
        dropped first and the press starts afresh (False)."""
        with self._state_lock:
            if self._pending is None:
                return False
            self._clear_pending()
            if at - self._pending_from <= self._tap_gate_s:
                self._latched = True
                self._latch_armed = False  # this press's own release comes first
                self._latch_press_at = at
                latched = True
            else:
                latched = False
                self._drop_chord(self._epoch)
        if latched:
            _log("hands-free on (double-tap)")
            return True
        self._api._cancel_recording()
        return False

    def _clear_pending(self) -> None:
        """Forget a waiting tap; caller holds _state_lock."""
        timer, self._pending_timer = self._pending_timer, None
        self._pending = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _release_held(self, at):
        """The held key let go; caller holds _state_lock. Sooner than the tap
        gate it's a tap: keep recording, wait for a second press, and return
        None. Otherwise end the recording (the result is for _spawn_finish)."""
        if (self._tap_gate_s and at is not None and self._pressed_at is not None
                and at - self._pressed_at < self._tap_gate_s):
            token = object()
            self._pending = token
            self._pending_from = at
            delay = max(0.0, at + self._tap_gate_s - self._clock())
            timer = self._timer_factory(
                delay, lambda: self._submit(lambda: self._expire_tap(token)))
            self._pending_timer = timer
            timer.start()
            return None
        return self._end_recording()

    def _expire_tap(self, token) -> None:
        """No second press came: the tap is dropped, silently."""
        with self._state_lock:
            if self._pending is not token:
                return
            held = ((self._pending_from - self._pressed_at)
                    if self._pressed_at is not None else 0.0)
            self._clear_pending()
            self._drop_chord(self._epoch)
        _log(f"tap dropped (held {held:.2f} s)")
        self._api._cancel_recording()

    def _claim_pane_open(self, key):
        """Open a refused permission's Settings page at most once a minute;
        caller holds _state_lock."""
        if not key:
            return None
        now = self._clock()
        if (self._pane_opened_at is not None
                and now - self._pane_opened_at < self.PANE_REOPEN_S):
            return None
        self._pane_opened_at = now
        return key

    def _flash_error(self) -> None:
        """Error pill, then hide; caller holds _state_lock. The timed hide
        runs on its own thread: nothing here may wait."""
        if not self._widget_enabled():
            return
        self._epoch += 1
        epoch = self._epoch
        self._widget_action("show_error", epoch)

        def hide_later():
            try:
                if self._hide_delay:
                    time.sleep(self._hide_delay)
                with self._state_lock:
                    if epoch == self._epoch:
                        self._widget_action("hide", epoch)
            except Exception:
                pass

        threading.Thread(target=hide_later, name=f"voisr-widget-failed-{epoch}",
                         daemon=True).start()

    def release(self, at=None) -> None:
        at = self._clock() if at is None else at
        with self._state_lock:
            if not self._pressed:
                # Cleared on key up, not on the next press: a microphone
                # plugged back in must recover on the very next hold.
                self._rejected = False
                return
            if self._latched:
                held_from, self._latch_press_at = self._latch_press_at, None
                if (held_from is not None
                        and at - held_from >= self._tap_gate_s):
                    # the second press was held, not tapped: a dictation
                    # that ends on its release, not a hands-free latch
                    finish = self._end_recording()
                else:
                    self._latch_armed = True  # hands-free: the next press stops it
                    return
            elif self._pending is not None:
                return  # already let go: a repeated edge
            elif self._starting:
                self._start_released = True  # press() ends it once started
                self._start_released_at = at
                return
            else:
                finish = self._release_held(at)
        if finish is not None:
            self._spawn_finish(*finish)

    def _end_recording(self):
        """Close the mic and show processing; caller holds _state_lock and
        passes the result to _spawn_finish after letting go of it."""
        self._pressed = False
        epoch = self._epoch
        show = self._widget_enabled()
        if show:
            self._widget_action("show_processing", epoch)
        future = self._api._release_recording(paste=True)
        return future, show, epoch

    def _spawn_finish(self, future, show: bool, epoch: int) -> None:
        threading.Thread(
            target=self._finish,
            args=(future, show, epoch),
            name=f"voisr-widget-{epoch}",
            daemon=True,
        ).start()

    def _finish(self, future, show: bool, epoch: int) -> None:
        try:
            result = future.result().to_api_dict()
        except Exception as error:
            result = {"error": f"processing failed: {error}"[:250]}
        try:
            with self._state_lock:
                if not show or epoch != self._epoch:
                    return
                if "error" in result:
                    self._widget_action("show_error", epoch)
                else:
                    self._widget_action("show_done", epoch)
            if self._hide_delay:
                time.sleep(self._hide_delay)
            with self._state_lock:
                if epoch == self._epoch:
                    self._widget_action("hide", epoch)
        except Exception:
            pass

    def _resolve_target(self, key_name=None):
        """Return (vk, label) for the push-to-talk key.

        Explicit key_name (tests/CLI) wins; otherwise the config's captured
        hotkey_vk, falling back to the named 'hotkey'."""
        if key_name is not None:
            vk = resolve_key(key_name)
            return vk, key_label(vk) if vk is not None else key_name
        cfg = self._api.get_config()
        vk = cfg.get("hotkey_vk")
        if vk is not None:
            return int(vk), cfg.get("hotkey_label") or key_label(int(vk))
        name = cfg.get("hotkey", DEFAULT_HOTKEY)
        vk = resolve_key(name)
        return vk, key_label(vk) if vk is not None else name

    def start(self, key_name=None) -> None:
        self._stopped = False
        vk, label = self._resolve_target(key_name)
        if vk != self._target_vk:
            self._key_trusted = False  # the probe was read on another key
        self._target_vk = vk
        self._key_name = label
        self._start_worker()
        self._spawn_listener()
        self._refresh_status()
        self._watch()
        self._start_ticker()

    def _toggle(self) -> None:
        """The tray's Dictate now: the first toggle starts a hands-free
        dictation, the second ends it, however it began."""
        if self._pressed:
            self._stop_hands_free()
        else:
            # every toggle is a press of its own, and a refused one gets no
            # release() to clear the once-per-press guard
            self._rejected = False
            self.press(latched=True)

    def toggle(self) -> None:
        """press()/release() pair for a single-action trigger (the tray
        menu's "Dictate now"), on the worker so the tray's own event loop
        never waits on queue admission."""
        self._submit(self._toggle)

    def _stop_hands_free(self) -> None:
        with self._state_lock:
            if not self._pressed:
                return
            if self._starting:
                self._start_released = True  # press() ends it once started
                self._start_released_at = None  # not a key hold: no tap gate
                return
            self._clear_pending()
            finish = self._end_recording()
        self._spawn_finish(*finish)

    def cancel_chord(self, at=None) -> None:
        """Another key went down while the hotkey was held. Within
        CHORD_WINDOW_S of the press it's a shortcut (Right Ctrl+C), not a
        dictation: cancel. Held longer, the person is dictating and the key
        is theirs. A hands-free dictation isn't held, so typing during it is
        left alone; a quick tap then typing is dropped. A double-tap's second
        press is still held until it is let go (the latch is provisional),
        so it counts as held here."""
        at = self._clock() if at is None else at
        with self._state_lock:
            if not self._pressed or (self._latched
                                     and self._latch_press_at is None):
                return
            was_held = self._pending is None
            if self._pending is not None:
                self._clear_pending()
            elif (self._pressed_at is not None
                    and at - self._pressed_at >= CHORD_WINDOW_S):
                return
            elif self._starting:
                # once let go, a key going down is typing, not a chord
                if not self._start_released:
                    self._start_cancelled = True
                return
            self._drop_chord(self._epoch)
        if was_held:
            _log("dictation cancelled (another key within 1 s)")
        self._api._cancel_recording()

    def cancel_escape(self) -> None:
        """Esc cancels a dictation in progress, however it began: the audio
        is dropped and nothing is pasted. Esc itself still reaches the app."""
        with self._state_lock:
            if not self._pressed:
                return
            if self._starting:
                self._start_cancelled = True
                return
            self._clear_pending()
            self._drop_chord(self._epoch)
        _log("dictation cancelled by Esc")
        self._api._cancel_recording()

    def _learn_key_state(self) -> None:
        """Trust the key-state probe once it has read the held hotkey as
        down: a probe that is blind (always False) would otherwise end every
        long dictation at the first stuck-key check."""
        if self._key_trusted or self._key_state is None:
            return
        try:
            down = self._key_state(self._target_vk)
        except Exception:
            return
        if down is True:
            self._key_trusted = True
            _log("key state readable — stuck-key check on")

    def _check_stale_key(self, force=False) -> None:
        """A held key whose release never arrived (lost while Windows had
        unhooked us) would record until the 600 s cap. A hold of
        STALE_AFTER_S or more -- or any hold, right after the hook was
        respawned -- is checked against the keyboard; up means the release
        is made up here. Runs on the worker, in order with the key events."""
        probe = self._key_state
        if probe is None or not self._key_trusted:
            return
        if self._stopped:
            return  # a check queued before stop() must not act on a dead service
        now = self._clock()
        with self._state_lock:
            if (not self._pressed or self._latched or self._starting
                    or self._pending is not None or self._pressed_at is None):
                return
            if not force and now - self._pressed_at < STALE_AFTER_S:
                return
            vk = self._target_vk
        try:
            down = probe(vk)
        except Exception:
            return
        if down is not False:
            return  # down, or unknown: unknown is never a release
        _log("the key is up but its release never arrived — ending the dictation")
        self.release(at=now)

    def _start_ticker(self) -> None:
        """Every _stale_interval, queue a stuck-key check on the worker."""
        if self._key_state is None or self._ticker is not None:
            return
        self._tick_wake.clear()

        def tick():
            me = threading.current_thread()
            while not self._stopped and self._ticker is me:
                if self._tick_wake.wait(self._stale_interval):
                    return
                if self._stopped or self._ticker is not me:
                    return
                self._submit(self._check_stale_key)

        self._ticker = threading.Thread(target=tick, name="voisr-hotkey-stale",
                                        daemon=True)
        self._ticker.start()

    def _drop_chord(self, epoch: int) -> None:
        """Un-press and hide; caller holds _state_lock, then cancels the
        recording after letting go of it: closing a mic can block, and the
        pill's callbacks take the lock."""
        self._pressed = False
        if self._widget_enabled():
            self._widget_action("hide", epoch)

    def _spawn_listener(self) -> None:
        target_vk = self._target_vk

        def handler(vk, kind):
            # Never raise, and never wait: a callback error must not kill
            # the hook, and a slow one gets it unregistered. Stopped: the
            # worker is gone, so _submit would run press() and friends
            # inline on the hook thread.
            if self._stopped:
                return
            try:
                # stamped here: the worker may run the op much later, and
                # the tap gate and chord window measure the keyboard's timing
                at = self._clock()
                if vk != target_vk:
                    if kind == "down":
                        # user is typing manually -> don't smart-join the
                        # next dictation onto the previous one
                        self._api.note_user_typing()
                        if vk == ESC_VK:
                            self._submit(self.cancel_escape)
                        else:
                            self._submit(functools.partial(self.cancel_chord,
                                                           at=at))
                    return
                if kind == "down":       # auto-repeats; press() guards
                    self._submit(functools.partial(self.press, at=at))
                elif kind == "up":
                    self._submit(functools.partial(self.release, at=at))
            except Exception:
                pass

        old, self._listener = self._listener, None
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        try:
            self._listener = self._listener_factory(handler)
        except Exception as e:
            _log(f"listener start failed: {e}")

    def _hook_state(self, granted: bool) -> str:
        """What Windows actually gave us: "running", "starting" (not decided
        yet) or "failed". A listener without wait_started is a test double
        (a bare handler, not a hook): it counts as running only if the
        permission check passed."""
        listener = self._listener
        if listener is None:
            return "failed"
        wait = getattr(listener, "wait_started", None)
        if wait is None:
            return "running" if granted else "failed"
        return wait(0.5)

    def _refresh_status(self) -> None:
        # May block up to 0.5 s in wait_started: only start() and the poll
        # thread call this, never the hook callback.
        before = self.status
        hook = self._hook_state(self._permissions_check())
        if self._stopped:
            return  # stop() already ran: health stays "off"
        if hook == "running":
            self.health = "active"
            self.status = f"active on {self._key_name}"
        elif hook == "starting":
            self.health = "starting"
            self.status = f"starting push-to-talk on {self._key_name}…"
        else:
            self.health = "failed"
            self.status = "the keyboard hook could not be installed — retrying"
        if self.status != before:  # the poll re-derives this every interval
            _log(self.status)

    def _watch(self) -> None:
        """Respawn the hook if it goes quiet. A WH_KEYBOARD_LL hook whose
        callback exceeds LowLevelHooksTimeout gets silently unregistered by
        Windows -- WindowsKeyListener.state goes "failed" when that happens
        (see voisr.keytap), which is what this polls for. A release may have
        been lost in the gap, so a respawn also checks the held key."""
        if self._watcher is not None:
            return
        self._wake.clear()

        def poll():
            me = threading.current_thread()
            while not self._stopped and self._watcher is me:
                # wait, not sleep: stop() wakes this immediately
                if self._wake.wait(self._poll_interval):
                    return
                if self._stopped or self._watcher is not me:
                    return
                listener = self._listener
                state = getattr(listener, "state", None)
                if listener is None or state == "failed" or (
                        state is None and not getattr(listener, "running", True)):
                    self._spawn_listener()
                    self._submit(functools.partial(self._check_stale_key,
                                                   force=True))
                self._refresh_status()

        self._watcher = threading.Thread(target=poll, name="voisr-hotkey-watch",
                                         daemon=True)
        self._watcher.start()

    def stop(self) -> None:
        self._stopped = True
        self._stop_worker()
        # Wake and join the poll so a following start() never races a
        # watcher that is still winding down.
        watcher, self._watcher = self._watcher, None
        if watcher is not None and watcher is not threading.current_thread():
            self._wake.set()
            watcher.join(timeout=2.0)
        ticker, self._ticker = self._ticker, None
        if ticker is not None and ticker is not threading.current_thread():
            self._tick_wake.set()
            ticker.join(timeout=2.0)
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass  # a dead listener's teardown must not crash the app
        # last, so a poll that was mid-refresh can't leave a stale health
        self.health = "off"

    def restart(self, key_name=None) -> None:
        self.stop()
        self.start(key_name)


_UPDATES_UNAVAILABLE = "Updates are unavailable in this build."


class Api:
    """What the dashboard talks to. Only BRIDGE_METHODS reach the page, via
    bridge_functions; never pass this as js_api."""

    SPACE_JOIN_WINDOW_S = 300  # only join dictations this close together
    APPEARANCES = ("system", "light", "dark")
    # How long read_config_cached() may serve a parsed config without going
    # back to disk. Short enough that a hand-edit of config.json lands almost
    # at once, long enough that polling status() is free. See that method.
    CONFIG_CACHE_TTL_S = 5.0
    # Grace period between answering install_update() and tearing the app
    # down, so the awaiting JS caller gets its response first.
    SHUTDOWN_DELAY_S = 0.75
    # How long the clean shutdown gets before the hard exit. See
    # _shutdown_soon for why an app still alive by then must not stay up.
    # menu_quit now destroys the pill as well as the dashboard, which is what
    # lets pywebview reach Application.Exit() at all -- and that path runs
    # clear_user_data(): a process.WaitForExit(3000) plus an rmtree of the
    # WebView2 profile. The budget still holds. Measured end-to-end against
    # the real EdgeChromium backend, both windows destroyed, the teardown
    # takes ~1.7s -- and _shutdown_soon arms this backstop *before* calling
    # quit_fn, so a slow quit spends slack instead of pushing the deadline
    # out. Don't shrink this below ~4s without re-measuring that teardown.
    SHUTDOWN_HARD_S = 5.0

    def __init__(self, config_path=None,
                 history=None, recorder_factory=StreamRecorder,
                 transcribe_fn=None,
                 clock=time.monotonic, hotkey_service=None,
                 fallback_recorder_factory=None,
                 capture_fn=capture_next_key,
                 paste_fn=paste_at_cursor,
                 clipboard_fn=set_clipboard,
                 save_dialog_fn=None,
                 timer_factory=threading.Timer,
                 exit_fn=None,
                 mic_check=None):
        self._config_path = Path(config_path) if config_path else default_config_path()
        self._history = history or HistoryStore(self._config_path.parent / "history.jsonl")
        self._transcribe_fn = transcribe_fn
        self._clock = clock
        self._hotkey_service = hotkey_service
        self._capture_fn = capture_fn
        self._paste_fn = paste_fn
        self._clipboard_fn = clipboard_fn
        self._save_dialog_fn = save_dialog_fn or _run_save_dialog
        self._timer_factory = timer_factory
        self._exit_fn = exit_fn or (lambda: os._exit(0))
        # None = no check, so tests never probe the machine's real privacy
        # settings
        self._mic_check = mic_check
        # Both None until the matching attach_*(): a headless or test Api has
        # no updater and no window, and every method below tolerates that.
        self._updates = None
        self._quit_fn = None
        # (timestamp, config) for read_config_cached; see it for the rules.
        # The generation counter is bumped by every invalidation so an
        # in-flight read can tell that its result is already out of date.
        self._config_cache = None
        self._config_cache_generation = 0
        self._config_cache_lock = threading.Lock()
        self._last_paste_text = None
        self._last_paste_ts = None
        self._typing_generation = 0
        self._last_paste_typing_generation = 0
        self._paste_state_lock = threading.Lock()
        self._window = None
        self._status_item = None
        self._is_recording = False
        self._quitting = False  # set by close(): queued dictations still land
        self._sessions_in_flight = set()
        self._glyph_busy = False
        self._active_sequence = None
        self._session_lock = threading.Lock()
        # Serialises every read-modify-write of config.json in this process
        # (set_config from pywebview workers, capture_hotkey, launch-at-login).
        self._config_lock = threading.RLock()
        self._config_stat_cache = None  # (stat key, parsed config) or None
        self._config_notice = None  # set when a corrupt config was moved aside
        self._recordings = RecordingQueue(
            recorder_factory=recorder_factory,
            fallback_recorder_factory=fallback_recorder_factory,
            processor=self._process_session,
            clock=clock,
        )

    def _default_transcribe(self, wav, cfg):
        """Transcribe with the release-time model and dictionary prompt."""
        from voisr.transcriber import resolve_model, transcribe as _transcribe
        return _transcribe(wav, model_path=resolve_model(cfg.get("model")),
                           prompt=glossary_prompt(cfg.get("dictionary")))

    def _transcribe_audio(self, wav, cfg):
        if self._transcribe_fn is not None:
            return self._transcribe_fn(wav)
        return self._default_transcribe(wav, cfg)

    def current_level(self):
        """Live mic level of the dictation being captured (0..1), or None.
        Read by the pill's 30 Hz feed; deliberately not a bridge method."""
        return self._recordings.active_level()

    def get_level(self):
        """The dashboard's 30 Hz poll while it records: the live mic level
        (0..1), or None when nothing is metered. Bridge method."""
        return self.current_level()

    def note_user_typing(self):
        """Record manual typing without blocking the event-tap callback."""
        with self._paste_state_lock:
            self._typing_generation += 1

    def attach_hotkey_service(self, service):
        self._hotkey_service = service

    def attach_window(self, window):
        """Dashboard window handle, used to push live-refresh events to JS."""
        self._window = window

    def attach_status_item(self, status):
        self._status_item = status

    def is_recording(self) -> bool:
        """True while the microphone is open -- not the whole dictation
        (transcription can still be running after this goes False). The
        tray glyph tracks the longer "session in flight" state instead;
        see _mark_session."""
        return self._is_recording

    def _mark_session(self, sequence, in_flight: bool) -> None:
        """Track dictations that are still in flight and mirror the
        aggregate to the tray glyph.

        A dictation is in flight from a successful start until its
        transcription completes -- releasing the key only closes the mic,
        and whisper is the slow part. Sessions are tracked by sequence
        rather than a single flag because the queue admits a new dictation
        while an earlier one is still transcribing: retiring the old one
        must not clear the glyph out from under the new one, and a
        repeated retire (belt and braces on the failure paths) must be a
        no-op.

        Only pushes on a change, so the tray call is not repeated
        needlessly. Never lets a status-item failure break recording."""
        with self._session_lock:
            if in_flight:
                self._sessions_in_flight.add(sequence)
            else:
                self._sessions_in_flight.discard(sequence)
            busy = bool(self._sessions_in_flight)
            status = self._status_item
            if status is None or busy == self._glyph_busy:
                return
            self._glyph_busy = busy
            try:
                status.set_recording(busy)
            except Exception:
                pass

    def _notify_dictation(self):
        """Tell the dashboard a dictation landed (hotkey dictations must show
        up without a view switch). Fire-and-forget off-thread: a slow or dead
        webview must never delay or break the record path."""
        win = self._window
        if win is None:
            return

        def go():
            try:
                win.evaluate_js(
                    'window.dispatchEvent(new Event("voisr:new-dictation"))')
            except Exception:
                pass

        threading.Thread(target=go, daemon=True).start()

    def capture_hotkey(self):
        """'Press any key' flow: suspend the live listener (so the pressed
        key doesn't trigger a dictation), capture the next key, persist it,
        and restart the listener on it."""
        svc = self._hotkey_service
        if svc is not None:
            svc.stop()
        try:
            got = self._capture_fn(8.0)
            if not got:
                return {"error": "no key detected — click Change and press a key"}
            vk, label = got
            self._update_config({"hotkey_vk": vk, "hotkey_label": label})
            return {"vk": vk, "label": label}
        finally:
            if svc is not None:
                svc.start()

    def _process_session(self, session: RecordingSession) -> SessionResult:
        """Queue processor. The dictation ends here -- not when the mic
        closed -- so this is where the session is retired from the
        in-flight set and the tray glyph is allowed to go idle."""
        try:
            return self._run_session(session)
        finally:
            self._mark_session(session.sequence, False)

    def _run_session(self, session: RecordingSession) -> SessionResult:
        wav = session.wav_path
        cfg = session.context
        duration = max((session.released_at - session.started_at), 0.1)
        # Some audio but too little is a tap, not a mic fault. No audio at
        # all still falls through to the permission message below.
        if 0 < wav_seconds(wav) < MIN_SPEECH_S:
            return SessionResult.failure(
                session.sequence, "audio",
                "too short — hold the key while you speak")
        if not wav.exists() or wav.stat().st_size < 1000:
            detail = session.capture_stderr.strip()[:120]
            return SessionResult.failure(
                session.sequence, "audio",
                f"recording failed (mic permission?) {detail}".rstrip())
        span = speech_gate.gate_wav(wav)  # None: unreadable, skip the gate
        if span is not None:
            if span.verdict == "all_zeros":
                return SessionResult.failure(
                    session.sequence, "audio", MIC_SILENT_MESSAGE)
            if span.verdict == "no_speech":
                return SessionResult.failure(
                    session.sequence, "audio", NO_SPEECH_MESSAGE)
            # Leading and trailing non-speech beyond the padding: fewer
            # [BLANK_AUDIO] hallucinations and less audio to decode.
            try:
                speech_gate.trim_wav(wav, span.start, span.end)
            except Exception as error:
                # an untrimmed clip still transcribes
                _update_log(f"audio: trim failed: {error}")
        try:
            raw = self._transcribe_audio(wav, cfg)
            text = clean(raw, dictionary=cfg.get("dictionary", {}))
        except Exception as error:
            return SessionResult.failure(
                session.sequence, "transcription",
                f"transcription failed: {error}"[:250])
        if not text:
            return SessionResult.failure(
                session.sequence, "transcription",
                "nothing transcribed — try speaking a bit longer")

        item = None
        warning = None
        try:
            item = self._history.add(text, duration_s=duration)
            self._notify_dictation()
        except Exception as error:
            warning = f"history failed: {error}"[:250]

        pasted = False
        paste_error = None
        fallback_safe = True
        typing_generation = None
        if session.paste_requested and cfg.get("paste") and not self._quitting:
            with self._paste_state_lock:
                typing_generation = self._typing_generation
                join = (
                    self._last_paste_text
                    and typing_generation == self._last_paste_typing_generation
                    and self._clock() - self._last_paste_ts <= self.SPACE_JOIN_WINDOW_S
                    and not self._last_paste_text[-1].isspace()
                )
            try:
                pasted = self._paste_fn((" " if join else "") + text)
            except PasteDeliveryError as error:
                pasted = error.pasted
                fallback_safe = error.fallback_safe
                paste_error = error
            except Exception as error:
                paste_error = error
            if pasted:
                with self._paste_state_lock:
                    self._last_paste_text = text
                    self._last_paste_ts = self._clock()
                    self._last_paste_typing_generation = typing_generation

        copied = pasted
        clipboard_error = None
        if not pasted and fallback_safe and not self._quitting:
            try:
                self._clipboard_fn(text)
                copied = True
            except Exception as error:
                clipboard_error = error

        fields = {"text": text, "copied": copied, "pasted": pasted}
        if item is not None:
            fields["item"] = item
        if warning is not None:
            fields["warning"] = warning
        if isinstance(paste_error, PasteDeliveryError):
            return SessionResult.failure(
                session.sequence, "delivery",
                f"delivery failed: {paste_error}"[:250], **fields)
        if clipboard_error is not None:
            detail = clipboard_error if paste_error is None else (
                f"{paste_error}; clipboard fallback failed: {clipboard_error}")
            return SessionResult.failure(
                session.sequence, "delivery",
                f"delivery failed: {detail}"[:250], **fields)
        return SessionResult.success(session.sequence, **fields)

    def start_recording(self):
        if self._mic_check is not None:
            try:
                mic = self._mic_check()
            except Exception:
                mic = None  # unknown never blocks
            if mic is False:
                return {"error": "Microphone access is switched off for Voisr",
                        "permission": "microphone"}
        try:
            session = self._recordings.start()
        except RecordingQueueError as error:
            return {"error": str(error)}
        self._active_sequence = session.sequence
        self._is_recording = True
        self._mark_session(session.sequence, True)
        return {"recording": True, "session_id": session.sequence}

    def _release_recording(self, paste=False):
        sequence, self._active_sequence = self._active_sequence, None
        self._is_recording = False
        future = self._recordings.release(
            paste_requested=paste, context=self.get_config())
        if sequence is not None:
            # Safety net for the path that never reaches _process_session --
            # a recorder whose stop() throws is completed straight from the
            # queue worker. Retiring twice (the normal path's finally also
            # calls this) is a no-op, guarded by _mark_session itself.
            future.add_done_callback(
                lambda _f, s=sequence: self._mark_session(s, False))
        return future

    def stop_recording(self, paste=False):
        return self._release_recording(paste).result().to_api_dict()

    def _cancel_recording(self) -> bool:
        """Close the mic and drop the capture. Private so the JS bridge
        can't reach it; only the hotkey path cancels."""
        sequence, self._active_sequence = self._active_sequence, None
        self._is_recording = False
        cancelled = self._recordings.cancel()
        if sequence is not None:
            self._mark_session(sequence, False)
        return cancelled

    def close(self):
        # Quit still finishes queued transcriptions (they are the user's
        # words and go to History), but nothing may paste or touch the
        # clipboard after the user has left the app.
        self._quitting = True
        self._recordings.close()
        if self._status_item is not None:
            self._status_item.release()

    def get_history(self):
        return self._history.list()

    def delete_history_item(self, item_id):
        self._history.delete(item_id)
        return {"ok": True}

    def get_stats(self):
        return self._history.stats()

    def _config_stat_key(self):
        try:
            st = self._config_path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def get_config(self):
        """Parsed config.json, served from memory while the file is unchanged
        (the hotkey worker reads it on every press and release).

        The parse runs outside _config_lock: a slow read must not hold up a
        set_config. A store-back racing a write is harmless, because the
        entry is keyed on the file's stat and the write changes it."""
        import copy
        key = self._config_stat_key()
        cached = self._config_stat_cache
        if key is not None and cached is not None and cached[0] == key:
            return copy.deepcopy(cached[1])
        cfg = load_config(self._config_path,
                          on_corrupt=self._note_corrupt_config)
        self._config_stat_cache = (key, copy.deepcopy(cfg))
        return cfg

    def _write_config_atomic(self, cfg: dict) -> None:
        """Temp file + fsync + os.replace, so a crash mid-write (or the
        installer's hard exit) can never leave a truncated config.json,
        which load_config would then quarantine and replace with defaults."""
        import tempfile
        path = self._config_path
        payload = json.dumps(cfg, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                   dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _update_config(self, updates: dict) -> dict:
        """Locked read-modify-write; returns the merged config."""
        with self._config_lock:
            cfg = self.get_config()
            if "hotkey" in updates and updates["hotkey"] != cfg.get("hotkey"):
                # the named key is now the source of truth; a stale captured
                # vk would otherwise keep winning in HotkeyService._resolve_target
                cfg.pop("hotkey_vk", None)
                cfg.pop("hotkey_label", None)
            cfg.update(updates)
            self._write_config_atomic(cfg)
            self._config_stat_cache = None
            # Before anything else can read it back: a stale
            # "check_for_updates" would make the Settings toggle look like it
            # did nothing.
            self._invalidate_config_cache()
            return cfg

    def _note_corrupt_config(self, saved):
        self._config_notice = (
            f"Your settings file couldn't be read, so Voisr started on "
            f"defaults. The old file is kept as {saved.name}.")

    def take_config_notice(self):
        """One-shot: the dashboard toasts this once at boot."""
        notice, self._config_notice = self._config_notice, None
        return {"notice": notice} if notice else {}

    def read_config_cached(self):
        """get_config() with a short TTL, for callers that ask repeatedly.

        This is what the update checker is given. UpdateChecker.status()
        calls its config reader, the dashboard polls status(), and
        load_config() re-reads and re-parses config.json every time -- so an
        uncached reader means a file read per poll, forever.

        A TTL rather than an unbounded cache because config.json is
        documented in the README and can be hand-edited while the app runs;
        set_config() invalidates outright so the Settings toggle is never
        subject to the delay.

        The disk read deliberately happens outside the lock, which leaves two
        races, only one of them harmless. Two *readers* racing may both read:
        one redundant parse, no correctness cost. A reader racing an
        *invalidation* is the dangerous one -- it parsed before the write
        landed, so storing its result would put the pre-toggle config back
        into a cache that was just cleared and serve it for the full TTL,
        which is precisely what the invalidation exists to prevent. The
        generation counter closes that: it is read before the parse and
        re-checked under the lock, and a store-back whose generation moved is
        dropped. The racing reader still returns its own view, which is a
        fair answer to a call that began before the write.

        Hands back a *top-level* copy, so a caller assigning a key cannot
        rewrite what the next caller sees -- get_config()'s result is treated
        as owned by its caller elsewhere in this class (see capture_hotkey).
        Nested values are shared, both with the cache and, for any key the
        file omits, with app.DEFAULT_CONFIG itself: load_config seeds from a
        shallow dict() of it, so a config.json without "dictionary" yields
        the module-level dict. Mutating a nested value therefore poisons the
        cache and possibly a process global. Read-only by contract; today the
        only caller is UpdateChecker._enabled(), which reads one scalar.
        """
        now = self._clock()
        with self._config_cache_lock:
            cached = self._config_cache
            generation = self._config_cache_generation
        if cached is not None and 0 <= now - cached[0] < self.CONFIG_CACHE_TTL_S:
            return dict(cached[1])
        cfg = self.get_config()
        with self._config_cache_lock:
            if generation == self._config_cache_generation:
                self._config_cache = (now, cfg)
        return dict(cfg)

    def _invalidate_config_cache(self):
        with self._config_cache_lock:
            self._config_cache = None
            self._config_cache_generation += 1

    def set_config(self, updates):
        # The bridge is the only way in from the dashboard: a wrong type here
        # would be saved and then break every dictation that reads it.
        errors = config_errors(updates)
        if errors:
            return {"error": "; ".join(errors)}
        with self._config_lock:
            old_hotkey = self.get_config().get("hotkey")
            cfg = self._update_config(updates)
        new_hotkey = cfg.get("hotkey")
        if self._hotkey_service and new_hotkey != old_hotkey:
            self._hotkey_service.restart(new_hotkey)
        return cfg

    def get_hotkey_options(self):
        # voisr.keys.hotkey_options() returns {"name", "label"}; the JS
        # dashboard consumes {"value", "label"} everywhere (see settings.js,
        # get_pill_themes) so this is the adapter between them.
        return [{"value": o["name"], "label": o["label"]}
                for o in hotkey_options()]

    def hotkey_display(self):
        """Human-readable label for the configured hotkey, resolved in
        priority order so a raw config identifier (e.g. "ctrl_r") never
        reaches the UI:
          1. config["hotkey_label"] — set once the user completes a
             "press any key" capture.
          2. the hotkey-options label matching config["hotkey"] — covers
             a fresh install, which ships a known value with no label.
          3. the raw config["hotkey"] value, if it's something unrecognized.
          4. "unset"."""
        cfg = self.get_config()
        label = cfg.get("hotkey_label")
        if label:
            return {"display": label}
        hotkey = cfg.get("hotkey")
        if hotkey:
            for opt in self.get_hotkey_options():
                if opt["value"] == hotkey:
                    return {"display": opt["label"]}
            return {"display": hotkey}
        return {"display": "unset"}

    def get_pill_themes(self):
        return PILL_THEMES

    def get_hotkey_status(self):
        """status is the sentence Settings shows; state is what Home's dot
        shows: "listening", "busy" (another recording owns the mic, so press()
        drops the key) or "off"."""
        svc = self._hotkey_service
        if svc is None:
            return {"status": "push-to-talk not running", "state": "off",
                    "health": "off"}
        status = svc.status
        if not (getattr(svc, "running", True) and status.startswith("active")):
            state = "off"
        elif self._is_recording and not getattr(svc, "holding", False):
            state = "busy"
        else:
            state = "listening"
        return {"status": status, "state": state,
                "health": getattr(svc, "health", "off")}

    def get_permissions(self):
        return {"panes": permissions.states()}

    def open_settings_pane(self, key):
        return {"ok": permissions.open_pane(key)}

    def restore_history_item(self, item):
        try:
            self._history.restore(item)
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True}

    def clear_history(self):
        try:
            removed = self._history.clear()
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True, "removed": removed}

    def get_models(self):
        from voisr.transcriber import MODELS_DIR, PREFERRED_MODELS
        labels = {"small.en": "Small — best accuracy",
                  "base.en": "Base — fastest"}
        models = []
        for name in PREFERRED_MODELS:
            path = MODELS_DIR / f"ggml-{name}.bin"
            installed = path.exists()
            models.append({"value": name, "label": labels.get(name, name),
                           "installed": installed,
                           "bytes": path.stat().st_size if installed else 0})
        return {"models": models, "active": self.get_config().get("model")}

    def set_launch_at_login(self, enabled):
        try:
            if enabled:
                launch_agent.enable()
            else:
                launch_agent.disable()
        except Exception as e:
            return {"error": str(e)}
        self.set_config({"launch_at_login": bool(enabled)})
        return {"ok": True, "enabled": bool(enabled)}

    # -- updates ---------------------------------------------------------
    def attach_update_checker(self, checker):
        """The UpdateChecker whose state the dashboard renders and whose
        install() the Install button drives. Absent in a headless Api, which
        every method below reports as "unavailable" rather than failing."""
        self._updates = checker

    def attach_quit(self, quit_fn):
        """The app's one shutdown route (main()'s tray Quit handler), so a
        finished install can end the process the same way the user would."""
        self._quit_fn = quit_fn

    def get_update_status(self):
        """Same keys as UpdateChecker.status() even with no checker attached:
        the dashboard renders one shape and never has to guess which fields
        exist."""
        if self._updates is None:
            return {"state": "idle", "current_version": voisr.__version__,
                    "version": None, "notes": None, "error": None,
                    "enabled": False}
        return self._updates.status()

    def check_for_updates_now(self):
        """Settings' "Check now". Forced, so it works with auto-check off,
        and it reports failures inline because the user is waiting."""
        if self._updates is None:
            return {"error": _UPDATES_UNAVAILABLE}
        return self._updates.check_now(force=True)

    def install_update(self):
        """Spawn the installer, then quit -- Inno cannot replace files under
        {app} while we hold them open.

        The exit is not optional bookkeeping: UpdateChecker's `installing`
        state is terminal by design, so an app that stayed up would sit in it
        with no way for the user to retry.
        """
        if self._updates is None:
            return {"error": _UPDATES_UNAVAILABLE}
        result = self._updates.install()
        if "error" not in result:
            self._shutdown_soon()
        return result

    def _arm(self, delay, fn):
        timer = self._timer_factory(delay, fn)
        timer.daemon = True
        timer.start()
        return timer

    def _shutdown_soon(self):
        """Quit after a short delay, off this thread, and make sure we
        actually go.

        Deferred on purpose: install_update() is a bridge call the dashboard
        is awaiting, and destroying the window from inside the handler can
        leave that promise unresolved -- the UI would hang instead of showing
        "Installing update". A timer thread is safe for the quit itself:
        pywebview marshals destroy() onto the GUI thread via Control.Invoke,
        so the caller's thread does not matter.

        The hard exit is not paranoia. `installing` is terminal -- nothing in
        UpdateChecker leaves it -- so a quit that silently does nothing
        strands the user with a dashboard stuck on "Installing", an Inno
        /SILENT install already running underneath, and no route back. It
        also keeps {app} open against the very installer that is replacing
        it.

        os._exit skips atexit and buffer flushing, which is the right trade
        against a live process whose files are being swapped out from under
        it -- but the reason history survives is atomicity, not timing.
        _run_session writes history from the transcription worker, and
        whisper can still be running well past the deadline; what protects it
        is history.py committing via os.replace. config.json has no such
        guarantee: set_config and capture_hotkey use Path.write_text, which
        truncates then writes, and an exit inside that window leaves a
        truncated file that load_config silently swaps for DEFAULT_CONFIG,
        costing the user their hotkey, dictionary, theme and launch-at-login.
        The window is microseconds wide and only opens on a write the user
        just triggered, so it is accepted here rather than fixed.

        Note this is a *local* guarantee on purpose. voisr.iss does set
        CloseApplications=force, but that is an external dependency one
        setup-file edit away from vanishing, and this invariant should not
        rest on it.
        """
        quit_fn = self._quit_fn
        if quit_fn is None:
            # A headless Api (tests, a future CLI) has no window to tear
            # down, and must certainly not take the process with it.
            _update_log("update installing, but no quit hook is attached")
            return

        def go():
            # Armed before quit_fn, not after: a quit that blocks rather than
            # returning would never reach a backstop armed below it.
            self._arm(self.SHUTDOWN_HARD_S, self._exit_fn)
            try:
                quit_fn()
            except Exception as e:
                _update_log(f"shutdown after install failed: {e}")

        self._arm(self.SHUTDOWN_DELAY_S, go)

    def set_check_for_updates(self, enabled):
        """Persist the toggle only. UpdateChecker._enabled() consults the
        config at the start of every check, so an off switch takes effect on
        the next tick without stopping the schedule -- and re-arming the
        schedule on each toggle would buy nothing but a second way to get the
        timer wrong."""
        self.set_config({"check_for_updates": bool(enabled)})
        return {"ok": True, "enabled": bool(enabled)}

    def set_appearance(self, mode):
        if mode not in self.APPEARANCES:
            return {"error": f"unknown appearance: {mode}"}
        self.set_config({"appearance": mode})
        try:
            dark = mode == "dark" or (mode == "system" and _system_prefers_dark())
            hwnd = _find_window_hwnd()
            if hwnd:
                apply_chrome(hwnd, dark=dark)
        except Exception:
            pass  # chrome is cosmetic; never break the appearance toggle
        return {"ok": True, "appearance": mode}

    def export_history(self, fmt):
        import datetime
        items = self._history.list()
        stamp = datetime.date.today().isoformat()
        if fmt == "json":
            text = json.dumps(items, indent=2)
        elif fmt == "md":
            def when(ts):
                # history keeps records whose ts can't be a date (e.g.
                # milliseconds); one of those must not sink the export
                try:
                    return f"{datetime.datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}"
                except (OverflowError, OSError, ValueError):
                    return "unknown time"
            text = "\n".join(f"- **{when(i['ts'])}** — {i['text']}" for i in items)
        elif fmt == "txt":
            text = "\n".join(i["text"] for i in items)
        else:
            return {"error": f"unsupported format: {fmt}"}
        return {"text": text, "suggested_name": f"voisr-history-{stamp}.{fmt}"}

    def save_text_file(self, text, suggested_name):
        """Write text to a user-chosen path via a native Save dialog. Returns
        ok=False with path=None when the user cancels."""
        try:
            path = self._save_dialog_fn(suggested_name)
        except Exception as e:
            return {"error": str(e)}
        if not path:
            return {"ok": False, "path": None}
        try:
            Path(path).write_text(text)
        except OSError as e:
            return {"error": str(e)}
        return {"ok": True, "path": str(path)}

    def prime_microphone(self):
        """Retained for interface parity — intentionally does NOT open the
        mic. The stream is opened only while recording so the mic-in-use
        indicator is off when idle; the permission prompt appears on the
        first real dictation."""
        return {"ok": True}

    def copy_text(self, text):
        set_clipboard(text)
        return {"ok": True}


# Every Api method the dashboard calls, and nothing else.
# tests/test_js_bridge_surface.py pins this to the V.api() calls in voisr/web/js.
BRIDGE_METHODS = (
    "capture_hotkey", "check_for_updates_now", "clear_history", "copy_text",
    "delete_history_item", "export_history", "get_config", "get_history",
    "get_hotkey_status", "get_level", "get_models", "get_permissions",
    "get_pill_themes", "get_stats", "get_update_status", "hotkey_display",
    "install_update", "open_settings_pane", "restore_history_item",
    "save_text_file", "set_appearance", "set_check_for_updates", "set_config",
    "set_launch_at_login", "start_recording", "stop_recording",
    "take_config_notice",
)


def bridge_functions(api):
    """The dashboard's bridge: one plain function per BRIDGE_METHODS name,
    for window.expose(). Passing the Api as js_api instead exposes it whole:
    pywebview resolves a call's dotted name with getattr and no underscore
    filter, so page script could reach "_history.path.unlink" or a method's
    __func__.__globals__. Exposed functions are looked up by exact name."""
    def forward(name):
        def call(*args):
            return getattr(api, name)(*args)
        call.__name__ = call.__qualname__ = name
        return call
    return [forward(name) for name in BRIDGE_METHODS]


def start_hotkey_service(service):
    """Start push-to-talk without letting a failure abort the rest of startup:
    the window chrome is applied after this, and an app with no hotkey can
    still be fixed from its own Settings. Returns the error text, or None."""
    try:
        service.start()
    except Exception as e:
        service.status = f"push-to-talk not running: {e}"[:200]
        _log(service.status)
        return str(e)
    return None


def main():
    import webview

    from voisr.inject import set_clipboard as _  # noqa: F401 -- import check
    from voisr.paths import find_binary
    whisper = find_binary("whisper-cli") or "MISSING"
    print(f"Voisr starting (whisper: {whisper})", flush=True)

    api = None
    service = None
    updates = None
    try:
        set_app_user_model_id()
        api = Api(mic_check=permissions.microphone_switch)
        widget = RecordingWidget(level_source=api.current_level)
        service = HotkeyService(api, widget=widget, key_state=hotkey_key_down)
        api.attach_hotkey_service(service)

        # pywebview allows exactly one webview.start() call per process,
        # and it must run every window's message loop together -- so every
        # window is created (registered) first, and started once below.
        # See voisr.overlay.RecordingWidget.create() for why the pill can't
        # own a second, independent start() call.
        widget.create()
        # No js_api: see bridge_functions
        window = webview.create_window(
            WINDOW_TITLE, str(WEB_DIR / "index.html"),
            width=1060, height=700, min_size=(900, 620))
        window.expose(*bridge_functions(api))
        api.attach_window(window)

        # Closing the dashboard window is not quitting the app -- that is
        # the whole point of the tray icon. pywebview's winforms backend
        # calls WinForms.Application.Exit() (ending the *entire* message
        # loop, both windows) once a Form actually closes, so an
        # unhandled X-button click would silently kill the process. The
        # `closing` event fires first and lets a handler cancel it: hide
        # the window instead, and only let the close through once the
        # tray's Quit has set `quitting`.
        quitting = threading.Event()

        def on_closing():
            if quitting.is_set():
                return True  # let the real close proceed
            try:
                window.hide()
            except Exception:
                pass
            return False  # cancel: keep running in the tray

        window.events.closing += on_closing

        def menu_dictate():
            # Routed through HotkeyService, not Api directly: Api.
            # stop_recording() blocks on the whole transcription, which
            # would freeze the tray's own event loop for the duration.
            # HotkeyService.toggle() hands the wait off to its own worker
            # thread and drives the pill, so a tray-started dictation
            # shows the same feedback as a hotkey-started one.
            service.toggle()

        def menu_open():
            present_window(window)

        def menu_quit():
            # `quitting` first: it is what lets on_closing above stop
            # cancelling the dashboard's close.
            quitting.set()
            destroy_windows(window, widget)

        # The one shutdown route: destroying *every* window ends the message
        # loop, and the finally below stops the hotkey listener and releases
        # the tray icon. The pill counts -- see destroy_windows for why
        # leaving it alive left a zombie holding the global hotkey. Handed to
        # Api so a finished install exits the same way -- a half-shutdown
        # would keep handles open under {app} and the installer could not
        # replace our files.
        api.attach_quit(menu_quit)

        status = StatusItem(
            on_dictate=menu_dictate,
            on_open=menu_open,
            on_quit=menu_quit,
            hotkey_label_fn=lambda: api.hotkey_display()["display"])
        api.attach_status_item(status)
        status.create()

        def on_started():
            # Runs on a worker thread after the GUI loop is up. Mic opens
            # only during an actual dictation, so nothing here blocks
            # startup or shows a mic-in-use indicator while idle.
            start_hotkey_service(service)  # config-driven: prefers captured hotkey_vk
            try:
                mode = api.get_config().get("appearance", "system")
                dark = mode == "dark" or (mode == "system" and _system_prefers_dark())
                hwnd = _find_window_hwnd()
                if hwnd:
                    apply_chrome(hwnd, dark=dark)
            except Exception:
                pass  # chrome is cosmetic

        # read_config_cached, not get_config: UpdateChecker.status() calls
        # this on every dashboard poll, and get_config() re-reads and
        # re-parses config.json each time.
        updates = UpdateChecker(
            current_version=voisr.__version__,
            updates_dir=app_data_dir() / "updates",
            config_reader=api.read_config_cached,
        )
        api.attach_update_checker(updates)
        updates.start()

        webview.start(on_started, gui="edgechromium", icon=app_icon())
    finally:
        try:
            if updates is not None:
                updates.stop()
        finally:
            try:
                if service is not None:
                    service.stop()
            finally:
                if api is not None:
                    api.close()


if __name__ == "__main__":
    main()
