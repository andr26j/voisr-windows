"""The pill as HotkeyService drives it: it comes down once shown, whatever the
Recording widget setting says by then, and a state with no dictation behind
it wears the saved theme. Ported from voisr-mac's tests/test_widget.py."""
import threading
from concurrent.futures import Future

import pytest

from voisr.recording_queue import SessionResult
from voisr.ui import HotkeyService


class PillApi:
    """Api's slice HotkeyService talks to, with a config the test can flip."""

    def __init__(self, **config):
        self.config = {"show_widget": True, "pill_theme": "classic",
                       "pill_color": None, **config}
        self.start_error = None
        self.allow = None  # an Event to hold transcription until set

    def get_config(self):
        return dict(self.config)

    def set_config(self, updates):
        self.config.update(updates)

    def start_recording(self):
        if self.start_error is not None:
            return {"error": self.start_error}
        return {"recording": True}

    def _release_recording(self, paste=False):
        future = Future()
        allow = self.allow

        def finish():
            if allow is not None:
                allow.wait(5)
            future.set_result(SessionResult.success(1, text="hello widget"))

        if allow is None:
            finish()
        else:
            threading.Thread(target=finish, daemon=True).start()
        return future

    def _cancel_recording(self):
        return True

    def note_user_typing(self):
        pass


class FakeWidget:
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


@pytest.fixture
def settle_widget():
    """Waits out the dictations finishing since the test began, so nothing
    more can reach the widget."""
    before = set(threading.enumerate())

    def settle():
        for thread in set(threading.enumerate()) - before:
            if thread.name.startswith("voisr-widget-"):
                thread.join(5)

    return settle


# (hands-free?, how the dictation ends or is cancelled)
DICTATION_ENDINGS = [
    pytest.param(False, lambda svc: svc.release(at=100.1), id="held-key-let-go"),
    pytest.param(True, lambda svc: svc.press(at=100.1), id="hands-free-second-press"),
    pytest.param(True, lambda svc: svc._stop_hands_free(), id="toggle-or-menu"),
    pytest.param(False, lambda svc: svc.cancel_chord(at=100.1), id="chord-drop"),
    pytest.param(True, lambda svc: svc.cancel_escape(), id="esc"),
]


@pytest.mark.parametrize("latched, end", DICTATION_ENDINGS)
def test_pill_comes_down_when_the_setting_went_off_during_the_dictation(
        settle_widget, latched, end):
    widget, api = FakeWidget(), PillApi()
    svc = HotkeyService(api, widget=widget, hide_delay=0)
    svc.press(latched=latched, at=100.0)
    assert widget.calls == ["recording"]

    api.set_config({"show_widget": False})
    end(svc)
    settle_widget()

    assert widget.calls == ["recording", "hide"]


@pytest.mark.parametrize("latched, end", DICTATION_ENDINGS)
def test_pill_never_starts_when_the_setting_came_on_during_the_dictation(
        settle_widget, latched, end):
    widget, api = FakeWidget(), PillApi(show_widget=False)
    svc = HotkeyService(api, widget=widget, hide_delay=0)
    svc.press(latched=latched, at=100.0)

    api.set_config({"show_widget": True})
    end(svc)
    settle_widget()

    assert [call for call in widget.calls if call != "hide"] == []


def test_pill_finishes_and_comes_down_when_the_setting_went_off_during_processing(
        settle_widget):
    widget, api = FakeWidget(), PillApi()
    api.allow = threading.Event()
    svc = HotkeyService(api, widget=widget, hide_delay=0)
    try:
        svc.press()
        svc.release()
        assert widget.calls == ["recording", "processing"]

        api.set_config({"show_widget": False})
    finally:
        api.allow.set()
    settle_widget()

    assert widget.calls == ["recording", "processing", "done", "hide"]


def test_new_dictation_takes_down_an_older_pill_when_the_setting_is_off(settle_widget):
    """The new epoch orphans the older pill's own hide, and with the setting
    off nothing else would replace it."""
    widget, api = FakeWidget(), PillApi()
    api.allow = threading.Event()
    svc = HotkeyService(api, widget=widget, hide_delay=0)
    try:
        svc.press()
        svc.release()
        assert widget.calls == ["recording", "processing"]

        api.set_config({"show_widget": False})
        svc.press()
        assert widget.calls == ["recording", "processing", "hide"]

        svc.release()
    finally:
        api.allow.set()
    settle_widget()

    assert widget.calls == ["recording", "processing", "hide"]


class QueuedWidget:
    """Like the real pill's ownership contract: each call waits until the
    test flushes it, where a newer dictation's epoch can still drop it."""
    _supports_ownership_action = True

    def __init__(self):
        self.calls = []
        self.pending = []

    def show_recording(self, theme="classic", color=None, if_current=None):
        self.pending.append(("recording", if_current))

    def show_processing(self, if_current=None):
        self.pending.append(("processing", if_current))

    def show_done(self, if_current=None):
        self.pending.append(("done", if_current))

    def hide(self, if_current=None):
        self.pending.append(("hide", if_current))

    def flush(self):
        pending, self.pending = self.pending, []
        for state, if_current in pending:
            if_current(lambda state=state: self.calls.append(state))


def test_a_hide_dropped_by_a_newer_dictation_does_not_count_as_down(settle_widget):
    widget, api = QueuedWidget(), PillApi()
    svc = HotkeyService(api, widget=widget, hide_delay=0)
    svc.press()
    widget.flush()
    assert widget.calls == ["recording"]

    # the first dictation's hide is queued but hasn't run when the next press
    # lands, so that press's new epoch drops it
    svc.release()
    settle_widget()
    assert widget.pending[-1][0] == "hide"

    api.set_config({"show_widget": False})
    svc.press()
    widget.flush()

    assert widget.calls == ["recording", "hide"]


class DressedWidget(FakeWidget):
    def set_look(self, theme, color):
        self.calls.append(("look", theme, color))


def test_an_error_with_no_dictation_behind_it_wears_the_saved_theme(settle_widget):
    """Only a recording sets the theme, so a press that can't start would
    otherwise flash in the last dictation's, or Signature after launch."""
    widget, api = DressedWidget(), PillApi(pill_theme="quiet", pill_color="#123456")
    api.start_error = "recorder failed: no microphone"
    svc = HotkeyService(api, widget=widget, hide_delay=0)
    svc.press()
    assert widget.calls[:2] == [("look", "quiet", "#123456"), "error"]


def test_a_widget_without_set_look_still_flashes_the_error(settle_widget):
    widget, api = FakeWidget(), PillApi()
    api.start_error = "recorder failed: no microphone"
    svc = HotkeyService(api, widget=widget, hide_delay=0)
    svc.press()
    assert widget.calls[0] == "error"
