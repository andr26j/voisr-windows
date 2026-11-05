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


def test_glyph_ignores_taskbar_theme():
    """The tray shows the Ribbon tile, which carries its own dark background
    and reads on a light or dark taskbar alike, so the theme argument is
    deliberately accepted and ignored."""
    light = glyph_image(recording=False, size=32, dark_taskbar=False).tobytes()
    dark = glyph_image(recording=False, size=32, dark_taskbar=True).tobytes()
    assert light == dark


def test_glyph_is_the_brand_mark_not_a_bespoke_glyph():
    """Guards against the tray drifting away from the taskbar icon: the
    idle tray image must be exactly what voisr.logo renders."""
    from voisr import logo
    assert glyph_image(recording=False, size=32).tobytes() == logo.render(32).tobytes()


def test_glyph_is_opaque_where_the_mark_sits():
    """A tray icon that renders as a transparent hole is a real failure
    mode and an easy one to miss -- the centre pixel must be painted."""
    img = glyph_image(recording=False, size=32)
    assert img.getpixel((16, 16))[3] == 255


def test_recording_state_survives_small_sizes():
    """16px is what the notification area actually asks for; the state has
    to still be distinguishable there."""
    idle = glyph_image(recording=False, size=16).tobytes()
    rec = glyph_image(recording=True, size=16).tobytes()
    assert idle != rec


def test_live_is_the_signal_colour_with_the_same_shape():
    """Like the Mac's menu-bar wave: the shape stays, colour tells live from
    idle. Here the tile takes the colour, ultramarine, not the old red badge."""
    idle = glyph_image(recording=False, size=32)
    live = glyph_image(recording=True, size=32)
    r, g, b, a = live.getpixel((3, 16))  # tile, clear of the wave
    assert b > 150 and r < 80 and g < 80, (r, g, b)
    assert idle.getchannel("A").tobytes() == live.getchannel("A").tobytes(), \
        "same tile outline, so nothing shifts in the tray"


def test_the_tray_never_gets_a_drawing_it_could_spoil():
    # voisr.logo caches each size; a caller drawing on its copy mustn't change it
    first = glyph_image(recording=False, size=32)
    first.putpixel((16, 16), (255, 0, 0, 255))
    assert glyph_image(recording=False, size=32).getpixel((16, 16)) != (255, 0, 0, 255)


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
