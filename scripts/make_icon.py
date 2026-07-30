"""Generate assets/voisr.ico and assets/icon_1024.png from the logo geometry.

The icon is drawn procedurally (rounded square + five waveform bars, matching
assets/logo.svg exactly) rather than downscaled from a single PNG:

* The old icon_1024.png carried macOS-style ~10% transparent padding on every
  side. Windows icons are edge-to-edge, so ours rendered visibly smaller than
  every neighbour on the taskbar.
* Each frame is rendered at 4x its target size and LANCZOS-downsampled, so
  the 16-32px sizes Windows actually uses in the taskbar stay crisp instead
  of inheriting one soft global downscale.

Geometry is taken 1:1 from logo.svg (viewBox 0 0 1024 1024): corner radius
228, bars 80 wide / rx 40 at x = 200,336,472,608,744 with heights 460, 300,
180, 300, 460 centred on y=512.
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
DEST_ICO = ROOT / "assets" / "voisr.ico"
DEST_PNG = ROOT / "assets" / "icon_1024.png"

# Windows consumes many sizes across DPI scales (taskbar 24/32, Alt-Tab,
# Start, Explorer). Baking the common ladder means the shell never has to
# resample our art itself.
SIZES = [16, 20, 24, 28, 32, 40, 48, 64, 96, 128, 256]

GRADIENT_TOP = (0xF0, 0x76, 0x4B)
GRADIENT_BOTTOM = (0xDE, 0x55, 0x30)

# logo.svg geometry in its 1024-unit space
CANVAS = 1024
CORNER_RADIUS = 228
BARS = [  # (x, y, width, height), rx = width / 2
    (200, 282, 80, 460),
    (336, 362, 80, 300),
    (472, 422, 80, 180),
    (608, 362, 80, 300),
    (744, 282, 80, 460),
]
SUPERSAMPLE = 4


def _lerp(a, b, t: float):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size: int) -> Image.Image:
    """One edge-to-edge frame at `size`, supersampled for clean edges."""
    big = size * SUPERSAMPLE
    scale = big / CANVAS

    # Vertical gradient strip, then clip it by the rounded-square mask.
    gradient = Image.new("RGB", (1, big))
    for y in range(big):
        gradient.putpixel((0, y), _lerp(GRADIENT_TOP, GRADIENT_BOTTOM, y / max(big - 1, 1)))
    gradient = gradient.resize((big, big))

    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=CORNER_RADIUS * scale, fill=255)

    frame = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    frame.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(frame)
    for x, y, w, h in BARS:
        draw.rounded_rectangle(
            (x * scale, y * scale, (x + w) * scale, (y + h) * scale),
            radius=(w / 2) * scale, fill=(255, 255, 255, 255))

    return frame.resize((size, size), Image.LANCZOS)


def main() -> None:
    frames = {size: render(size) for size in SIZES}
    # append_images supplies every frame explicitly -- Pillow embeds each one
    # as-is instead of downscaling the largest itself.
    largest = frames[max(SIZES)]
    largest.save(
        DEST_ICO, format="ICO",
        append_images=[frames[s] for s in SIZES if s != max(SIZES)],
        sizes=[(s, s) for s in SIZES])
    render(1024).save(DEST_PNG)
    print(f"wrote {DEST_ICO} ({len(SIZES)} sizes) and {DEST_PNG}")


if __name__ == "__main__":
    main()
