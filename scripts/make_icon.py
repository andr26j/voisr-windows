"""Generate assets/voisr.ico from assets/icon_1024.png.

Windows picks a different size for the taskbar, the Start Menu, Alt-Tab and
file listings, so a single-size .ico looks wrong somewhere -- this bakes in
every commonly used size.
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "icon_1024.png"
DEST = ROOT / "assets" / "voisr.ico"

SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> None:
    im = Image.open(SOURCE).convert("RGBA")
    im.save(DEST, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {DEST}")


if __name__ == "__main__":
    main()
