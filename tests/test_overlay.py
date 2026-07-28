import voisr.overlay as overlay
from voisr.overlay import RecordingWidget, sanitize_pill_color, sanitize_pill_theme


class FakeWindow:
    def __init__(self):
        self.evaluated = []

    def evaluate_js(self, script):
        self.evaluated.append(script)


def widget_with_window():
    widget = RecordingWidget()
    widget._window = FakeWindow()
    widget._shown = True
    return widget


def test_sanitize_theme_passes_known_values():
    assert sanitize_pill_theme("ember") == "ember"


def test_sanitize_theme_rejects_unknown():
    assert sanitize_pill_theme("'); alert(1); //") == "classic"
    assert sanitize_pill_theme(None) == "classic"


def test_sanitize_color_accepts_hex():
    assert sanitize_pill_color("#FF0080") == "#FF0080"


def test_sanitize_color_rejects_non_hex():
    assert sanitize_pill_color("red") is None
    assert sanitize_pill_color("#FF0080; alert(1)") is None
    assert sanitize_pill_color(None) is None


def test_show_recording_sets_state():
    widget = widget_with_window()
    widget.show_recording()
    assert any('setState("recording")' in s for s in widget._window.evaluated)


def test_show_recording_applies_theme_and_color():
    widget = widget_with_window()
    widget.show_recording(theme="ember", color="#FF0080")
    joined = " ".join(widget._window.evaluated)
    assert 'setTheme("ember")' in joined
    assert 'setColor("#FF0080")' in joined


def test_unsafe_theme_never_reaches_js():
    widget = widget_with_window()
    widget.show_recording(theme='"); alert(1); //')
    joined = " ".join(widget._window.evaluated)
    assert "alert" not in joined
    assert 'setTheme("classic")' in joined


def test_unsafe_color_becomes_null():
    widget = widget_with_window()
    widget.show_recording(theme="ember", color='"); alert(1); //')
    joined = " ".join(widget._window.evaluated)
    assert "alert" not in joined
    assert "setColor(null)" in joined


def test_show_processing_sets_state():
    widget = widget_with_window()
    widget.show_processing()
    assert any('setState("processing")' in s for s in widget._window.evaluated)


def test_show_done_sets_state():
    widget = widget_with_window()
    widget.show_done()
    assert any('setState("done")' in s for s in widget._window.evaluated)


def test_show_error_sets_state():
    widget = widget_with_window()
    widget.show_error()
    assert any('setState("error")' in s for s in widget._window.evaluated)


def test_calls_before_create_are_dropped_not_raised():
    RecordingWidget().show_recording()  # must not raise


def test_epoch_guard_drops_stale_updates():
    widget = widget_with_window()
    widget.show_recording()
    before = len(widget._window.evaluated)
    widget.show_done(is_current=lambda: False)
    assert len(widget._window.evaluated) == before


def test_epoch_guard_allows_current_updates():
    widget = widget_with_window()
    widget.show_recording()
    before = len(widget._window.evaluated)
    widget.show_done(is_current=lambda: True)
    assert len(widget._window.evaluated) > before


def test_evaluate_js_failure_never_propagates():
    widget = RecordingWidget()

    class Exploding:
        def evaluate_js(self, script):
            raise RuntimeError("webview gone")

    widget._window = Exploding()
    widget._shown = True
    widget.show_recording()  # cosmetic failure must never break dictation
