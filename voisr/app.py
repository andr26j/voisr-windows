"""Push-to-talk dictation loop.

Transcription runs off the hook thread: a low-level keyboard hook whose
callback blocks gets silently unregistered by Windows, and whisper takes
seconds.
"""
import argparse
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from voisr.cleanup import clean
from voisr.history import HistoryStore
from voisr.inject import paste_at_cursor, set_clipboard
from voisr.keys import DEFAULT_HOTKEY, key_label, resolve_key
from voisr.keytap import WindowsKeyListener
from voisr.pasteboard import PasteDeliveryError
from voisr.paths import ensure_app_data_dir
from voisr.stream_recorder import StreamRecorder
from voisr.transcriber import transcribe

DEFAULT_CONFIG = {
    "hotkey": DEFAULT_HOTKEY,
    "paste": True,
    "dictionary": {},
    "model": None,
    "show_widget": True,
    "pill_theme": "classic",
    "pill_color": None,
    "launch_at_login": False,
    "appearance": "system",
    "check_for_updates": True,
}


def config_path() -> Path:
    return ensure_app_data_dir() / "config.json"


def _is_str(v):
    return isinstance(v, str)


def _is_bool(v):
    return isinstance(v, bool)


def _is_optional_str(v):
    return v is None or isinstance(v, str)


def _is_vk(v):
    # bool is an int subclass; True is not a key code
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 0xFFFF


def _is_dictionary(v):
    return isinstance(v, dict) and all(
        isinstance(k, str) and k and isinstance(w, str) for k, w in v.items())


CONFIG_VALIDATORS = {
    "hotkey": _is_str, "paste": _is_bool,
    "dictionary": _is_dictionary, "show_widget": _is_bool,
    "pill_theme": _is_str, "pill_color": _is_optional_str,
    "appearance": _is_str, "launch_at_login": _is_bool,
    "model": _is_optional_str, "hotkey_vk": _is_vk, "hotkey_label": _is_str,
    "check_for_updates": _is_bool,
}


def config_errors(updates: dict) -> list[str]:
    """Why `updates` can't be saved, one line per bad key; [] when it can."""
    if not isinstance(updates, dict):
        return ["settings must be an object"]
    errors = []
    for key, value in updates.items():
        check = CONFIG_VALIDATORS.get(key)
        if check is None:
            errors.append(f"unknown setting: {key}")
        elif not check(value):
            errors.append(f"invalid value for {key}")
    return errors


def _salvage(data: dict, path) -> dict:
    """Keep every valid value from a hand-edited file. A bad value falls back
    to its default instead of crashing whatever reads it later; a dictionary
    keeps its good entries, since those are the user's work."""
    out = {}
    for key, value in data.items():
        check = CONFIG_VALIDATORS.get(key)
        if check is None or check(value):
            out[key] = value  # unknown keys are the user's; leave them be
        elif key == "dictionary" and isinstance(value, dict):
            out[key] = {k: w for k, w in value.items()
                        if isinstance(k, str) and k and isinstance(w, str)}
            print(f"voisr: dropped invalid dictionary entries in {path}", flush=True)
        else:
            print(f"voisr: ignoring invalid {key} in {path}", flush=True)
    return out


def _quarantine(p: Path) -> Path | None:
    """Move an unparseable config aside so the next save can't overwrite the
    only copy of the user's dictionary."""
    target = p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
    n = 1
    while target.exists():
        n += 1
        target = target.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
    try:
        os.replace(p, target)
    except OSError as e:
        print(f"voisr: could not move aside corrupt config {p}: {e}", flush=True)
        return None
    return target


def load_config(path=None, on_corrupt=None) -> dict:
    """Defaults overlaid with config.json. A file that can't be parsed is
    moved to config.json.corrupt-<ts> and on_corrupt(saved_path) is called."""
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path) if path else config_path()
    if not p.exists():
        return cfg
    try:
        # utf-8-sig: Notepad saves a hand-edited file with a BOM
        data = json.loads(p.read_bytes().decode("utf-8-sig"))
    except OSError as e:
        # unreadable right now (e.g. Defender holding it), not proven
        # corrupt: leave the file alone
        print(f"voisr: ignoring unreadable config {p}: {e}", flush=True)
        return cfg
    except ValueError as e:  # JSONDecodeError and UnicodeDecodeError
        data = None
        print(f"voisr: config {p} is not valid JSON: {e}", flush=True)
    if not isinstance(data, dict):
        saved = _quarantine(p)
        if saved is not None and on_corrupt is not None:
            on_corrupt(saved)
        return cfg
    cfg.update(_salvage(data, p))
    return cfg


@dataclass
class DictationResult:
    text: str
    delivered: bool
    blocked: bool = False


class DictationSession:
    """One press/release cycle. Owns the temp WAV and its cleanup."""

    def __init__(self, recorder=None, config=None, history=None):
        self.recorder = recorder or StreamRecorder()
        self.config = config or DEFAULT_CONFIG
        self.history = history or HistoryStore(ensure_app_data_dir() / "history.jsonl")
        self._wav = None
        self._press_time = None

    def press(self, wav_path=None) -> None:
        if self._wav is not None:
            return  # already recording; auto-repeat sends many keydowns
        if wav_path is None:
            fd, name = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            wav_path = Path(name)
        self._wav = Path(wav_path)
        self._press_time = time.time()
        self.recorder.start(self._wav)

    def _record(self, text: str, duration_s: float) -> None:
        try:
            self.history.add(text, duration_s)
        except Exception as error:
            # History is secondary: a full disk or lock failure must not
            # cost the user their dictation.
            print(f"voisr: failed to record history: {error}", flush=True)

    def release(self) -> DictationResult | None:
        wav, self._wav = self._wav, None
        press_time, self._press_time = self._press_time, None
        if wav is None:
            return None
        try:
            self.recorder.stop()
            prompt = ", ".join(self.config.get("dictionary") or {}) or None
            text = clean(
                transcribe(wav, model_path=None, prompt=prompt),
                dictionary=self.config.get("dictionary"),
            )
            if not text.strip():
                return DictationResult("", delivered=False)
            duration_s = time.time() - press_time if press_time else 0.0
            if not self.config.get("paste", True):
                set_clipboard(text)
                self._record(text, duration_s)
                return DictationResult(text, delivered=False)
            try:
                paste_at_cursor(text)
            except PasteDeliveryError as error:
                self._record(text, duration_s)
                return DictationResult(text, delivered=bool(error.pasted),
                                       blocked=not error.pasted)
            self._record(text, duration_s)
            return DictationResult(text, delivered=True)
        finally:
            wav.unlink(missing_ok=True)


def hotkey_mode(cfg) -> None:
    target = resolve_key(cfg["hotkey"])
    if target is None:
        raise SystemExit(f"unknown hotkey: {cfg['hotkey']}")
    session = DictationSession(config=cfg)
    lock = threading.Lock()

    def finish():
        with lock:
            result = session.release()
        if result is None:
            return
        if result.blocked:
            print(f"blocked (clipboard preserved): {result.text}")
        elif result.delivered:
            print(f"pasted: {result.text}")
        elif result.text:
            print(f"copied: {result.text}")
        else:
            print("(nothing transcribed)")

    def handler(vk, kind):
        if vk != target:
            return
        if kind == "down":
            with lock:
                session.press()
        else:
            # off the hook thread: a slow callback gets the hook unregistered
            threading.Thread(target=finish, daemon=True).start()

    listener = WindowsKeyListener(handler)
    listener.start()
    print(f"Hold [{key_label(target)}] to dictate. Ctrl-C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="voisr: on-device push-to-talk dictation")
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-paste", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.no_paste:
        cfg["paste"] = False
    hotkey_mode(cfg)


if __name__ == "__main__":
    main()
