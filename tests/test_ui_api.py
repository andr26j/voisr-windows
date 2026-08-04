import json
import threading
import time

import pytest

from voisr.history import HistoryStore
from voisr.pasteboard import PasteDeliveryError
from voisr.recording_queue import RecordingQueueError
from voisr.ui import Api, HotkeyService


class FakeRecorder:
    def __init__(self):
        self.recording = False

    def start(self, path):
        self.recording = True
        self.path = path

    def stop(self):
        self.recording = False
        with open(self.path, "wb") as f:
            f.write(b"\0" * 2000)
        return ""


def make_api(tmp_path, **overrides):
    kwargs = dict(config_path=tmp_path / "config.json",
                  history=HistoryStore(tmp_path / "h.jsonl"),
                  recorder_factory=FakeRecorder,
                  transcribe_fn=lambda wav: "um, hello world",
                  clock=lambda: 10.0,
                  clipboard_fn=lambda text: None)
    kwargs.update(overrides)
    return Api(**kwargs)


def test_record_cycle_produces_cleaned_history(tmp_path):
    api = make_api(tmp_path)
    assert api.start_recording()["recording"] is True
    out = api.stop_recording()
    assert out["text"] == "Hello world"
    items = api.get_history()
    assert len(items) == 1 and items[0]["text"] == "Hello world"


def test_stop_without_start_errors(tmp_path):
    api = make_api(tmp_path)
    assert "error" in api.stop_recording()


def test_config_roundtrip_and_dictionary(tmp_path):
    api = make_api(tmp_path)
    cfg = api.get_config()
    assert cfg["paste"] is True
    api.set_config({"paste": False, "dictionary": {"trademake": "TradeMate"}})
    assert api.get_config()["paste"] is False
    assert json.loads((tmp_path / "config.json").read_text())["dictionary"] == {"trademake": "TradeMate"}


def test_successful_dictation_notifies_window(tmp_path):
    class FakeWindow:
        def __init__(self):
            self.calls = []

        def evaluate_js(self, js):
            self.calls.append(js)

    api = make_api(tmp_path)
    win = FakeWindow()
    api.attach_window(win)
    api.start_recording()
    api.stop_recording()
    deadline = time.monotonic() + 2.0  # the notify is fire-and-forget off-thread
    while not win.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert any("voisr:new-dictation" in c for c in win.calls)


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    api = make_api(tmp_path)
    cfg = api.get_config()
    assert cfg["paste"] is True and cfg["hotkey"] == "ctrl_r"


def test_stats_shape(tmp_path):
    api = make_api(tmp_path)
    s = api.get_stats()
    assert set(s) == {"total_words", "dictations", "avg_wpm", "streak_days", "total_seconds", "time_saved_seconds", "daily"}


def test_delete_history_item(tmp_path):
    api = make_api(tmp_path)
    api.start_recording()
    api.stop_recording()
    item_id = api.get_history()[0]["id"]
    api.delete_history_item(item_id)
    assert api.get_history() == []


def test_default_config_includes_pill_theme(tmp_path):
    api = make_api(tmp_path)
    assert api.get_config()["pill_theme"] == "classic"


def test_pill_theme_roundtrip(tmp_path):
    api = make_api(tmp_path)
    api.set_config({"pill_theme": "ember"})
    assert api.get_config()["pill_theme"] == "ember"


def test_get_pill_themes_shape(tmp_path):
    api = make_api(tmp_path)
    themes = api.get_pill_themes()
    assert [t["value"] for t in themes] == [
        "classic", "ember", "aurora", "ocean", "neon", "breathe", "scanner"]
    assert all(set(t) == {"value", "label"} for t in themes)


def test_default_config_includes_pill_color_none(tmp_path):
    api = make_api(tmp_path)
    assert api.get_config()["pill_color"] is None


def test_pill_color_roundtrip(tmp_path):
    api = make_api(tmp_path)
    api.set_config({"pill_color": "#FF00AA"})
    assert api.get_config()["pill_color"] == "#FF00AA"


def test_get_hotkey_options_maps_name_to_value(tmp_path):
    """voisr.keys.hotkey_options() returns {"name", "label"}; the JS
    dashboard (settings.js, get_pill_themes) consistently expects
    {"value", "label"}. Api is the adapter -- this pins the shape it must
    hand back, and that it actually includes the Windows default."""
    api = make_api(tmp_path)
    options = api.get_hotkey_options()
    assert all(set(o) == {"value", "label"} for o in options)
    assert "ctrl_r" in [o["value"] for o in options]


def test_hotkey_display_prefers_captured_label(tmp_path):
    api = make_api(tmp_path)
    api.set_config({"hotkey_vk": 0x7C, "hotkey_label": "F13"})
    assert api.hotkey_display() == {"display": "F13"}


def test_hotkey_display_falls_back_to_option_label(tmp_path):
    api = make_api(tmp_path)
    api.set_config({"hotkey": "alt_r"})
    assert api.hotkey_display()["display"] == "Right Alt"


def test_hotkey_display_unset_when_nothing_configured(tmp_path):
    api = make_api(tmp_path, config_path=tmp_path / "empty.json")
    (tmp_path / "empty.json").write_text(json.dumps({"hotkey": None}))
    assert api.hotkey_display() == {"display": "unset"}


def test_released_sessions_paste_and_enter_history_fifo(tmp_path):
    first_started = threading.Event()
    allow_first = threading.Event()
    recorders = []
    paste_log = []
    call_number = 0
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"paste": True}))

    def recorder_factory():
        recorder = FakeRecorder()
        recorder.stopped = threading.Event()
        original_stop = recorder.stop

        def stop():
            result = original_stop()
            recorder.stopped.set()
            return result

        recorder.stop = stop
        recorders.append(recorder)
        return recorder

    def transcribe_audio(wav):
        nonlocal call_number
        call_number += 1
        if call_number == 1:
            first_started.set()
            assert allow_first.wait(2)
            return "first"
        return "second"

    api = Api(
        config_path=cfg,
        history=HistoryStore(tmp_path / "ordered.jsonl"),
        recorder_factory=recorder_factory,
        transcribe_fn=transcribe_audio,
        paste_fn=lambda text: paste_log.append(text) or True,
        clipboard_fn=lambda text: None,
    )
    try:
        api.start_recording()
        first = api._release_recording(paste=True)
        assert first_started.wait(2)

        api.start_recording()
        second = api._release_recording(paste=True)
        assert recorders[1].stopped.wait(2)
        assert second.done() is False

        allow_first.set()
        assert first.result(timeout=2).to_api_dict()["text"] == "First"
        assert second.result(timeout=2).to_api_dict()["text"] == "Second"
        assert paste_log == ["First", " Second"]
        stored = [json.loads(line)["text"]
                  for line in (tmp_path / "ordered.jsonl").read_text().splitlines()]
        assert stored == ["First", "Second"]
    finally:
        allow_first.set()
        api.close()


def test_release_snapshots_dictionary_before_queue_wait(tmp_path):
    first_started = threading.Event()
    allow_first = threading.Event()
    calls = 0
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "paste": False,
        "dictionary": {"trade mate": "TradeMate"},
    }))

    def transcribe_audio(wav):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            assert allow_first.wait(2)
            return "first"
        return "trade mate"

    api = Api(
        config_path=cfg,
        history=HistoryStore(tmp_path / "snapshot.jsonl"),
        recorder_factory=FakeRecorder,
        transcribe_fn=transcribe_audio,
        clipboard_fn=lambda text: None,
    )
    try:
        api.start_recording()
        first = api._release_recording()
        assert first_started.wait(2)
        api.start_recording()
        second = api._release_recording()
        api.set_config({"dictionary": {"trade mate": "Changed Later"}})

        allow_first.set()
        first.result(timeout=2)
        assert second.result(timeout=2).to_api_dict()["text"] == "TradeMate"
    finally:
        allow_first.set()
        api.close()


class ExplodingHistory:
    def add(self, text, duration_s):
        raise OSError("history disk unavailable")

    def list(self):
        return []


def test_history_failure_warns_but_still_delivers(tmp_path):
    copied = []
    api = Api(
        config_path=tmp_path / "config.json",
        history=ExplodingHistory(),
        recorder_factory=FakeRecorder,
        transcribe_fn=lambda wav: "still usable",
        clipboard_fn=copied.append,
        clock=iter([1.0, 2.0]).__next__,
    )
    try:
        api.start_recording()
        result = api.stop_recording()
        assert result["text"] == "Still usable"
        assert result["warning"].startswith("history failed:")
        assert "item" not in result
        assert copied == ["Still usable"]
    finally:
        api.close()


def test_paste_failure_uses_clipboard_and_queue_continues(tmp_path):
    copied = []
    calls = iter([RuntimeError("paste exploded"), None])

    def paste(text):
        failure = next(calls)
        if failure:
            raise failure
        return True

    api = Api(
        config_path=tmp_path / "config.json",
        history=HistoryStore(tmp_path / "paste.jsonl"),
        recorder_factory=FakeRecorder,
        transcribe_fn=lambda wav: "hello",
        paste_fn=paste,
        clipboard_fn=copied.append,
    )
    api.set_config({"paste": True})
    try:
        api.start_recording()
        first = api.stop_recording(paste=True)
        api.start_recording()
        second = api.stop_recording(paste=True)
        assert first["pasted"] is False and first["copied"] is True
        assert copied == ["Hello"]
        assert second["pasted"] is True
    finally:
        api.close()


def test_clipboard_failure_returns_text_and_history_then_advances(tmp_path):
    calls = 0

    def clipboard(text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("pasteboard unavailable")

    api = Api(
        config_path=tmp_path / "config.json",
        history=HistoryStore(tmp_path / "clipboard.jsonl"),
        recorder_factory=FakeRecorder,
        transcribe_fn=lambda wav: "recoverable text",
        clipboard_fn=clipboard,
    )
    try:
        api.start_recording()
        first = api.stop_recording()
        api.start_recording()
        second = api.stop_recording()
        assert first["text"] == "Recoverable text"
        assert first["item"]["text"] == "Recoverable text"
        assert first["error"].startswith("delivery failed:")
        assert first["copied"] is False
        assert second["text"] == "Recoverable text"
        assert "error" not in second
    finally:
        api.close()


@pytest.mark.parametrize("failure", ["history", "paste", "clipboard"])
def test_admitted_session_after_delivery_failure_completes_in_fifo_order(
    tmp_path, failure
):
    first_processing = threading.Event()
    allow_first = threading.Event()
    transcriptions = 0
    clipboard = []
    pasted = []
    paste_attempts = 0

    class FirstHistoryFails:
        def __init__(self):
            self.items = []

        def add(self, text, duration_s):
            if not self.items:
                self.items.append("failed")
                raise OSError("history unavailable")
            self.items.append(text)
            return {"text": text}

        def list(self):
            return []

    def transcribe_audio(wav):
        nonlocal transcriptions
        transcriptions += 1
        if transcriptions == 1:
            first_processing.set()
            assert allow_first.wait(2)
            return "first"
        return "second"

    def paste(text):
        nonlocal paste_attempts
        paste_attempts += 1
        if failure == "paste" and paste_attempts == 1:
            raise OSError("paste unavailable")
        pasted.append(text)
        return True

    def copy(text):
        if failure == "clipboard" and not clipboard:
            clipboard.append("failed")
            raise OSError("clipboard unavailable")
        clipboard.append(text)

    history = FirstHistoryFails() if failure == "history" else HistoryStore(
        tmp_path / f"{failure}.jsonl"
    )
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"paste": failure == "paste"}))
    api = Api(
        config_path=cfg,
        history=history,
        recorder_factory=FakeRecorder,
        transcribe_fn=transcribe_audio,
        paste_fn=paste,
        clipboard_fn=copy,
    )
    try:
        api.start_recording()
        first = api._release_recording(paste=True)
        assert first_processing.wait(2)
        api.start_recording()
        second = api._release_recording(paste=True)
        assert second.done() is False

        allow_first.set()
        first_result = first.result(timeout=2).to_api_dict()
        second_result = second.result(timeout=2).to_api_dict()
        assert [first_result["text"], second_result["text"]] == ["First", "Second"]
        assert "error" not in second_result
        if failure == "history":
            assert first_result["warning"].startswith("history failed:")
        elif failure == "paste":
            assert first_result["pasted"] is False and first_result["copied"] is True
            assert second_result["pasted"] is True
        else:
            assert first_result["copied"] is False
            assert first_result["error"].startswith("delivery failed:")
    finally:
        allow_first.set()
        api.close()


def test_preserved_clipboard_error_suppresses_plain_text_fallback(tmp_path):
    copied = []

    def paste(text):
        raise PasteDeliveryError(
            "unrepresentable clipboard", pasted=False, fallback_safe=False)

    api = Api(
        config_path=tmp_path / "config.json",
        history=HistoryStore(tmp_path / "preserved.jsonl"),
        recorder_factory=FakeRecorder,
        transcribe_fn=lambda wav: "safe in history",
        paste_fn=paste,
        clipboard_fn=copied.append,
    )
    api.set_config({"paste": True})
    try:
        api.start_recording()
        result = api.stop_recording(paste=True)
        assert result["pasted"] is False
        assert result["copied"] is False
        assert result["item"]["text"] == "Safe in history"
        assert result["error"].startswith("delivery failed:")
        assert copied == []
    finally:
        api.close()


def test_restore_error_reports_paste_landed_without_fallback(tmp_path):
    copied = []

    def paste(text):
        raise PasteDeliveryError(
            "restore failed", pasted=True, fallback_safe=False)

    api = Api(
        config_path=tmp_path / "config.json",
        history=HistoryStore(tmp_path / "restore.jsonl"),
        recorder_factory=FakeRecorder,
        transcribe_fn=lambda wav: "already delivered",
        paste_fn=paste,
        clipboard_fn=copied.append,
    )
    api.set_config({"paste": True})
    try:
        api.start_recording()
        result = api.stop_recording(paste=True)
        assert result["pasted"] is True
        assert result["copied"] is True
        assert result["error"].startswith("delivery failed:")
        assert copied == []
    finally:
        api.close()


def test_preservation_failure_resolves_then_next_queued_paste_runs(tmp_path):
    attempts = 0
    copied = []

    def paste(text):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PasteDeliveryError(
                "snapshot failed", pasted=False, fallback_safe=False)
        return True

    api = Api(
        config_path=tmp_path / "config.json",
        history=HistoryStore(tmp_path / "queue-preserved.jsonl"),
        recorder_factory=FakeRecorder,
        transcribe_fn=lambda wav: "queued",
        paste_fn=paste,
        clipboard_fn=copied.append,
    )
    api.set_config({"paste": True})
    try:
        api.start_recording()
        first = api.stop_recording(paste=True)
        api.start_recording()
        second = api.stop_recording(paste=True)
        assert first["error"].startswith("delivery failed:")
        assert first["copied"] is False
        assert second["pasted"] is True
        assert copied == []
        assert attempts == 2
    finally:
        api.close()


def test_paste_landed_error_keeps_smart_spacing_for_next_dictation(tmp_path):
    pasted = []
    copied = []

    def paste(text):
        pasted.append(text)
        if len(pasted) == 1:
            raise PasteDeliveryError(
                "restore failed", pasted=True, fallback_safe=False)
        return True

    api = Api(
        config_path=tmp_path / "config.json",
        history=HistoryStore(tmp_path / "paste-landed.jsonl"),
        recorder_factory=FakeRecorder,
        transcribe_fn=lambda wav: "queued",
        paste_fn=paste,
        clipboard_fn=copied.append,
        clock=lambda: 1.0,
    )
    api.set_config({"paste": True})
    try:
        api.start_recording()
        first = api.stop_recording(paste=True)
        api.start_recording()
        second = api.stop_recording(paste=True)
        assert first["error"].startswith("delivery failed:")
        assert first["pasted"] is True and first["copied"] is True
        assert second["pasted"] is True
        assert pasted == ["Queued", " Queued"]
        assert copied == []
    finally:
        api.close()


def test_get_permissions_returns_microphone_only(tmp_path):
    api = make_api(tmp_path)
    panes = api.get_permissions()["panes"]
    assert [p["key"] for p in panes] == ["microphone"]
    assert all("settings_url" in p for p in panes)


def test_open_settings_pane_rejects_unknown_key(tmp_path):
    api = make_api(tmp_path)
    assert api.open_settings_pane("bogus") == {"ok": False}


def test_restore_history_item_puts_it_back(tmp_path):
    api = make_api(tmp_path)
    api.start_recording()
    api.stop_recording()
    item = api.get_history()[0]
    api.delete_history_item(item["id"])
    assert api.get_history() == []
    assert api.restore_history_item(item) == {"ok": True}
    assert api.get_history()[0]["id"] == item["id"]


def test_get_models_lists_known_models_with_installed_flags(tmp_path):
    api = make_api(tmp_path)
    out = api.get_models()
    values = [m["value"] for m in out["models"]]
    assert values == ["small.en", "base.en"]
    assert all(isinstance(m["installed"], bool) for m in out["models"])


def test_set_launch_at_login_round_trips(tmp_path, monkeypatch):
    from voisr import launch_agent

    calls = []
    monkeypatch.setattr(launch_agent, "enable", lambda: calls.append("enable"))
    monkeypatch.setattr(launch_agent, "disable", lambda: calls.append("disable"))

    api = make_api(tmp_path)
    assert api.set_launch_at_login(True)["enabled"] is True
    assert calls == ["enable"]
    assert api.get_config()["launch_at_login"] is True
    assert api.set_launch_at_login(False)["enabled"] is False
    assert calls == ["enable", "disable"]
    assert api.get_config()["launch_at_login"] is False


def test_set_launch_at_login_reports_failure(tmp_path, monkeypatch):
    from voisr import launch_agent

    def explode():
        raise OSError("registry unavailable")

    monkeypatch.setattr(launch_agent, "enable", explode)
    api = make_api(tmp_path)
    assert api.set_launch_at_login(True) == {"error": "registry unavailable"}


def test_clear_history_wipes_everything(tmp_path):
    api = make_api(tmp_path)
    api.start_recording()
    api.stop_recording()
    assert api.clear_history() == {"ok": True, "removed": 1}
    assert api.get_history() == []


def test_export_history_txt_and_json(tmp_path):
    api = make_api(tmp_path)
    api.start_recording()
    api.stop_recording()
    txt = api.export_history("txt")
    assert "Hello world" in txt["text"]
    assert txt["suggested_name"].endswith(".txt")
    parsed = json.loads(api.export_history("json")["text"])
    assert parsed[0]["text"] == "Hello world"
    assert api.export_history("exe").get("error")


def test_set_appearance_accepts_only_known_modes(tmp_path, monkeypatch):
    from voisr import ui as ui_module

    monkeypatch.setattr(ui_module, "_find_window_hwnd", lambda: None)
    api = make_api(tmp_path)
    assert api.set_appearance("dark")["appearance"] == "dark"
    assert api.get_config()["appearance"] == "dark"
    assert api.set_appearance("light")["appearance"] == "light"
    assert api.set_appearance("system")["appearance"] == "system"
    assert api.set_appearance("hot-pink").get("error")
    assert api.get_config()["appearance"] == "system"  # unchanged by the bad value


def test_set_appearance_applies_chrome_when_a_window_exists(tmp_path, monkeypatch):
    from voisr import ui as ui_module

    calls = []
    monkeypatch.setattr(ui_module, "_find_window_hwnd", lambda: 4321)
    monkeypatch.setattr(ui_module, "apply_chrome",
                         lambda hwnd, dark: calls.append((hwnd, dark)))
    api = make_api(tmp_path)
    api.set_appearance("dark")
    assert calls == [(4321, True)]


class FakeWindow:
    def __init__(self, show_error=None):
        self.shown = 0
        self._show_error = show_error

    def show(self):
        self.shown += 1
        if self._show_error is not None:
            raise self._show_error


class FakeUser32:
    """Records the window-state calls present_window makes."""

    def __init__(self, minimized=False):
        self.minimized = minimized
        self.restored = []
        self.checked = []

    def is_minimized(self, hwnd):
        self.checked.append(hwnd)
        return self.minimized

    def restore(self, hwnd):
        self.restored.append(hwnd)


def test_present_window_restores_a_minimized_window(monkeypatch):
    from voisr import ui as ui_module

    monkeypatch.setattr(ui_module, "_find_window_hwnd", lambda: 4321)
    window = FakeWindow()
    user32 = FakeUser32(minimized=True)
    ui_module.present_window(window, user32=user32)
    assert window.shown == 1
    assert user32.restored == [4321]


def test_present_window_leaves_an_unminimized_window_untouched(monkeypatch):
    from voisr import ui as ui_module

    monkeypatch.setattr(ui_module, "_find_window_hwnd", lambda: 4321)
    window = FakeWindow()
    user32 = FakeUser32(minimized=False)
    ui_module.present_window(window, user32=user32)
    assert window.shown == 1
    # A maximized dashboard must stay maximized -- SW_RESTORE would shrink it.
    assert user32.restored == []


def test_present_window_shows_even_when_no_hwnd_is_found(monkeypatch):
    from voisr import ui as ui_module

    monkeypatch.setattr(ui_module, "_find_window_hwnd", lambda: None)
    window = FakeWindow()
    user32 = FakeUser32(minimized=True)
    ui_module.present_window(window, user32=user32)
    assert window.shown == 1
    assert user32.checked == []


def test_present_window_restores_even_when_show_raises(monkeypatch):
    from voisr import ui as ui_module

    monkeypatch.setattr(ui_module, "_find_window_hwnd", lambda: 4321)
    window = FakeWindow(show_error=RuntimeError("backend gone"))
    user32 = FakeUser32(minimized=True)
    ui_module.present_window(window, user32=user32)
    assert user32.restored == [4321]


def test_present_window_survives_a_failing_user32(monkeypatch):
    from voisr import ui as ui_module

    class Exploding:
        def is_minimized(self, hwnd):
            raise OSError("no user32 here")

        def restore(self, hwnd):
            raise OSError("no user32 here")

    monkeypatch.setattr(ui_module, "_find_window_hwnd", lambda: 4321)
    window = FakeWindow()
    ui_module.present_window(window, user32=Exploding())  # must not raise
    assert window.shown == 1


def test_is_recording_tracks_the_record_cycle(tmp_path):
    api = make_api(tmp_path)
    assert api.is_recording() is False
    api.start_recording()
    assert api.is_recording() is True
    api.stop_recording()
    assert api.is_recording() is False


def test_save_text_file_writes_through_the_injected_dialog(tmp_path):
    api = make_api(tmp_path)
    target = tmp_path / "out.txt"
    api._save_dialog_fn = lambda name: str(target)
    out = api.save_text_file("hello there", "out.txt")
    assert out["ok"] is True
    assert target.read_text() == "hello there"


def test_save_text_file_reports_cancellation(tmp_path):
    api = make_api(tmp_path)
    api._save_dialog_fn = lambda name: None
    assert api.save_text_file("x", "a.txt") == {"ok": False, "path": None}


# ---------------------------------------------------------------------------
# Task 8: wiring the pill to HotkeyService.press()/release()
# ---------------------------------------------------------------------------

from concurrent.futures import Future

from voisr.recording_queue import SessionResult


class FakeWidget:
    def __init__(self):
        self.calls = []

    def show_recording(self, **kw):
        self.calls.append("recording")

    def show_processing(self, **kw):
        self.calls.append("processing")

    def show_done(self, **kw):
        self.calls.append("done")

    def show_error(self, **kw):
        self.calls.append("error")

    def hide(self, **kw):
        self.calls.append("hide")


class FakeHotkeyApi:
    """Stands in for Api's slice HotkeyService actually talks to."""

    def __init__(self, show_widget=True, fail=False, start_error=None):
        self._config = {"show_widget": show_widget, "pill_theme": "classic",
                         "pill_color": None}
        self._fail = fail
        self.start_error = start_error
        self.start_calls = 0

    def get_config(self):
        return dict(self._config)

    def start_recording(self):
        self.start_calls += 1
        if self.start_error is not None:
            return {"error": self.start_error}
        return {"recording": True}

    def _release_recording(self, paste=True):
        future = Future()
        if self._fail:
            future.set_result(SessionResult.failure(1, "transcription", "boom"))
        else:
            future.set_result(SessionResult.success(1, text="hi"))
        return future


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def hotkey_service_with_widget():
    widget = FakeWidget()
    service = HotkeyService(FakeHotkeyApi(), widget=widget, hide_delay=0)
    return service, widget


@pytest.fixture
def hotkey_service_failing():
    widget = FakeWidget()
    service = HotkeyService(FakeHotkeyApi(fail=True), widget=widget, hide_delay=0)
    return service, widget


@pytest.fixture
def hotkey_service_no_widget():
    widget = FakeWidget()
    service = HotkeyService(FakeHotkeyApi(show_widget=False), widget=widget,
                             hide_delay=0)
    return service, widget


def test_press_shows_the_recording_pill(hotkey_service_with_widget):
    service, widget = hotkey_service_with_widget
    service.press()
    assert widget.calls == ["recording"]


def test_release_shows_processing_then_done(hotkey_service_with_widget):
    service, widget = hotkey_service_with_widget
    service.press()
    service.release()
    assert _wait_until(lambda: widget.calls[-1:] in (["done"], ["hide"]))
    assert "processing" in widget.calls
    assert widget.calls[-1] in ("done", "hide")


def test_transcription_failure_shows_the_error_pill(hotkey_service_failing):
    service, widget = hotkey_service_failing
    service.press()
    service.release()
    assert _wait_until(lambda: "error" in widget.calls)
    assert "error" in widget.calls


def test_widget_disabled_in_config_is_never_shown(hotkey_service_no_widget):
    service, widget = hotkey_service_no_widget
    service.press()
    service.release()
    time.sleep(0.1)  # give the background finish thread a chance to (not) act
    assert widget.calls == []


def test_epoch_guard_drops_a_stale_finish(hotkey_service_with_widget):
    """A finishing dictation must not touch the pill once a newer one has
    already started -- otherwise a slow finish could hide the pill for a
    dictation that is still running. Exercised directly against the guard
    rather than by racing real threads, so it is deterministic."""
    service, widget = hotkey_service_with_widget
    service.press()   # epoch -> 1
    service.release()
    service.press()   # epoch -> 2, a new dictation has started
    stale_epoch = 1
    ran = []
    service._run_widget_action(stale_epoch, lambda: ran.append("stale"))
    assert ran == [], "a stale epoch must never be allowed to act on the widget"
    ran2 = []
    service._run_widget_action(service._epoch, lambda: ran2.append("current"))
    assert ran2 == ["current"]


# ---------------------------------------------------------------------------
# Task 3 (Phase 4): _mark_session tray bookkeeping
# ---------------------------------------------------------------------------

class FakeStatus:
    def __init__(self):
        self.states = []

    def set_recording(self, value):
        self.states.append(bool(value))

    def release(self):
        self.states.append("released")


class ExplodingStatus:
    def set_recording(self, value):
        raise RuntimeError("status item boom")

    def release(self):
        raise RuntimeError("status item boom")


@pytest.fixture
def api(tmp_path):
    return make_api(tmp_path)


@pytest.fixture
def api_with_status(tmp_path):
    api = make_api(tmp_path)
    status = FakeStatus()
    api.attach_status_item(status)
    return api, status


@pytest.fixture
def api_with_status_failing(tmp_path):
    api = make_api(tmp_path)
    api.attach_status_item(ExplodingStatus())
    return api, None


def test_status_lights_while_a_dictation_is_in_flight(api_with_status):
    api, status = api_with_status
    api._mark_session(1, in_flight=True)
    assert status.states == [True]


def test_status_clears_when_the_last_session_finishes(api_with_status):
    api, status = api_with_status
    api._mark_session(1, in_flight=True)
    api._mark_session(1, in_flight=False)
    assert status.states == [True, False]


def test_overlapping_dictations_keep_the_status_lit(api_with_status):
    """Two queued dictations must not clear the icon when the first ends."""
    api, status = api_with_status
    api._mark_session(1, in_flight=True)
    api._mark_session(2, in_flight=True)
    api._mark_session(1, in_flight=False)
    assert status.states == [True]
    api._mark_session(2, in_flight=False)
    assert status.states == [True, False]


def test_status_is_only_pushed_on_change(api_with_status):
    api, status = api_with_status
    api._mark_session(1, in_flight=True)
    api._mark_session(2, in_flight=True)
    assert status.states == [True]


def test_status_failure_never_breaks_a_dictation(api_with_status_failing):
    api, _ = api_with_status_failing
    api._mark_session(1, in_flight=True)  # must not raise


def test_api_without_a_status_item_works(api):
    api._mark_session(1, in_flight=True)  # must not raise


def test_record_cycle_lights_and_clears_the_status_item(tmp_path):
    """End-to-end through start_recording/stop_recording, not just the
    bookkeeping helper directly."""
    api = make_api(tmp_path)
    status = FakeStatus()
    api.attach_status_item(status)
    api.start_recording()
    assert status.states == [True]
    api.stop_recording()
    assert status.states == [True, False]


def test_close_releases_the_status_item(tmp_path):
    api = make_api(tmp_path)
    status = FakeStatus()
    api.attach_status_item(status)
    api.close()
    assert status.states == ["released"]


class FakeDestroyable:
    """A pywebview window or the pill, recording destroys onto a shared log so
    a test can assert the order the two are torn down in."""

    def __init__(self, name, log, error=None):
        self.name = name
        self._log = log
        self._error = error

    def destroy(self):
        self._log.append(self.name)
        if self._error is not None:
            raise self._error


def test_destroy_windows_destroys_the_dashboard_and_the_pill():
    """pywebview's winforms backend only calls WinForms.Application.Exit()
    once BrowserView.instances is empty, and main() creates the pill first so
    it holds the 'master' uid. Destroying the dashboard alone left the process
    alive with right-Ctrl still globally captured: the window vanished, the
    PID answered 25s later, and a relaunch produced a second instance."""
    from voisr import ui as ui_module

    log = []
    ui_module.destroy_windows(FakeDestroyable("dashboard", log),
                              FakeDestroyable("pill", log))
    # Dashboard first: the visible window goes at once, and 'master' -- the
    # form owning Application.Run() -- is destroyed last, as in the ordinary
    # single-window case.
    assert log == ["dashboard", "pill"]


def test_destroy_windows_destroys_the_pill_even_when_the_dashboard_raises():
    """The exact failure that would recreate the zombie: one raising destroy
    must not strand the other window and keep the message loop alive."""
    from voisr import ui as ui_module

    log = []
    ui_module.destroy_windows(
        FakeDestroyable("dashboard", log, error=RuntimeError("backend gone")),
        FakeDestroyable("pill", log))
    assert log == ["dashboard", "pill"]


def test_destroy_windows_never_raises_out_of_the_quit_handler():
    """menu_quit runs on the tray thread and is also the route a finished
    install exits by, so it must not propagate a backend failure."""
    from voisr import ui as ui_module

    log = []
    ui_module.destroy_windows(
        FakeDestroyable("dashboard", log, error=RuntimeError("dashboard")),
        FakeDestroyable("pill", log, error=RuntimeError("pill")))
    assert log == ["dashboard", "pill"]


def test_destroy_windows_survives_a_pill_that_never_got_created():
    """RecordingWidget.create() swallows its own failures, so the pill can be
    absent; the dashboard must still be destroyed."""
    from voisr import ui as ui_module

    log = []
    ui_module.destroy_windows(FakeDestroyable("dashboard", log), None)
    assert log == ["dashboard"]


# --- a hotkey press that cannot start recording -----------------------------
# A disconnected microphone made press() return before the pill was ever
# shown, with nothing logged. The app looked completely dead: no pill, no
# dictation, no explanation -- indistinguishable from a broken build, which
# is exactly what it got blamed on.

_MIC_GONE = "recorder failed: Error querying device -1"


def _failing_start_service(monkeypatch, show_widget=True):
    from voisr import ui as ui_module

    logged = []
    monkeypatch.setattr(ui_module, "_update_log", logged.append)
    widget = FakeWidget()
    api = FakeHotkeyApi(show_widget=show_widget, start_error=_MIC_GONE)
    service = HotkeyService(api, widget=widget, hide_delay=0)
    return service, widget, api, logged


def test_a_start_failure_flashes_the_error_pill(monkeypatch):
    service, widget, _api, _logged = _failing_start_service(monkeypatch)
    service.press()
    assert "error" in widget.calls, (
        "a press that cannot record must look like a failure, not a no-op")


def test_a_start_failure_is_logged_with_its_reason(monkeypatch):
    """The log is the only durable trace. Without it a user cannot tell a
    missing microphone from a broken release."""
    service, _widget, _api, logged = _failing_start_service(monkeypatch)
    service.press()
    assert any(_MIC_GONE in line for line in logged), logged


def test_auto_repeat_reports_a_start_failure_only_once(monkeypatch):
    """Holding the hotkey auto-repeats WM_KEYDOWN. _pressed stays False when
    the start failed, so without a guard every repeat would re-report --
    spamming the log and respawning a hide thread per repeat."""
    service, widget, api, logged = _failing_start_service(monkeypatch)
    for _ in range(6):
        service.press()
    assert widget.calls.count("error") == 1, widget.calls
    assert len(logged) == 1, logged
    assert api.start_calls == 1, "the queue must not be hammered per repeat"


def test_releasing_lets_the_next_press_try_again(monkeypatch):
    """The once-per-hold guard must clear on key up, or plugging the mic back
    in would never recover without a restart."""
    service, widget, api, _logged = _failing_start_service(monkeypatch)
    service.press()
    service.release()
    api.start_error = None          # microphone reconnected
    service.press()
    assert "recording" in widget.calls


def test_a_start_failure_is_logged_even_with_the_pill_disabled(monkeypatch):
    service, widget, _api, logged = _failing_start_service(
        monkeypatch, show_widget=False)
    service.press()
    assert widget.calls == []
    assert len(logged) == 1
