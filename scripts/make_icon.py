"""Render assets/voisr.ico, assets/icon_1024.png and assets/logo.svg from the
Ribbon geometry in voisr/logo_wave.py (drawn by voisr/logo.py).

Every icon size is drawn on its own. Scaling one master down can't work here:
the small sizes use a different curve, not a smaller copy of the large one.

Windows icons are edge to edge, so the tile fills each frame; the Mac's icon
sits inside the macOS grid with a margin and a shadow instead.

Usage: python scripts/make_icon.py   (needs Pillow)
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voisr import logo  # noqa: E402  -- needs ROOT on sys.path first
from voisr import logo_wave  # noqa: E402

ASSETS = ROOT / "assets"
DEST_ICO = ASSETS / "voisr.ico"
DEST_PNG = ASSETS / "icon_1024.png"
DEST_SVG = ASSETS / "logo.svg"

# Windows consumes many sizes across DPI scales (taskbar 24/32, Alt-Tab,
# Start, Explorer, the installer). Baking the common ladder means the shell
# never has to resample our art itself.
SIZES = [16, 20, 24, 28, 32, 40, 48, 64, 96, 128, 256]


def logo_svg():
    """The logo as a flat tile filling its viewBox (the same as the Mac's)."""
    angle = math.radians(logo_wave.TILE_ANGLE_DEG)
    dx, dy = math.sin(angle), -math.cos(angle)
    half = 100 * (abs(dx) + abs(dy)) / 2
    x1, y1 = 50 - dx * half, 50 - dy * half
    x2, y2 = 50 + dx * half, 50 + dy * half
    margin = 50 * (1 - logo_wave.GLYPH_SCALE)
    points = logo_wave.wave_points(logo_wave.LARGE, samples=160)
    path = "M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in points)
    return f"""<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Voisr logo">
  <defs>
    <linearGradient id="voisr-tile" gradientUnits="userSpaceOnUse" x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}">
      <stop offset="0" stop-color="{logo_wave.TILE_START}"/>
      <stop offset="1" stop-color="{logo_wave.TILE_END}"/>
    </linearGradient>
  </defs>
  <rect width="100" height="100" rx="{100 * logo_wave.TILE_RADIUS:g}" fill="url(#voisr-tile)"/>
  <path transform="translate({margin:g} {margin:g}) scale({logo_wave.GLYPH_SCALE:g})" d="{path}" fill="none" stroke="{logo_wave.WAVE_COLOUR}" stroke-width="{logo_wave.LARGE.stroke:g}" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


def write_ico(dest):
    frames = {size: logo.render(size) for size in SIZES}
    # append_images supplies every frame explicitly -- Pillow embeds each one
    # as-is instead of downscaling the largest itself.
    largest = frames[max(SIZES)]
    largest.save(
        dest, format="ICO",
        append_images=[frames[s] for s in SIZES if s != max(SIZES)],
        sizes=[(s, s) for s in SIZES])


def main() -> None:
    write_ico(DEST_ICO)
    logo.render(1024).save(DEST_PNG)
    DEST_SVG.write_text(logo_svg(), encoding="utf-8")
    print(f"wrote {DEST_ICO} ({len(SIZES)} sizes), {DEST_PNG} and {DEST_SVG}")


if __name__ == "__main__":
    main()
