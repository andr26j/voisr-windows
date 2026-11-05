"""The kept-loaded whisper-server manager and the routing around it. Fakes
stand in for the binary and the HTTP calls; the job object that ends the
server with Voisr is tested for real in tests/test_whisper_guard.py, and the
real binary in tests/test_whisper_server_real.py."""
import json
import threading
import time

import pytest

from voisr import transcriber, whisper_server as ws
from voisr.history import HistoryStore
from voisr.ui import Api, glossary_prompt
from voisr.whisper_server import ServerError, WhisperServer

from tests.test_silence_pipeline import recorder_writing, tone


class FakePopen:
    def __init__(self, command):
        self.command = command
        self.code = None
        self.stopped = 0
        self.pid = 4000

    def terminate(self):          # TerminateProcess is how a server is stopped
        self.stopped += 1
        self.code = 1

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


def loaded(server):
    with server._cond:
        return bool(server._proc and server._ready and server._proc.alive())


class Rig:
    """A WhisperServer wired to fakes, with a clock the test moves."""

    def __init__(self, tmp_path, binary=r"C:\Voisr\bin\whisper-server.exe", **kw):
        self.spawned = []
        self.requests = []
        self.now = 1000.0
        self.probe_ok = True
        self.infer_result = "hello world"
        self.model = tmp_path / "ggml-small.en.bin"
        self.model.write_bytes(b"m")
        self.wav = tmp_path / "clip.wav"
        self.wav.write_bytes(b"\0" * 32000)
        self.server = WhisperServer(
            find=lambda name: binary,
            spawn=self._spawn, probe=lambda port, t: self.probe_ok,
            infer=self._infer, port_fn=lambda: 40000 + len(self.spawned),
            clock=kw.pop("clock", lambda: self.now), poll=0.01, **kw)

    def _spawn(self, command):
        p = FakePopen(command)
        self.spawned.append(p)
        return p

    def _infer(self, port, wav, fields, timeout):
        self.requests.append((port, wav, dict(fields), timeout))
        if isinstance(self.infer_result, Exception):
            raise self.infer_result
        return self.infer_result

    def ready(self, model=None, timeout=5):
        self.server.prewarm(model or self.model)
        end = time.monotonic() + timeout
        while not loaded(self.server) and time.monotonic() < end:
            time.sleep(0.005)
        assert loaded(self.server)


def test_prewarm_binds_loopback_only_and_loads_once(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    rig.server.prewarm(rig.model)
    rig.server.prewarm(rig.model)
    time.sleep(0.05)
    assert len(rig.spawned) == 1
    cmd = rig.spawned[0].command
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert "0.0.0.0" not in cmd and "::" not in cmd
    assert cmd[cmd.index("-l") + 1] == "en"
    assert cmd[cmd.index("--beam-size") + 1] == "5"


def test_transcribe_sends_prompt_and_settings(tmp_path):
    rig = Rig(tmp_path)
    prompt = glossary_prompt({"voice are": "Voisr"})
    text, info = rig.server.transcribe(rig.wav, rig.model, prompt=prompt)
    assert text == "hello world"
    assert info["model_wait"] >= 0
    port, wav, fields, timeout = rig.requests[0]
    assert fields["prompt"] == "Glossary: Voisr."
    assert fields["language"] == "en" and fields["beam_size"] == "5"
    assert fields["token_timestamps"] == "false"   # else segments split mid-word
    assert wav == rig.wav.read_bytes()
    assert timeout == 30 + 2 * 1.0   # tighter than cli's 120 s: a hung server holds the queue


def test_no_prompt_field_without_a_dictionary(tmp_path):
    rig = Rig(tmp_path)
    rig.server.transcribe(rig.wav, rig.model)
    assert "prompt" not in rig.requests[0][2]


def test_immediately_never_starts_a_server(tmp_path):
    rig = Rig(tmp_path)
    rig.server.prewarm(rig.model, unload="immediately")
    with pytest.raises(ServerError):
        rig.server.transcribe(rig.wav, rig.model)
    time.sleep(0.05)
    assert rig.spawned == []


def test_choosing_immediately_unloads_a_loaded_server(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    rig.server.configure("immediately")
    assert not loaded(rig.server) and rig.spawned[0].stopped == 1


def test_missing_binary_raises_and_does_not_retry_during_cooldown(tmp_path):
    rig = Rig(tmp_path, binary=None)
    with pytest.raises(ServerError):
        rig.server.transcribe(rig.wav, rig.model)
    with pytest.raises(ServerError, match="recently failed"):
        rig.server.transcribe(rig.wav, rig.model)
    assert rig.spawned == []


def test_retries_after_the_cooldown(tmp_path):
    rig = Rig(tmp_path, cooldown=60)
    rig.infer_result = ServerError("boom")
    with pytest.raises(ServerError):
        rig.server.transcribe(rig.wav, rig.model)
    assert not loaded(rig.server)
    rig.now += 61
    rig.infer_result = "back"
    assert rig.server.transcribe(rig.wav, rig.model)[0] == "back"


def test_request_failure_unloads_the_server(tmp_path):
    rig = Rig(tmp_path)
    rig.infer_result = ServerError("bad response")
    with pytest.raises(ServerError):
        rig.server.transcribe(rig.wav, rig.model)
    assert rig.spawned[0].stopped == 1


def test_a_server_that_never_answers_is_bounded(tmp_path):
    rig = Rig(tmp_path, ready_timeout=0.2, wait_for_ready=0.3,
              clock=time.monotonic)
    rig.probe_ok = False
    start = time.monotonic()
    with pytest.raises(ServerError):
        rig.server.transcribe(rig.wav, rig.model)
    assert time.monotonic() - start < 3


def test_a_server_that_exits_while_loading_fails_fast(tmp_path):
    rig = Rig(tmp_path)
    rig.probe_ok = False
    orig = rig._spawn
    rig._spawn = lambda c: (lambda p: (setattr(p, "code", 1), p)[1])(orig(c))
    rig.server._spawn = rig._spawn
    start = time.monotonic()
    with pytest.raises(ServerError):
        rig.server.transcribe(rig.wav, rig.model)
    assert time.monotonic() - start < 3


def test_switching_models_replaces_the_loaded_one(tmp_path):
    rig = Rig(tmp_path)
    other = tmp_path / "ggml-base.en.bin"
    other.write_bytes(b"m")
    rig.ready()
    rig.server.transcribe(rig.wav, other)
    assert len(rig.spawned) == 2
    assert rig.spawned[0].stopped == 1
    assert rig.spawned[1].command[2] == str(other)
    assert rig.requests[0][0] == 40001   # served by the new process


def test_forget_model_unloads_only_that_model(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    rig.server.forget_model(tmp_path / "other.bin")
    assert loaded(rig.server)
    rig.server.forget_model(rig.model)
    assert not loaded(rig.server)


def test_deleted_model_file_is_a_clean_error_not_a_hang(tmp_path):
    rig = Rig(tmp_path)
    rig.model.unlink()
    with pytest.raises(ServerError, match="model missing"):
        rig.server.transcribe(rig.wav, rig.model)


def wait_until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_idle_timeout_unloads(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    rig.now += 299
    time.sleep(0.1)
    assert loaded(rig.server)
    rig.now += 2
    assert wait_until(lambda: not loaded(rig.server))


def test_idle_timer_does_not_expire_while_a_dictation_is_in_flight(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    rig.server.set_busy(True)
    rig.now += 7200
    time.sleep(0.15)
    assert loaded(rig.server)
    rig.server.set_busy(False)       # the clock restarts when it ends
    time.sleep(0.15)
    assert loaded(rig.server)
    rig.now += 301
    assert wait_until(lambda: not loaded(rig.server))


def test_idle_timer_does_not_expire_during_a_request(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    gate = threading.Event()

    def slow(port, wav, fields, timeout):
        gate.wait(3)
        return "done"
    rig.server._infer = slow
    result = []
    t = threading.Thread(target=lambda: result.append(
        rig.server.transcribe(rig.wav, rig.model)))
    t.start()
    time.sleep(0.05)
    rig.now += 7200
    time.sleep(0.15)
    assert loaded(rig.server)
    gate.set()
    t.join(3)
    assert result[0][0] == "done"


def test_never_means_never(tmp_path):
    rig = Rig(tmp_path)
    rig.server.configure("never")
    rig.ready()
    rig.now += 10 ** 7
    time.sleep(0.15)
    assert loaded(rig.server)


def test_shutdown_stops_the_server_and_refuses_new_work(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    rig.server.shutdown()
    assert rig.spawned[0].stopped == 1
    with pytest.raises(ServerError):
        rig.server.transcribe(rig.wav, rig.model)


def test_unload_choices_match_handys(tmp_path):
    assert set(ws.UNLOAD_SECONDS) == {
        "never", "immediately", "min2", "min5", "min10", "min15", "hour1"}
    assert ws.DEFAULT_UNLOAD == "min5"
    assert ws.unload_seconds("min2") == 120 and ws.unload_seconds("hour1") == 3600
    assert ws.unload_seconds("never") is None
    assert ws.unload_seconds("garbage") == 300


# ---- routing ---------------------------------------------------------------

class FakeServer:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.calls = text, error, []

    def transcribe(self, wav, model, prompt=None):
        self.calls.append((wav, model, prompt))
        if self.error:
            raise self.error
        return self.text, {"model_wait": 12}


@pytest.fixture
def cli(monkeypatch):
    calls = []
    monkeypatch.setattr(transcriber, "transcribe",
                        lambda wav, model_path=None, whisper_bin=None, prompt=None:
                        calls.append((wav, model_path, prompt)) or "from cli")
    return calls


def test_routed_uses_the_server_when_it_works(cli):
    srv = FakeServer("from server")
    text, info = transcriber.transcribe_routed("a.wav", "m", "Glossary: X.", srv, "min5")
    assert (text, info["mode"], info["timings"]) == ("from server", "warm", {"model_wait": 12})
    assert cli == []


def test_routed_falls_back_to_cli_on_any_server_failure(cli):
    for error in (ServerError("x"), OSError("y"), RuntimeError("z"), ValueError("w")):
        text, info = transcriber.transcribe_routed(
            "a.wav", "m", "Glossary: X.", FakeServer(error=error), "min5")
        assert text == "from cli" and info["mode"] == "cli-fallback"
        assert "server_attempt" in info["timings"]
    assert cli[0] == ("a.wav", "m", "Glossary: X.")


def test_routed_immediately_and_no_server_are_plain_cli(cli):
    srv = FakeServer("from server")
    assert transcriber.transcribe_routed("a", "m", None, srv, "immediately")[1]["mode"] == "cli"
    assert transcriber.transcribe_routed("a", "m", None, None, "min5")[1]["mode"] == "cli"
    assert srv.calls == []


# ---- Api -------------------------------------------------------------------

class FakeWarm:
    def __init__(self, **kw):
        self.events = []
        self.server = FakeServer(**kw)

    def prewarm(self, model, unload=None):
        self.events.append(("prewarm", str(model), unload))

    def set_busy(self, busy):
        self.events.append(("busy", busy))

    def transcribe(self, *a, **k):
        return self.server.transcribe(*a, **k)

    def unload(self):
        self.events.append(("unload",))

    def configure(self, unload):
        self.events.append(("configure", unload))

    def forget_model(self, path):
        self.events.append(("forget", path.name))

    def shutdown(self):
        self.events.append(("shutdown",))


def make_api(tmp_path, warm, history=None):
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    for name in ("small.en", "base.en"):
        (models / f"ggml-{name}.bin").write_bytes(b"m")
    return Api(config_path=tmp_path / "config.json",
               history=history or HistoryStore(tmp_path / "h.jsonl"),
               recorder_factory=recorder_writing(tone(1.0, 8000)),
               models_dir=models, warm_server=warm)


def dictate(api):
    assert api.start_recording().get("recording")
    return api.stop_recording()


def test_key_down_starts_the_load_and_a_dictation_runs_warm(tmp_path):
    warm = FakeWarm(text="Hello Voisr")
    api = make_api(tmp_path, warm)
    api.PREWARM_DELAY_S = 0
    res = dictate(api)
    assert res["text"] == "Hello Voisr"
    assert wait_until(lambda: any(e[0] == "prewarm" for e in warm.events))
    pre = next(e for e in warm.events if e[0] == "prewarm")
    # base.en: the default on Windows, ahead of a downloaded small.en
    assert pre[1].endswith("ggml-base.en.bin") and pre[2] == "min5"
    # busy from key down to the end of the dictation, then released
    busy = [e[1] for e in warm.events if e[0] == "busy"]
    assert busy[0] is True and busy[-1] is False
    item = api.get_history()[0]
    assert item["mode"] == "warm" and item["model"] == "base.en"
    assert {"transcribe", "cleanup", "model_wait"} <= set(item["timings_ms"])


def test_a_failing_server_loses_no_dictation_and_is_recorded(tmp_path, cli):
    api = make_api(tmp_path, FakeWarm(error=ServerError("crashed")))
    res = dictate(api)
    assert res["text"] == "From cli"      # cleaned text from whisper-cli
    item = api.get_history()[0]
    assert item["mode"] == "cli-fallback"
    assert "server_attempt" in item["timings_ms"]


def test_queued_dictations_stay_in_order_through_a_failure(tmp_path, cli):
    warm = FakeWarm(error=ServerError("x"))
    api = make_api(tmp_path, warm)
    results = [dictate(api) for _ in range(3)]
    assert all("error" not in r and r["text"] for r in results)
    assert len(api.get_history()) == 3


def test_immediately_setting_uses_cli_and_records_it(tmp_path, cli):
    warm = FakeWarm(text="never used")
    api = make_api(tmp_path, warm)
    api.set_config({"model_unload": "immediately"})
    dictate(api)
    assert api.get_history()[0]["mode"] == "cli"
    assert ("configure", "immediately") in warm.events


def test_changing_model_unloads_the_old_one(tmp_path):
    warm = FakeWarm(text="x")
    api = make_api(tmp_path, warm)
    api.set_config({"model": "base.en"})
    assert ("unload",) in warm.events
    warm.events.clear()
    api.set_config({"paste": False})
    assert ("unload",) not in warm.events


def test_deleting_a_model_drops_it_from_the_server_first(tmp_path):
    warm = FakeWarm(text="x")
    api = make_api(tmp_path, warm)
    assert api.delete_model("small.en") == {"ok": True}   # base.en is in use
    assert ("forget", "ggml-small.en.bin") in warm.events


def test_close_shuts_the_server_down(tmp_path):
    warm = FakeWarm(text="x")
    make_api(tmp_path, warm).close()
    assert ("shutdown",) in warm.events


def test_model_unload_setting_is_validated(tmp_path):
    api = make_api(tmp_path, FakeWarm(text="x"))
    assert "error" in api.set_config({"model_unload": "soon"})
    assert api.set_config({"model_unload": "hour1"})["model_unload"] == "hour1"
    assert api.get_config()["model_unload"] == "hour1"


def test_default_unload_is_five_minutes(tmp_path):
    api = make_api(tmp_path, None)
    assert api.get_config()["model_unload"] == "min5"


def test_prewarm_failure_never_reaches_the_hotkey_path(tmp_path):
    warm = FakeWarm(text="x")
    warm.prewarm = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    api = make_api(tmp_path, warm)
    assert api.start_recording().get("recording")
    api.stop_recording()


def test_requests_go_to_loopback_without_proxy_settings(monkeypatch):
    seen = []
    real = ws.http.client.HTTPConnection

    class Spy(real):
        def __init__(self, host, port=None, **kw):
            seen.append(host)
            super().__init__(host, port, **kw)
    monkeypatch.setattr(ws.http.client, "HTTPConnection", Spy)
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    assert ws._http_probe(ws.free_port(), 0.2) is False    # nothing listens
    assert seen == ["127.0.0.1"]


# ---- review fixes ------------------------------------------------------------

def test_hand_edited_unhashable_unload_value_is_rejected_not_a_crash(tmp_path):
    from voisr.app import config_errors, load_config
    assert config_errors({"model_unload": []}) == ["invalid value for model_unload"]
    assert config_errors({"model_unload": {}}) == ["invalid value for model_unload"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model_unload": [], "paste": False}))
    cfg = load_config(path)
    assert cfg["model_unload"] == "min5" and cfg["paste"] is False
    server = WhisperServer()
    server.configure([])                       # no TypeError
    assert ws.unload_seconds([]) == 300


def test_a_request_after_an_unload_always_gets_a_live_loader(tmp_path):
    rig = Rig(tmp_path)
    rig.probe_ok = False                       # the first loader never finishes
    rig.server.prewarm(rig.model)
    assert wait_until(lambda: len(rig.spawned) == 1)
    rig.server.unload()                        # its loader is now stale
    rig.probe_ok = True
    result = []
    t = threading.Thread(target=lambda: result.append(
        rig.server.transcribe(rig.wav, rig.model)))
    start = time.monotonic()
    t.start()
    t.join(5)
    assert result and result[0][0] == "hello world"
    assert time.monotonic() - start < 3, "waited for a loader that was gone"
    assert len(rig.spawned) == 2
    assert wait_until(lambda: rig.spawned[0].stopped == 1)   # stale one stopped


def test_prewarm_after_unload_starts_a_new_loader(tmp_path):
    rig = Rig(tmp_path)
    rig.probe_ok = False
    rig.server.prewarm(rig.model)
    assert wait_until(lambda: len(rig.spawned) == 1)
    rig.server.unload()
    rig.probe_ok = True
    rig.server.prewarm(rig.model)
    assert wait_until(lambda: loaded(rig.server))
    assert len(rig.spawned) == 2


def test_unload_during_a_request_lets_it_finish_and_starts_no_cooldown(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    gate = threading.Event()

    def slow(port, wav, fields, timeout):
        gate.wait(5)
        return "finished"
    rig.server._infer = slow
    result = []
    t = threading.Thread(target=lambda: result.append(
        rig.server.transcribe(rig.wav, rig.model)))
    t.start()
    assert wait_until(lambda: rig.server._proc and rig.server._proc.inflight == 1)
    rig.server.unload()                        # a setting or model change
    time.sleep(0.1)
    assert rig.spawned[0].stopped == 0, "killed a request in flight"
    gate.set()
    t.join(5)
    assert result[0][0] == "finished"
    assert wait_until(lambda: rig.spawned[0].stopped == 1)
    rig.server._infer = rig._infer
    assert rig.server.transcribe(rig.wav, rig.model)[0] == "hello world"  # no cooldown


def test_a_request_cut_off_by_an_unload_is_not_a_server_failure(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    box = {}

    def dies(port, wav, fields, timeout):
        rig.server.unload()                    # shutdown-style unload mid-request
        raise ServerError("connection reset")
    rig.server._infer = dies
    with pytest.raises(ServerError):
        rig.server.transcribe(rig.wav, rig.model)
    assert rig.server._failed_at is None       # no cooldown
    rig.server._infer = rig._infer
    assert rig.server.transcribe(rig.wav, rig.model)[0] == "hello world"


def test_a_real_request_failure_still_starts_the_cooldown(tmp_path):
    rig = Rig(tmp_path)
    rig.infer_result = ServerError("bad")
    with pytest.raises(ServerError):
        rig.server.transcribe(rig.wav, rig.model)
    assert rig.server._failed_at is not None


def test_idle_watcher_skips_when_activity_just_happened(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    rig.now += 400
    rig.server.set_busy(True)                  # a recording starts as it fires
    time.sleep(0.15)
    assert loaded(rig.server)
    rig.server.set_busy(False)                 # the clock restarts at the end
    time.sleep(0.15)
    assert loaded(rig.server)


def test_a_stale_loader_failure_does_not_start_a_cooldown(tmp_path):
    rig = Rig(tmp_path)
    rig.probe_ok = False
    rig.server.prewarm(rig.model)
    assert wait_until(lambda: len(rig.spawned) == 1)
    rig.server.unload()
    rig.spawned[0].code = 1                    # the stale server dies afterwards
    time.sleep(0.2)
    assert rig.server._failed_at is None


def test_a_tap_loads_nothing_but_a_held_key_does(tmp_path):
    warm = FakeWarm(text="x")
    api = make_api(tmp_path, warm)
    api.PREWARM_DELAY_S = 0.2
    assert api.start_recording().get("recording")
    api.stop_recording()                       # released before the delay
    time.sleep(0.5)
    assert not [e for e in warm.events if e[0] == "prewarm"]
    assert api.start_recording().get("recording")
    assert wait_until(lambda: any(e[0] == "prewarm" for e in warm.events))
    assert api.is_recording()                  # loaded while still held
    api.stop_recording()


# ---- prewarm through the real HotkeyService and its tap gate -------------------

def hotkey_rig(tmp_path, gate=0.4):
    from voisr.ui import HotkeyService
    warm = FakeWarm(text="x")
    api = make_api(tmp_path, warm)
    svc = HotkeyService(api, tap_gate_s=gate)
    api.attach_hotkey_service(svc)
    return warm, api, svc


def prewarms(warm):
    return [e for e in warm.events if e[0] == "prewarm"]


def test_an_accidental_tap_through_the_hotkey_loads_nothing(tmp_path):
    warm, api, svc = hotkey_rig(tmp_path)
    svc.press()
    time.sleep(0.05)
    svc.release()                       # a 0.05 s tap: held under the 0.4 s gate
    time.sleep(1.0)                     # past the prewarm delay and the gate
    assert prewarms(warm) == []
    assert not api.is_recording()       # the tap was dropped


def test_a_press_held_past_the_gate_still_gets_its_head_start(tmp_path):
    warm, api, svc = hotkey_rig(tmp_path)
    svc.press()
    assert wait_until(lambda: prewarms(warm), 2)
    assert svc.holding                  # loaded while the key is still down
    svc.release()


def test_a_double_tap_latch_prewarms_when_it_latches(tmp_path):
    warm, api, svc = hotkey_rig(tmp_path, gate=0.5)
    svc.press()
    time.sleep(0.05)
    svc.release()                       # tap one: pending
    time.sleep(0.1)
    assert prewarms(warm) == []
    svc.press()                         # tap two within the gate: hands-free
    assert wait_until(lambda: prewarms(warm), 2)
    svc.release()
    api._cancel_recording()


def test_a_toggle_start_prewarms_at_once(tmp_path):
    warm, api, svc = hotkey_rig(tmp_path)
    svc.press(latched=True)
    assert wait_until(lambda: prewarms(warm), 2)
    api._cancel_recording()


def test_prewarm_never_runs_under_the_hotkey_state_lock(tmp_path):
    warm, api, svc = hotkey_rig(tmp_path, gate=0.1)
    held = []
    warm.prewarm = lambda *a, **k: held.append(
        svc._state_lock._is_owned())    # RLock: owned by *this* thread?
    svc.press()
    assert wait_until(lambda: held, 2)
    assert held == [False]
    svc.release()


# ---- second review ------------------------------------------------------------

def test_a_queued_dictation_does_not_overwrite_a_newer_unload_setting(tmp_path):
    warm = FakeWarm(text="x")
    api = make_api(tmp_path, warm)
    api.set_config({"model_unload": "hour1"})         # saved after the dictation began
    warm.events.clear()
    cfg_at_release = dict(api.get_config(), model_unload="min2")   # the stale snapshot
    raw, info = api._default_transcribe(tmp_path / "a.wav", cfg_at_release)
    assert [e for e in warm.events if e[0] == "configure"] == [("configure", "hour1")]
    assert info["mode"] == "warm"


def test_the_server_transcribe_takes_no_unload_setting(tmp_path):
    import inspect
    assert "unload" not in inspect.signature(WhisperServer.transcribe).parameters


def test_a_dead_ready_server_is_replaced_not_a_failure(tmp_path):
    rig = Rig(tmp_path)
    rig.ready()
    rig.spawned[0].code = 1                    # the server died while idle
    text, _ = rig.server.transcribe(rig.wav, rig.model)
    assert text == "hello world"
    assert len(rig.spawned) == 2
    assert rig.server._failed_at is None       # no 60 s cooldown


def test_forget_model_drops_a_model_that_is_still_loading(tmp_path):
    rig = Rig(tmp_path)
    rig.probe_ok = False
    rig.server.prewarm(rig.model)
    assert wait_until(lambda: len(rig.spawned) == 1)
    rig.server.forget_model(tmp_path / "other.bin")     # unrelated: nothing happens
    time.sleep(0.05)
    assert rig.spawned[0].stopped == 0
    rig.server.forget_model(rig.model)
    assert wait_until(lambda: rig.spawned[0].stopped == 1)


# ---- Windows ------------------------------------------------------------------

def test_there_is_nothing_to_clean_up_at_startup():
    # the job object ends a server with the Voisr that started it
    assert WhisperServer().start_cleanup() == []


def test_servers_start_inside_the_job_by_default():
    assert WhisperServer()._spawn is ws._spawn_in_job


def test_the_probe_does_not_wait_out_a_slow_refusal():
    # Windows retries a refused loopback SYN for about two seconds; a probe
    # that waited for it would notice a loaded model that much late
    started = time.monotonic()
    assert ws._http_probe(ws.free_port(), 5.0) is False
    assert time.monotonic() - started < 1.5


def test_a_server_that_dies_while_loading_logs_why(tmp_path, monkeypatch):
    rows = []
    monkeypatch.setattr(ws, "_log", rows.append)
    rig = Rig(tmp_path)
    rig.probe_ok = False
    orig = rig._spawn

    def missing_dll(command):
        p = orig(command)
        p.code = 0xC0000135
        return p
    rig.server._spawn = missing_dll
    with pytest.raises(ServerError):
        rig.server.transcribe(rig.wav, rig.model)
    assert any("exit code 0xc0000135" in r and "using whisper-cli" in r for r in rows)


def test_a_server_that_cannot_be_put_in_the_job_falls_back(tmp_path):
    rig = Rig(tmp_path)

    def refused(command):
        raise OSError(5, "AssignProcessToJobObject failed: Access is denied.")
    rig.server._spawn = refused
    with pytest.raises(ServerError, match="failed to load|recently failed"):
        rig.server.transcribe(rig.wav, rig.model)
    assert rig.server._failed_at is not None


def test_exit_codes_read_as_windows_documents_them():
    assert ws._describe_exit(1) == "exit code 1"
    assert ws._describe_exit(3221225781) == "exit code 0xc0000135"
    assert ws._describe_exit(-1073741515) == "exit code 0xc0000135"
    assert ws._describe_exit(None) == "running"


def test_stop_terminates_and_waits():
    p = FakePopen(["x"])
    ws._Process(p, 1, "m").stop()
    assert p.stopped == 1 and p.poll() is not None


def test_make_warm_server_never_raises(monkeypatch):
    from voisr import ui

    def broken(*a, **k):
        raise RuntimeError("no server today")
    monkeypatch.setattr(ws, "WhisperServer", broken)
    assert ui.make_warm_server() is None
    monkeypatch.undo()
    server = ui.make_warm_server()
    try:
        assert isinstance(server, WhisperServer)
    finally:
        server.shutdown()
