"""Config writes are atomic and serialised, get_config is served from memory
while the file is unchanged, the whisper glossary is bounded, and Home's
listener dot reports the real state. Ported from the macOS 2026-10-12 audit
(H1, L2, L4, L5, M6)."""
import json
import os
import threading
import time

import pytest

from voisr import ui as ui_mod
from voisr.history import HistoryStore
from voisr.ui import Api, HotkeyService, glossary_prompt


class FakeRecorder:
    def start(self, path):
        self.path = path

    def stop(self):
        import array
        import wave
        with wave.open(str(self.path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(array.array("h", [0, 900, -800, 1200] * 4000).tobytes())
        return ""

    def cancel(self):
        pass


def make_api(tmp_path, service=None, **kwargs):
    kwargs.setdefault("recorder_factory", FakeRecorder)
    api = Api(config_path=tmp_path / "config.json",
              history=HistoryStore(tmp_path / "h.jsonl"),
              transcribe_fn=lambda wav: "hello", clock=lambda: 1.0, **kwargs)
    if service is not None:
        api.attach_hotkey_service(service)
    return api


# ---- atomic, serialised writes (H1) ----

def test_set_config_failed_write_leaves_config_intact_and_no_temp_file(tmp_path, monkeypatch):
    """A config write that dies mid-way must not leave a truncated file
    (which load_config would quarantine, costing the dictionary)."""
    api = make_api(tmp_path)
    api.set_config({"dictionary": {"trademake": "TradeMate"}})
    before = (tmp_path / "config.json").read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        api.set_config({"paste": False})

    assert (tmp_path / "config.json").read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if p.name != "config.json"
            and not p.name.startswith("h.jsonl")] == []


def test_concurrent_set_config_calls_keep_every_update(tmp_path, monkeypatch):
    """Two bridge calls updating different keys at once must not clobber
    each other's write."""
    real_load = ui_mod.load_config

    def slow_load(path, **kwargs):
        cfg = real_load(path, **kwargs)
        time.sleep(0.02)   # widen the read-modify-write window
        return cfg

    monkeypatch.setattr(ui_mod, "load_config", slow_load)
    api = make_api(tmp_path)
    updates = [("pill_theme", "ember"), ("pill_color", "#112233"),
               ("appearance", "dark"), ("model", "base.en"),
               ("hotkey_label", "X"), ("paste", False), ("show_widget", False),
               ("check_for_updates", False)]
    barrier = threading.Barrier(len(updates))

    def worker(i):
        barrier.wait()
        api.set_config(dict([updates[i]]))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(updates))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cfg = api.get_config()
    assert all(cfg.get(k) == v for k, v in updates), cfg


def test_capture_hotkey_write_is_atomic(tmp_path, monkeypatch):
    api = make_api(tmp_path, capture_fn=lambda timeout: (0x7C, "F13"))
    api.set_config({"dictionary": {"a": "b"}})
    before = (tmp_path / "config.json").read_bytes()
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        api.capture_hotkey()
    assert (tmp_path / "config.json").read_bytes() == before


def test_set_config_hotkey_name_clears_a_captured_vk(tmp_path):
    """A named-key change restarts the listener with that name, so a stale
    captured hotkey_vk must not keep winning in _resolve_target."""
    restarted = []

    class Service:
        def restart(self, key_name=None):
            restarted.append(key_name)

    (tmp_path / "config.json").write_text(json.dumps(
        {"hotkey": "ctrl_r", "hotkey_vk": 0x7C, "hotkey_label": "F13"}))
    api = make_api(tmp_path, Service())
    api.set_config({"hotkey": "alt_r"})
    cfg = api.get_config()
    assert cfg["hotkey"] == "alt_r"
    assert "hotkey_vk" not in cfg and "hotkey_label" not in cfg
    assert restarted == ["alt_r"]


# ---- get_config is served from memory (L5) ----

def test_get_config_does_not_reread_an_unchanged_file(tmp_path, monkeypatch):
    """get_config runs on the hotkey worker on every press and release; an
    unchanged file must be served from memory."""
    calls = []
    real = ui_mod.load_config

    def counting(path, **kwargs):
        calls.append(1)
        return real(path, **kwargs)

    monkeypatch.setattr(ui_mod, "load_config", counting)
    api = make_api(tmp_path)
    api.set_config({"paste": False})
    calls.clear()
    assert api.get_config()["paste"] is False
    assert api.get_config()["paste"] is False
    assert len(calls) == 1


def test_get_config_sees_an_external_edit(tmp_path):
    api = make_api(tmp_path)
    api.set_config({"paste": False})
    assert api.get_config()["paste"] is False
    (tmp_path / "config.json").write_text(json.dumps({"paste": True, "dictionary": {}}))
    assert api.get_config()["paste"] is True


def test_get_config_returns_a_private_copy(tmp_path):
    api = make_api(tmp_path)
    api.set_config({"dictionary": {"a": "b"}})
    cfg = api.get_config()
    cfg["dictionary"]["zzz"] = "mutated"
    assert "zzz" not in api.get_config()["dictionary"]


# ---- the whisper glossary (L4) ----

def test_glossary_prompt_is_capped_to_the_most_recent_terms():
    """whisper.cpp truncates --prompt at ~224 tokens with no warning, so a
    big dictionary silently loses its newest (most useful) terms."""
    dictionary = {f"spoken{i}": f"Written{i}" for i in range(200)}
    prompt = glossary_prompt(dictionary)
    assert prompt.startswith("Glossary: ")
    assert "Written199" in prompt          # newest kept
    assert "Written0" not in prompt        # oldest dropped
    assert len(prompt) <= 600


def test_glossary_prompt_is_none_for_an_empty_dictionary():
    assert glossary_prompt({}) is None
    assert glossary_prompt(None) is None


# ---- Home's dot reports the listener's real state (M6) ----

class StateService:
    def __init__(self, status="active on Right Ctrl", running=True, holding=False):
        self.status = status
        self.running = running
        self.holding = holding
        self.health = "active"


def test_hotkey_state_listening_when_active(tmp_path):
    assert make_api(tmp_path, StateService()).get_hotkey_status()["state"] == "listening"


def test_hotkey_state_off_without_a_service_or_a_live_hook(tmp_path):
    assert make_api(tmp_path).get_hotkey_status()["state"] == "off"
    failed = make_api(tmp_path, StateService(
        status="the keyboard hook could not be installed — retrying"))
    assert failed.get_hotkey_status()["state"] == "off"


def test_hotkey_state_off_while_the_listener_is_stopped(tmp_path):
    # capture_hotkey stops the listener for up to 8 s; status still reads
    # "active on ..." then, so it can't be trusted alone
    api = make_api(tmp_path, StateService(running=False))
    assert api.get_hotkey_status()["state"] == "off"


def test_hotkey_state_busy_while_the_dock_is_recording(tmp_path):
    # press() drops every key press while another recording owns the mic
    api = make_api(tmp_path, StateService())
    api.start_recording()
    assert api.get_hotkey_status()["state"] == "busy"
    api.stop_recording()
    assert api.get_hotkey_status()["state"] == "listening"
    api.close()


def test_hotkey_state_listening_while_the_key_itself_records(tmp_path):
    api = make_api(tmp_path, StateService(holding=True))
    api.start_recording()
    assert api.get_hotkey_status()["state"] == "listening"
    api.stop_recording()
    api.close()


def test_hotkey_service_reports_running_and_holding(tmp_path):
    api = make_api(tmp_path)
    svc = HotkeyService(api, listener_factory=lambda h: None,
                        permissions_check=lambda: True, tap_gate_s=0)
    assert svc.running is False and svc.holding is False
    svc.start("ctrl_r")
    assert svc.running is True
    svc.press()
    assert svc.holding is True
    svc.release()
    svc.stop()
    assert svc.running is False
    api.close()


def test_stop_wakes_and_joins_the_watcher(tmp_path):
    """stop() used to just flag the poll thread and leave it sleeping (up to
    poll_interval); a start() racing its exit could find _watcher still set
    and spawn no new one. stop() wakes the thread and joins it."""
    svc = HotkeyService(make_api(tmp_path), hide_delay=0,
                        permissions_check=lambda: True,
                        poll_interval=999, listener_factory=lambda h: h)
    svc.start("ctrl_r")
    t0 = time.monotonic()
    svc.stop()
    assert time.monotonic() - t0 < 1.0, "stop() waited on a sleeping poll"
    assert svc._watcher is None
    svc.start("ctrl_r")
    assert svc._watcher is not None
    svc.stop()


# ---- the hook callback never waits (H2) ----

def test_hook_handler_returns_fast_while_previous_capture_is_finalizing(tmp_path):
    """A press that lands while the previous recorder is still in stop()
    must not block the hook callback: Windows unregisters a low-level hook
    whose callback overruns LowLevelHooksTimeout, and every key event in the
    gap -- including the release that ends the dictation -- is lost."""
    (tmp_path / "config.json").write_text(json.dumps({"hotkey_vk": 0x7C, "paste": False}))
    allow_stop = threading.Event()
    stop_entered = threading.Event()
    made = []

    class BlockingStopRecorder(FakeRecorder):
        def stop(self):
            stop_entered.set()
            assert allow_stop.wait(5)
            return super().stop()

    def recorder_factory():
        rec = BlockingStopRecorder() if not made else FakeRecorder()
        made.append(rec)
        return rec

    class Widget:
        _supports_ownership_action = False

        def __init__(self):
            self.calls = []

        def show_recording(self, theme="classic", color=None):
            self.calls.append("recording")

        def show_processing(self):
            self.calls.append("processing")

        def show_done(self):
            self.calls.append("done")

        def show_error(self):
            self.calls.append("error")

        def hide(self):
            self.calls.append("hide")

    def wait_for(predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        assert predicate()

    handlers = []
    widget = Widget()
    api = make_api(tmp_path, recorder_factory=recorder_factory)
    svc = HotkeyService(api, widget=widget, hide_delay=0, tap_gate_s=0,
                        listener_factory=lambda h: handlers.append(h) or h)
    svc.start()
    handler = handlers[-1]
    try:
        handler(0x7C, "down")
        wait_for(lambda: widget.calls == ["recording"])
        handler(0x7C, "up")
        assert stop_entered.wait(2)

        t0 = time.monotonic()
        handler(0x7C, "down")       # second press while stop() is stuck
        elapsed = time.monotonic() - t0
        assert elapsed < 0.2, f"hook handler blocked for {elapsed:.2f}s"

        handler(0x7C, "up")         # release must not be lost either
        allow_stop.set()
        wait_for(lambda: widget.calls.count("recording") == 2)
        wait_for(lambda: widget.calls[-2:] == ["done", "hide"])
        assert len(made) == 2
        wait_for(lambda: len(api.get_history()) == 2)
    finally:
        allow_stop.set()
        svc.stop()
        api.close()
