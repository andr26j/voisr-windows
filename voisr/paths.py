import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """Where bundled resources live: the repo when running from source,
    PyInstaller's extraction dir when frozen."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else REPO_ROOT


def bin_dir() -> Path:
    return resource_root() / "bin"


def models_dir() -> Path:
    return resource_root() / "models"


def web_dir() -> Path:
    return resource_root() / "voisr" / "web"


def assets_dir() -> Path:
    return resource_root() / "assets"


# Module-level constants kept for compatibility with existing call sites and
# tests that monkeypatch them directly. Safe as one-time values because
# sys.frozen/_MEIPASS are set by the bootloader before any app code runs, so
# they never change over the life of the process.
BIN_DIR = bin_dir()
ASSETS_DIR = assets_dir()


def find_binary(name: str) -> str | None:
    """Resolve a CLI tool: bundled bin/ first, then PATH.

    bin/ wins because the whisper build ships its DLLs alongside the exe,
    and Windows searches an executable's own directory first — a
    whisper-cli.exe found on PATH would not have them.
    """
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    bundled = Path(BIN_DIR) / exe
    if bundled.is_file():
        return str(bundled)
    return shutil.which(exe)


def app_data_dir() -> Path:
    """Per-user data root: %LOCALAPPDATA%\\Voisr.

    Deliberately independent of resource_root(): config and history must
    survive upgrades and must not live inside a one-file build's temp
    extraction dir, which disappears when the process exits.
    """
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "Voisr"


def ensure_app_data_dir() -> Path:
    target = app_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def app_icon() -> str | None:
    """Absolute path to the .ico, or None if it is missing.

    Without this, pywebview's winforms backend falls back to extracting the
    icon out of sys.executable (winforms.py:247) — so the taskbar shows
    pythonw.exe's Python icon instead of ours.
    """
    icon = ASSETS_DIR / "voisr.ico"
    return str(icon) if icon.is_file() else None
