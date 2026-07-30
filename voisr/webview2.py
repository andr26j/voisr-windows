"""WebView2 runtime detection.

The dashboard is a WebView2 window. The runtime ships with Windows 11 and
current Windows 10, but is not guaranteed -- without it the app starts and
shows nothing. This checks the same registry keys the Evergreen bootstrapper
writes, covering both machine-wide and per-user installs.
"""
import winreg

DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_CLIENT_GUID}"),
    (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_CLIENT_GUID}"),
    (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_CLIENT_GUID}"),
]


class WinRegistry:
    """Thin wrapper over winreg, reading "pv" across the known WebView2
    Client keys (HKLM 64-bit, HKLM WOW6432Node, HKCU)."""

    def read(self, name):
        for hive, subkey in _KEYS:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                    if value:
                        return value
            except FileNotFoundError:
                continue
        return None


def _default_registry():
    return WinRegistry()


def is_runtime_installed(registry=None) -> bool:
    """True if the WebView2 runtime appears installed.

    A registry read failure assumes installed -- a false negative would
    refuse to start on a machine where the runtime is actually fine.
    """
    registry = registry or _default_registry()
    try:
        version = registry.read("pv")
    except OSError:
        return True
    if not version:
        return False
    return version != "0.0.0.0"
