"""The Voisr mark, drawn procedurally.

Single source of truth for the brand geometry, shared by the taskbar/app
icon (scripts/make_icon.py) and the notification-area icon
(voisr.status_item). Drawing it rather than shipping a PNG keeps every
size crisp and means the tray, the taskbar and the Start Menu cannot
drift apart.

Geometry is taken 1:1 from assets/logo.svg (viewBox 0 0 1024 1024).
"""
from PIL import Image, ImageDraw

CANVAS = 1024
CORNER_RADIUS = 228

GRADIENT_TOP = (0xF0, 0x76, 0x4B)
GRADIENT_BOTTOM = (0xDE, 0x55, 0x30)

BARS = [  # (x, y, width, height); corner radius is width / 2
    (200, 282, 80, 460),
    (336, 362, 80, 300),
    (472, 422, 80, 180),
    (608, 362, 80, 300),
    (744, 282, 80, 460),
]

# Rendering at a multiple of the target size and downsampling is what keeps
# the 16-24px notification-area sizes from looking ragged.
SUPERSAMPLE = 4


def _lerp(a, b, t: float):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size: int, supersample: int = SUPERSAMPLE) -> Image.Image:
    """One edge-to-edge RGBA frame of the mark at `size` pixels."""
    big = max(1, size * supersample)
    scale = big / CANVAS

    gradient = Image.new("RGB", (1, big))
    for y in range(big):
        gradient.putpixel(
            (0, y), _lerp(GRADIENT_TOP, GRADIENT_BOTTOM, y / max(big - 1, 1)))
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
