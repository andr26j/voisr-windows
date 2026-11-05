import re
from pathlib import Path

from voisr import window_chrome
from voisr.window_chrome import apply_chrome, colorref, launch_background

TOKENS = Path(__file__).resolve().parent.parent / "voisr" / "web" / "css" / "tokens.css"


class FakeDwm:
    def __init__(self, fail_attributes=()):
        self.calls = []
        self.fail = set(fail_attributes)

    def set_attribute(self, hwnd, attribute, value):
        self.calls.append((attribute, value))
        return 0 if attribute not in self.fail else 1


def test_dark_mode_is_applied():
    dwm = FakeDwm()
    result = apply_chrome(1234, dark=True, dwm=dwm)
    assert result["dark_titlebar"] is True
    assert (20, 1) in dwm.calls


def test_light_mode_clears_dark_titlebar():
    dwm = FakeDwm()
    apply_chrome(1234, dark=False, dwm=dwm)
    assert (20, 0) in dwm.calls


def test_the_caption_is_the_sidebars_colour_with_the_title_in_ink():
    """The Mac's flat window, with no band over the sidebar: here the band is
    Windows' titlebar, so it takes the sidebar's colour."""
    dwm = FakeDwm()
    assert apply_chrome(1234, dark=True, dwm=dwm)["caption"] is True
    assert (35, colorref("#141413")) in dwm.calls
    assert (36, colorref("#F2F1EC")) in dwm.calls
    light = FakeDwm()
    apply_chrome(1234, dark=False, dwm=light)
    assert (35, colorref("#F5F4F1")) in light.calls
    assert (36, colorref("#191918")) in light.calls


def test_no_mica_backdrop_tints_the_band():
    dwm = FakeDwm()
    apply_chrome(1234, dark=True, dwm=dwm)
    assert 38 not in [attribute for attribute, _ in dwm.calls]


def test_windows_10_without_caption_colours_is_reported_not_raised():
    dwm = FakeDwm(fail_attributes={35, 36})
    result = apply_chrome(1234, dark=True, dwm=dwm)
    assert result == {"dark_titlebar": True, "caption": False}


def test_dark_titlebar_failure_is_reported_not_raised():
    dwm = FakeDwm(fail_attributes={20})
    result = apply_chrome(1234, dark=True, dwm=dwm)
    assert result["dark_titlebar"] is False


def test_exception_never_propagates():
    class Exploding:
        def set_attribute(self, *args):
            raise OSError("dwmapi missing")

    result = apply_chrome(1234, dark=True, dwm=Exploding())
    assert result == {"dark_titlebar": False, "caption": False}


def test_colorref_is_bgr():
    assert colorref("#112233") == 0x332211


def _theme_value(theme, token):
    css = TOKENS.read_text(encoding="utf-8")
    start = css.index(f':root[data-appearance="{theme}"]')
    block = css[start:css.index("}", start)]
    return re.search(rf"{token}:\s*(#[0-9A-Fa-f]{{6}})", block).group(1)


def test_the_window_and_titlebar_are_the_sidebar_colour():
    """Different colours would show as a flash before the page's first paint,
    and as a band over the sidebar after it."""
    for theme, colour in window_chrome.WINDOW_BACKGROUND.items():
        assert colour == _theme_value(theme, "--bg"), theme
    for theme, colour in window_chrome.TITLE_TEXT.items():
        assert colour == _theme_value(theme, "--ink"), theme


def test_a_pinned_appearance_ignores_the_system():
    assert launch_background("light", lambda: True) == "#F5F4F1"
    assert launch_background("dark", lambda: False) == "#141413"


def test_system_appearance_follows_windows():
    assert launch_background("system", lambda: True) == "#141413"
    assert launch_background("system", lambda: False) == "#F5F4F1"


def test_launch_background_never_raises():
    def broken():
        raise OSError("no registry")

    assert launch_background("system", broken) == "#F5F4F1"
    # a hand-edited config can hold anything
    assert launch_background(["dark"], lambda: False) == "#F5F4F1"
    assert launch_background("system") == "#F5F4F1"


def test_the_dashboard_opens_in_the_saved_appearances_colour(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from voisr import ui
    monkeypatch.setattr(ui, "_system_prefers_dark", lambda: True)
    pinned = SimpleNamespace(get_config=lambda: {"appearance": "light"})
    assert ui._window_background(pinned) == "#F5F4F1"
    system = SimpleNamespace(get_config=lambda: {"appearance": "system"})
    assert ui._window_background(system) == "#141413"

    def broken():
        raise OSError("config unreadable")
    assert ui._window_background(SimpleNamespace(get_config=broken)) == "#141413"
