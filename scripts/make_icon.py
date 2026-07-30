"""Generate assets/voisr.ico and assets/icon_1024.png from the logo geometry.

The geometry itself lives in voisr/logo.py, shared with the notification-area
icon so the tray, the taskbar and the Start Menu can never drift apart.

The mark is drawn procedurally rather than downscaled from a single PNG:

* The old icon_1024.png carried macOS-style ~10% transparent padding on every
  side. Windows icons are edge-to-edge, so ours rendered visibly smaller than
  every neighbour on the taskbar.
* Each frame is rendered at 4x its target size and LANCZOS-downsampled, so
  the 16-32px sizes Windows actually uses stay crisp instead of inheriting
  one soft global downscale.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voisr import logo  # noqa: E402  -- needs ROOT on sys.path first

DEST_ICO = ROOT / "assets" / "voisr.ico"
DEST_PNG = ROOT / "assets" / "icon_1024.png"

# Windows consumes many sizes across DPI scales (taskbar 24/32, Alt-Tab,
# Start, Explorer). Baking the common ladder means the shell never has to
# resample our art itself.
SIZES = [16, 20, 24, 28, 32, 40, 48, 64, 96, 128, 256]


def main() -> None:
    frames = {size: logo.render(size) for size in SIZES}
    # append_images supplies every frame explicitly -- Pillow embeds each one
    # as-is instead of downscaling the largest itself.
    largest = frames[max(SIZES)]
    largest.save(
        DEST_ICO, format="ICO",
        append_images=[frames[s] for s in SIZES if s != max(SIZES)],
        sizes=[(s, s) for s in SIZES])
    logo.render(1024).save(DEST_PNG)
    print(f"wrote {DEST_ICO} ({len(SIZES)} sizes) and {DEST_PNG}")


if __name__ == "__main__":
    main()
