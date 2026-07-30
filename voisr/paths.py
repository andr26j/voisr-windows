import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def find_binary(name: str) -> str | None:
    """Resolve a CLI tool: bundled bin/ first, then PATH.

    bin/ wins because the CUDA whisper build ships its DLLs alongside the
    exe, and Windows searches an executable's own directory first — a
    whisper-cli.exe found on PATH would not have them.
    """
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    bundled = Path(BIN_DIR) / exe
    if bundled.is_file():
        return str(bundled)
    return shutil.which(exe)


def app_data_dir() -> Path:
    """Per-user data root: %LOCALAPPDATA%\\Voisr."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "Voisr"


def ensure_app_data_dir() -> Path:
    target = app_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target
