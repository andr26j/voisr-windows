import subprocess
from pathlib import Path

from voisr.paths import find_binary

CREATE_NO_WINDOW = 0x08000000


def to_wav16k(src, dst) -> Path:
    """Convert any audio file to 16 kHz mono PCM WAV (whisper.cpp's format)."""
    subprocess.run(
        [find_binary("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error",
         "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
         str(dst)],
        check=True, creationflags=CREATE_NO_WINDOW)
    return Path(dst)
