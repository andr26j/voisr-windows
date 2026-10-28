"""Live mic level: recorder metering -> queue -> Api -> 30 Hz pill feed."""
import math
import struct

import pytest

from voisr.recording_queue import RecordingQueue, SessionResult
from voisr.stream_recorder import StreamRecorder, level_from_int16


def tone(amplitude, n=1600):
    """A full-cycle sine block of int16 samples at the given peak amplitude."""
    return struct.pack(f"<{n}h", *(int(amplitude * math.sin(2 * math.pi * i / 32))
                                   for i in range(n)))


def dbfs_amplitude(dbfs):
    """Peak amplitude whose sine RMS sits at `dbfs`."""
    return 32767 * math.sqrt(2) * 10 ** (dbfs / 20)


# ---------- level_from_int16 ----------

def test_silence_is_zero():
    assert level_from_int16(b"\x00\x00" * 512) == 0.0


def test_empty_block_is_zero():
    assert level_from_int16(b"") == 0.0


def test_floor_and_ceiling_map_to_the_ends():
    assert level_from_int16(tone(dbfs_amplitude(-60))) == 0.0
    assert level_from_int16(tone(dbfs_amplitude(-55))) == pytest.approx(0.0, abs=0.02)
    assert level_from_int16(tone(dbfs_amplitude(-12))) == pytest.approx(1.0, abs=0.02)
    assert level_from_int16(tone(32767)) == 1.0  # full scale clips at the top


def test_level_is_linear_in_decibels():
    mid = level_from_int16(tone(dbfs_amplitude(-33.5)))
    assert mid == pytest.approx(0.5, abs=0.02)


# ---------- StreamRecorder.level ----------

def test_recorder_level_follows_the_latest_block(tmp_path):
    rec = StreamRecorder()
    assert rec.level == 0.0
    rec._recording = True
    rec._out = str(tmp_path / "x.wav")
    loud = tone(dbfs_amplitude(-12))
    rec._on_audio(loud, len(loud) // 2, None, None)
    assert rec.level == pytest.approx(1.0, abs=0.02)
    quiet = b"\x00\x00" * 256
    rec._on_audio(quiet, 256, None, None)
    assert rec.level == 0.0


def test_recorder_level_resets_when_capture_ends(tmp_path):
    rec = StreamRecorder()
    rec._recording = True
    rec._out = str(tmp_path / "y.wav")
    loud = tone(dbfs_amplitude(-12))
    rec._on_audio(loud, len(loud) // 2, None, None)
    rec.stop()
    assert rec.level == 0.0

    rec._recording = True
    rec._on_audio(loud, len(loud) // 2, None, None)
    rec.cancel()
    assert rec.level == 0.0


def test_a_meter_failure_never_loses_audio(tmp_path, monkeypatch):
    import voisr.stream_recorder as sr

    def boom(block):
        raise TypeError("odd-length buffer")

    monkeypatch.setattr(sr, "level_from_int16", boom)
    rec = StreamRecorder()
    rec._recording = True
    rec._out = str(tmp_path / "m.wav")
    block = b"\x01\x00" * 64
    rec._on_audio(block, 64, None, None)  # must not raise
    assert rec._frames == [block]


def test_stop_resets_level_after_the_stream_has_closed(tmp_path):
    """A callback still in flight when stop() begins must not leave a
    stale level behind: the reset comes after the stream is closed."""
    rec = StreamRecorder()
    rec._recording = True
    rec._out = str(tmp_path / "z.wav")
    loud = tone(dbfs_amplitude(-12))

    class LateCallbackStream:
        def stop(self):
            rec._recording = True   # simulate a callback that already passed
            rec._on_audio(loud, len(loud) // 2, None, None)  # the check
            rec._recording = False

        def close(self):
            pass

    rec._stream = LateCallbackStream()
    rec.stop()
    assert rec.level == 0.0
    rec._recording = True
    rec._stream = LateCallbackStream()
    rec.cancel()
    assert rec.level == 0.0


def test_level_ignores_blocks_after_stop(tmp_path):
    rec = StreamRecorder()
    loud = tone(dbfs_amplitude(-12))
    rec._on_audio(loud, len(loud) // 2, None, None)  # not recording
    assert rec.level == 0.0


# ---------- RecordingQueue.active_level ----------

class MeteredRecorder:
    def __init__(self, level):
        self.level = level

    def start(self, path):
        self.path = path

    def stop(self):
        self.path.write_bytes(b"audio" * 400)
        return ""

    def cancel(self):
        pass


class UnmeteredRecorder(MeteredRecorder):
    def __init__(self):
        pass


def make_queue(tmp_path, factory):
    return RecordingQueue(
        recorder_factory=factory,
        fallback_recorder_factory=None,
        processor=lambda s: SessionResult.success(s.sequence, text="x"),
        temp_path_factory=lambda: tmp_path / "c.wav",
    )


def test_active_level_is_none_when_idle(tmp_path):
    queue = make_queue(tmp_path, lambda: MeteredRecorder(0.4))
    try:
        assert queue.active_level() is None
    finally:
        queue.close()


def test_active_level_reads_the_capturing_recorder(tmp_path):
    queue = make_queue(tmp_path, lambda: MeteredRecorder(0.4))
    try:
        queue.start()
        assert queue.active_level() == 0.4
        queue.release(False, {}).result(2)
        assert queue.active_level() is None
    finally:
        queue.close()


def test_active_level_is_none_for_a_recorder_without_a_meter(tmp_path):
    queue = make_queue(tmp_path, UnmeteredRecorder)
    try:
        queue.start()
        assert queue.active_level() is None  # e.g. the ffmpeg fallback
    finally:
        queue.cancel()
        queue.close()


# ---------- RecordingWidget feed ----------

def test_level_js_formats_and_clamps():
    from voisr.overlay import RecordingWidget
    assert RecordingWidget.level_js(0.42) == "window.setLevel && setLevel(0.420)"
    assert RecordingWidget.level_js(1.7) == "window.setLevel && setLevel(1.000)"
    assert RecordingWidget.level_js(-3) == "window.setLevel && setLevel(0.000)"
    for bad in (None, float("nan"), "0.5", True):
        assert RecordingWidget.level_js(bad) is None


# ---------- the pill's 30 Hz feed (voisr.overlay, Windows) ----------

class FakeWindow:
    """pywebview window stand-in: records every script handed to it."""

    def __init__(self, fail_state=False):
        self.js = []
        self.fail_state = fail_state

    def evaluate_js(self, js):
        if self.fail_state and js.startswith("setState"):
            raise RuntimeError("webview gone")
        self.js.append(js)

    def run_js(self, js):
        self.js.append(js)


def make_widget(level_source, window=None):
    import voisr.overlay as overlay
    widget = overlay.RecordingWidget(level_source=level_source)
    widget._window = window or FakeWindow()
    widget._show_window = lambda: None  # no HWND in tests
    return widget


def run_feed(widget, ticks):
    """Run the feed the last show started, synchronously, for `ticks`
    pushes; returns the level scripts it sent."""
    gen = widget._gen
    count = [0]

    def sleep(_):
        count[0] += 1
        if count[0] >= ticks:
            widget._next_gen()  # end the loop like a state change would

    widget._feed(gen, sleep=sleep)
    return [js for js in widget._window.js if "setLevel" in js]


@pytest.fixture
def no_feed_thread(monkeypatch):
    """Keep show_recording from starting a real feed thread; remember the
    threads it asked for instead."""
    import voisr.overlay as overlay
    started = []

    class Thread:
        def __init__(self, target, args=(), **kw):
            started.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(overlay.threading, "Thread", Thread)
    return started


def test_recording_starts_a_level_feed(no_feed_thread):
    widget = make_widget(lambda: 0.5)
    widget.show_recording()
    assert len(no_feed_thread) == 1
    assert run_feed(widget, 3) == ["window.setLevel && setLevel(0.500)"] * 3


@pytest.mark.parametrize("state", ["show_processing", "show_done", "show_error"])
def test_other_states_start_no_feed(no_feed_thread, state):
    widget = make_widget(lambda: 0.5)
    getattr(widget, state)()
    assert no_feed_thread == []


@pytest.mark.parametrize("leave", ["show_processing", "show_error", "hide"])
def test_any_later_state_ends_the_feed(no_feed_thread, leave):
    widget = make_widget(lambda: 0.5)
    widget.show_recording()
    gen = widget._gen
    if leave == "hide":
        widget.hide(timer_factory=lambda d, fn: type(
            "T", (), {"daemon": False, "start": lambda self: None})())
    else:
        getattr(widget, leave)()
    sent = []
    widget._feed(gen, sleep=lambda _: sent.append(1))
    assert sent == []
    assert not any("setLevel" in js for js in widget._window.js)


def test_a_failing_state_change_still_ends_the_old_feed(no_feed_thread):
    widget = make_widget(lambda: 0.5, window=FakeWindow(fail_state=True))
    widget.show_recording()
    gen = widget._gen
    widget.show_processing()
    assert not widget._is_gen(gen)


def test_an_unmetered_recorder_ends_the_feed(no_feed_thread):
    widget = make_widget(lambda: None)
    widget.show_recording()
    assert run_feed(widget, 5) == []


def test_a_failing_level_source_never_breaks_the_feed(no_feed_thread):
    readings = iter([RuntimeError("stream closed"), 0.25])

    def source():
        value = next(readings)
        if isinstance(value, Exception):
            raise value
        return value

    widget = make_widget(source)
    widget.show_recording()
    assert run_feed(widget, 2) == ["window.setLevel && setLevel(0.250)"]


def test_no_level_source_means_no_feed(no_feed_thread):
    widget = make_widget(None)
    widget.show_recording()
    assert no_feed_thread == []


def test_api_current_level_tracks_the_capture(tmp_path):
    from voisr.history import HistoryStore
    from voisr.ui import Api
    api = Api(config_path=tmp_path / "config.json",
              history=HistoryStore(tmp_path / "h.jsonl"),
              recorder_factory=lambda: MeteredRecorder(0.7),
              transcribe_fn=lambda wav: "hello",
              clock=lambda: 1.0,
              paste_fn=lambda text: True,
              clipboard_fn=lambda text: None)
    try:
        assert api.current_level() is None
        api.start_recording()
        assert api.current_level() == 0.7
        api.stop_recording()
        assert api.current_level() is None
    finally:
        api.close()


def test_current_level_is_not_exposed_to_the_page():
    from voisr.ui import BRIDGE_METHODS
    assert "current_level" not in BRIDGE_METHODS


# ---------- exit: the pill animates out before the window hides ----------

class ManualTimer:
    def __init__(self, delay, fn):
        self.delay, self.fn, self.daemon = delay, fn, False

    def start(self):
        pass


def test_hide_plays_the_exit_then_hides_the_window():
    widget = make_widget(None)
    widget._shown = True
    timers = []
    widget.hide(timer_factory=lambda d, fn: timers.append(ManualTimer(d, fn))
                or timers[-1])
    assert widget._window.js == ["window.hide && hide()"]
    assert widget._shown is True  # still on screen while the pill animates out
    import voisr.overlay as overlay
    assert timers[0].delay == overlay.EXIT_S
    timers[0].fn()
    assert widget._shown is False


def test_a_show_during_the_exit_keeps_the_window_visible(no_feed_thread):
    widget = make_widget(None)
    timers = []
    widget.hide(timer_factory=lambda d, fn: timers.append(ManualTimer(d, fn))
                or timers[-1])
    widget.show_recording()
    widget._shown = True
    timers[0].fn()  # the old exit's hide arrives late
    assert widget._shown is True


def test_exit_outlasts_the_pill_animation():
    import voisr.overlay as overlay
    assert overlay.EXIT_S >= 0.16  # widget.html's exit runs 160 ms


def test_window_leaves_room_for_the_entrance_rise():
    import voisr.overlay as overlay
    assert overlay.HEIGHT == 56  # 44 px pill + 6 px above and below
    assert overlay.BOTTOM_MARGIN == 70  # the pill's centre stays where it was


def test_get_level_is_the_dock_bridge_to_the_live_level(tmp_path):
    from voisr.history import HistoryStore
    from voisr.ui import Api, BRIDGE_METHODS
    assert "get_level" in BRIDGE_METHODS
    api = Api(config_path=tmp_path / "config.json",
              history=HistoryStore(tmp_path / "h.jsonl"),
              recorder_factory=lambda: MeteredRecorder(0.3),
              transcribe_fn=lambda wav: "hello",
              clock=lambda: 1.0,
              paste_fn=lambda text: True,
              clipboard_fn=lambda text: None)
    try:
        assert api.get_level() is None
        api.start_recording()
        assert api.get_level() == 0.3
        api.stop_recording()
    finally:
        api.close()
