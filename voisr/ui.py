"""Dashboard window: the js_api surface (`Api`), the push-to-talk
`HotkeyService`, and `main()`.

Ported from the macOS `voisr/ui.py`. Everything that already lives in a
dedicated Windows module is imported rather than re-implemented:
`voisr.keys` (hotkey table), `voisr.keytap` (the WH_KEYBOARD_LL listener),
`voisr.overlay` (the floating pill), `voisr.permissions` (microphone probe),
`voisr.window_chrome` (DWM dark titlebar), `voisr.launch_agent` (per-user Run
key), and `voisr.status_item` (the tray icon).
"""
import json
import threading
import time
from pathlib import Path

from voisr import launch_agent, permissions
from voisr.app import config_path as default_config_path
from voisr.app import load_config
from voisr.cleanup import clean
from voisr.history import HistoryStore
from voisr.inject import paste_at_cursor, set_clipboard
from voisr.keys import DEFAULT_HOTKEY, hotkey_options, key_label, resolve_key
from voisr.keytap import WindowsKeyListener, capture_next_vk
from voisr.overlay import RecordingWidget, sanitize_pill_color, sanitize_pill_theme
from voisr.pasteboard import PasteDeliveryError
from voisr.paths import app_icon
from voisr.paths import web_dir as _web_dir

# Taskbar identity. Without an explicit AppUserModelID, Windows attributes
# our window to the host process (pythonw.exe) -- so it groups with any
# other Python app and shows their icon and name rather than ours.
APP_USER_MODEL_ID = "Voisr.Voisr"


def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Claim a distinct taskbar identity. Cosmetic: never raises."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception:
        return False
from voisr.recording_queue import (
    RecordingQueue,
    RecordingQueueError,
    RecordingSession,
    SessionResult,
)
from voisr.status_item import StatusItem
from voisr.stream_recorder import StreamRecorder
from voisr.window_chrome import apply_chrome

WEB_DIR = _web_dir()
WINDOW_TITLE = "Voisr"

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


def wav_peak(path) -> int:
    """Peak |amplitude| of a 16-bit WAV, -1 if unparseable (skip the check)."""
    import array
    import wave
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                return -1
            data = w.readframes(w.getnframes())
        if not data:
            return 0
        samples = array.array("h")
        samples.frombytes(data[:len(data) // 2 * 2])
        return max(abs(s) for s in samples) if samples else 0
    except Exception:
        return -1


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


class HotkeyService:
    """Global push-to-talk listener driving the shared Api record path."""

    def __init__(self, api, widget=None, hide_delay=1.2,
                 permissions_check=lambda: True, poll_interval=3.0,
                 listener_factory=None):
        self._api = api
        self._widget = widget
        self._hide_delay = hide_delay
        self._permissions_check = permissions_check
        self._poll_interval = poll_interval
        self._listener_factory = listener_factory or WindowsKeyListener
        self._listener = None
        self._watcher = None
        self._stopped = False
        self._pressed = False
        self._key_name = None
        self._target_vk = None
        self._state_lock = threading.RLock()
        self._epoch = 0  # bumped per recording; stale _finish threads must not touch the widget
        self.status = "not started"

    def _widget_enabled(self) -> bool:
        return self._widget is not None and bool(self._api.get_config().get("show_widget", True))

    def _run_widget_action(self, epoch: int, action) -> None:
        with self._state_lock:
            if epoch == self._epoch:
                action()

    def _widget_action(self, name: str, epoch: int, *args, **kwargs) -> None:
        method = getattr(self._widget, name)
        method(*args, **kwargs,
               if_current=lambda action: self._run_widget_action(epoch, action))

    def press(self) -> None:
        with self._state_lock:
            if self._pressed:
                return
            result = self._api.start_recording()
            if "error" in result:
                return  # e.g. a dashboard recording is already running
            self._pressed = True
            self._epoch += 1
            epoch = self._epoch
            cfg = self._api.get_config()
            if self._widget is not None and bool(cfg.get("show_widget", True)):
                self._widget_action(
                    "show_recording", epoch,
                    theme=sanitize_pill_theme(cfg.get("pill_theme")),
                    color=sanitize_pill_color(cfg.get("pill_color")))

    def release(self) -> None:
        with self._state_lock:
            if not self._pressed:
                return
            self._pressed = False
            epoch = self._epoch
            show = self._widget_enabled()
            if show:
                self._widget_action("show_processing", epoch)
            future = self._api._release_recording(paste=True)
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
        hotkey_vk, falling back to the legacy 'hotkey' name."""
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
        self._target_vk = vk
        self._key_name = label
        self._spawn_listener()
        self._refresh_status()
        self._watch()

    def _spawn_listener(self) -> None:
        target_vk = self._target_vk

        def handler(vk, kind):
            # Never raise: a callback error must not kill the hook.
            try:
                if vk != target_vk:
                    if kind == "down":
                        # user is typing manually -> don't smart-join the
                        # next dictation onto the previous one
                        self._api.note_user_typing()
                    return
                if kind == "down":       # auto-repeats; press() guards
                    self.press()
                elif kind == "up":
                    self.release()
            except Exception:
                pass

        old, self._listener = self._listener, None
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        try:
            listener = self._listener_factory(handler)
            listener.start()
            self._listener = listener
        except Exception as e:
            print(f"Voisr hotkey: listener start failed: {e}", flush=True)

    def _refresh_status(self) -> None:
        if self._permissions_check():
            self.status = f"active on {self._key_name}"
        else:
            self.status = "waiting for permission"
        print(f"Voisr hotkey: {self.status}", flush=True)

    def _watch(self) -> None:
        """Respawn the hook if it goes quiet. A WH_KEYBOARD_LL hook whose
        callback exceeds LowLevelHooksTimeout gets silently unregistered by
        Windows -- WindowsKeyListener.running goes False when that happens
        (see voisr.keytap), which is what this polls for."""
        if self._watcher is not None:
            return

        def poll():
            while not self._stopped:
                time.sleep(self._poll_interval)
                if self._stopped:
                    break
                listener = self._listener
                if listener is not None and not getattr(listener, "running", True):
                    self._spawn_listener()
                    self._refresh_status()
            self._watcher = None

        self._watcher = threading.Thread(target=poll, daemon=True)
        self._watcher.start()

    def stop(self) -> None:
        self._stopped = True
        listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:
            pass  # a dead listener's teardown must not crash the app

    def restart(self, key_name=None) -> None:
        self.stop()
        self.start(key_name)

    def toggle(self) -> None:
        """press()/release() pair for a single-action trigger (the tray
        menu's "Dictate now"), reusing the same path the hotkey itself
        drives so the pill shows the same feedback either way."""
        with self._state_lock:
            pressed = self._pressed
        if pressed:
            self.release()
        else:
            self.press()


class Api:
    """JS<->Python bridge exposed to the dashboard via pywebview."""

    SPACE_JOIN_WINDOW_S = 300  # only join dictations this close together
    APPEARANCES = ("system", "light", "dark")

    def __init__(self, config_path=None,
                 history=None, recorder_factory=StreamRecorder,
                 transcribe_fn=None,
                 clock=time.monotonic, hotkey_service=None,
                 fallback_recorder_factory=None,
                 capture_fn=capture_next_key,
                 paste_fn=paste_at_cursor,
                 clipboard_fn=set_clipboard,
                 save_dialog_fn=None):
        self._config_path = Path(config_path) if config_path else default_config_path()
        self._history = history or HistoryStore(self._config_path.parent / "history.jsonl")
        self._transcribe_fn = transcribe_fn
        self._clock = clock
        self._hotkey_service = hotkey_service
        self._capture_fn = capture_fn
        self._paste_fn = paste_fn
        self._clipboard_fn = clipboard_fn
        self._save_dialog_fn = save_dialog_fn or _run_save_dialog
        self._last_paste_text = None
        self._last_paste_ts = None
        self._typing_generation = 0
        self._last_paste_typing_generation = 0
        self._paste_state_lock = threading.Lock()
        self._window = None
        self._status_item = None
        self._is_recording = False
        self._sessions_in_flight = set()
        self._glyph_busy = False
        self._active_sequence = None
        self._session_lock = threading.Lock()
        self._recordings = RecordingQueue(
            recorder_factory=recorder_factory,
            fallback_recorder_factory=fallback_recorder_factory,
            processor=self._process_session,
            clock=clock,
        )

    def _default_transcribe(self, wav, cfg):
        """Transcribe with the release-time model and dictionary prompt."""
        from voisr.transcriber import resolve_model, transcribe as _transcribe
        vocab = sorted({v for v in (cfg.get("dictionary") or {}).values()})
        prompt = ("Glossary: " + ", ".join(vocab) + ".") if vocab else None
        return _transcribe(wav, model_path=resolve_model(cfg.get("model")),
                           prompt=prompt)

    def _transcribe_audio(self, wav, cfg):
        if self._transcribe_fn is not None:
            return self._transcribe_fn(wav)
        return self._default_transcribe(wav, cfg)

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
            cfg = self.get_config()
            cfg["hotkey_vk"] = vk
            cfg["hotkey_label"] = label
            self._config_path.write_text(json.dumps(cfg, indent=2))
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
        if not wav.exists() or wav.stat().st_size < 1000:
            detail = session.capture_stderr.strip()[:120]
            return SessionResult.failure(
                session.sequence, "audio",
                f"recording failed (mic permission?) {detail}".rstrip())
        peak = wav_peak(wav)
        if 0 <= peak < 60:
            return SessionResult.failure(
                session.sequence, "audio",
                "no audio captured — allow Voisr to use the Microphone in "
                "Settings → Privacy & security, then try again")
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
        if session.paste_requested and cfg.get("paste"):
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
        if not pasted and fallback_safe:
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

    def close(self):
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

    def get_config(self):
        return load_config(self._config_path)

    def set_config(self, updates):
        old_hotkey = self.get_config().get("hotkey")
        cfg = self.get_config()
        cfg.update(updates)
        self._config_path.write_text(json.dumps(cfg, indent=2))
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
        if self._hotkey_service is None:
            return {"status": "push-to-talk not running"}
        return {"status": self._hotkey_service.status}

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
            text = "\n".join(
                f"- **{datetime.datetime.fromtimestamp(i['ts']):%Y-%m-%d %H:%M}** "
                f"— {i['text']}" for i in items)
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


def main():
    import webview

    from voisr.inject import set_clipboard as _  # noqa: F401 -- import check
    from voisr.paths import find_binary
    whisper = find_binary("whisper-cli") or "MISSING"
    print(f"Voisr starting (whisper: {whisper})", flush=True)

    api = None
    service = None
    try:
        set_app_user_model_id()
        api = Api()
        widget = RecordingWidget()
        service = HotkeyService(api, widget=widget)
        api.attach_hotkey_service(service)

        # pywebview allows exactly one webview.start() call per process,
        # and it must run every window's message loop together -- so every
        # window is created (registered) first, and started once below.
        # See voisr.overlay.RecordingWidget.create() for why the pill can't
        # own a second, independent start() call.
        widget.create()
        window = webview.create_window(
            WINDOW_TITLE, str(WEB_DIR / "index.html"),
            js_api=api, width=1060, height=700, min_size=(900, 620))
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
            try:
                window.show()
            except Exception:
                pass

        def menu_quit():
            quitting.set()
            try:
                window.destroy()
            except Exception:
                pass

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
            service.start()  # config-driven: prefers captured hotkey_vk
            try:
                mode = api.get_config().get("appearance", "system")
                dark = mode == "dark" or (mode == "system" and _system_prefers_dark())
                hwnd = _find_window_hwnd()
                if hwnd:
                    apply_chrome(hwnd, dark=dark)
            except Exception:
                pass  # chrome is cosmetic

        webview.start(on_started, gui="edgechromium", icon=app_icon())
    finally:
        try:
            if service is not None:
                service.stop()
        finally:
            if api is not None:
                api.close()


if __name__ == "__main__":
    main()
