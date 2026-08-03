import voisr.overlay as overlay
from voisr.overlay import RecordingWidget, sanitize_pill_color, sanitize_pill_theme


class FakeWindow:
    def __init__(self):
        self.evaluated = []
        self.destroyed = 0

    def evaluate_js(self, script):
        self.evaluated.append(script)

    def destroy(self):
        self.destroyed += 1


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


def test_create_does_not_start_the_webview_loop(monkeypatch):
    """webview.start() may be called exactly once per process, on the main
    thread, after every window has been created -- pywebview raises if it
    is called off-thread, and a second call (e.g. one owned internally by
    the pill) can never coexist with the dashboard's own start() call.
    create() must only register the window, never start the loop itself."""
    calls = []
    monkeypatch.setattr(overlay, "webview", type("_FakeWebview", (), {
        "create_window": staticmethod(lambda *a, **kw: FakeWindow()),
        "start": staticmethod(lambda *a, **kw: calls.append("start")),
    })())
    widget = RecordingWidget()
    widget.create()
    assert calls == [], "create() must not call webview.start()"


def test_evaluate_js_failure_never_propagates():
    widget = RecordingWidget()

    class Exploding:
        def evaluate_js(self, script):
            raise RuntimeError("webview gone")

    widget._window = Exploding()
    widget._shown = True
    widget.show_recording()  # cosmetic failure must never break dictation


def test_destroy_destroys_the_underlying_window():
    """The pill is a real pywebview window and holds pywebview's 'master' uid
    (voisr.ui.main creates it first). The winforms backend only calls
    Application.Exit() once every BrowserView instance is gone, so without
    this the process outlives the tray's Quit."""
    widget = widget_with_window()
    window = widget._window
    widget.destroy()
    assert window.destroyed == 1


def test_destroy_before_create_is_a_no_op():
    """create() swallows its own failures, so the pill may never exist. Quit
    must not raise on the way out."""
    RecordingWidget().destroy()


def test_destroy_survives_a_failing_backend():
    class Exploding:
        def destroy(self):
            raise RuntimeError("backend gone")

    widget = RecordingWidget()
    widget._window = Exploding()
    widget.destroy()  # must not raise -- it runs on the tray's Quit path


def test_destroy_stops_the_guards_touching_a_dead_hwnd():
    """Both guard threads poll self._hwnd forever at 30ms. Once the window is
    destroyed that handle is dead and may be recycled by another process, so
    destroy() clears it rather than leaving the guards calling ShowWindow and
    SetForegroundWindow on whatever inherits the number."""
    widget = widget_with_window()
    widget._hwnd = 1234
    widget.destroy()
    assert widget._hwnd is None
    assert widget._shown is False


def test_destroy_is_idempotent():
    widget = widget_with_window()
    window = widget._window
    widget.destroy()
    widget.destroy()
    assert window.destroyed == 1
