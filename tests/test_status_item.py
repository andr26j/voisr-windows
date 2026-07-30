import pytest

from voisr.status_item import StatusItem, glyph_image


def make(**overrides):
    calls = []
    kwargs = dict(
        on_dictate=lambda: calls.append("dictate"),
        on_open=lambda: calls.append("open"),
        on_quit=lambda: calls.append("quit"),
        hotkey_label_fn=lambda: "Right Ctrl",
    )
    kwargs.update(overrides)
    return StatusItem(**kwargs), calls


def test_glyph_renders_at_the_requested_size():
    assert glyph_image(recording=False, size=32).size == (32, 32)


def test_recording_glyph_differs_from_idle():
    idle = glyph_image(recording=False, size=32).tobytes()
    rec = glyph_image(recording=True, size=32).tobytes()
    assert idle != rec


def test_glyph_adapts_to_taskbar_theme():
    light = glyph_image(recording=False, size=32, dark_taskbar=False).tobytes()
    dark = glyph_image(recording=False, size=32, dark_taskbar=True).tobytes()
    assert light != dark


def test_dispatch_routes_each_action():
    item, calls = make()
    item._dispatch("dictate")
    item._dispatch("open")
    item._dispatch("quit")
    assert calls == ["dictate", "open", "quit"]


def test_dispatch_ignores_unknown_action():
    item, calls = make()
    item._dispatch("nonsense")
    assert calls == []


def test_handler_exception_never_escapes():
    def explode():
        raise RuntimeError("handler bug")

    item, _ = make(on_dictate=explode)
    item._dispatch("dictate")  # must not raise


def test_menu_shows_the_current_hotkey():
    item, _ = make(hotkey_label_fn=lambda: "F13")
    assert "F13" in item._hotkey_menu_text()


def test_menu_survives_a_failing_hotkey_lookup():
    def explode():
        raise RuntimeError("no config")

    item, _ = make(hotkey_label_fn=explode)
    assert item._hotkey_menu_text()  # some text, no exception


def test_set_recording_before_create_is_a_noop():
    item, _ = make()
    item.set_recording(True)  # must not raise


def test_release_before_create_is_a_noop():
    item, _ = make()
    item.release()


def test_set_recording_updates_the_icon():
    item, _ = make()

    class FakeIcon:
        def __init__(self):
            self.icon = None

    item._icon = FakeIcon()
    item.set_recording(True)
    assert item._icon.icon is not None
