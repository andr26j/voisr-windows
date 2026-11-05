"""The Ribbon logo: the Mac's geometry, drawn edge to edge for Windows."""
import math
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from voisr import logo, logo_wave

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"


def _pixels(img):
    raw = img.convert("RGBA").tobytes()
    return [tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)]


def test_the_two_curves_are_the_macs():
    # voisr-mac's voisr/logo_wave.py, which the board approved
    assert logo_wave.LARGE == (2.5, 6.5, math.pi / 2)
    assert logo_wave.SMALL == (1.5, 12.0, -math.pi / 2)
    assert logo_wave.SMALL_MAX_PX == 34


def test_the_tile_values_are_the_macs():
    assert (logo_wave.TILE_START, logo_wave.TILE_END) == ("#2A2926", "#101010")
    assert logo_wave.WAVE_COLOUR == "#B8BAFF"
    assert logo_wave.TILE_RADIUS == 0.23
    assert logo_wave.GLYPH_SCALE == 0.78


def test_the_curve_changes_above_a_44_px_tile():
    assert logo_wave.style_for(44 * 0.78) is logo_wave.SMALL
    assert logo_wave.style_for(48 * 0.78) is logo_wave.LARGE


@pytest.mark.parametrize("size", [16, 32, 48, 256])
def test_an_icon_is_the_tile_edge_to_edge(size):
    img = logo.render(size)
    assert img.size == (size, size) and img.mode == "RGBA"
    middle = size // 2
    assert img.getpixel((middle, 1))[3] == 255, "Windows icons fill the frame"
    assert img.getpixel((0, 0))[3] < 128, "with the tile's round corners"


@pytest.mark.parametrize("size", [16, 32, 256])
def test_the_old_orange_is_gone(size):
    img = logo.render(size)
    for r, g, b, a in _pixels(img):
        if a > 200:
            assert not (r > 200 and 60 < g < 140 and b < 90), (r, g, b)


def test_the_wave_is_the_periwinkle():
    img = logo.render(256)
    assert (184, 186, 255) in {p[:3] for p in _pixels(img) if p[3] == 255}


def test_small_and_large_sizes_are_drawn_not_scaled():
    # 32 px takes the small curve: one and a half cycles, so a down-scaled 256
    # would not match it
    scaled = logo.render(256).resize((32, 32), Image.Resampling.LANCZOS)
    assert scaled.tobytes() != logo.render(32).tobytes()


def test_the_committed_ico_has_every_size_and_is_what_the_script_draws():
    ico = Image.open(ASSETS / "voisr.ico")
    sizes = sorted(ico.ico.sizes())
    assert sizes == [(s, s) for s in (16, 20, 24, 28, 32, 40, 48, 64, 96, 128, 256)]
    for size in (16, 32, 256):
        frame = ico.ico.getimage((size, size)).convert("RGBA")
        assert frame.tobytes() == logo.render(size).tobytes(), size


def test_the_committed_png_and_svg_are_what_the_script_draws():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import make_icon
    finally:
        sys.path.pop(0)
    assert (ASSETS / "logo.svg").read_text(encoding="utf-8") == make_icon.logo_svg()
    png = Image.open(ASSETS / "icon_1024.png").convert("RGBA")
    assert png.tobytes() == logo.render(1024).tobytes()


def test_the_sidebar_mark_is_the_small_ribbon():
    """index.html's brand mark is drawn from the same curve, at its small size."""
    import re
    html = (ROOT / "voisr" / "web" / "index.html").read_text(encoding="utf-8")
    d = re.search(r'<path id="brand-wave" d="([^"]+)"', html).group(1)
    first = [float(v) for v in re.findall(r"[\d.]+", d)[:4]]
    points = logo_wave.wave_points(logo_wave.SMALL, samples=44)
    assert first == pytest.approx([*points[0], *points[1]], abs=0.01)
