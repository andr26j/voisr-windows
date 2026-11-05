"""The Voisr mark as a bitmap: the Ribbon wave on its near-black tile.

The curve itself lives in voisr/logo_wave.py, shared with the Mac app, so the
exe, the installer, the tray and the dashboard's sidebar show one mark. This
module only draws it, for scripts/make_icon.py (assets/voisr.ico,
assets/icon_1024.png) and voisr.status_item (the notification-area icon).

Windows icons are edge to edge: the tile fills the frame, where the Mac's sits
inside the macOS icon grid with a margin and a shadow. Each size is drawn on
its own rather than scaled from one master, because below a certain size the
wave takes a different curve (logo_wave.style_for), not a smaller copy.
"""
import functools
import math

from PIL import Image, ImageChops, ImageDraw

from voisr import logo_wave as wave

# The board's tile carries a 1 px lit top edge and a darker bottom edge from
# 40 px up; below that, only a fainter top edge, which is all that reads.
EDGE_MIN_TILE = 40
HIGHLIGHT_LARGE = 0.34
LOWLIGHT_LARGE = 0.20
HIGHLIGHT_SMALL = 0.40
HIGHLIGHT_PER_TILE = 1 / 250     # 1 px on the board's 250 px tile
HIGHLIGHT_SMALL_PX = 0.5

# While a dictation is in flight the tray tile turns the app's signal colour
# (ultramarine, --accent in web/css/tokens.css) with a near-white wave: the
# Mac's menu-bar wave goes coloured while live, and on a coloured tile the
# same idea has to move to the tile to read at 16 px.
LIVE_TILE_START = "#2A2FE0"
LIVE_TILE_END = "#1D20A6"
LIVE_WAVE_COLOUR = "#FDFDFC"


def _rgb(hex_colour):
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


def _supersample(size):
    return max(4, min(16, 8192 // size))


def _gradient(side, start_hex, end_hex):
    """The tile's gradient, following the CSS definition of an angled one."""
    start, end = _rgb(start_hex), _rgb(end_hex)
    angle = math.radians(wave.TILE_ANGLE_DEG)
    dx, dy = math.sin(angle), -math.cos(angle)
    length = side * (abs(dx) + abs(dy))
    img = Image.new("RGB", (side, side))
    px = img.load()
    middle = (side - 1) / 2
    for y in range(side):
        for x in range(side):
            t = ((x - middle) * dx + (y - middle) * dy) / length + 0.5
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(start, end))
    return img


def _wave_mask(side, glyph_px, style):
    """Coverage of the wave on a `side`-square canvas, with its box centred."""
    ss = _supersample(side)
    mask = Image.new("L", (side * ss, side * ss), 0)
    draw = ImageDraw.Draw(mask)
    k = glyph_px * ss / 100
    offset = (side * ss - glyph_px * ss) / 2
    r = style.stroke * k / 2
    # Stamped circles give exact round caps and joins. Dense enough that the
    # gaps between them can't show as scallops at this resolution.
    for x, y in wave.wave_points(style, samples=1500):
        cx, cy = offset + x * k, offset + y * k
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    return mask.resize((side, side), Image.Resampling.BOX)


def _tile_mask_hi(side, ss):
    mask = Image.new("L", (side * ss, side * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, side * ss - 1, side * ss - 1],
        radius=side * ss * wave.TILE_RADIUS, fill=255)
    return mask


def _edge(hi, ss, side, px_thick, direction):
    """The part of the tile within px_thick of its top (-1) or bottom (+1)."""
    shift = max(1, round(px_thick * ss))
    moved = Image.new("L", hi.size, 0)
    moved.paste(hi, (0, -direction * shift))
    edge = ImageChops.subtract(hi, moved)
    return edge.resize((side, side), Image.Resampling.BOX)


@functools.lru_cache(maxsize=32)
def _render(size, live):
    ss = _supersample(size)
    hi = _tile_mask_hi(size, ss)
    tile_mask = hi.resize((size, size), Image.Resampling.BOX)

    start, end = (LIVE_TILE_START, LIVE_TILE_END) if live else (wave.TILE_START, wave.TILE_END)
    tile = _gradient(size, start, end).convert("RGBA")
    large = size >= EDGE_MIN_TILE
    thick = size * HIGHLIGHT_PER_TILE if large else HIGHLIGHT_SMALL_PX

    top = _edge(hi, ss, size, thick, -1)
    alpha = HIGHLIGHT_LARGE if large else HIGHLIGHT_SMALL
    white = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    white.putalpha(top.point(lambda v: round(v * alpha)))
    tile.alpha_composite(white)

    if large:
        bottom = _edge(hi, ss, size, thick, 1)
        black = Image.new("RGBA", (size, size), (0, 0, 0, 255))
        black.putalpha(bottom.point(lambda v: round(v * LOWLIGHT_LARGE)))
        tile.alpha_composite(black)

    glyph_px = size * wave.GLYPH_SCALE
    colour = LIVE_WAVE_COLOUR if live else wave.WAVE_COLOUR
    stroke = Image.new("RGBA", (size, size), _rgb(colour) + (255,))
    stroke.putalpha(_wave_mask(size, glyph_px, wave.style_for(glyph_px)))
    tile.alpha_composite(stroke)
    tile.putalpha(tile_mask)
    return tile


def render(size: int, live: bool = False) -> Image.Image:
    """One edge-to-edge RGBA frame of the mark at `size` pixels. `live` is the
    tray's dictation-in-flight tile. Returns a fresh copy: callers may draw
    on it, and every size is cached once drawn."""
    return _render(int(size), bool(live)).copy()
