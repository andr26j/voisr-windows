"""Launch-at-login control via the per-user Run registry key.

Deliberately targets HKEY_CURRENT_USER (not HKEY_LOCAL_MACHINE): this app
runs unelevated, and per-user Run entries don't require admin rights.
"""
import sys
import winreg

RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Voisr"


class WinRegistry:
    """Thin wrapper over winreg, scoped to HKEY_CURRENT_USER\\...\\Run."""

    def read(self, name):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_SUBKEY) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return value
        except FileNotFoundError:
            return None

    def write(self, name, value):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_SUBKEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def remove(self, name):
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _RUN_SUBKEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def _default_registry():
    return WinRegistry()


def _launch_command() -> str:
    # A frozen build's sys.executable IS Voisr.exe -- there is no python.exe
    # to substitute pythonw.exe for, and no voisr.ui module to -m into. This
    # must match what build/voisr.iss writes to the same Run value, or the
    # Settings toggle and the installer's login task fight each other.
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # voisr.ui, not voisr.app: launching at login should bring up the
    # dashboard (which starts the hotkey service itself), not the bare CLI.
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pythonw}" -m voisr.ui'


def is_enabled(registry=None) -> bool:
    registry = registry or _default_registry()
    return registry.read(VALUE_NAME) is not None


def enable(registry=None):
    registry = registry or _default_registry()
    registry.write(VALUE_NAME, _launch_command())


def disable(registry=None):
    registry = registry or _default_registry()
    registry.remove(VALUE_NAME)
