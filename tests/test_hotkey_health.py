"""HotkeyService reports health from the hook Windows actually gave it,
respawns a hook Windows unregistered, and refuses a press while microphone
access is switched off. Windows port of the macOS suite: there is no tap mode
or TCC grant here, only the WH_KEYBOARD_LL hook's own state."""
import threading
import time
from types import SimpleNamespace

from voisr.history import HistoryStore
from voisr.ui import Api, HotkeyService


class ConfigApi:
    def __init__(self, **cfg):
        self.cfg = {"hotkey": "ctrl_r", **cfg}

    def get_config(self):
        return dict(self.cfg)

    def note_user_typing(self):
        pass


class FakeListener:
    def __init__(self, state):
        self.state = state
        self.stopped = False

    def wait_started(self, timeout):
        return self.state

    def stop(self):
        self.stopped = True


def make(api, *, granted=True, hook_state="running", poll_interval=60):
    made = []

    def factory(handler):
        listener = FakeListener(hook_state)
        made.append(listener)
        return listener

    svc = HotkeyService(api, permissions_check=lambda: granted,
                        listener_factory=factory, poll_interval=poll_interval)
    return svc, made


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_running_hook_is_active():
    svc, _ = make(ConfigApi(), hook_state="running")
    svc.start()
    assert svc.health == "active" and svc.status.startswith("active on ")
    svc.stop()


def test_a_slow_hook_is_starting():
    svc, _ = make(ConfigApi(), hook_state="starting")
    svc.start()
    assert svc.health == "starting"
    assert svc.status.startswith("starting push-to-talk on ")
    svc.stop()


def test_a_failed_hook_says_so():
    svc, _ = make(ConfigApi(), hook_state="failed")
    svc.start()
    assert svc.health == "failed" and "could not be installed" in svc.status
    svc.stop()


def test_listener_without_wait_started_follows_the_permission_check():
    # a bare handler (test double) has no hook state: it is running only if
    # the permission check says so
    for granted, health in ((True, "active"), (False, "failed")):
        svc = HotkeyService(ConfigApi(), permissions_check=lambda g=granted: g,
                            listener_factory=lambda handler: handler,
                            poll_interval=60)
        svc.start()
        assert svc.health == health
        svc.stop()


def test_stop_sets_health_off():
    svc, _ = make(ConfigApi())
    assert svc.health == "off"
    svc.start()
    assert svc.health == "active"
    svc.stop()
    assert svc.health == "off"


def test_an_unregistered_hook_is_respawned_and_the_held_key_checked():
    # Windows silently unregisters a hook whose callback overran
    # LowLevelHooksTimeout; the release that ends a dictation can be lost
    # in the gap, so a respawn also asks the keyboard about the held key.
    svc, made = make(ConfigApi(), poll_interval=0.02)
    forced = []
    svc._check_stale_key = lambda force=False: forced.append(force)
    svc.start()
    made[0].state = "failed"
    assert wait_until(lambda: len(made) >= 2 and svc.health == "active")
    assert made[0].stopped
    assert True in forced
    svc.stop()


def test_a_live_hook_is_left_alone_by_the_poll():
    svc, made = make(ConfigApi(), poll_interval=0.02)
    svc.start()
    time.sleep(0.15)  # several polls
    assert len(made) == 1 and not made[0].stopped
    svc.stop()


def test_health_stays_off_after_stop_even_if_a_refresh_races():
    release, entered = threading.Event(), threading.Event()

    class Blocking(FakeListener):
        def wait_started(self, timeout):
            entered.set()
            release.wait(2)
            return self.state

    listeners = [FakeListener("failed"), Blocking("running")]
    svc = HotkeyService(ConfigApi(), listener_factory=lambda h: listeners.pop(0)
                        if listeners else FakeListener("running"),
                        poll_interval=0.02)
    svc.start()
    assert svc.health == "failed"
    assert entered.wait(3)  # the poll is now blocked inside wait_started
    threading.Timer(0.1, release.set).start()
    svc.stop()
    assert svc.health == "off"
    time.sleep(0.1)
    assert svc.health == "off"


def test_get_hotkey_status_reports_health(tmp_path):
    api = Api(config_path=tmp_path / "config.json",
              history=HistoryStore(tmp_path / "h.jsonl"))
    assert api.get_hotkey_status()["health"] == "off"

    class Svc:
        health = "failed"
        status = "the keyboard hook could not be installed — retrying"
        running = True
        holding = False

    api.attach_hotkey_service(Svc())
    assert api.get_hotkey_status()["health"] == "failed"


def _api(tmp_path, mic):
    return Api(config_path=tmp_path / "config.json",
               history=HistoryStore(tmp_path / "h.jsonl"),
               mic_check=lambda: mic)


def test_switched_off_mic_refuses_the_press_and_names_the_permission(tmp_path):
    result = _api(tmp_path, False).start_recording()
    assert result["permission"] == "microphone" and "error" in result


def test_unknown_mic_switch_does_not_block(tmp_path):
    api = _api(tmp_path, None)
    api._recordings.start = lambda: SimpleNamespace(sequence=1)  # never opens a real mic
    assert api.start_recording() == {"recording": True, "session_id": 1}


def test_a_raising_mic_check_does_not_block(tmp_path):
    def boom():
        raise OSError("registry unavailable")

    api = Api(config_path=tmp_path / "config.json",
              history=HistoryStore(tmp_path / "h.jsonl"), mic_check=boom)
    api._recordings.start = lambda: SimpleNamespace(sequence=1)
    assert api.start_recording() == {"recording": True, "session_id": 1}


def test_settings_opens_at_most_once_a_minute_on_repeated_refusals():
    class RefusingApi(ConfigApi):
        def start_recording(self):
            return {"error": "mic off", "permission": "microphone"}

    opened = []
    svc = HotkeyService(RefusingApi(), permissions_check=lambda: True,
                        listener_factory=lambda h: h)
    svc._pane_opener = opened.append
    clock = [1000.0]
    svc._clock = lambda: clock[0]
    for _ in range(5):            # auto-repeat and fresh presses alike
        svc.press(); svc.release()
    assert opened == ["microphone"]
    clock[0] += 61
    svc.press(); svc.release()
    assert opened == ["microphone", "microphone"]


def test_auto_repeat_of_a_refused_press_does_not_retry():
    # Holding the key auto-repeats WM_KEYDOWN: one refusal per physical hold.
    calls = []

    class RefusingApi(ConfigApi):
        def start_recording(self):
            calls.append(True)
            return {"error": "no microphone"}

    svc = HotkeyService(RefusingApi(), listener_factory=lambda h: h)
    for _ in range(4):
        svc.press()
    assert len(calls) == 1
    svc.release()
    svc.press()
    assert len(calls) == 2


def test_a_late_event_after_stop_runs_nothing_on_the_hook_thread():
    handlers = []
    typed = []

    class TypingApi(ConfigApi):
        def note_user_typing(self):
            typed.append(True)

    svc = HotkeyService(TypingApi(), permissions_check=lambda: True,
                        listener_factory=lambda h: handlers.append(h)
                        or FakeListener("running"), poll_interval=60)
    ran = []
    svc.press = lambda *a, **k: ran.append("press")
    svc.release = lambda *a, **k: ran.append("release")
    svc.cancel_chord = lambda *a, **k: ran.append("chord")
    svc.start()
    svc.stop()
    handler = handlers[-1]
    handler(0xA3, "down")      # the hotkey (Right Ctrl)
    handler(0xA3, "up")
    handler(0x41, "down")      # any other key
    assert ran == [] and typed == []
